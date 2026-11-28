#!/usr/bin/env python3
"""Fast exact parity verification, hash-pinned stock-oracle cache, and offline runner.

Pure Python 3.11+ stdlib so this module runs cleanly on host machines and inside
CI without Frappe installed.

Trust model (matching PR #16 stock_oracle_reference.py):
- Cached unmodified stock ERPNext report outputs are keyed by
  (db_fingerprint, erpnext_source_sha256, report, canonical_filters, user,
  precision_settings) and pinned by a SHA-256 manifest.
- Any fingerprint, source hash, precision, filter, user, byte-size, or digest
  mismatch fails closed by raising OracleCacheError.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import concurrent.futures
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Literal

CACHE_SCHEMA_VERSION = "fast-verify-oracle-cache-v1"
PARITY_CACHE_SCHEMA_VERSION = "perf-parity-oracle-cache-v1"
ACCEPTED_CACHE_SCHEMAS = frozenset({CACHE_SCHEMA_VERSION, PARITY_CACHE_SCHEMA_VERSION})
MANIFEST_FILENAME = "manifest.json"
REQUIRED_KEY_FIELDS = (
    "db_fingerprint",
    "erpnext_source_sha256",
    "report",
    "filters",
    "user",
    "precision_settings",
)

NumericPrecision = Literal["currency", "float", "exact"]

# Stock gross_profit.py, get_columns/get_column_names and GrossProfitGenerator.
# Fieldtype describes presentation. The two rates use float precision and the
# percentage uses currency precision in the stock calculations.
GROSS_PROFIT_PRECISION: Mapping[str, NumericPrecision] = {
    "qty": "float",
    "avg._selling_rate": "float",
    "valuation_rate": "float",
    "selling_amount": "currency",
    "base_amount": "currency",  # Internal alias used by older cached row shapes.
    "buying_amount": "currency",
    "gross_profit": "currency",
    "gross_profit_%": "currency",
    "allocated_amount": "currency",
    "indent": "exact",  # Invoice tree metadata.
}

DEFAULT_MONETARY_FIELDS = frozenset(
    {
        "selling_amount",
        "base_amount",
        "buying_amount",
        "gross_profit",
        "debit",
        "credit",
        "debit_in_account_currency",
        "credit_in_account_currency",
        "amount",
        "grand_total",
        "net_total",
        "rounded_total",
        "paid_amount",
        "change_amount",
        "outstanding_amount",
        "total_taxes_and_charges",
        "discount_amount",
        "opening_val",
        "in_val",
        "out_val",
        "bal_val",
        "stock_value",
        "stock_value_difference",
    }
)

DEFAULT_FLOAT_FIELDS = frozenset(
    {
        "qty",
        "opening_qty",
        "in_qty",
        "out_qty",
        "bal_qty",
        "actual_qty",
        "projected_qty",
        "reserved_qty",
        "stock_qty",
        "rate",
    }
)

DEFAULT_STRING_ID_FIELDS = frozenset(
    {
        "item_code",
        "item_name",
        "warehouse",
        "s_warehouse",
        "t_warehouse",
        "sales_invoice",
        "parent_invoice",
        "invoice_or_item",
        "voucher_no",
        "voucher_type",
        "voucher_detail_no",
        "account",
        "party",
        "party_type",
        "customer",
        "company",
        "currency",
        "monthly",
        "month",
        "posting_period",
        "uom",
        "stock_uom",
        "brand",
        "item_group",
    }
)

COLUMN_METADATA_FIELDS = ("fieldname", "label", "fieldtype", "options", "width")

OFFLINE_TEST_GROUPS: dict[str, list[str]] = {
    "fast_verify": ["-m", "unittest", "tools.test_fast_verify"],
    "parity_comparator": ["-m", "unittest", "tools.test_perf_parity_comparator"],
    "fast_check_guardrails": ["-m", "unittest", "tools.test_fast_check_guardrails"],
    "memory_summary": ["-m", "unittest", "tools.test_memory_summary"],
    "khitmee_ops_unit": [
        "-m",
        "unittest",
        "discover",
        "-s",
        "apps/khitmee_ops/khitmee_ops/tests",
        "-t",
        "apps/khitmee_ops",
        "-p",
        "test_*_unit.py",
    ],
}


class OracleCacheError(ValueError):
    """Raised when a hash-pinned stock-oracle cache fails closed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(relative: Any) -> str:
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")):
        raise OracleCacheError(f"unsafe cache artifact path: {relative!r}")
    parts = Path(relative).parts
    if any(part in ("", ".", "..") for part in parts):
        raise OracleCacheError(f"unsafe cache artifact path: {relative!r}")
    return relative


def normalize_oracle_key(key: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize an oracle cache key; fail closed on missing fields."""
    if not isinstance(key, dict):
        raise OracleCacheError("oracle cache key must be a dict")
    normalized: dict[str, Any] = {}
    for field in REQUIRED_KEY_FIELDS:
        if field not in key:
            raise OracleCacheError(f"oracle cache key missing required field: {field}")
        val = key[field]
        if field in ("filters", "precision_settings"):
            if not isinstance(val, dict):
                raise OracleCacheError(f"oracle cache key field {field!r} must be a dict")
            if field == "precision_settings" and not val:
                raise OracleCacheError("oracle cache key precision_settings must not be empty")
            # Round-trip through canonical JSON to reject NaN/non-JSON values.
            try:
                normalized[field] = json.loads(canonical_json(val))
            except (TypeError, ValueError) as exc:
                raise OracleCacheError(f"oracle cache key field {field!r} is not canonical JSON: {exc}")
        else:
            if not isinstance(val, str) or not val.strip():
                raise OracleCacheError(f"oracle cache key field {field!r} must be a non-empty string")
            normalized[field] = val.strip()
    return normalized


def compute_oracle_key_hash(key: dict[str, Any]) -> str:
    return canonical_sha256(normalize_oracle_key(key))


def _entry_core(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_hash": entry["key_hash"],
        "key": entry["key"],
        "payload_path": entry["payload_path"],
        "payload_sha256": entry["payload_sha256"],
        "payload_bytes": entry["payload_bytes"],
        "columns_sha256": entry["columns_sha256"],
        "rows_sha256": entry["rows_sha256"],
        "row_count": entry["row_count"],
    }


def _manifest_entries_root_sha256(entries: dict[str, dict[str, Any]]) -> str:
    ordered = [
        {"key_hash": kh, "entry_sha256": entries[kh]["entry_sha256"]}
        for kh in sorted(entries)
    ]
    return canonical_sha256(ordered)


class StockOracleCache:
    """Hash-pinned cache for verified unmodified stock ERPNext report outputs."""

    def __init__(self, cache_dir: str | Path, manifest_path: str | Path | None = None):
        self.cache_dir = Path(cache_dir)
        self.manifest_path = Path(manifest_path) if manifest_path is not None else (self.cache_dir / MANIFEST_FILENAME)
        self._manifest_schema_version: str | None = None

    def _load_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise OracleCacheError(f"manifest path is not a regular file: {self.manifest_path}")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OracleCacheError(f"corrupted oracle cache manifest: {exc}")
        if not isinstance(manifest, dict):
            raise OracleCacheError("oracle cache manifest must be a JSON object")
        if manifest.get("schema_version") not in ACCEPTED_CACHE_SCHEMAS:
            raise OracleCacheError(f"unsupported oracle cache manifest schema: {manifest.get('schema_version')!r}")
        entries = manifest.get("entries")
        if not isinstance(entries, dict):
            raise OracleCacheError("oracle cache manifest entries must be a dict")
        for key_hash, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("key_hash") != key_hash:
                raise OracleCacheError(f"manifest entry key_hash mismatch for {key_hash!r}")
            try:
                expected_entry_sha = canonical_sha256(_entry_core(entry))
            except KeyError as exc:
                raise OracleCacheError(f"manifest entry {key_hash!r} missing required field {exc}")
            if entry.get("entry_sha256") != expected_entry_sha:
                raise OracleCacheError(f"manifest entry digest mismatch for {key_hash!r}")
        expected_root = _manifest_entries_root_sha256(entries)
        if manifest.get("entries_root_sha256") != expected_root:
            raise OracleCacheError("oracle cache manifest entries_root_sha256 mismatch (manifest tampered)")
        self._manifest_schema_version = str(manifest["schema_version"])
        return manifest

    def _write_manifest(self, entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
        schema_version = self._manifest_schema_version or CACHE_SCHEMA_VERSION
        manifest = {
            "schema_version": schema_version,
            "updated_utc": utc_now(),
            "entries": {kh: entries[kh] for kh in sorted(entries)},
            "entries_root_sha256": _manifest_entries_root_sha256(entries),
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, self.manifest_path)
        return manifest

    def put(
        self,
        key: dict[str, Any],
        columns: list[Any],
        rows: list[Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_key = normalize_oracle_key(key)
        key_hash = canonical_sha256(normalized_key)
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise OracleCacheError("columns and rows must be lists")

        existing_manifest = self._load_manifest()
        schema_version = self._manifest_schema_version or CACHE_SCHEMA_VERSION
        payload = {
            "schema_version": schema_version,
            "key_hash": key_hash,
            "key": normalized_key,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "columns_sha256": canonical_sha256(columns),
            "rows_sha256": canonical_sha256(rows),
            "metadata": metadata or {},
            "created_utc": utc_now(),
        }
        payload_bytes = (canonical_json(payload) + "\n").encode("utf-8")
        rel_path = f"entries/{key_hash}.json"
        dst = self.cache_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(payload_bytes)
        os.replace(tmp, dst)

        entry = {
            "key_hash": key_hash,
            "key": normalized_key,
            "payload_path": rel_path,
            "payload_sha256": sha256_bytes(payload_bytes),
            "payload_bytes": len(payload_bytes),
            "columns_sha256": payload["columns_sha256"],
            "rows_sha256": payload["rows_sha256"],
            "row_count": len(rows),
            "created_utc": payload["created_utc"],
        }
        entry["entry_sha256"] = canonical_sha256(_entry_core(entry))

        entries = dict(existing_manifest["entries"]) if existing_manifest is not None else {}
        entries[key_hash] = entry
        self._write_manifest(entries)
        return entry

    def _verify_and_load_entry(
        self,
        entry: dict[str, Any],
        expected_key: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rel_path = _safe_relative_path(entry.get("payload_path"))
        payload_file = self.cache_dir / rel_path
        if payload_file.is_symlink():
            raise OracleCacheError(f"cached oracle payload must not be a symlink: {rel_path}")
        if not payload_file.is_file():
            raise OracleCacheError(f"cached oracle payload missing: {rel_path}")
        actual_size = payload_file.stat().st_size
        if actual_size != entry.get("payload_bytes"):
            raise OracleCacheError(
                f"cached oracle payload size mismatch for {rel_path}: expected {entry.get('payload_bytes')}, got {actual_size}"
            )
        actual_sha = sha256_file(payload_file)
        if actual_sha != entry.get("payload_sha256"):
            raise OracleCacheError(f"cached oracle payload sha256 mismatch for {rel_path}")
        try:
            payload = json.loads(payload_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OracleCacheError(f"corrupted cached oracle payload {rel_path}: {exc}")
        if payload.get("schema_version") not in ACCEPTED_CACHE_SCHEMAS:
            raise OracleCacheError(f"cached payload schema mismatch for {rel_path}")
        if payload.get("key_hash") != entry["key_hash"]:
            raise OracleCacheError(f"cached payload key_hash mismatch for {rel_path}")
        if canonical_json(payload.get("key")) != canonical_json(entry["key"]):
            raise OracleCacheError(f"cached payload key mismatch for {rel_path}")
        if expected_key is not None and canonical_json(entry["key"]) != canonical_json(expected_key):
            raise OracleCacheError(f"cached entry key drift for {rel_path}")
        columns = payload.get("columns")
        rows = payload.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise OracleCacheError(f"cached payload columns/rows invalid for {rel_path}")
        if len(rows) != entry["row_count"] or payload.get("row_count") != len(rows):
            raise OracleCacheError(f"cached payload row_count mismatch for {rel_path}")
        if canonical_sha256(columns) != entry["columns_sha256"]:
            raise OracleCacheError(f"cached payload columns_sha256 mismatch for {rel_path}")
        if canonical_sha256(rows) != entry["rows_sha256"]:
            raise OracleCacheError(f"cached payload rows_sha256 mismatch for {rel_path}")
        return payload

    def get(
        self,
        key: dict[str, Any],
        *,
        expected_db_fingerprint: str | None = None,
        expected_source_sha256: str | None = None,
        expected_precision: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        normalized_key = normalize_oracle_key(key)
        if expected_db_fingerprint is not None and normalized_key["db_fingerprint"] != expected_db_fingerprint:
            raise OracleCacheError("db_fingerprint mismatch against expected runtime fingerprint")
        if expected_source_sha256 is not None and normalized_key["erpnext_source_sha256"] != expected_source_sha256:
            raise OracleCacheError("erpnext_source_sha256 mismatch against expected runtime source digest")
        if expected_precision is not None and canonical_json(normalized_key["precision_settings"]) != canonical_json(expected_precision):
            raise OracleCacheError("precision_settings mismatch against expected runtime precision")

        manifest = self._load_manifest()
        if manifest is None:
            return None
        key_hash = canonical_sha256(normalized_key)
        entry = manifest["entries"].get(key_hash)
        if entry is None:
            return None
        return self._verify_and_load_entry(entry, expected_key=normalized_key)

    def verify_all(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        if manifest is None:
            raise OracleCacheError(f"oracle cache manifest not found: {self.manifest_path}")
        verified = 0
        for _key_hash, entry in sorted(manifest["entries"].items()):
            self._verify_and_load_entry(entry)
            verified += 1
        return {
            "ok": True,
            "verified_entries": verified,
            "entries_root_sha256": manifest["entries_root_sha256"],
        }


# ---------------------------------------------------------------------------
# Precision-aware & type-aware exact financial comparator
# ---------------------------------------------------------------------------


def _is_numeric_value(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _to_exact_decimal(value: Any) -> Decimal | None:
    if not _is_numeric_value(value):
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return None
    if not dec.is_finite():
        return None
    return dec


def _scrub_label(label: Any) -> str:
    return str(label or "").strip().lower().replace(" ", "_").replace("-", "_")


def _column_fieldnames(columns: list[Any] | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for idx, col in enumerate(columns or []):
        raw = None
        if isinstance(col, dict):
            raw = col.get("fieldname") or _scrub_label(col.get("label"))
        elif isinstance(col, str) and col.strip():
            raw = _scrub_label(col.split(":", 1)[0])
        base = str(raw) if raw else f"__col_{idx}"
        name = base if base not in seen else f"{base}__{idx}"
        seen.add(name)
        names.append(name)
    return names


def _row_to_dict(columns: list[Any] | None, row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    fnames = _column_fieldnames(columns)
    return {fnames[idx] if idx < len(fnames) else f"__col_{idx}": val for idx, val in enumerate(row)}


def _classify_fields(
    columns: list[Any] | None,
    monetary_fields: frozenset[str],
    float_fields: frozenset[str],
    string_id_fields: frozenset[str],
) -> tuple[set[str], set[str], set[str]]:
    monetary = set(monetary_fields)
    floats = set(float_fields)
    string_ids = set(string_id_fields)
    resolved_names = _column_fieldnames(columns)
    for idx, col in enumerate(columns or []):
        if not isinstance(col, dict):
            continue
        fname = resolved_names[idx] if idx < len(resolved_names) else col.get("fieldname")
        ftype = str(col.get("fieldtype") or "")
        if not fname:
            continue
        if ftype == "Currency":
            monetary.add(fname)
            string_ids.discard(fname)
        elif ftype in ("Float", "Percent"):
            floats.add(fname)
            string_ids.discard(fname)
        elif ftype in ("Data", "Link", "Select"):
            string_ids.add(fname)
            monetary.discard(fname)
            floats.discard(fname)
    return monetary, floats, string_ids


def compare_column_metadata_exact(
    candidate_columns: list[Any] | None,
    oracle_columns: list[Any] | None,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    cand = candidate_columns or []
    orac = oracle_columns or []
    cand_names = _column_fieldnames(cand)
    orac_names = _column_fieldnames(orac)
    if cand_names != orac_names:
        mismatches.append({"kind": "columns", "candidate": cand_names, "oracle": orac_names})
    if len(cand) != len(orac):
        mismatches.append({"kind": "column_count", "candidate": len(cand), "oracle": len(orac)})
    for idx, (cc, oc) in enumerate(zip(cand, orac)):
        cc_d = cc if isinstance(cc, dict) else {}
        oc_d = oc if isinstance(oc, dict) else {}
        for field in COLUMN_METADATA_FIELDS:
            if field not in cc_d and field not in oc_d:
                continue
            if type(cc_d.get(field)) is not type(oc_d.get(field)) or cc_d.get(field) != oc_d.get(field):
                mismatches.append(
                    {
                        "kind": "column_metadata",
                        "column_index": idx,
                        "field": field,
                        "candidate": cc_d.get(field),
                        "oracle": oc_d.get(field),
                    }
                )
    return mismatches


def compare_financial_value(
    field: str,
    candidate_val: Any,
    oracle_val: Any,
    *,
    location: str,
    monetary_fields: set[str],
    numeric_precisions: Mapping[str, int],
    string_id_fields: set[str],
) -> dict[str, Any] | None:
    """Compare one cell with strict type/identifier safety and declared ERPNext precision."""
    if candidate_val is None or oracle_val is None:
        if candidate_val is None and oracle_val is None:
            return None
        return {
            "kind": "null_mismatch",
            "field": field,
            "location": location,
            "candidate": candidate_val,
            "oracle": oracle_val,
        }

    # String identifiers and any string value are NEVER coerced to float/Decimal.
    if field in string_id_fields or isinstance(candidate_val, str) or isinstance(oracle_val, str):
        if type(candidate_val) is not type(oracle_val):
            return {
                "kind": "type_mismatch",
                "field": field,
                "location": location,
                "candidate": candidate_val,
                "candidate_type": type(candidate_val).__name__,
                "oracle": oracle_val,
                "oracle_type": type(oracle_val).__name__,
            }
        if candidate_val != oracle_val:
            return {
                "kind": "identifier_mismatch",
                "field": field,
                "location": location,
                "candidate": candidate_val,
                "oracle": oracle_val,
            }
        return None

    # Booleans must match strictly and are never coerced to numbers.
    if isinstance(candidate_val, bool) or isinstance(oracle_val, bool):
        if type(candidate_val) is not type(oracle_val) or candidate_val != oracle_val:
            return {
                "kind": "type_mismatch" if type(candidate_val) is not type(oracle_val) else "value_mismatch",
                "field": field,
                "location": location,
                "candidate": candidate_val,
                "oracle": oracle_val,
            }
        return None

    if field in numeric_precisions or _is_numeric_value(candidate_val) or _is_numeric_value(oracle_val):
        cand_dec = _to_exact_decimal(candidate_val)
        orac_dec = _to_exact_decimal(oracle_val)
        if cand_dec is None or orac_dec is None:
            return {
                "kind": "type_mismatch",
                "field": field,
                "location": location,
                "candidate": candidate_val,
                "oracle": oracle_val,
            }
        precision = numeric_precisions.get(field)
        if precision is None:
            if cand_dec == orac_dec:
                return None
            return {
                "kind": "numeric_mismatch",
                "field": field,
                "location": location,
                "candidate": str(cand_dec),
                "oracle": str(orac_dec),
            }
        quantum = Decimal(10) ** (-precision)
        cand_q = cand_dec.quantize(quantum, rounding=ROUND_HALF_EVEN)
        orac_q = orac_dec.quantize(quantum, rounding=ROUND_HALF_EVEN)
        diff = abs(cand_dec - orac_dec)
        if cand_q != orac_q or diff >= quantum:
            return {
                "kind": "monetary_mismatch" if field in monetary_fields else "numeric_mismatch",
                "field": field,
                "location": location,
                "candidate": str(cand_dec),
                "oracle": str(orac_dec),
                "candidate_quantized": str(cand_q),
                "oracle_quantized": str(orac_q),
                "diff": str(diff),
                "precision": precision,
            }
        return None

    if type(candidate_val) is not type(oracle_val) or candidate_val != oracle_val:
        return {
            "kind": "value_mismatch",
            "field": field,
            "location": location,
            "candidate": candidate_val,
            "oracle": oracle_val,
        }
    return None


def _signature_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return ["Decimal", str(value)]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, (int, float)):
        return ["num", str(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, list):
        return ["list", [_signature_value(v) for v in value]]
    if isinstance(value, dict):
        return ["dict", {str(k): _signature_value(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}]
    return [type(value).__name__, str(value)]


def _canonical_row_signature(row: dict[str, Any]) -> str:
    return canonical_json({str(k): _signature_value(v) for k, v in row.items() if not str(k).startswith("_")})


def _financial_signature_value(
    field: str,
    value: Any,
    *,
    numeric_precisions: Mapping[str, int],
) -> Any:
    """Canonicalize one value with the same numeric semantics as compare_financial_value."""
    if _is_numeric_value(value):
        dec = _to_exact_decimal(value)
        if dec is None:
            return ["invalid_num", type(value).__name__, str(value)]
        precision = numeric_precisions.get(field)
        if precision is None:
            return ["exact_num", *dec.as_integer_ratio()]
        quantum = Decimal(10) ** (-precision)
        try:
            quantized = dec.quantize(quantum, rounding=ROUND_HALF_EVEN)
        except InvalidOperation:
            return ["invalid_num", type(value).__name__, str(value)]
        return ["num", precision, format(quantized, "f")]
    return _signature_value(value)


def _financial_row_signature(
    row: dict[str, Any],
    *,
    numeric_precisions: Mapping[str, int],
) -> str:
    return canonical_json(
        {
            str(k): _financial_signature_value(str(k), v, numeric_precisions=numeric_precisions)
            for k, v in row.items()
            if not str(k).startswith("_")
        }
    )


def compare_financial_reports(
    candidate_columns: list[Any] | None,
    candidate_rows: list[Any] | None,
    oracle_columns: list[Any] | None,
    oracle_rows: list[Any] | None,
    *,
    currency_precision: int = 3,
    float_precision: int = 2,
    monetary_fields: frozenset[str] | set[str] | None = None,
    float_fields: frozenset[str] | set[str] | None = None,
    string_id_fields: frozenset[str] | set[str] | None = None,
    check_order: bool = True,
    check_column_metadata: bool = True,
    identity_fields: tuple[str, ...] | None = None,
    precision_fields: Mapping[str, NumericPrecision] | None = None,
) -> list[dict[str, Any]]:
    """Exact precision-aware and type-aware comparator for financial reports.

    Catches:
    - 1-unit monetary errors at declared currency precision (e.g. 0.001 at precision=3)
    - shop-swap errors even when company totals are identical
    - duplicate rows (both via row count and identity key multiplicity)
    - string identifier corruption (e.g. "001" vs "1" or "001" vs 1)
    - None vs 0 coercion
    - column metadata drift
    When precision_fields is supplied, numeric fields outside it compare exactly.
    """
    mismatches: list[dict[str, Any]] = []
    if check_column_metadata and (candidate_columns or oracle_columns):
        mismatches.extend(compare_column_metadata_exact(candidate_columns, oracle_columns))

    mon_set, flt_set, str_set = _classify_fields(
        oracle_columns or candidate_columns,
        frozenset(monetary_fields) if monetary_fields is not None else DEFAULT_MONETARY_FIELDS,
        frozenset(float_fields) if float_fields is not None else DEFAULT_FLOAT_FIELDS,
        frozenset(string_id_fields) if string_id_fields is not None else DEFAULT_STRING_ID_FIELDS,
    )
    if precision_fields is None:
        precision_fields = {
            **dict.fromkeys(flt_set, "float"),
            **dict.fromkeys(mon_set, "currency"),
            **GROSS_PROFIT_PRECISION,
        }
    precisions = {"currency": currency_precision, "float": float_precision}
    numeric_precisions = {
        field: precisions[rule] for field, rule in precision_fields.items() if rule != "exact"
    }

    cand_dicts = [_row_to_dict(candidate_columns, r) for r in (candidate_rows or [])]
    orac_dicts = [_row_to_dict(oracle_columns, r) for r in (oracle_rows or [])]

    if len(cand_dicts) != len(orac_dicts):
        mismatches.append(
            {
                "kind": "row_count",
                "candidate": len(cand_dicts),
                "oracle": len(orac_dicts),
            }
        )

    # Detect duplicate rows when identity_fields are specified or when exact row multiplicity differs.
    if identity_fields:
        for side_name, rows_list in (("candidate", cand_dicts), ("oracle", orac_dicts)):
            seen: dict[tuple[Any, ...], int] = {}
            for idx, row in enumerate(rows_list):
                ident = tuple(
                    canonical_json(
                        _financial_signature_value(
                            f,
                            row.get(f),
                            numeric_precisions=numeric_precisions,
                        )
                    )
                    for f in identity_fields
                )
                if ident in seen:
                    mismatches.append(
                        {
                            "kind": "duplicate_row",
                            "side": side_name,
                            "row_index": idx,
                            "first_row_index": seen[ident],
                            "identity": {f: row.get(f) for f in identity_fields},
                        }
                    )
                else:
                    seen[ident] = idx
    else:
        cand_counts: dict[str, int] = {}
        orac_counts: dict[str, int] = {}
        for row in cand_dicts:
            sig = _financial_row_signature(row, numeric_precisions=numeric_precisions)
            cand_counts[sig] = cand_counts.get(sig, 0) + 1
        for row in orac_dicts:
            sig = _financial_row_signature(row, numeric_precisions=numeric_precisions)
            orac_counts[sig] = orac_counts.get(sig, 0) + 1
        for sig, count in cand_counts.items():
            if count > orac_counts.get(sig, 0) and count > 1:
                mismatches.append(
                    {
                        "kind": "duplicate_row",
                        "side": "candidate",
                        "candidate_count": count,
                        "oracle_count": orac_counts.get(sig, 0),
                    }
                )

    if not check_order:
        cand_dicts = sorted(cand_dicts, key=_canonical_row_signature)
        orac_dicts = sorted(orac_dicts, key=_canonical_row_signature)

    for idx, (cand_row, orac_row) in enumerate(zip(cand_dicts, orac_dicts)):
        fields = sorted((set(cand_row) | set(orac_row)) - {"_row_index"}, key=str)
        for field in fields:
            diff = compare_financial_value(
                field,
                cand_row.get(field),
                orac_row.get(field),
                location=f"row:{idx}",
                monetary_fields=mon_set,
                numeric_precisions=numeric_precisions,
                string_id_fields=str_set,
            )
            if diff is not None:
                mismatches.append(diff)

    return mismatches


def compare_gross_profit_reports(
    candidate_columns: list[Any] | None,
    candidate_rows: list[Any] | None,
    oracle_columns: list[Any] | None,
    oracle_rows: list[Any] | None,
    *,
    currency_precision: int = 3,
    float_precision: int = 2,
) -> list[dict[str, Any]]:
    return compare_financial_reports(
        candidate_columns,
        candidate_rows,
        oracle_columns,
        oracle_rows,
        currency_precision=currency_precision,
        float_precision=float_precision,
        precision_fields=GROSS_PROFIT_PRECISION,
    )


# ---------------------------------------------------------------------------
# Fast offline test runner
# ---------------------------------------------------------------------------


def _run_test_group(repo_root: Path, group_name: str, args: list[str]) -> dict[str, Any]:
    start = time.monotonic()
    cmd = [sys.executable, "-B", *args]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=60,
        env=env,
    )
    elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)
    match = re.search(r"Ran (\d+) tests?", proc.stdout or "")
    test_count = int(match.group(1)) if match else 0
    ok = proc.returncode == 0 and test_count > 0
    return {
        "group": group_name,
        "ok": ok,
        "returncode": proc.returncode,
        "tests": test_count,
        "elapsed_ms": elapsed_ms,
        "output_tail": (proc.stdout or "").strip()[-800:] if not ok else "",
    }


def run_offline_suite(
    repo_root: str | Path,
    groups: list[str] | None = None,
    max_workers: int = 5,
) -> dict[str, Any]:
    """Run non-conflicting offline test groups in parallel with per-group timings."""
    root = Path(repo_root).resolve()
    selected_names = groups if groups else list(OFFLINE_TEST_GROUPS.keys())
    unknown = [g for g in selected_names if g not in OFFLINE_TEST_GROUPS]
    if unknown:
        raise ValueError(f"unknown offline test group(s): {', '.join(unknown)}")

    start = time.monotonic()
    results_by_name: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(selected_names)))) as pool:
        futures = {
            pool.submit(_run_test_group, root, name, OFFLINE_TEST_GROUPS[name]): name
            for name in selected_names
        }
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            results_by_name[res["group"]] = res

    ordered_results = [results_by_name[name] for name in selected_names]
    total_ms = round((time.monotonic() - start) * 1000.0, 2)
    total_tests = sum(r["tests"] for r in ordered_results)
    all_ok = all(r["ok"] for r in ordered_results)
    return {
        "mode": "offline",
        "ok": all_ok,
        "total_tests": total_tests,
        "total_elapsed_ms": total_ms,
        "groups": ordered_results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast exact parity verifier and offline test runner")
    parser.add_argument("--mode", choices=("offline", "verify-cache"), default="offline")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--groups", default=None, help="comma-separated offline test groups")
    parser.add_argument("--cache-dir", default=None, help="oracle cache directory for --mode verify-cache")
    parser.add_argument("--manifest", default=None, help="optional external manifest path")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "verify-cache":
        if not args.cache_dir:
            print("error: --cache-dir is required for --mode verify-cache", file=sys.stderr)
            return 2
        cache = StockOracleCache(args.cache_dir, manifest_path=args.manifest)
        try:
            summary = cache.verify_all()
        except OracleCacheError as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            else:
                print(f"FAIL oracle-cache: {exc}", file=sys.stderr)
            return 1
        if args.as_json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"PASS oracle-cache: verified {summary['verified_entries']} entries")
        return 0

    groups = [g.strip() for g in args.groups.split(",") if g.strip()] if args.groups else None
    summary = run_offline_suite(args.repo, groups=groups)
    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        for group in summary["groups"]:
            status = "PASS" if group["ok"] else "FAIL"
            print(f"{status} {group['group']}: {group['tests']} tests in {group['elapsed_ms']:.1f}ms")
            if group["output_tail"]:
                print(group["output_tail"])
        print(
            f"TOTAL {'PASS' if summary['ok'] else 'FAIL'}: "
            f"{summary['total_tests']} tests in {summary['total_elapsed_ms']:.1f}ms"
        )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
