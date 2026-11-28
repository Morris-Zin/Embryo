#!/usr/bin/env python3
"""Exact financial evidence verifier for ERPNext seasonal fixtures.

The module is importable on a plain host Python installation; Frappe is required
only by the collector that produces evidence.  A verifier input is evidence, not
a pass/fail flag: independent reference and candidate snapshots, retry
observations, voucher rows, stock rows, GL rows and persisted count deltas.

CLI entry points:
  python tools/financial_verifier.py --evidence evidence.json
  python tools/financial_verifier.py --negative-controls

Exit status is 0 for a passing verification, 1 for a financial mismatch or a
failed diagnostic control, and 2 for missing or invalid input.  Missing input
never produces a pass.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


VOUCHER_KINDS = ("sale", "transfer", "purchase_receipt", "purchase_invoice")
VOUCHER_DOCTYPES = {
    "sale": "Sales Invoice",
    "transfer": "Stock Entry",
    "purchase_receipt": "Purchase Receipt",
    "purchase_invoice": "Purchase Invoice",
}
ROLLBACK_FIXTURE = "rollback_fixture"
COMMITTED_RETRY = "committed_retry"
COMMITTED_POSTING = "committed_posting"
RETRY_MODES = (ROLLBACK_FIXTURE, COMMITTED_RETRY)
EXECUTION_MODES = (*RETRY_MODES, COMMITTED_POSTING)

REQUIRED_SNAPSHOT_SECTIONS = ("doc", "items", "gl", "sle", "gl_summary", "sle_summary", "bin")
ROW_SECTIONS = ("items", "payments", "gl", "sle")
GENERIC_SECTIONS = ("gl_summary", "sle_summary", "bin")

GL_ROW_FIELDS = (
    "account",
    "debit",
    "credit",
    "debit_in_account_currency",
    "credit_in_account_currency",
    "account_currency",
    "cost_center",
    "party_type",
    "party",
    "against_voucher_type",
    "is_cancelled",
    "voucher_detail_ordinal",
)
SLE_ROW_FIELDS = (
    "item_code",
    "warehouse",
    "actual_qty",
    "qty_after_transaction",
    "valuation_rate",
    "stock_value",
    "stock_value_difference",
    "is_cancelled",
    "voucher_detail_ordinal",
)
SALE_ITEM_ROW_FIELDS = ("item_code", "warehouse", "qty", "stock_qty", "rate", "amount")
TRANSFER_ITEM_ROW_FIELDS = ("item_code", "s_warehouse", "t_warehouse", "qty", "transfer_qty", "valuation_rate", "amount")
PAYMENT_ROW_FIELDS = ("mode_of_payment", "amount", "account")

# Precision is evidence metadata, never a comparison tolerance.  Values are
# compared as exact decimal numbers; the precision tells diagnostics what one
# indivisible posting unit is for a field.
DEFAULT_DECIMAL_PRECISIONS = {
    "actual_qty": 6,
    "actual_qty_net": 6,
    "actual_qty_sum": 6,
    "amount": 2,
    "base_amount": 2,
    "buying_amount": 2,
    "change_amount": 2,
    "credit": 2,
    "credit_in_account_currency": 2,
    "credit_sum": 2,
    "debit": 2,
    "debit_in_account_currency": 2,
    "debit_sum": 2,
    "discount_amount": 2,
    "grand_total": 2,
    "net_total": 2,
    "outstanding_amount": 2,
    "paid_amount": 2,
    "qty": 3,
    "qty_after_transaction": 6,
    "rate": 2,
    "rounded_total": 2,
    "stock_qty": 6,
    "stock_value": 6,
    "stock_value_difference": 6,
    "stock_value_difference_sum": 6,
    "stock_value_net": 6,
    "total_taxes_and_charges": 2,
    "transfer_qty": 6,
    "valuation_rate": 6,
}
DEFAULT_DECIMAL_FIELDS = frozenset(DEFAULT_DECIMAL_PRECISIONS)

VOLATILE_ROW_FIELDS = frozenset(
    {
        "name",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "idx",
    }
)


class VerificationInputError(ValueError):
    """Evidence is absent or structurally unable to prove the requested claim."""


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not _is_mapping(value):
        raise VerificationInputError(f"{path} must be an object")
    return value


def require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise VerificationInputError(f"{path} must be a list")
    return value


def require_nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationInputError(f"{path} must be a non-empty string")
    return value.strip()


def decimal_value(value: Any) -> tuple[Decimal | None, str | None]:
    """Return (Decimal, error); strings use their decimal representation.

    Floats are converted through str(float), which is the shortest decimal that
    round-trips to that float.  No tolerance or binary comparison is used.
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "boolean is not a decimal value"
    if isinstance(value, Decimal):
        return value, None
    if isinstance(value, int):
        return Decimal(value), None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None, "non-finite float is not a decimal value"
        return Decimal(str(value)), None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "empty string is not a decimal value"
        try:
            return Decimal(text), None
        except InvalidOperation:
            return None, f"invalid decimal literal {value!r}"
    return None, f"unsupported decimal value type {type(value).__name__}"


def decimal_text(value: Decimal) -> str:
    """Canonical numeric text for equality and fingerprints."""
    if value == 0:
        value = Decimal(0)
    return format(value.normalize(), "f")


def decimal_display(value: Decimal) -> str:
    """Human evidence text preserving the captured decimal scale."""
    if value == 0:
        value = Decimal(0).quantize(value)
    return format(value, "f")


def _canonical_jsonable(value: Any) -> Any:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, Decimal):
        return ["decimal", decimal_text(value)]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise VerificationInputError("non-finite float cannot be fingerprinted")
        return ["float", decimal_text(Decimal(str(value)))]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (datetime, date, time)):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, Mapping):
        entries = []
        for key, item in value.items():
            entries.append([canonical_json(key), _canonical_jsonable(item)])
        return ["dict", sorted(entries)]
    if isinstance(value, (list, tuple)):
        return ["seq", [_canonical_jsonable(item) for item in value]]
    if isinstance(value, (set, frozenset)):
        return ["set", sorted((_canonical_jsonable(item) for item in value), key=canonical_json)]
    raise VerificationInputError(f"unsupported evidence value type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_canonical_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def evidence_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _display_value(value: Any) -> Any:
    number, error = decimal_value(value)
    if error is None and number is not None:
        return decimal_text(number)
    if value is None:
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return decimal_text(Decimal(str(value))) if math.isfinite(value) else str(value)
    return canonical_json(value)


def compare_records(
    reference: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    decimal_fields=DEFAULT_DECIMAL_FIELDS,
    path: str = "record",
) -> list[dict[str, Any]]:
    """Compare two field dictionaries exactly.

    Decimal fields use exact Decimal equality.  Null is a distinct value in all
    fields; in particular, null never equals zero.  All fields present in either
    record are compared, so an unexpected field cannot be hidden by an allowlist.
    """
    require_mapping(reference, path + " reference")
    require_mapping(candidate, path + " candidate")
    differences = []
    decimal_fields = frozenset(decimal_fields or ())
    for field in sorted(set(reference) | set(candidate), key=str):
        field_path = f"{path}.{field}"
        if field not in reference or field not in candidate:
            differences.append(
                {
                    "kind": "missing_field",
                    "path": field_path,
                    "field": field,
                    "missing_in": "reference" if field not in reference else "candidate",
                }
            )
            continue
        expected = reference[field]
        actual = candidate[field]
        if field in decimal_fields:
            expected_decimal, expected_error = decimal_value(expected)
            actual_decimal, actual_error = decimal_value(actual)
            if expected_error or actual_error:
                differences.append(
                    {
                        "kind": "invalid_decimal",
                        "path": field_path,
                        "field": field,
                        "expected_error": expected_error,
                        "actual_error": actual_error,
                        "expected": _display_value(expected),
                        "actual": _display_value(actual),
                    }
                )
            elif expected_decimal is None or actual_decimal is None:
                if expected_decimal is not actual_decimal:
                    differences.append(
                        {
                            "kind": "null_mismatch",
                            "path": field_path,
                            "field": field,
                            "expected": None,
                            "actual": decimal_display(actual_decimal) if actual_decimal is not None else None,
                        }
                        if expected_decimal is None
                        else {
                            "kind": "null_mismatch",
                            "path": field_path,
                            "field": field,
                            "expected": decimal_display(expected_decimal),
                            "actual": None,
                        }
                    )
            elif expected_decimal != actual_decimal:
                differences.append(
                    {
                        "kind": "decimal_mismatch",
                        "path": field_path,
                        "field": field,
                        "expected": decimal_display(expected_decimal),
                        "actual": decimal_display(actual_decimal),
                        "difference": decimal_text(actual_decimal - expected_decimal),
                        "precision": DEFAULT_DECIMAL_PRECISIONS.get(field),
                    }
                )
            continue
        if canonical_json(expected) != canonical_json(actual):
            kind = "null_mismatch" if (expected is None) != (actual is None) else "exact_mismatch"
            differences.append(
                {
                    "kind": kind,
                    "path": field_path,
                    "field": field,
                    "expected": _display_value(expected),
                    "actual": _display_value(actual),
                }
            )
    return differences


def _row_fields(kind: str, section: str) -> tuple[str, ...] | None:
    if section == "gl":
        return GL_ROW_FIELDS
    if section == "sle":
        return SLE_ROW_FIELDS
    if section == "payments":
        return PAYMENT_ROW_FIELDS
    if section == "items":
        return TRANSFER_ITEM_ROW_FIELDS if kind == "transfer" else SALE_ITEM_ROW_FIELDS
    return None


def rows_as_mappings(rows: Any, *, section: str, kind: str, path: str) -> list[dict[str, Any]]:
    rows = require_list(rows, path)
    tuple_fields = _row_fields(kind, section)
    output = []
    for index, row in enumerate(rows):
        row_path = f"{path}[{index}]"
        if _is_mapping(row):
            output.append(dict(row))
            continue
        if isinstance(row, (list, tuple)):
            if tuple_fields is None or len(row) != len(tuple_fields):
                raise VerificationInputError(f"{row_path} has no canonical tuple field mapping")
            output.append(dict(zip(tuple_fields, row)))
            continue
        raise VerificationInputError(f"{row_path} must be an object or canonical tuple")
    return output


def _canonical_comparison_value(value: Any, is_decimal: bool) -> Any:
    if is_decimal:
        if value is None:
            return ("decimal-null",)
        number, error = decimal_value(value)
        if error is None:
            return ("decimal", decimal_text(number))
        return ("invalid-decimal", canonical_json(value))
    return ("exact", canonical_json(value))


def _canonical_row(row: Mapping[str, Any], decimal_fields) -> tuple:
    return tuple(
        (str(field), _canonical_comparison_value(row[field], field in decimal_fields))
        for field in sorted(row, key=str)
    )


def _unmatched_rows(reference_rows, candidate_rows, decimal_fields):
    remaining = Counter(_canonical_row(row, decimal_fields) for row in candidate_rows)
    unmatched_reference = []
    for row in reference_rows:
        key = _canonical_row(row, decimal_fields)
        if remaining[key]:
            remaining[key] -= 1
        else:
            unmatched_reference.append(row)
    unmatched_candidate = []
    remaining = Counter(_canonical_row(row, decimal_fields) for row in reference_rows)
    for row in candidate_rows:
        key = _canonical_row(row, decimal_fields)
        if remaining[key]:
            remaining[key] -= 1
        else:
            unmatched_candidate.append(row)
    return unmatched_reference, unmatched_candidate


def compare_row_collections(
    reference_rows: Any,
    candidate_rows: Any,
    *,
    section: str,
    kind: str,
    decimal_fields=DEFAULT_DECIMAL_FIELDS,
    path: str | None = None,
) -> list[dict[str, Any]]:
    """Compare two row collections as order-independent multisets.

    Sorting must not collapse duplicates: multiplicity is part of the financial
    fact.  A duplicated or missing row is therefore a first-class mismatch even
    when every individual row value is otherwise valid.
    """
    path = path or section
    reference = rows_as_mappings(reference_rows, section=section, kind=kind, path=path + ".reference")
    candidate = rows_as_mappings(candidate_rows, section=section, kind=kind, path=path + ".candidate")
    decimal_fields = frozenset(decimal_fields or ())
    reference_counter = Counter(_canonical_row(row, decimal_fields) for row in reference)
    candidate_counter = Counter(_canonical_row(row, decimal_fields) for row in candidate)
    differences = []
    for key in sorted(set(reference_counter) | set(candidate_counter), key=repr):
        expected_count = reference_counter[key]
        actual_count = candidate_counter[key]
        if expected_count == actual_count:
            continue
        row = next((row for row in reference if _canonical_row(row, decimal_fields) == key), None)
        if row is None:
            row = next(row for row in candidate if _canonical_row(row, decimal_fields) == key)
        differences.append(
            {
                "kind": "row_multiplicity",
                "path": path,
                "section": section,
                "expected_count": expected_count,
                "actual_count": actual_count,
                "row": canonical_json(row),
            }
        )

    unmatched_reference, unmatched_candidate = _unmatched_rows(reference, candidate, decimal_fields)
    if len(unmatched_reference) == len(unmatched_candidate) == 1:
        differences.extend(
            {**difference, "row_detail": True}
            for difference in compare_records(
                unmatched_reference[0],
                unmatched_candidate[0],
                decimal_fields=decimal_fields,
                path=path + ".unmatched_row",
            )
        )
    return differences


def compare_generic(
    reference: Any,
    candidate: Any,
    *,
    path: str,
) -> list[dict[str, Any]]:
    """Exact structural comparison for derived summaries and bin deltas."""
    if _is_mapping(reference) and _is_mapping(candidate):
        differences = []
        for key in sorted(set(reference) | set(candidate), key=str):
            key_path = f"{path}.{key}"
            if key not in reference or key not in candidate:
                differences.append({"kind": "missing_field", "path": key_path, "field": key, "missing_in": "reference" if key not in reference else "candidate"})
            else:
                differences.extend(compare_generic(reference[key], candidate[key], path=key_path))
        return differences
    if isinstance(reference, (list, tuple)) and isinstance(candidate, (list, tuple)):
        differences = []
        if len(reference) != len(candidate):
            differences.append({"kind": "row_multiplicity", "path": path, "expected_count": len(reference), "actual_count": len(candidate)})
        for index, (expected, actual) in enumerate(zip(reference, candidate)):
            differences.extend(compare_generic(expected, actual, path=f"{path}[{index}]"))
        return differences
    if canonical_json(reference) == canonical_json(candidate):
        return []
    kind = "null_mismatch" if (reference is None) != (candidate is None) else "exact_mismatch"
    return [{"kind": kind, "path": path, "expected": _display_value(reference), "actual": _display_value(candidate)}]


def _validate_sources(reference_source: Any, candidate_source: Any) -> tuple[str, str]:
    reference_source = require_nonempty_string(reference_source, "reference_source")
    candidate_source = require_nonempty_string(candidate_source, "candidate_source")
    if reference_source == candidate_source:
        raise VerificationInputError("reference_source and candidate_source must be independent and distinct")
    return reference_source, candidate_source


def compare_voucher_snapshots(
    reference: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    kind: str,
    reference_source: str,
    candidate_source: str,
) -> dict[str, Any]:
    """Compare independently captured reference and candidate voucher snapshots.

    The function refuses a same-object or same-source comparison.  Identical
    content can be valid only when it was captured through two distinct sources;
    both source labels and full snapshot fingerprints are returned as evidence.
    """
    if kind not in VOUCHER_KINDS:
        raise VerificationInputError(f"kind must be one of {', '.join(VOUCHER_KINDS)}")
    reference_source, candidate_source = _validate_sources(reference_source, candidate_source)
    require_mapping(reference, "reference snapshot")
    require_mapping(candidate, "candidate snapshot")
    if reference is candidate:
        raise VerificationInputError("reference and candidate snapshots must be independently captured objects")

    required = set(REQUIRED_SNAPSHOT_SECTIONS)
    if kind == "sale":
        required.add("payments")
    for side, snapshot in (("reference", reference), ("candidate", candidate)):
        missing = sorted(section for section in required if section not in snapshot)
        if missing:
            raise VerificationInputError(f"{side} snapshot missing required sections: {', '.join(missing)}")

    differences = []
    compared_sections = []
    differences.extend(compare_records(reference["doc"], candidate["doc"], path="doc"))
    compared_sections.append("doc")
    for section in ROW_SECTIONS:
        if section in reference or section in candidate:
            if section not in reference or section not in candidate:
                differences.append({"kind": "missing_section", "path": section, "missing_in": "reference" if section not in reference else "candidate"})
            else:
                differences.extend(compare_row_collections(reference[section], candidate[section], section=section, kind=kind, path=section))
            compared_sections.append(section)
    for section in GENERIC_SECTIONS:
        differences.extend(compare_generic(reference[section], candidate[section], path=section))
        compared_sections.append(section)

    extra_sections = sorted((set(reference) | set(candidate)) - set(compared_sections) - {"name", "doctype"}, key=str)
    for section in extra_sections:
        if section not in reference or section not in candidate:
            differences.append({"kind": "missing_section", "path": section, "missing_in": "reference" if section not in reference else "candidate"})
        else:
            differences.extend(compare_generic(reference[section], candidate[section], path=section))
        compared_sections.append(section)

    return {
        "ok": not differences,
        "kind": kind,
        "reference": {"source": reference_source, "snapshot_sha256": evidence_sha256(reference)},
        "candidate": {"source": candidate_source, "snapshot_sha256": evidence_sha256(candidate)},
        "independent_sources": True,
        "compared_sections": sorted(compared_sections),
        "differences": differences,
        "difference_count": len(differences),
    }


def _semantic_row_key(row: Mapping[str, Any]) -> str:
    return canonical_json({field: value for field, value in row.items() if field not in VOLATILE_ROW_FIELDS})


def duplicate_semantic_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(_semantic_row_key(row) for row in rows)
    duplicates = []
    for key in sorted((key for key, count in counts.items() if count > 1), key=repr):
        row = next(row for row in rows if _semantic_row_key(row) == key)
        duplicates.append({"key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(), "count": counts[key], "row": canonical_json(row)})
    return duplicates


def _integer_count(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationInputError(f"{path} must be an integer")
    return value


def _decimal_sum(rows: list[Mapping[str, Any]], field: str, path: str) -> Decimal:
    total = Decimal(0)
    for index, row in enumerate(rows):
        value, error = decimal_value(row.get(field))
        if error or value is None:
            raise VerificationInputError(f"{path}[{index}].{field} must be a decimal value")
        total += value
    return total


def _rows_as_objects(value: Any, path: str) -> list[dict[str, Any]]:
    rows = require_list(value, path)
    output = []
    for index, row in enumerate(rows):
        require_mapping(row, f"{path}[{index}]")
        output.append(dict(row))
    return output


def reconcile_retry_evidence(evidence: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """Verify lost-confirmation retry evidence without trusting a caller verdict.

    `rollback_fixture` evidence may demonstrate rollback cleanliness, but it is
    explicitly not proof of a committed retry.  Only `committed_retry` evidence
    with one persisted document, identical first/retry voucher names and exact
    GL/stock count reconciliation can set `proves_committed_retry`.
    """
    if kind not in VOUCHER_KINDS:
        raise VerificationInputError(f"kind must be one of {', '.join(VOUCHER_KINDS)}")
    require_mapping(evidence, "retry evidence")
    required = ("mode", "client_voucher_id", "first_voucher", "retry_voucher", "documents", "gl_entries", "stock_entries", "count_deltas")
    missing = [field for field in required if field not in evidence]
    if missing:
        raise VerificationInputError(f"retry evidence missing required fields: {', '.join(missing)}")

    mode = evidence["mode"]
    if mode not in RETRY_MODES:
        raise VerificationInputError(f"retry mode must be one of {', '.join(RETRY_MODES)}")
    client_voucher_id = require_nonempty_string(evidence["client_voucher_id"], "retry client_voucher_id")
    first_voucher = require_nonempty_string(evidence["first_voucher"], "retry first_voucher")
    retry_voucher = require_nonempty_string(evidence["retry_voucher"], "retry retry_voucher")
    documents = _rows_as_objects(evidence["documents"], "retry documents")
    gl_entries = _rows_as_objects(evidence["gl_entries"], "retry gl_entries")
    stock_entries = _rows_as_objects(evidence["stock_entries"], "retry stock_entries")
    count_deltas = require_mapping(evidence["count_deltas"], "retry count_deltas")
    for field in ("documents", "gl_entries", "stock_entries"):
        if field not in count_deltas:
            raise VerificationInputError(f"retry count_deltas missing {field}")
    deltas = {field: _integer_count(count_deltas[field], f"retry count_deltas.{field}") for field in ("documents", "gl_entries", "stock_entries")}

    differences = []
    if first_voucher != retry_voucher:
        differences.append({"kind": "retry_voucher_mismatch", "expected": first_voucher, "actual": retry_voucher})

    if mode == COMMITTED_RETRY:
        expected_document_count = 1
        if len(documents) != expected_document_count:
            differences.append({"kind": "document_multiplicity", "expected_count": expected_document_count, "actual_count": len(documents)})
        elif documents[0].get("name") != first_voucher:
            differences.append({"kind": "retry_document_mismatch", "expected": first_voucher, "actual": documents[0].get("name")})
        if documents and documents[0].get("docstatus") != 1:
            differences.append({"kind": "document_not_submitted", "voucher": documents[0].get("name"), "docstatus": documents[0].get("docstatus")})
        if deltas["documents"] != 1:
            differences.append({"kind": "count_delta_mismatch", "field": "documents", "expected": 1, "actual": deltas["documents"]})
    else:
        if len(documents) or len(gl_entries) or len(stock_entries):
            differences.append({"kind": "rollback_rows_visible", "documents": len(documents), "gl_entries": len(gl_entries), "stock_entries": len(stock_entries)})
        for field, delta in deltas.items():
            if delta != 0:
                differences.append({"kind": "rollback_persistence", "field": field, "expected": 0, "actual": delta})

    idempotency_entries = []
    if "idempotency_entries" in evidence:
        idempotency_entries = _rows_as_objects(evidence["idempotency_entries"], "retry idempotency_entries")
    elif mode == COMMITTED_RETRY and kind in {"sale", "transfer"}:
        raise VerificationInputError(f"{kind} committed retry evidence missing required idempotency_entries")
    if mode == COMMITTED_RETRY and kind in {"sale", "transfer"}:
        if len(idempotency_entries) != 1:
            differences.append({"kind": "idempotency_multiplicity", "expected_count": 1, "actual_count": len(idempotency_entries)})
        else:
            entry = idempotency_entries[0]
            expected_doctype = VOUCHER_DOCTYPES[kind]
            if entry.get("reference_name") != first_voucher or entry.get("reference_doctype") != expected_doctype:
                differences.append({
                    "kind": "idempotency_reference_mismatch",
                    "expected": {"reference_doctype": expected_doctype, "reference_name": first_voucher},
                    "actual": {"reference_doctype": entry.get("reference_doctype"), "reference_name": entry.get("reference_name")},
                })
            if not entry.get("idempotency_key") or not entry.get("request_fingerprint"):
                differences.append({"kind": "idempotency_entry_incomplete"})
    elif mode == ROLLBACK_FIXTURE and idempotency_entries:
        differences.append({"kind": "rollback_idempotency_rows_visible", "actual_count": len(idempotency_entries)})

    if mode == COMMITTED_RETRY:
        if deltas["gl_entries"] != len(gl_entries):
            differences.append({"kind": "count_delta_mismatch", "field": "gl_entries", "expected": len(gl_entries), "actual": deltas["gl_entries"]})
        if deltas["stock_entries"] != len(stock_entries):
            differences.append({"kind": "count_delta_mismatch", "field": "stock_entries", "expected": len(stock_entries), "actual": deltas["stock_entries"]})

    if gl_entries:
        debit = _decimal_sum(gl_entries, "debit", "retry gl_entries")
        credit = _decimal_sum(gl_entries, "credit", "retry gl_entries")
        if debit != credit:
            differences.append({"kind": "unbalanced_gl", "debit": decimal_text(debit), "credit": decimal_text(credit)})
    elif mode == COMMITTED_RETRY and kind != "transfer":
        differences.append({"kind": "missing_gl_entries"})

    if mode == COMMITTED_RETRY and kind in {"sale", "transfer", "purchase_receipt"} and not stock_entries:
        differences.append({"kind": "missing_stock_entries"})

    if mode == COMMITTED_RETRY and kind == "purchase_receipt" and stock_entries:
        has_stock_increase = False
        for index, row in enumerate(stock_entries):
            actual_qty, error = decimal_value(row.get("actual_qty"))
            if error or actual_qty is None:
                raise VerificationInputError(f"retry stock_entries[{index}].actual_qty must be a decimal value")
            if actual_qty > 0:
                has_stock_increase = True
        if not has_stock_increase:
            differences.append({"kind": "missing_stock_increase"})

    if mode == COMMITTED_RETRY and kind == "purchase_invoice":
        if "links" not in evidence:
            raise VerificationInputError("purchase invoice retry evidence missing required links")
        links = _rows_as_objects(evidence["links"], "retry links")
        if not links:
            differences.append({"kind": "missing_purchase_receipt_link"})
        else:
            wrong_invoice = [row for row in links if row.get("purchase_invoice") != first_voucher]
            if wrong_invoice:
                differences.append({"kind": "purchase_receipt_link_invoice_mismatch", "expected": first_voucher, "actual_count": len(wrong_invoice)})
            receipts = sorted({row.get("purchase_receipt") for row in links if row.get("purchase_receipt")})
            if len(receipts) != 1:
                differences.append({"kind": "purchase_receipt_link_multiplicity", "expected_count": 1, "actual_count": len(receipts), "receipts": receipts})
            missing_detail = [row for row in links if not row.get("pr_detail")]
            if missing_detail:
                differences.append({"kind": "purchase_receipt_link_missing_detail", "actual_count": len(missing_detail)})

        supplier_rows = [row for row in gl_entries if row.get("party_type") == "Supplier"]
        if len(supplier_rows) != 1:
            differences.append({"kind": "payable_multiplicity", "expected_count": 1, "actual_count": len(supplier_rows)})
        else:
            row = supplier_rows[0]
            credit, credit_error = decimal_value(row.get("credit"))
            debit, debit_error = decimal_value(row.get("debit"))
            if credit_error or debit_error or credit is None or debit is None:
                raise VerificationInputError("purchase invoice payable GL row must include decimal debit and credit")
            if credit <= 0 or debit != 0:
                differences.append({"kind": "invalid_payable_posting", "debit": decimal_text(debit), "credit": decimal_text(credit)})

    for duplicate in duplicate_semantic_rows(stock_entries):
        differences.append({"kind": "duplicate_posting", "section": "stock_entries", **duplicate})
    gl_duplicate_keys = {
        _semantic_row_key(row)
        for row in gl_entries
        if row.get("voucher_detail_no") not in (None, "")
    }
    for duplicate in duplicate_semantic_rows(gl_entries):
        # GL rows may legitimately repeat when a voucher has identical ordinary
        # lines.  A repeated non-empty voucher_detail_no, however, is a duplicate
        # posting of the same document line and is never valid retry evidence.
        if duplicate["key_sha256"] in {
            hashlib.sha256(key.encode("utf-8")).hexdigest() for key in gl_duplicate_keys
        }:
            differences.append({"kind": "duplicate_posting", "section": "gl_entries", **duplicate})

    return {
        "ok": not differences,
        "mode": mode,
        "kind": kind,
        "client_voucher_id": client_voucher_id,
        "first_voucher": first_voucher,
        "retry_voucher": retry_voucher,
        "proves_committed_retry": mode == COMMITTED_RETRY and not differences,
        "document_sha256": evidence_sha256(documents),
        "gl_sha256": evidence_sha256(gl_entries),
        "stock_sha256": evidence_sha256(stock_entries),
        "count_deltas": deltas,
        "differences": differences,
    }


def _evidence_part(value: Any, path: str) -> tuple[str, Mapping[str, Any]]:
    require_mapping(value, path)
    source = require_nonempty_string(value.get("source"), path + ".source")
    if "snapshot" not in value:
        raise VerificationInputError(path + " missing snapshot")
    snapshot = require_mapping(value["snapshot"], path + ".snapshot")
    return source, snapshot


def verify_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    require_mapping(operation, "operation")
    kind = operation.get("kind")
    if kind not in VOUCHER_KINDS:
        raise VerificationInputError(f"operation kind must be one of {', '.join(VOUCHER_KINDS)}")
    client_voucher_id = require_nonempty_string(operation.get("client_voucher_id"), "operation client_voucher_id")
    mode = operation.get("mode")
    if mode not in EXECUTION_MODES:
        raise VerificationInputError(f"operation mode must be one of {', '.join(EXECUTION_MODES)}")

    result = {
        "ok": True,
        "kind": kind,
        "client_voucher_id": client_voucher_id,
        "mode": mode,
        "proves_committed_retry": False,
        "differences": [],
    }

    has_comparison = "reference" in operation or "candidate" in operation
    if has_comparison:
        if "reference" not in operation or "candidate" not in operation:
            raise VerificationInputError("operation must include both reference and candidate evidence")
        reference_source, reference = _evidence_part(operation["reference"], "operation reference")
        candidate_source, candidate = _evidence_part(operation["candidate"], "operation candidate")
        comparison = compare_voucher_snapshots(
            reference,
            candidate,
            kind=kind,
            reference_source=reference_source,
            candidate_source=candidate_source,
        )
        result["comparison"] = comparison
        result["ok"] = result["ok"] and comparison["ok"]
        result["differences"].extend({**difference, "check": "comparison"} for difference in comparison["differences"])
    elif mode in (ROLLBACK_FIXTURE, COMMITTED_POSTING):
        raise VerificationInputError(f"{mode} operation missing reference/candidate comparison evidence")

    if "retry" in operation:
        if mode == COMMITTED_POSTING:
            raise VerificationInputError("committed_posting cannot supply retry evidence")
        retry = dict(require_mapping(operation["retry"], "retry"))
        if retry.get("mode", mode) != mode or retry.get("client_voucher_id", client_voucher_id) != client_voucher_id:
            raise VerificationInputError("retry mode and client_voucher_id must match the operation")
        retry.setdefault("mode", mode)
        retry.setdefault("client_voucher_id", client_voucher_id)
        retry_evidence = reconcile_retry_evidence(retry, kind=kind)
        result["retry"] = retry_evidence
        result["proves_committed_retry"] = retry_evidence["proves_committed_retry"]
        result["ok"] = result["ok"] and retry_evidence["ok"]
        result["differences"].extend({**difference, "check": "retry"} for difference in retry_evidence["differences"])
    elif mode == COMMITTED_RETRY:
        raise VerificationInputError("committed retry operation missing retry evidence")

    result["difference_count"] = len(result["differences"])
    return result


def verify_evidence_document(document: Mapping[str, Any]) -> dict[str, Any]:
    require_mapping(document, "evidence document")
    missing = [field for field in ("fixture_id", "upstream_revision", "candidate_revision", "permissions", "seed", "operations") if field not in document]
    if missing:
        raise VerificationInputError(f"evidence document missing required fields: {', '.join(missing)}")
    fixture_id = require_nonempty_string(document.get("fixture_id"), "fixture_id")
    upstream_revision = require_nonempty_string(document.get("upstream_revision"), "upstream_revision")
    candidate_revision = require_nonempty_string(document.get("candidate_revision"), "candidate_revision")
    permissions = require_mapping(document.get("permissions"), "permissions")
    seed = document.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, (int, str)) or (isinstance(seed, str) and not seed.strip()):
        raise VerificationInputError("seed must be a non-empty string or integer")
    operations = require_list(document.get("operations"), "operations")
    if not operations:
        raise VerificationInputError("operations must contain at least one operation")
    results = []
    for index, operation in enumerate(operations):
        try:
            results.append(verify_operation(operation))
        except VerificationInputError as exc:
            raise VerificationInputError(f"operations[{index}]: {exc}") from exc
    return {
        "ok": all(result["ok"] for result in results),
        "fixture_id": fixture_id,
        "upstream_revision": upstream_revision,
        "candidate_revision": candidate_revision,
        "permissions_sha256": evidence_sha256(permissions),
        "seed": seed,
        "operation_count": len(results),
        "operations": results,
        "evidence_sha256": evidence_sha256(document),
    }


def _control_snapshot(amount: str, warehouse: str) -> dict[str, Any]:
    amount_decimal = Decimal(amount)
    return {
        "doc": {"grand_total": amount_decimal, "outstanding_amount": None, "posting_date": "2026-11-27", "docstatus": 1},
        "items": [{"item_code": "ITEM-1", "warehouse": warehouse, "qty": Decimal("1"), "stock_qty": Decimal("1"), "rate": amount_decimal, "amount": amount_decimal}],
        "payments": [],
        "gl": [
            {"account": "Cash", "debit": amount_decimal, "credit": Decimal(0), "voucher_detail_no": None},
            {"account": "Sales", "debit": Decimal(0), "credit": amount_decimal, "voucher_detail_no": None},
        ],
        "sle": [{"item_code": "ITEM-1", "warehouse": warehouse, "actual_qty": Decimal("-1"), "qty_after_transaction": Decimal("9"), "valuation_rate": Decimal("70"), "stock_value": Decimal("630"), "stock_value_difference": Decimal("-70"), "is_cancelled": 0, "voucher_detail_no": "ROW-1"}],
        "gl_summary": {"count": 2, "debit_sum": amount_decimal, "credit_sum": amount_decimal},
        "sle_summary": {"count": 1, "actual_qty_sum": Decimal("-1"), "stock_value_difference_sum": Decimal("-70")},
        "bin": {"count": 1, "actual_qty_net": Decimal("-1"), "stock_value_net": Decimal("-70"), "rows": []},
    }


def _control_retry() -> dict[str, Any]:
    return {
        "mode": COMMITTED_RETRY,
        "client_voucher_id": "negative-control-sale",
        "first_voucher": "SINV-CONTROL",
        "retry_voucher": "SINV-CONTROL",
        "documents": [{"name": "SINV-CONTROL", "docstatus": 1}],
        "gl_entries": [
            {"name": "GL-1", "account": "Cash", "debit": Decimal("100"), "credit": Decimal(0), "voucher_detail_no": None},
            {"name": "GL-2", "account": "Sales", "debit": Decimal(0), "credit": Decimal("100"), "voucher_detail_no": None},
        ],
        "stock_entries": [{"name": "SLE-1", "voucher_detail_no": "ROW-1", "item_code": "ITEM-1", "warehouse": "Shop 1", "actual_qty": Decimal("-1"), "stock_value_difference": Decimal("-70")}],
        "idempotency_entries": [{"idempotency_key": "control-key", "request_fingerprint": "control-fingerprint", "reference_doctype": "Sales Invoice", "reference_name": "SINV-CONTROL"}],
        "count_deltas": {"documents": 1, "gl_entries": 2, "stock_entries": 1},
    }


def _control_result(name: str, caught: bool, difference_kinds: list[str], **extra) -> dict[str, Any]:
    return {"name": name, "caught": caught, "difference_kinds": sorted(set(difference_kinds)), **extra}


def run_negative_controls(*, precision: int = 3) -> dict[str, Any]:
    """Run verifier diagnostics against three deliberately corrupted fixtures.

    `ok` means each corruption was caught.  These controls probe the harness;
    they are not evidence of incorrect production postings.
    """
    if isinstance(precision, bool) or not isinstance(precision, int) or precision < 0 or precision > 12:
        raise VerificationInputError("precision must be an integer from 0 through 12")
    unit = Decimal(1).scaleb(-precision)
    controls = []

    reference = _control_snapshot("100.000", "Shop 1")
    candidate = copy.deepcopy(reference)
    candidate["doc"]["grand_total"] += unit
    verdict = compare_voucher_snapshots(reference, candidate, kind="sale", reference_source="negative-control-reference", candidate_source="negative-control-candidate")
    controls.append(
        _control_result(
            "precision_unit",
            not verdict["ok"],
            [difference["kind"] for difference in verdict["differences"]],
            unit=decimal_text(unit),
            injected="doc.grand_total plus one posting unit",
        )
    )

    reference = _control_snapshot("100.000", "Shop 1")
    second_item = copy.deepcopy(reference["items"][0])
    second_item["warehouse"] = "Shop 2"
    second_item["rate"] = Decimal("200.000")
    second_item["amount"] = Decimal("200.000")
    reference["items"].append(second_item)
    reference["doc"]["grand_total"] = Decimal("300.000")
    candidate = copy.deepcopy(reference)
    candidate["items"][0]["rate"] = Decimal("200.000")
    candidate["items"][0]["amount"] = Decimal("200.000")
    candidate["items"][1]["rate"] = Decimal("100.000")
    candidate["items"][1]["amount"] = Decimal("100.000")
    verdict = compare_voucher_snapshots(reference, candidate, kind="sale", reference_source="negative-control-reference", candidate_source="negative-control-candidate")
    controls.append(
        _control_result(
            "swapped_warehouses",
            not verdict["ok"],
            [difference["kind"] for difference in verdict["differences"]],
            company_total_preserved=reference["doc"]["grand_total"] == candidate["doc"]["grand_total"],
            injected="Shop 1 and Shop 2 amounts swapped",
        )
    )

    retry = _control_retry()
    duplicate = copy.deepcopy(retry["stock_entries"][0])
    duplicate["name"] = "SLE-DUPLICATE"
    retry["stock_entries"].append(duplicate)
    retry["count_deltas"]["stock_entries"] = 2
    verdict = reconcile_retry_evidence(retry, kind="sale")
    controls.append(
        _control_result(
            "retry_duplicate_posting",
            not verdict["ok"],
            [difference["kind"] for difference in verdict["differences"]],
            injected="second stock movement with identical voucher detail semantics",
        )
    )

    return {"ok": all(control["caught"] for control in controls), "precision": precision, "controls": controls}


def _json_dump(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify exact ERPNext financial evidence")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--evidence", type=Path, help="JSON evidence document to verify")
    action.add_argument("--negative-controls", action="store_true", help="prove the verifier catches its three diagnostic corruptions")
    parser.add_argument("--precision", type=int, default=3, help="posting precision for the precision-unit diagnostic")
    parser.add_argument("--output", type=Path, help="optional verdict JSON output path")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.negative_controls:
            payload = run_negative_controls(precision=args.precision)
            exit_code = 0 if payload["ok"] else 1
        else:
            try:
                document = json.loads(args.evidence.read_text(encoding="utf-8"))
            except FileNotFoundError:
                print(f"missing evidence file: {args.evidence}", file=sys.stderr)
                return 2
            except (OSError, json.JSONDecodeError) as exc:
                print(f"invalid evidence file: {exc}", file=sys.stderr)
                return 2
            payload = verify_evidence_document(document)
            exit_code = 0 if payload["ok"] else 1
    except VerificationInputError as exc:
        print(f"invalid verification input: {exc}", file=sys.stderr)
        return 2

    rendered = _json_dump(payload)
    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"could not write verdict output: {exc}", file=sys.stderr)
            return 2
    print(rendered, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
