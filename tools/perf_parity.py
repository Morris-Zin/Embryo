#!/usr/bin/env python3
"""Maintained live parity suite for the isolated khitmee-perf stack.

The module is importable on a plain host Python: comparison helpers stay pure and
Frappe is imported lazily only inside the perf backend container.
"""
import argparse
import calendar
import gc
import hashlib
import importlib
import inspect
import json
import os
import random
import stat
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT = "khitmee-perf"
FORBIDDEN_PROJECT = "khitmee-erpnext"
COMPANY = "Khit Me Distribution"
ALL_SECTIONS = ("gp", "sb", "search", "voucher")
PERSISTED_TABLES = (
    "tabSales Invoice",
    "tabStock Entry",
    "tabKhitmee Idempotency Log",
    "tabGL Entry",
    "tabStock Ledger Entry",
    "tabUser",
    "tabUser Permission",
)
PACKED_ITEM_PREFILTER_SQL = (
    "SELECT DISTINCT `parent_item` FROM `tabPacked Item` "
    "WHERE `parenttype` = 'Sales Invoice' AND `docstatus` = 1"
)
SLE_VOUCHER_SQL = """
SELECT
    `item_code`, `warehouse`, `voucher_type`, `voucher_no`, `voucher_detail_no`,
    `stock_value`, `stock_value_difference`, `actual_qty`, `posting_datetime`, `creation`
FROM `tabStock Ledger Entry`
WHERE `voucher_no` IN (%s)
    AND `voucher_type` = 'Sales Invoice'
    AND `company` = %s
    AND `is_cancelled` = 0
"""
WAREHOUSES = ("Shop 1", "Dist Hub 1")
ALL_SWEEP_WAREHOUSES = ("Dist Hub 1", "Dist Hub 2", "Shop 1", "Shop 2", "Shop 3", "Shop 4")
DATE_30D_FROM = "2026-10-27"
DATE_1D_FROM = "2026-11-26"
DATE_1Y_FROM = "2025-11-27"
DATE_TO = "2026-11-26"
GP_NUMERIC_FIELDS = (
    "qty",
    "avg._selling_rate",
    "valuation_rate",
    "selling_amount",
    "base_amount",
    "buying_amount",
    "gross_profit",
    "gross_profit_%",
)
GP_EXACT_FIELDS = ("currency",)
STOCK_NUMERIC_FIELDS = (
    "opening_qty", "opening_val", "in_qty", "in_val",
    "out_qty", "out_val", "bal_qty", "bal_val",
)


def as_number(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def numbers_close(left, right, tolerance=0.001):
    left_num = as_number(left)
    right_num = as_number(right)
    if left_num is None or right_num is None:
        return left_num == right_num, 0.0 if left_num == right_num else None
    diff = abs(left_num - right_num)
    return diff <= tolerance + 1e-9, diff


def scalar(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 9)
    return value


def normalize_for_compare(value):
    return scalar(value)


def compare_tree(left, right, path="", differences=None):
    if differences is None:
        differences = []
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            compare_tree(left.get(key), right.get(key), f"{path}.{key}" if path else str(key), differences)
    elif isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            differences.append({"path": path, "kind": "length", "left": len(left), "right": len(right)})
        else:
            for index, (lval, rval) in enumerate(zip(left, right)):
                compare_tree(lval, rval, f"{path}[{index}]", differences)
    else:
        left_norm = normalize_for_compare(left)
        right_norm = normalize_for_compare(right)
        if isinstance(left_norm, float) or isinstance(right_norm, float):
            close, diff = numbers_close(left_norm, right_norm, 0.0000001)
            if not close:
                differences.append({"path": path, "left": left_norm, "right": right_norm, "diff": diff})
        elif left_norm != right_norm:
            differences.append({"path": path, "left": left_norm, "right": right_norm})
    return differences


def column_fieldnames(columns):
    fields = []
    for column in columns or []:
        fields.append(column.get("fieldname") if isinstance(column, dict) else None)
    return fields


def row_as_dict(columns, row, index):
    if isinstance(row, dict):
        result = dict(row)
    else:
        result = dict(zip(column_fieldnames(columns), row))
    result["_row_index"] = index
    return result


TOTAL_ROW_FIELDS = (
    "sales_invoice",
    "item_code",
    "invoice_or_item",
    "warehouse",
    "monthly",
    "month",
    "posting_period",
)
COLUMN_METADATA_FIELDS = ("fieldname", "label", "fieldtype", "options", "width")
ORDER_TIE_FIELDS = ("query_phase", "posting_date", "posting_time")


def is_total_row(row, group_field=None):
    if not isinstance(row, dict):
        return False
    if group_field and row.get(group_field) == "Total":
        return True
    if any(row.get(field) == "Total" for field in TOTAL_ROW_FIELDS):
        return True
    first_key = next((key for key in row if not str(key).startswith("_")), None)
    return first_key is not None and row.get(first_key) == "Total"


def invoice_header_key(row):
    return row.get("sales_invoice") or row.get("invoice_or_item")


def invoice_child_parent_key(row):
    return row.get("parent_invoice") or row.get("sales_invoice")


def row_indent(row):
    try:
        return int(row.get("indent") or 0)
    except (TypeError, ValueError):
        return 0


def validate_invoice_tree(rows):
    errors = []
    current_parent = None
    current_open = False
    seen_children = set()
    for row in rows:
        if is_total_row(row):
            continue
        indent = row_indent(row)
        if indent == 0:
            key = invoice_header_key(row)
            current_parent = key
            current_open = bool(key)
            continue
        if indent == 1:
            parent = invoice_child_parent_key(row)
            if not current_open or current_parent is None:
                errors.append({"kind": "invoice_tree", "error": "indent=1 row has no open indent=0 parent", "row": row.get("_row_index")})
            elif parent != current_parent:
                errors.append({
                    "kind": "invoice_tree",
                    "error": "indent=1 row does not match the contiguous open parent",
                    "row": row.get("_row_index"),
                    "expected_parent": scalar(current_parent),
                    "actual_parent": scalar(parent),
                })
            if parent in seen_children and parent != current_parent:
                errors.append({"kind": "invoice_tree", "error": "child rows for a parent are not contiguous", "row": row.get("_row_index")})
            seen_children.add(parent)
    return errors


def gp_sort_token(value):
    value = scalar(value)
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, float(value))
    return (3, str(value))


def gp_row_tiebreaker(row):
    return tuple(
        (field, gp_sort_token(row.get(field)))
        for field in sorted(set(row) - {"_row_index"}, key=lambda field: str(field))
    )


def canonical_child_sort_key(row):
    return (
        gp_sort_token(row.get("item_code")),
        gp_sort_token(row.get("warehouse")),
        round(as_number(row.get("qty")) or 0, 4),
        round(as_number(row.get("selling_amount")) or 0, 3),
        round(as_number(row.get("buying_amount")) or 0, 3),
        gp_sort_token(row.get("item_name")),
        gp_row_tiebreaker(row),
    )


def canonical_group_sort_key(row, group_field=None):
    return (
        gp_sort_token(row.get("item_code") or row.get(group_field)),
        round(as_number(row.get("selling_amount")) or 0, 3),
        gp_row_tiebreaker(row),
    )


def compare_gp_row_fields(fast_row, upstream_row, tolerance, location):
    mismatches = []
    fields = sorted((set(fast_row) | set(upstream_row)) - {"_row_index"}, key=lambda field: str(field))
    for field in fields:
        if field in GP_NUMERIC_FIELDS:
            fast_raw = fast_row.get(field)
            upstream_raw = upstream_row.get(field)
            if (fast_raw is None) != (upstream_raw is None):
                mismatches.append({
                    "kind": "null_numeric", "field": field, "location": location,
                    "fast": scalar(fast_raw), "upstream": scalar(upstream_raw),
                    "tolerance": tolerance,
                })
                continue
            close, diff = numbers_close(fast_raw, upstream_raw, tolerance)
            if not close:
                mismatches.append({
                    "kind": "numeric_field", "field": field, "location": location,
                    "fast": scalar(fast_raw), "upstream": scalar(upstream_raw),
                    "diff": diff, "tolerance": tolerance,
                })
            continue
        fast_value = scalar(fast_row.get(field))
        upstream_value = scalar(upstream_row.get(field))
        if fast_value != upstream_value:
            mismatches.append({
                "kind": "exact_field" if field in GP_EXACT_FIELDS else "identity_field",
                "field": field, "location": location,
                "fast": fast_value, "upstream": upstream_value,
            })
    return mismatches


def parse_invoice_rows(rows):
    parsed = {}
    order = []
    duplicates = []
    current_key = None
    for row in rows:
        if is_total_row(row):
            continue
        indent = row_indent(row)
        if indent == 0:
            key = invoice_header_key(row)
            current_key = key
            if key in parsed:
                duplicates.append(key)
                continue
            parsed[key] = {"header": row, "children": []}
            order.append(key)
        elif indent == 1:
            parent = invoice_child_parent_key(row)
            target = parent if parent in parsed else current_key
            if target in parsed:
                parsed[target]["children"].append(row)
    return parsed, order, duplicates


def invoice_mode_rows(rows):
    for row in rows:
        if is_total_row(row):
            continue
        if row_indent(row) == 1 or row.get("parent_invoice") is not None:
            return True
        if row_indent(row) == 0 and invoice_header_key(row):
            return True
    return False


def compare_column_metadata(fast_columns, upstream_columns):
    """Compare report column metadata without breaking legacy fieldname-only callers.

    Legacy callers often construct bare ``{"fieldname": ...}`` dictionaries.  A
    metadata key is therefore compared strictly when at least one side supplies
    it, while keys omitted by both sides remain compatible.
    """
    mismatches = []
    if not fast_columns or not upstream_columns:
        return mismatches
    if len(fast_columns) != len(upstream_columns):
        mismatches.append({
            "kind": "column_count",
            "fast": len(fast_columns),
            "upstream": len(upstream_columns),
        })
    for index, (fast_column, upstream_column) in enumerate(zip(fast_columns, upstream_columns)):
        fast_column = fast_column if isinstance(fast_column, dict) else {}
        upstream_column = upstream_column if isinstance(upstream_column, dict) else {}
        for field in COLUMN_METADATA_FIELDS:
            if field not in fast_column and field not in upstream_column:
                continue
            fast_value = scalar(fast_column.get(field))
            upstream_value = scalar(upstream_column.get(field))
            if fast_value != upstream_value:
                mismatches.append({
                    "kind": "column_metadata",
                    "column_index": index,
                    "field": field,
                    "fast": fast_value,
                    "upstream": upstream_value,
                })
    return mismatches


def normalize_order_key(value):
    value = scalar(value)
    if isinstance(value, (list, tuple)):
        return tuple(normalize_order_key(part) for part in value)
    if isinstance(value, dict):
        return tuple(
            (str(key), normalize_order_key(value[key]))
            for key in sorted(value, key=lambda item: str(item))
        )
    return value


def normalize_order_key_sequences(order_keys, fast_rows, upstream_rows):
    """Normalize optional per-row order annotations.

    ``order_keys`` may be ``{"fast": [...], "upstream": [...]}``, a
    ``(fast_keys, upstream_keys)`` pair, or one sequence applied to both sides.
    Sequences are indexed by original body-row positions; a final Total row may
    be omitted.  Row-carried ``_order_key``/``order_key`` annotations
    always take precedence, which is the preferred form for offline fixtures.
    """
    fast_sequence = None
    upstream_sequence = None
    errors = []
    if order_keys is None:
        return fast_sequence, upstream_sequence, errors

    if isinstance(order_keys, dict):
        fast_sequence = order_keys.get("fast")
        upstream_sequence = order_keys.get("upstream")
    elif (
        isinstance(order_keys, (list, tuple))
        and len(order_keys) == 2
        and all(isinstance(sequence, (list, tuple)) for sequence in order_keys)
    ):
        fast_sequence, upstream_sequence = order_keys
    else:
        fast_sequence = upstream_sequence = order_keys

    for side, sequence, rows in (
        ("fast", fast_sequence, fast_rows),
        ("upstream", upstream_sequence, upstream_rows),
    ):
        required_keys = len(rows) - 1 if rows and is_total_row(rows[-1]) else len(rows)
        if sequence is not None and len(sequence) < required_keys:
            errors.append({
                "kind": "order_keys_length",
                "side": side,
                "keys": len(sequence),
                "rows": required_keys,
            })
    return fast_sequence, upstream_sequence, errors


def row_order_key(row, explicit_sequence=None):
    for field in ("_order_key", "order_key", "_order_keys", "order_keys", "_first_seen_key", "first_seen_key", "min_first_seen_key"):
        if field in row:
            return normalize_order_key(row.get(field))

    def tie_value(field):
        if field in row:
            return row.get(field)
        return row.get("_" + field)

    tie_fields_present = [field for field in ORDER_TIE_FIELDS if field in row or "_" + field in row]
    if (
        "query_phase" in tie_fields_present
        or "posting_time" in tie_fields_present
        or len(tie_fields_present) == len(ORDER_TIE_FIELDS)
    ):
        return tuple(normalize_order_key(tie_value(field)) for field in ORDER_TIE_FIELDS)

    row_index = row.get("_row_index")
    if explicit_sequence is not None and isinstance(row_index, int) and row_index < len(explicit_sequence):
        return normalize_order_key(explicit_sequence[row_index])
    return ("__position__", row.get("_order_position", row_index))


def collapse_rows_by_order_bucket(rows, explicit_sequence=None, side="fast"):
    buckets = []
    errors = []
    seen_keys = set()
    for row in rows:
        key = row_order_key(row, explicit_sequence)
        if buckets and buckets[-1][0] == key:
            buckets[-1][1].append(row)
            continue
        if key in seen_keys:
            errors.append({
                "kind": "order_bucket_not_contiguous",
                "side": side,
                "bucket": key,
                "row": row.get("_row_index"),
            })
        seen_keys.add(key)
        buckets.append((key, [row]))
    return buckets, errors


def compare_group_row_bucket(fast_rows, upstream_rows, tolerance, location, group_field=None):
    mismatches = []
    fast_sorted = sorted(fast_rows, key=lambda row: canonical_group_sort_key(row, group_field))
    upstream_sorted = sorted(upstream_rows, key=lambda row: canonical_group_sort_key(row, group_field))
    if len(fast_sorted) != len(upstream_sorted):
        mismatches.append({
            "kind": "group_bucket_row_count",
            "location": location,
            "fast": len(fast_sorted),
            "upstream": len(upstream_sorted),
        })
    for index, (fast_row, upstream_row) in enumerate(zip(fast_sorted, upstream_sorted)):
        mismatches.extend(compare_gp_row_fields(
            fast_row,
            upstream_row,
            tolerance,
            f"{location}:row:{index}",
        ))
    return mismatches


def compare_order_sensitive_group_rows(
    fast_rows,
    upstream_rows,
    tolerance,
    group_field=None,
    fast_sequence=None,
    upstream_sequence=None,
):
    """Compare exact distinct-bucket order while permitting intra-tie permutations."""
    mismatches = []
    fast_buckets, fast_bucket_errors = collapse_rows_by_order_bucket(fast_rows, fast_sequence, "fast")
    upstream_buckets, upstream_bucket_errors = collapse_rows_by_order_bucket(upstream_rows, upstream_sequence, "upstream")
    mismatches.extend(fast_bucket_errors)
    mismatches.extend(upstream_bucket_errors)

    fast_bucket_keys = [key for key, _rows in fast_buckets]
    upstream_bucket_keys = [key for key, _rows in upstream_buckets]
    if fast_bucket_keys != upstream_bucket_keys:
        mismatches.append({
            "kind": "group_bucket_order",
            "fast": fast_bucket_keys,
            "upstream": upstream_bucket_keys,
        })
    if len(fast_buckets) != len(upstream_buckets):
        mismatches.append({
            "kind": "group_bucket_count",
            "fast": len(fast_buckets),
            "upstream": len(upstream_buckets),
        })

    for bucket_index, ((fast_key, fast_bucket_rows), (upstream_key, upstream_bucket_rows)) in enumerate(
        zip(fast_buckets, upstream_buckets)
    ):
        if fast_key != upstream_key:
            mismatches.append({
                "kind": "group_bucket_order_at_position",
                "position": bucket_index,
                "fast": fast_key,
                "upstream": upstream_key,
            })
        mismatches.extend(compare_group_row_bucket(
            fast_bucket_rows,
            upstream_bucket_rows,
            tolerance,
            f"group-bucket:{bucket_index}",
            group_field,
        ))
    return mismatches


def invoice_order_rows(invoices, order):
    rows = []
    for ordinal, key in enumerate(order):
        invoice = invoices.get(key)
        if not invoice:
            continue
        row = dict(invoice["header"])
        row["_order_position"] = ordinal
        rows.append((key, row))
    return rows


def collapse_invoice_order_buckets(invoices, order, side, explicit_sequence=None):
    buckets = []
    errors = []
    seen_keys = set()
    for invoice_key, row in invoice_order_rows(invoices, order):
        bucket_key = row_order_key(row, explicit_sequence)
        if buckets and buckets[-1][0] == bucket_key:
            buckets[-1][1].append(invoice_key)
            continue
        if bucket_key in seen_keys:
            errors.append({
                "kind": "invoice_order_bucket_not_contiguous",
                "side": side,
                "bucket": bucket_key,
                "invoice": scalar(invoice_key),
            })
        seen_keys.add(bucket_key)
        buckets.append((bucket_key, [invoice_key]))
    return buckets, errors


def invoice_key_sort_value(value):
    return str(scalar(value))


def invoice_query_phase(row):
    for field in ("query_phase", "_query_phase"):
        if field in row:
            return normalize_order_key(row.get(field))
    for field in ("first_seen_key", "_first_seen_key", "min_first_seen_key"):
        value = row.get(field)
        if value is not None:
            parts = str(scalar(value)).split("|", 1)
            if parts and parts[0] in {"0", "1"}:
                return int(parts[0])
    return None


def compare_order_sensitive_invoice_order(
    fast_invoices,
    fast_order,
    upstream_invoices,
    upstream_order,
    fast_sequence=None,
    upstream_sequence=None,
):
    mismatches = []
    fast_buckets, fast_errors = collapse_invoice_order_buckets(fast_invoices, fast_order, "fast", fast_sequence)
    upstream_buckets, upstream_errors = collapse_invoice_order_buckets(upstream_invoices, upstream_order, "upstream", upstream_sequence)
    mismatches.extend(fast_errors)
    mismatches.extend(upstream_errors)

    fast_bucket_keys = [key for key, _invoice_keys in fast_buckets]
    upstream_bucket_keys = [key for key, _invoice_keys in upstream_buckets]
    if fast_bucket_keys != upstream_bucket_keys:
        mismatches.append({
            "kind": "invoice_bucket_order",
            "fast": fast_bucket_keys,
            "upstream": upstream_bucket_keys,
        })
    if len(fast_buckets) != len(upstream_buckets):
        mismatches.append({
            "kind": "invoice_bucket_count",
            "fast": len(fast_buckets),
            "upstream": len(upstream_buckets),
        })

    for position, ((fast_key, fast_invoice_keys), (upstream_key, upstream_invoice_keys)) in enumerate(
        zip(fast_buckets, upstream_buckets)
    ):
        if fast_key != upstream_key:
            mismatches.append({
                "kind": "invoice_bucket_order_at_position",
                "position": position,
                "fast": fast_key,
                "upstream": upstream_key,
            })
        if sorted(fast_invoice_keys, key=invoice_key_sort_value) != sorted(upstream_invoice_keys, key=invoice_key_sort_value):
            mismatches.append({
                "kind": "invoice_order",
                "position": position,
                "fast": [scalar(key) for key in fast_invoice_keys],
                "upstream": [scalar(key) for key in upstream_invoice_keys],
            })
    return mismatches


def compare_gross_profit_rows(
    fast_columns,
    fast_data,
    upstream_columns,
    upstream_data,
    tolerance=0.001,
    check_column_metadata=True,
    compare_group_order=False,
    order_keys=None,
):
    """Compare fast and stock Gross Profit rows.

    By default this preserves the historical tie-canonicalizing behavior used
    by the live perf suite.  With ``compare_group_order=True``, grouped and
    invoice outputs are compared as an exact sequence of distinct
    ``(query_phase, posting_date, posting_time)`` buckets; permutations are
    accepted only within rows explicitly annotated into the same tie bucket.
    """
    fast_rows = [row_as_dict(fast_columns, row, i) for i, row in enumerate(fast_data or [])]
    upstream_rows = [row_as_dict(upstream_columns, row, i) for i, row in enumerate(upstream_data or [])]
    mismatches = []

    fast_fields = column_fieldnames(fast_columns)
    upstream_fields = column_fieldnames(upstream_columns)
    group_field = fast_fields[0] if fast_fields else None
    if fast_fields != upstream_fields:
        mismatches.append({"kind": "columns", "fast": fast_fields, "upstream": upstream_fields})
    if check_column_metadata:
        mismatches.extend(compare_column_metadata(fast_columns, upstream_columns))
    if len(fast_rows) != len(upstream_rows):
        mismatches.append({"kind": "row_count", "fast": len(fast_rows), "upstream": len(upstream_rows)})

    fast_sequence, upstream_sequence, order_key_errors = normalize_order_key_sequences(
        order_keys,
        fast_rows,
        upstream_rows,
    )
    mismatches.extend(order_key_errors)

    fast_totals = [row for row in fast_rows if is_total_row(row, group_field)]
    upstream_totals = [row for row in upstream_rows if is_total_row(row, group_field)]
    if len(fast_totals) != 1 or len(upstream_totals) != 1:
        if fast_rows or upstream_rows:
            mismatches.append({"kind": "total_row_count", "fast": len(fast_totals), "upstream": len(upstream_totals)})
    else:
        fast_total_index = fast_rows.index(fast_totals[0])
        upstream_total_index = upstream_rows.index(upstream_totals[0])
        if fast_total_index != len(fast_rows) - 1:
            mismatches.append({
                "kind": "total_row_not_final",
                "side": "fast",
                "position": fast_total_index,
                "expected_position": len(fast_rows) - 1,
            })
        if upstream_total_index != len(upstream_rows) - 1:
            mismatches.append({
                "kind": "total_row_not_final",
                "side": "upstream",
                "position": upstream_total_index,
                "expected_position": len(upstream_rows) - 1,
            })
        if fast_total_index != upstream_total_index:
            mismatches.append({"kind": "total_row_position", "fast": fast_total_index, "upstream": upstream_total_index})
        mismatches.extend(compare_gp_row_fields(fast_totals[0], upstream_totals[0], tolerance, "total"))

    invoice_mode = invoice_mode_rows(fast_rows) or invoice_mode_rows(upstream_rows)
    if invoice_mode:
        mismatches.extend({**error, "side": "fast"} for error in validate_invoice_tree(fast_rows))
        mismatches.extend({**error, "side": "upstream"} for error in validate_invoice_tree(upstream_rows))

        fast_invoices, fast_order, fast_duplicates = parse_invoice_rows(fast_rows)
        upstream_invoices, upstream_order, upstream_duplicates = parse_invoice_rows(upstream_rows)
        for key in fast_duplicates:
            mismatches.append({"kind": "duplicate_invoice_key", "side": "fast", "invoice": scalar(key)})
        for key in upstream_duplicates:
            mismatches.append({"kind": "duplicate_invoice_key", "side": "upstream", "invoice": scalar(key)})

        if compare_group_order:
            mismatches.extend(compare_order_sensitive_invoice_order(
                fast_invoices,
                fast_order,
                upstream_invoices,
                upstream_order,
                fast_sequence,
                upstream_sequence,
            ))

        if set(fast_invoices) != set(upstream_invoices):
            mismatches.append({
                "kind": "invoice_keys",
                "missing_fast": sorted((str(key) for key in set(upstream_invoices) - set(fast_invoices)))[:10],
                "missing_upstream": sorted((str(key) for key in set(fast_invoices) - set(upstream_invoices)))[:10],
            })

        posting_entries = [
            (
                scalar(fast_invoices[key]["header"].get("posting_date")),
                invoice_query_phase(fast_invoices[key]["header"]),
            )
            for key in fast_order
        ]
        for index, (previous, current) in enumerate(zip(posting_entries, posting_entries[1:])):
            previous_date, previous_phase = previous
            current_date, current_phase = current
            same_phase = previous_phase == current_phase
            if same_phase and previous_date is not None and current_date is not None and str(previous_date) < str(current_date):
                mismatches.append({
                    "kind": "invoice_posting_date_order",
                    "side": "fast",
                    "index": index,
                    "previous": previous_date,
                    "current": current_date,
                    "query_phase": previous_phase,
                })

        for key in sorted(set(fast_invoices) & set(upstream_invoices), key=str):
            fast_invoice = fast_invoices[key]
            upstream_invoice = upstream_invoices[key]
            location = f"invoice:{key}:header"
            mismatches.extend(compare_gp_row_fields(
                fast_invoice["header"], upstream_invoice["header"], tolerance, location
            ))
            fast_children = sorted(fast_invoice["children"], key=canonical_child_sort_key)
            upstream_children = sorted(upstream_invoice["children"], key=canonical_child_sort_key)
            if len(fast_children) != len(upstream_children):
                mismatches.append({
                    "kind": "invoice_child_count", "invoice": scalar(key),
                    "fast": len(fast_children), "upstream": len(upstream_children),
                })
            for child_index, (fast_child, upstream_child) in enumerate(zip(fast_children, upstream_children)):
                mismatches.extend(compare_gp_row_fields(
                    fast_child, upstream_child, tolerance, f"invoice:{key}:child:{child_index}"
                ))
        return mismatches

    fast_body = [row for row in fast_rows if not is_total_row(row, group_field)]
    upstream_body = [row for row in upstream_rows if not is_total_row(row, group_field)]
    if compare_group_order:
        mismatches.extend(compare_order_sensitive_group_rows(
            fast_body,
            upstream_body,
            tolerance,
            group_field,
            fast_sequence,
            upstream_sequence,
        ))
        return mismatches

    fast_body = sorted(fast_body, key=lambda row: canonical_group_sort_key(row, group_field))
    upstream_body = sorted(upstream_body, key=lambda row: canonical_group_sort_key(row, group_field))
    for index, (fast_row, upstream_row) in enumerate(zip(fast_body, upstream_body)):
        mismatches.extend(compare_gp_row_fields(fast_row, upstream_row, tolerance, f"group-row:{index}"))
    return mismatches

def stock_key(row):
    return (row.get("item_code"), row.get("warehouse"))


def compare_stock_balance_rows(normal_columns, normal_data, ignore_columns, ignore_data, tolerance=0.001):
    normal_rows = [row_as_dict(normal_columns, row, i) for i, row in enumerate(normal_data or [])]
    ignore_rows = [row_as_dict(ignore_columns, row, i) for i, row in enumerate(ignore_data or [])]
    normal_by_key = {stock_key(row): row for row in normal_rows}
    ignore_by_key = {stock_key(row): row for row in ignore_rows}
    mismatches = []
    if len(normal_by_key) != len(normal_rows) or len(ignore_by_key) != len(ignore_rows):
        mismatches.append({"kind": "duplicate_keys"})
    if set(normal_by_key) != set(ignore_by_key):
        mismatches.append({
            "kind": "keys",
            "missing_normal": sorted(set(ignore_by_key) - set(normal_by_key), key=str)[:10],
            "missing_ignore": sorted(set(normal_by_key) - set(ignore_by_key), key=str)[:10],
        })
    for key in sorted(set(normal_by_key) & set(ignore_by_key), key=str):
        for field in STOCK_NUMERIC_FIELDS:
            close, diff = numbers_close(normal_by_key[key].get(field), ignore_by_key[key].get(field), tolerance)
            if not close:
                mismatches.append({
                    "kind": "numeric_field", "key": key, "field": field,
                    "normal": scalar(normal_by_key[key].get(field)),
                    "ignore": scalar(ignore_by_key[key].get(field)),
                    "diff": diff,
                })
    return mismatches


def normalize_search_payload(payload):
    items = payload.get("items", []) if isinstance(payload, dict) else []
    normalized = []
    for row in items:
        normalized.append({key: scalar(value) for key, value in sorted((row or {}).items())})
    return normalized


def compare_search_results(new_payload, reference_payload):
    return compare_tree(normalize_search_payload(new_payload), normalize_search_payload(reference_payload), path="items")


def gp_explain_offenses(rows, label="query"):
    offenses = []
    si2_seen = False
    sii2_seen = False
    outer_seen = {"tabSales Invoice": False, "tabSales Invoice Item": False}
    forbidden_extras = (
        "bnl join",
        "start temporary",
        "end temporary",
        "using temporary",
        "using filesort",
    )
    for row in rows or []:
        table = str(row.get("table") or row.get("Table") or "")
        access_type = str(row.get("type") or row.get("Type") or "")
        select_type = str(row.get("select_type") or row.get("Select_type") or row.get("SELECT_TYPE") or "")
        key = row.get("key") if "key" in row else row.get("Key")
        key_text = str(key or "")
        extra = str(row.get("Extra") or row.get("extra") or "")
        extra_lower = extra.lower()
        lowered = " ".join((select_type, extra)).lower()

        if access_type == "ALL":
            offenses.append({"query": label, "kind": "full_table_scan", "table": table})
        for forbidden in forbidden_extras:
            if forbidden in lowered:
                offenses.append({"query": label, "kind": forbidden.replace(" ", "_"), "table": table, "extra": extra})

        if table == "si2":
            si2_seen = True
            if key_text != "khitmee_si_co_ds_ret_date" or "using index" not in extra_lower:
                offenses.append({"query": label, "kind": "si2_covering_index", "table": table, "type": access_type, "key": key, "extra": extra})
        elif table == "sii2":
            sii2_seen = True
            if key_text != "khitmee_sii_parent_warehouse" or "using index" not in extra_lower:
                offenses.append({"query": label, "kind": "sii2_covering_index", "table": table, "type": access_type, "key": key, "extra": extra})
        elif table in outer_seen:
            outer_seen[table] = True
            if access_type != "eq_ref" or key_text != "PRIMARY":
                offenses.append({"query": label, "kind": "outer_access", "table": table, "type": access_type, "key": key})

    if not si2_seen:
        offenses.append({"query": label, "kind": "si2_missing"})
    if not sii2_seen:
        offenses.append({"query": label, "kind": "sii2_missing"})
    for table, seen in outer_seen.items():
        if not seen:
            offenses.append({"query": label, "kind": "outer_table_missing", "table": table})
    return offenses


def explain_row_parts(row):
    table = str(row.get("table") or row.get("Table") or "")
    access_type = str(row.get("type") or row.get("Type") or "")
    select_type = str(row.get("select_type") or row.get("Select_type") or row.get("SELECT_TYPE") or "")
    key = row.get("key") if "key" in row else row.get("Key")
    extra = str(row.get("Extra") or row.get("extra") or "")
    return table, access_type, select_type, key, extra


def targeted_explain_offenses(rows, label, expected_table, expected_key, require_covering_index=False):
    """Fail closed unless one exact table access uses the required index without a scan/filesort."""
    offenses = []
    table_seen = False
    for row in rows or []:
        table, access_type, select_type, key, extra = explain_row_parts(row)
        extra_lower = extra.lower()
        if access_type == "ALL":
            offenses.append({"query": label, "kind": "full_table_scan", "table": table})
        if "using filesort" in (select_type + " " + extra).lower():
            offenses.append({"query": label, "kind": "using_filesort", "table": table, "extra": extra})
        if table != expected_table:
            continue
        table_seen = True
        if str(key or "") != expected_key:
            offenses.append({"query": label, "kind": "wrong_index", "table": table, "expected_key": expected_key, "key": key})
        if require_covering_index and "using index" not in extra_lower:
            offenses.append({"query": label, "kind": "covering_index_missing", "table": table, "key": key, "extra": extra})
    if not table_seen:
        offenses.append({"query": label, "kind": "table_missing", "table": expected_table})
    return offenses


def packed_item_prefilter_explain_offenses(rows, label="packed_item_prefilter"):
    return targeted_explain_offenses(
        rows,
        label,
        expected_table="tabPacked Item",
        expected_key="khitmee_pi_pt_ds_item",
        require_covering_index=True,
    )


def sle_voucher_explain_offenses(rows, label="sle_voucher"):
    return targeted_explain_offenses(
        rows,
        label,
        expected_table="tabStock Ledger Entry",
        expected_key="voucher_no_voucher_type_index",
        require_covering_index=False,
    )


def query_rows(frappe, sql, params, fields):
    rows = frappe.db.sql(sql, params, as_dict=True)
    normalized = []
    for row in rows:
        normalized.append(tuple(normalize_for_compare(row.get(field)) for field in fields))
    return sorted(normalized, key=lambda row: tuple(str(value) for value in row))


def emit(kind, payload):
    print(kind + " " + json.dumps(payload, default=str, sort_keys=True), flush=True)


class ParityFailure(RuntimeError):
    pass


def _load_fast_verify_module():
    try:
        return importlib.import_module("fast_verify")
    except ImportError as exc:
        raise ParityFailure(
            "precision-aware live comparator unavailable: fast_verify.py was not embedded or importable"
        ) from exc


def compare_live_gross_profit_reports(
    fast_columns,
    fast_data,
    upstream_columns,
    upstream_data,
    precision_settings,
):
    """Compare live GP output with the exact precision-aware fast_verify comparator.

    The comparator is deliberately fail-closed: without verified precision metadata or
    the legacy banker's-rounding semantics it implements, live parity cannot pass.
    """
    if not isinstance(precision_settings, dict):
        raise ParityFailure("live GP parity requires captured precision settings")
    rounding_method = str(precision_settings.get("rounding_method") or "")
    if "banker" not in rounding_method.lower():
        raise ParityFailure(f"unsupported ERPNext rounding method for exact GP parity: {rounding_method!r}")
    try:
        currency_precision = int(precision_settings["currency_precision"])
        float_precision = int(precision_settings["float_precision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParityFailure("live GP parity requires integer currency/float precision settings") from exc
    verifier = _load_fast_verify_module()
    return verifier.compare_financial_reports(
        fast_columns,
        fast_data,
        upstream_columns,
        upstream_data,
        currency_precision=currency_precision,
        float_precision=float_precision,
        check_order=True,
        check_column_metadata=True,
    )


def resolve_warehouse(frappe, logical_name):
    if frappe.db.exists("Warehouse", logical_name):
        return logical_name
    rows = frappe.get_list("Warehouse", filters={"warehouse_name": logical_name, "disabled": 0}, pluck="name", limit_page_length=5)
    if len(rows) == 1:
        return rows[0]
    rows = frappe.get_list("Warehouse", filters={"name": ["like", logical_name + " - %"], "disabled": 0}, pluck="name", limit_page_length=5)
    if len(rows) == 1:
        return rows[0]
    raise ParityFailure(f"could not resolve warehouse {logical_name!r}")


def company_for_warehouse(frappe, warehouse):
    company = frappe.get_value("Warehouse", warehouse, "company")
    if not company:
        raise ParityFailure(f"warehouse {warehouse} has no company")
    return company


def run_gross_profit(frappe, module_name, filters):
    if module_name == "fast":
        from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import execute
    else:
        from erpnext.accounts.report.gross_profit.gross_profit import execute
    start = time.monotonic()
    columns, data = execute(frappe._dict(dict(filters)))
    return columns, data, time.monotonic() - start


def run_fast_gross_profit_capture_semijoin(frappe, filters):
    """Run the real report once while capturing the exact semi-join SQL it executes."""
    from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import execute

    captured = []
    original_sql = frappe.db.sql

    def capturing_sql(query, *args, **kwargs):
        if (
            isinstance(query, str)
            and "khitmee_si_co_ds_ret_date" in query
            and "`tabSales Invoice` AS `si2`" in query
            and "`tabSales Invoice Item` AS `sii2`" in query
        ):
            params = args[0] if args else kwargs.get("values", kwargs.get("params"))
            captured.append((query, list(params or [])))
        return original_sql(query, *args, **kwargs)

    start = time.monotonic()
    try:
        frappe.db.sql = capturing_sql
        try:
            columns, data = execute(frappe._dict(dict(filters)))
        finally:
            frappe.db.sql = original_sql
    finally:
        if getattr(frappe.db, "sql", None) is capturing_sql:
            frappe.db.sql = original_sql
    return columns, data, time.monotonic() - start, captured


def semijoin_query_label(sql):
    compact = " ".join(str(sql).split()).lower()
    if "`si2`.`is_return` = 1" in compact and "`si2`.`return_against` is not null" in compact:
        return "return"
    return "invoice"


def date_token(value):
    value = scalar(value)
    return str(value or "")[:10]


def invoice_warehouses(invoice):
    warehouses = invoice.get("warehouses", "")
    if isinstance(warehouses, (list, tuple, set)):
        return {str(value) for value in warehouses if value}
    return {part.strip() for part in str(warehouses or "").split(",") if part.strip()}


def invoice_covered_by_case(invoice, filters):
    if filters.get("company") and str(invoice.get("company") or "") != str(filters.get("company")):
        return False
    posting_date = date_token(invoice.get("posting_date"))
    from_date = date_token(filters.get("from_date"))
    to_date = date_token(filters.get("to_date"))
    if from_date and posting_date < from_date:
        return False
    if to_date and posting_date > to_date:
        return False
    warehouse = filters.get("warehouse")
    if warehouse and str(warehouse) not in invoice_warehouses(invoice):
        return False
    is_return = bool(as_number(invoice.get("is_return")) or 0)
    if is_return and not as_number(filters.get("include_returned_invoices")):
        return False
    return True


def audit_gp_coverage_rows(rows, cases):
    """Measure whether structural GP cases exercise real return and bundle invoice rows."""
    case_list = list(cases or [])
    return_case_count = sum(1 for _label, filters in case_list if as_number(filters.get("include_returned_invoices")))
    return_total = 0
    return_covered = 0
    bundle_total = 0
    bundle_covered = 0
    covered_return_cases = set()
    covered_bundle_cases = set()
    for row in rows or []:
        is_return = bool(as_number(row.get("is_return")) or 0)
        has_bundle = bool(as_number(row.get("has_bundle")) or row.get("has_bundle") is True)
        matching = [
            label for label, filters in case_list
            if invoice_covered_by_case(row, filters)
        ]
        if is_return:
            return_total += 1
            if matching:
                return_covered += 1
                covered_return_cases.update(matching)
        if has_bundle:
            bundle_total += 1
            if matching:
                bundle_covered += 1
                covered_bundle_cases.update(matching)

    failures = []
    if not return_case_count:
        failures.append("no_gp_case_includes_returns")
    if return_total == 0:
        failures.append("no_return_invoices_present_in_site")
    elif return_covered == 0:
        failures.append("return_invoices_not_covered_by_gp_cases")
    if bundle_total and bundle_covered == 0:
        failures.append("packed_item_bundle_invoices_not_covered_by_gp_cases")
    return {
        "return_case_count": return_case_count,
        "return_invoices_in_site": return_total,
        "return_invoices_covered": return_covered,
        "return_coverage_cases": sorted(covered_return_cases),
        "bundle_invoices_in_site": bundle_total,
        "bundle_invoices_covered": bundle_covered,
        "bundle_coverage_cases": sorted(covered_bundle_cases),
        "failures": failures,
    }


def load_gp_coverage_invoice_rows(frappe):
    return frappe.db.sql(
        """
        SELECT
            si.name,
            si.company,
            si.posting_date,
            si.is_return,
            GROUP_CONCAT(DISTINCT sii.warehouse) AS warehouses,
            EXISTS(
                SELECT 1
                FROM `tabPacked Item` AS pi
                WHERE pi.parenttype = 'Sales Invoice'
                    AND pi.parent = si.name
                    AND pi.docstatus = 1
            ) AS has_bundle
        FROM `tabSales Invoice` AS si
        INNER JOIN `tabSales Invoice Item` AS sii ON sii.parent = si.name
        WHERE si.docstatus = 1
        GROUP BY si.name, si.company, si.posting_date, si.is_return
        """,
        as_dict=True,
    )


def condensed_explain_rows(rows):
    return [
        {
            "select_type": row.get("select_type") or row.get("Select_type"),
            "table": row.get("table"),
            "type": row.get("type"),
            "key": row.get("key"),
            "rows": row.get("rows"),
            "extra": row.get("Extra"),
        }
        for row in rows or []
    ]


def explain_query_offenses(frappe, label, sql, params, checker):
    rows = frappe.db.sql("EXPLAIN " + sql, params, as_dict=True)
    emit("PARITY_GP_EXPLAIN", {"query": label, "access": condensed_explain_rows(rows)})
    return checker(rows, label)


def _batched_ids(values, size=1000):
    items = list(values or [])
    for offset in range(0, len(items), max(1, int(size))):
        yield items[offset : offset + max(1, int(size))]


def _format_posting_time_key(value):
    if value is None:
        return "00:00:00.000000"
    if isinstance(value, timedelta):
        total_us = max(0, round(value.total_seconds() * 1_000_000))
        hours, rem = divmod(total_us, 3_600_000_000)
        minutes, rem = divmod(rem, 60_000_000)
        seconds, microseconds = divmod(rem, 1_000_000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{microseconds:06d}"
    text = str(value).strip()
    if "." in text:
        head, frac = text.split(".", 1)
        parts = head.split(":")
        h, m, s = (int(parts[i]) if i < len(parts) and parts[i] else 0 for i in range(3))
        return f"{h:02d}:{m:02d}:{s:02d}.{frac[:6].ljust(6, '0')}"
    parts = text.split(":")
    h, m, s = (int(float(parts[i])) if i < len(parts) and parts[i] else 0 for i in range(3))
    return f"{h:02d}:{m:02d}:{s:02d}.000000"


def build_live_invoice_order_keys(frappe, columns, data):
    """Derive exact (query_phase, posting_date, posting_time) tie buckets for Invoice rows."""
    rows = [row_as_dict(columns, row, index) for index, row in enumerate(data or [])]
    if not rows:
        return []
    header_names = []
    header_slots = {}
    for row in rows:
        if is_total_row(row) or row_indent(row) != 0:
            continue
        inv_name = invoice_header_key(row)
        if inv_name:
            name_str = str(inv_name)
            header_names.append(name_str)
            header_slots.setdefault(name_str, []).append(row["_row_index"])

    header_set = set(header_names)
    meta_by_name = {}
    for chunk in _batched_ids(sorted(header_set), 1000):
        placeholders = ", ".join(["%s"] * len(chunk))
        fetched = frappe.db.sql(
            f"""
            SELECT `name`, `posting_date`, `posting_time`, `is_return`, `return_against`
            FROM `tabSales Invoice`
            WHERE `name` IN ({placeholders})
            """,
            chunk,
            as_dict=True,
        )
        for item in fetched or []:
            meta_by_name[str(item.get("name"))] = item

    sequence = [None] * len(rows)
    for inv_name, slots in header_slots.items():
        meta = meta_by_name.get(inv_name)
        if not meta:
            continue
        is_linked_return = bool(int(meta.get("is_return") or 0) == 1 and meta.get("return_against"))
        query_phase = 0 if is_linked_return and str(meta.get("return_against")) not in header_set else 1
        key = (
            query_phase,
            str(scalar(meta.get("posting_date")) or ""),
            _format_posting_time_key(meta.get("posting_time")),
        )
        for slot in slots:
            sequence[slot] = key
    return sequence


def _month_windows(from_date_str, to_date_str):
    start = date.fromisoformat(str(from_date_str)[:10])
    end = date.fromisoformat(str(to_date_str)[:10])
    windows = []
    cur = start
    while cur <= end:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        month_end = min(end, date(cur.year, cur.month, last_day))
        windows.append((cur.isoformat(), month_end.isoformat()))
        if month_end >= end:
            break
        cur = month_end + timedelta(days=1)
    return windows


def run_upstream_gross_profit_piecewise_by_month(frappe, filters):
    """Run stock Gross Profit month-by-month when zero linked returns cross months."""
    from frappe.utils import cint, flt
    from erpnext.accounts.report.gross_profit.gross_profit import execute as stock_execute

    company = filters.get("company")
    from_date = str(filters.get("from_date"))[:10]
    to_date = str(filters.get("to_date"))[:10]
    group_by = filters.get("group_by")
    cross_month = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabSales Invoice` AS `ret`
        INNER JOIN `tabSales Invoice` AS `orig` ON `orig`.`name` = `ret`.`return_against`
        WHERE `ret`.`company` = %s
          AND `ret`.`docstatus` = 1
          AND `ret`.`is_return` = 1
          AND IFNULL(`ret`.`return_against`, '') != ''
          AND (`ret`.`posting_date` BETWEEN %s AND %s OR `orig`.`posting_date` BETWEEN %s AND %s)
          AND DATE_FORMAT(`ret`.`posting_date`, '%%Y-%%m') != DATE_FORMAT(`orig`.`posting_date`, '%%Y-%%m')
        """,
        (company, from_date, to_date, from_date, to_date),
    )
    if cross_month and int(cross_month[0][0] or 0) > 0:
        return run_gross_profit(frappe, "upstream", filters)

    currency_precision = cint(frappe.db.get_default("currency_precision")) or 3
    float_precision = cint(frappe.db.get_default("float_precision")) or 2
    # Stock folds Warehouse qty as one running flt(sum, float_precision); summing
    # per-month rounded totals only equals that when every line qty is already at
    # float_precision.  Otherwise use the exact single full-range run.
    fractional = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabSales Invoice Item` AS `sii`
        INNER JOIN `tabSales Invoice` AS `si` ON `si`.`name` = `sii`.`parent`
        WHERE `si`.`company` = %s
          AND `si`.`docstatus` = 1
          AND `si`.`posting_date` BETWEEN %s AND %s
          AND `sii`.`stock_qty` <> ROUND(`sii`.`stock_qty`, %s)
        """,
        (company, from_date, to_date, float_precision),
    )
    if fractional and int(fractional[0][0] or 0) > 0:
        return run_gross_profit(frappe, "upstream", filters)
    currency = filters.get("currency") or frappe.get_cached_value("Company", company, "default_currency")
    columns = None
    group_field = "warehouse" if group_by == "Warehouse" else "monthly"

    start_wall = time.monotonic()
    monthly_rows = []
    wh_accum = {}
    wh_order = {}
    for month_idx, (m_start, m_end) in enumerate(_month_windows(from_date, to_date)):
        m_filters = frappe._dict({**filters, "from_date": m_start, "to_date": m_end, "currency": currency})
        m_cols, m_data = stock_execute(m_filters)
        if columns is None:
            columns = m_cols
        fields = column_fieldnames(m_cols)
        for row_idx, raw_row in enumerate(m_data or []):
            row = row_as_dict(m_cols, raw_row, row_idx)
            if is_total_row(row, group_field):
                continue
            if group_by == "Monthly":
                ordered_row = [row.get(f) for f in fields] if not isinstance(raw_row, dict) else dict(raw_row)
                monthly_rows.append((row, ordered_row))
            else:
                wh = str(row.get("warehouse") or "")
                wh_order.setdefault(wh, (month_idx, row_idx))
                acc = wh_accum.setdefault(wh, {"qty": Decimal("0"), "base": Decimal("0"), "buying": Decimal("0")})
                acc["qty"] += Decimal(str(row.get("qty") or 0))
                acc["base"] += Decimal(str(row.get("selling_amount") or 0))
                acc["buying"] += Decimal(str(row.get("buying_amount") or 0))
        gc.collect()

    if columns is None:
        m_cols, m_data = stock_execute(frappe._dict({**filters, "from_date": from_date, "to_date": to_date, "currency": currency}))
        return m_cols, m_data, time.monotonic() - start_wall

    fields = column_fieldnames(columns)
    output = []
    total_base = 0.0
    total_buying = 0.0
    if group_by == "Monthly":
        for row_dict, raw_item in monthly_rows:
            total_base += float(row_dict.get("selling_amount") or 0.0)
            total_buying += float(row_dict.get("buying_amount") or 0.0)
            output.append(raw_item)
    else:
        for wh in sorted(wh_accum, key=lambda key: wh_order[key]):
            acc = wh_accum[wh]
            qty_val = flt(float(acc["qty"]), float_precision)
            selling_val = flt(float(acc["base"]), currency_precision)
            buying_val = flt(float(acc["buying"]), currency_precision)
            gross_val = flt(selling_val + abs(buying_val) if buying_val < 0 else selling_val - buying_val, currency_precision)
            row_map = {
                "warehouse": wh,
                "qty": qty_val,
                "avg._selling_rate": flt(selling_val / qty_val, float_precision) if qty_val else 0,
                "valuation_rate": flt(buying_val / qty_val, float_precision) if qty_val else 0,
                "selling_amount": selling_val,
                "buying_amount": buying_val,
                "gross_profit": gross_val,
                "gross_profit_%": flt((gross_val / abs(selling_val)) * 100.0, currency_precision) if selling_val else 0,
                "currency": currency,
            }
            total_base += selling_val
            total_buying += buying_val
            output.append([row_map.get(f) for f in fields])

    total_gross = flt(total_base + abs(total_buying) if total_buying < 0 else total_base - total_buying)
    total_pct = flt((total_gross / abs(total_base)) * 100.0, currency_precision) if total_base else 0
    total_map = {
        group_field: "Total",
        "qty": None,
        "avg._selling_rate": None,
        "valuation_rate": None,
        "selling_amount": total_base,
        "buying_amount": total_buying,
        "gross_profit": total_gross,
        "gross_profit_%": total_pct,
        "currency": currency,
    }
    output.append([total_map.get(f) for f in fields])
    return columns, output, time.monotonic() - start_wall


ORACLE_CACHE_SCHEMA_VERSION = "perf-parity-oracle-cache-v1"
ORACLE_CACHE_REQUIRED_FIELDS = (
    "db_fingerprint",
    "erpnext_source_sha256",
    "report",
    "filters",
    "user",
    "precision_settings",
)


class OracleCacheDriftError(ParityFailure):
    """Raised when a hash-pinned oracle cache entry fails closed."""


def canonical_oracle_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)


def canonical_oracle_sha256(value):
    return hashlib.sha256(canonical_oracle_json(value).encode("utf-8")).hexdigest()


def compute_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_oracle_cache_key(
    *,
    db_fingerprint,
    erpnext_source_sha256,
    report,
    filters,
    user,
    precision_settings,
):
    key = {
        "db_fingerprint": db_fingerprint,
        "erpnext_source_sha256": erpnext_source_sha256,
        "report": report,
        "filters": filters,
        "user": user,
        "precision_settings": precision_settings,
    }
    normalized = {}
    for field in ORACLE_CACHE_REQUIRED_FIELDS:
        val = key.get(field)
        if field in ("filters", "precision_settings"):
            if not isinstance(val, dict) or (field == "precision_settings" and not val):
                raise OracleCacheDriftError(f"oracle cache key field {field!r} must be a non-empty dict")
            normalized[field] = json.loads(canonical_oracle_json(val))
        else:
            if not isinstance(val, str) or not val.strip():
                raise OracleCacheDriftError(f"oracle cache key field {field!r} must be a non-empty string")
            normalized[field] = val.strip()
    return normalized


class ParityOracleCache:
    """Self-contained hash-pinned stock-oracle cache for tools/perf_parity.py."""

    def __init__(self, cache_dir, manifest_path=None):
        self.cache_dir = Path(cache_dir)
        if manifest_path is not None:
            resolved_manifest = Path(manifest_path).resolve(strict=False)
            resolved_cache = self.cache_dir.resolve(strict=False)
            try:
                inside = resolved_manifest.is_relative_to(resolved_cache)
            except AttributeError:
                inside = str(resolved_manifest).startswith(str(resolved_cache) + os.sep)
            if inside:
                raise OracleCacheDriftError(
                    f"external oracle manifest must be rooted outside oracle cache dir: {manifest_path}"
                )
            self.manifest_path = Path(manifest_path)
        else:
            self.manifest_path = self.cache_dir / "manifest.json"

    @staticmethod
    def _entry_core(entry):
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

    @staticmethod
    def _entries_root_sha256(entries):
        return canonical_oracle_sha256(
            [{"key_hash": kh, "entry_sha256": entries[kh]["entry_sha256"]} for kh in sorted(entries)]
        )

    def _load_manifest(self):
        if not self.manifest_path.exists():
            return None
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise OracleCacheDriftError(f"oracle cache manifest is not a regular file: {self.manifest_path}")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OracleCacheDriftError(f"corrupted oracle cache manifest: {exc}")
        if not isinstance(manifest, dict) or manifest.get("schema_version") != ORACLE_CACHE_SCHEMA_VERSION:
            raise OracleCacheDriftError("unsupported oracle cache manifest schema")
        entries = manifest.get("entries")
        if not isinstance(entries, dict):
            raise OracleCacheDriftError("oracle cache manifest entries must be a dict")
        for kh, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("key_hash") != kh:
                raise OracleCacheDriftError(f"oracle cache entry key_hash mismatch for {kh}")
            try:
                expected = canonical_oracle_sha256(self._entry_core(entry))
            except KeyError as exc:
                raise OracleCacheDriftError(f"oracle cache entry {kh} missing field {exc}")
            if entry.get("entry_sha256") != expected:
                raise OracleCacheDriftError(f"oracle cache entry digest mismatch for {kh}")
        if manifest.get("entries_root_sha256") != self._entries_root_sha256(entries):
            raise OracleCacheDriftError("oracle cache manifest root hash mismatch (tampered)")
        return manifest

    def _write_manifest(self, entries):
        manifest = {
            "schema_version": ORACLE_CACHE_SCHEMA_VERSION,
            "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "entries": {kh: entries[kh] for kh in sorted(entries)},
            "entries_root_sha256": self._entries_root_sha256(entries),
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, self.manifest_path)

    def put(self, key, columns, rows):
        normalized_key = build_oracle_cache_key(**key)
        key_hash = canonical_oracle_sha256(normalized_key)
        # Round-trip through canonical JSON so cached types/values are exact JSON.
        clean_columns = json.loads(canonical_oracle_json(columns or []))
        clean_rows = json.loads(canonical_oracle_json(rows or []))
        payload = {
            "schema_version": ORACLE_CACHE_SCHEMA_VERSION,
            "key_hash": key_hash,
            "key": normalized_key,
            "columns": clean_columns,
            "rows": clean_rows,
            "row_count": len(clean_rows),
            "columns_sha256": canonical_oracle_sha256(clean_columns),
            "rows_sha256": canonical_oracle_sha256(clean_rows),
        }
        payload_bytes = (canonical_oracle_json(payload) + "\n").encode("utf-8")
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
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "payload_bytes": len(payload_bytes),
            "columns_sha256": payload["columns_sha256"],
            "rows_sha256": payload["rows_sha256"],
            "row_count": len(clean_rows),
        }
        entry["entry_sha256"] = canonical_oracle_sha256(self._entry_core(entry))
        manifest = self._load_manifest()
        entries = dict(manifest["entries"]) if manifest is not None else {}
        entries[key_hash] = entry
        self._write_manifest(entries)
        return entry

    def get(self, key, *, expected_db_fingerprint=None, expected_source_sha256=None, expected_precision=None):
        normalized_key = build_oracle_cache_key(**key)
        if expected_db_fingerprint is not None and normalized_key["db_fingerprint"] != expected_db_fingerprint:
            raise OracleCacheDriftError("oracle cache db_fingerprint mismatch")
        if expected_source_sha256 is not None and normalized_key["erpnext_source_sha256"] != expected_source_sha256:
            raise OracleCacheDriftError("oracle cache erpnext_source_sha256 mismatch")
        if expected_precision is not None and canonical_oracle_json(normalized_key["precision_settings"]) != canonical_oracle_json(expected_precision):
            raise OracleCacheDriftError("oracle cache precision_settings mismatch")

        manifest = self._load_manifest()
        if manifest is None:
            return None
        key_hash = canonical_oracle_sha256(normalized_key)
        entry = manifest["entries"].get(key_hash)
        if entry is None:
            return None

        rel_path = entry.get("payload_path")
        if not isinstance(rel_path, str) or not rel_path or rel_path.startswith(("/", "\\")) or ".." in Path(rel_path).parts:
            raise OracleCacheDriftError(f"unsafe oracle cache payload path: {rel_path!r}")
        payload_file = self.cache_dir / rel_path
        if payload_file.is_symlink() or not payload_file.is_file():
            raise OracleCacheDriftError(f"oracle cache payload missing or symlink: {rel_path}")
        if payload_file.stat().st_size != entry.get("payload_bytes"):
            raise OracleCacheDriftError(f"oracle cache payload byte size mismatch: {rel_path}")
        if compute_file_sha256(payload_file) != entry.get("payload_sha256"):
            raise OracleCacheDriftError(f"oracle cache payload sha256 mismatch: {rel_path}")
        try:
            payload = json.loads(payload_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OracleCacheDriftError(f"corrupted oracle cache payload {rel_path}: {exc}")
        if payload.get("key_hash") != key_hash or canonical_oracle_json(payload.get("key")) != canonical_oracle_json(normalized_key):
            raise OracleCacheDriftError(f"oracle cache payload key drift: {rel_path}")
        columns = payload.get("columns")
        rows = payload.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list) or len(rows) != entry["row_count"]:
            raise OracleCacheDriftError(f"oracle cache payload row count mismatch: {rel_path}")
        if canonical_oracle_sha256(columns) != entry["columns_sha256"]:
            raise OracleCacheDriftError(f"oracle cache payload columns_sha256 mismatch: {rel_path}")
        if canonical_oracle_sha256(rows) != entry["rows_sha256"]:
            raise OracleCacheDriftError(f"oracle cache payload rows_sha256 mismatch: {rel_path}")
        return columns, rows


FINGERPRINT_EXTRA_TABLES = (
    "tabRepost Item Valuation",
    "tabSales Invoice Item",
    "tabDelivery Note",
    "tabDelivery Note Item",
    "tabPacked Item",
    "tabProduct Bundle",
    "tabItem",
)


def capture_db_fingerprint(frappe, site=None):
    counts = persisted_table_counts(frappe)
    si_meta = frappe.db.sql(
        "SELECT COUNT(*), MAX(modified), MAX(posting_date) FROM `tabSales Invoice` WHERE docstatus = 1"
    )[0]
    sle_meta = frappe.db.sql(
        "SELECT COUNT(*), MAX(modified), MAX(posting_date), MAX(creation), "
        "SUM(stock_value_difference), SUM(actual_qty) "
        "FROM `tabStock Ledger Entry` WHERE is_cancelled = 0"
    )[0]
    table_watermarks = {}
    for table in FINGERPRINT_EXTRA_TABLES:
        row = frappe.db.sql(f"SELECT COUNT(*), MAX(modified) FROM `{table}`")[0]
        table_watermarks[table] = [str(x) if x is not None else None for x in row]
    return canonical_oracle_sha256({
        "site": site or getattr(getattr(frappe, "local", None), "site", None) or "",
        "counts": counts,
        "sales_invoice_meta": [str(x) if x is not None else None for x in si_meta],
        "sle_meta": [str(x) if x is not None else None for x in sle_meta],
        "table_watermarks": table_watermarks,
    })


def capture_erpnext_gp_source_sha256():
    import erpnext.accounts.report.gross_profit.gross_profit as gp_mod

    source_file = inspect.getsourcefile(gp_mod) or getattr(gp_mod, "__file__", None)
    if not source_file or not Path(source_file).is_file():
        raise OracleCacheDriftError("cannot locate upstream ERPNext gross_profit.py source for hash pinning")
    return compute_file_sha256(source_file)


def capture_precision_settings(frappe):
    from frappe.utils import cint

    return {
        "currency_precision": cint(frappe.db.get_default("currency_precision")) or 3,
        "float_precision": cint(frappe.db.get_default("float_precision")) or 2,
        "rounding_method": str(frappe.db.get_default("rounding_method") or "Banker's Rounding (legacy)"),
    }


def format_section_timings(section_timings, total_seconds):
    return {
        "sections": {name: round(float(sec), 3) for name, sec in sorted(section_timings.items())},
        "total_seconds": round(float(total_seconds), 3),
    }


def run_gp_parity(frappe, args):
    mismatches = []
    explain_offenses = []
    cases = []
    first_company = company_for_warehouse(frappe, resolve_warehouse(frappe, "Shop 1"))
    for window, from_date in (("1d", DATE_1D_FROM), ("30d", DATE_30D_FROM)):
        for logical in WAREHOUSES:
            warehouse = resolve_warehouse(frappe, logical)
            company = company_for_warehouse(frappe, warehouse)
            for group_by in ("Invoice", "Item Code"):
                for include_returned in (0, 1):
                    cases.append((f"{window}-{logical.replace(' ', '_')}-{group_by.replace(' ', '_')}-ret{include_returned}", {
                        "company": company,
                        "from_date": from_date,
                        "to_date": DATE_TO,
                        "warehouse": warehouse,
                        "group_by": group_by,
                        "include_returned_invoices": include_returned,
                    }))
    grouped_ranges = [
        ("30d", DATE_30D_FROM, DATE_TO),
        ("start_2m", "2025-11-26", "2025-12-31"),
        ("1y", DATE_1Y_FROM, DATE_TO),
    ]
    custom_from_date = getattr(args, "custom_from_date", None)
    custom_to_date = getattr(args, "custom_to_date", None)
    if custom_from_date or custom_to_date:
        if not custom_from_date or not custom_to_date:
            raise ParityFailure("custom gross-profit ranges require both --custom-from-date and --custom-to-date")
        grouped_ranges.append(("custom", custom_from_date, custom_to_date))
    long_range_mode = getattr(args, "gp_1y", None) or "warehouse"
    for window, from_date, to_date in grouped_ranges:
        for group_by in ("Warehouse", "Monthly"):
            if window == "1y" and (long_range_mode == "none" or (long_range_mode == "warehouse" and group_by != "Warehouse")):
                # Each 1y stock-oracle case costs ~30 min; 1y qty/amount is
                # covered cheaply by the ledger reconciliation case below.
                emit("PARITY_GP_CASE_SKIPPED_BY_PLAN", {"case": f"{window}-{group_by}", "gp_1y": long_range_mode})
                continue
            for include_returned in (0, 1):
                cases.append((f"{window}-{group_by}-ret{include_returned}", {
                    "company": first_company,
                    "from_date": from_date,
                    "to_date": to_date,
                    "group_by": group_by,
                    "include_returned_invoices": include_returned,
                }))
    cases.append(("empty-range", {
        "company": first_company,
        "from_date": "1999-01-01",
        "to_date": "1999-01-01",
        "warehouse": resolve_warehouse(frappe, "Shop 1"),
        "group_by": "Invoice",
        "include_returned_invoices": 0,
    }))

    executed_cases = 0
    failed_cases = 0
    captured_semijoin = {}
    oracle_cache = None
    oracle_db_fingerprint = None
    oracle_source_sha256 = None
    oracle_precision = None
    oracle_cache_dir = getattr(args, "oracle_cache_dir", None)
    oracle_manifest = getattr(args, "oracle_manifest", None)
    runtime_precision = capture_precision_settings(frappe)
    if oracle_manifest and not oracle_cache_dir:
        raise OracleCacheDriftError("--oracle-manifest requires --oracle-cache-dir")
    if oracle_cache_dir:
        oracle_cache = ParityOracleCache(oracle_cache_dir, manifest_path=oracle_manifest)
        oracle_db_fingerprint = capture_db_fingerprint(frappe, getattr(args, "site", None))
        oracle_source_sha256 = capture_erpnext_gp_source_sha256()
        oracle_precision = runtime_precision
    for label, filters in cases:
        capture_for_explain = label.startswith("30d-") and filters.get("group_by") == "Invoice" and filters.get("include_returned_invoices") == 1
        if capture_for_explain:
            fast_columns, fast_data, fast_seconds, captured = run_fast_gross_profit_capture_semijoin(frappe, filters)
            logical = "Shop 1" if "Shop_1" in label else "Dist Hub 1"
            for sql, params in captured:
                captured_semijoin.setdefault((logical, semijoin_query_label(sql)), (sql, params))
        else:
            fast_columns, fast_data, fast_seconds = run_gross_profit(frappe, "fast", filters)
        oracle_cache_hit = False
        if oracle_cache is not None:
            cache_key = build_oracle_cache_key(
                db_fingerprint=oracle_db_fingerprint,
                erpnext_source_sha256=oracle_source_sha256,
                report="Gross Profit",
                filters=filters,
                user=str(getattr(getattr(frappe, "session", None), "user", None) or "Administrator"),
                precision_settings=oracle_precision,
            )
            t_cache = time.monotonic()
            cached = oracle_cache.get(
                cache_key,
                expected_db_fingerprint=oracle_db_fingerprint,
                expected_source_sha256=oracle_source_sha256,
                expected_precision=oracle_precision,
            )
            if cached is not None:
                upstream_columns, upstream_data = cached
                upstream_seconds = time.monotonic() - t_cache
                oracle_cache_hit = True
            else:
                if label.startswith("1y-") and filters.get("group_by") in {"Warehouse", "Monthly"}:
                    upstream_columns, upstream_data, upstream_seconds = run_upstream_gross_profit_piecewise_by_month(frappe, filters)
                else:
                    upstream_columns, upstream_data, upstream_seconds = run_gross_profit(frappe, "upstream", filters)
                oracle_cache.put(cache_key, upstream_columns, upstream_data)
        elif label.startswith("1y-") and filters.get("group_by") in {"Warehouse", "Monthly"}:
            upstream_columns, upstream_data, upstream_seconds = run_upstream_gross_profit_piecewise_by_month(frappe, filters)
        else:
            upstream_columns, upstream_data, upstream_seconds = run_gross_profit(frappe, "upstream", filters)
        case_mismatches = compare_live_gross_profit_reports(
            fast_columns,
            fast_data,
            upstream_columns,
            upstream_data,
            runtime_precision,
        )
        executed_cases += 1
        if case_mismatches:
            failed_cases += 1
        mismatches.extend({**mismatch, "case": label} for mismatch in case_mismatches)
        case_event = {
            "case": label,
            "fast_rows": len(fast_data or []),
            "upstream_rows": len(upstream_data or []),
            "fast_seconds": round(fast_seconds, 3),
            "upstream_seconds": round(upstream_seconds, 3),
            "mismatches": len(case_mismatches),
        }
        if oracle_cache is not None:
            case_event["oracle_cache_hit"] = oracle_cache_hit
        emit("PARITY_GP_CASE", case_event)
        for mismatch in case_mismatches[:10]:
            emit("PARITY_GP_MISMATCH", {"case": label, **mismatch})

    from khitmee_ops.profit_summary.reconcile import reconcile_with_coverage

    reconcile_start = time.monotonic()
    reconcile = reconcile_with_coverage(first_company, DATE_1Y_FROM, DATE_TO)
    drifts = list(reconcile["drifts"])
    if reconcile["clean_days"] < reconcile["total_days"]:
        # Zero drift over unbuilt days proves nothing: require full coverage.
        drifts.append({
            "field": "clean_day_coverage",
            "stored": reconcile["clean_days"],
            "ledger": reconcile["total_days"],
        })
    executed_cases += 1
    if drifts:
        failed_cases += 1
        mismatches.extend({"kind": "summary_ledger_drift", "case": "1y-summary-ledger-reconcile", **drift} for drift in drifts)
    emit("PARITY_GP_CASE", {
        "case": "1y-summary-ledger-reconcile",
        "fast_seconds": round(time.monotonic() - reconcile_start, 3),
        "mismatches": len(drifts),
        "clean_days": reconcile["clean_days"],
        "total_days": reconcile["total_days"],
    })
    for drift in drifts[:10]:
        emit("PARITY_GP_MISMATCH", {"case": "1y-summary-ledger-reconcile", **drift})

    coverage = audit_gp_coverage_rows(load_gp_coverage_invoice_rows(frappe), cases)
    emit("PARITY_GP_COVERAGE", coverage)
    explain_offenses.extend({"query": "coverage", "kind": failure} for failure in coverage["failures"])

    for logical in WAREHOUSES:
        for query_label in ("invoice", "return"):
            captured = captured_semijoin.get((logical, query_label))
            if captured is None:
                explain_offenses.append({"query": f"{logical}:{query_label}", "kind": "semijoin_sql_not_executed"})
                continue
            sql, params = captured
            rows = frappe.db.sql("EXPLAIN " + sql, params, as_dict=True)
            emit("PARITY_GP_EXPLAIN", {"warehouse": logical, "query": query_label, "access": condensed_explain_rows(rows)})
            explain_offenses.extend(gp_explain_offenses(rows, f"{logical}:{query_label}"))

    explain_offenses.extend(explain_query_offenses(
        frappe,
        "packed_item_prefilter",
        PACKED_ITEM_PREFILTER_SQL,
        [],
        packed_item_prefilter_explain_offenses,
    ))
    explain_offenses.extend(explain_query_offenses(
        frappe,
        "sle_voucher",
        SLE_VOUCHER_SQL,
        ["PARITY-MISSING-VOUCHER", first_company],
        sle_voucher_explain_offenses,
    ))
    stats = {
        "executed_cases": executed_cases,
        "skipped_cases": 0,
        "failed_cases": failed_cases + (1 if explain_offenses else 0),
    }
    emit("PARITY_GP_SUMMARY", {"mismatches": len(mismatches), "explain_offenses": len(explain_offenses), **stats})
    if mismatches or explain_offenses:
        raise ParityFailure(f"Fast Gross Profit parity failed: mismatches={len(mismatches)} explain_offenses={len(explain_offenses)}")
    return stats


def run_stock_balance(filters):
    import frappe
    from erpnext.stock.report.stock_balance.stock_balance import execute
    start = time.monotonic()
    columns, data = execute(frappe._dict(dict(filters)))
    return columns, data, time.monotonic() - start


def expected_stock_closing_entry(frappe, company, from_date):
    rows = frappe.get_list(
        "Stock Closing Entry",
        filters={"company": company, "to_date": ["<", from_date], "docstatus": 1, "status": "Completed"},
        fields=["name", "to_date", "status"],
        order_by="to_date desc, creation desc",
        limit_page_length=1,
    )
    return rows[0] if rows else None


def selected_stock_closing_entry(frappe, filters):
    from erpnext.stock.doctype.stock_closing_entry.stock_closing_entry import StockClosing
    closing = StockClosing(filters["company"], filters["from_date"], filters["from_date"])
    selected = closing.last_closing_balance
    if isinstance(selected, dict):
        return selected.get("name")
    return getattr(selected, "name", None)


def run_sb_parity(frappe, args):
    mismatches = []
    for logical in WAREHOUSES:
        warehouse = resolve_warehouse(frappe, logical)
        company = company_for_warehouse(frappe, warehouse)
        for window, from_date in (("1d", DATE_1D_FROM), ("30d", DATE_30D_FROM)):
            filters = {"company": company, "warehouse": warehouse, "from_date": from_date, "to_date": DATE_TO}
            expected = expected_stock_closing_entry(frappe, company, from_date)
            selected_name = selected_stock_closing_entry(frappe, filters)
            expected_name = expected.get("name") if expected else None
            closing_ok = selected_name == expected_name
            emit("PARITY_SB_CLOSING", {
                "case": f"{window}-{logical}", "expected": expected_name, "selected": selected_name, "ok": closing_ok,
            })
            if not closing_ok:
                mismatches.append({"kind": "stock_closing_entry", "case": f"{window}-{logical}", "expected": expected_name, "selected": selected_name})

            normal_columns, normal_data, normal_seconds = run_stock_balance(filters)
            ignore_filters = dict(filters)
            ignore_filters["ignore_closing_balance"] = 1
            ignore_columns, ignore_data, ignore_seconds = run_stock_balance(ignore_filters)
            case_mismatches = compare_stock_balance_rows(normal_columns, normal_data, ignore_columns, ignore_data)
            mismatches.extend({**mismatch, "case": f"{window}-{logical}"} for mismatch in case_mismatches)
            emit("PARITY_SB_CASE", {
                "case": f"{window}-{logical}",
                "rows": len(normal_data or []),
                "normal_seconds": round(normal_seconds, 3),
                "ignore_seconds": round(ignore_seconds, 3),
                "mismatches": len(case_mismatches),
            })
    emit("PARITY_SB_SUMMARY", {"mismatches": len(mismatches)})
    if mismatches:
        raise ParityFailure(f"Stock Balance parity failed: mismatches={len(mismatches)}")


def reference_search_items(search_module, term, warehouse, price_list, limit=15, customer=None, uom=None):
    from frappe.utils import cint
    term = str(term or "").strip()
    if not term:
        return {"items": []}
    limit = min(max(cint(limit) or 15, 1), 25)
    ordered = []
    seen = set()

    def add(item_code):
        if item_code and item_code not in seen:
            seen.add(item_code)
            ordered.append(item_code)

    add(search_module._exact_barcode_item(term))
    for row in search_module._prefix_item_codes(term, limit):
        add(row.item_code)
    for item_code in search_module._substring_item_codes(term, max(limit * 4, limit)):
        add(item_code)
    candidate_codes = search_module._permitted_item_codes(ordered[: max(limit * 4, limit)])
    metadata = search_module._fetch_item_metadata(candidate_codes)
    metadata = [row for row in metadata if search_module._item_matches_term(row, term)]
    order = {item_code: index for index, item_code in enumerate(candidate_codes)}
    metadata.sort(key=lambda row: order.get(row.item_code, len(order)))
    metadata = metadata[:limit]
    search_module._fetch_rates_and_quantities(metadata, warehouse, price_list, customer=customer, uom=uom)
    from khitmee_ops.api.payloads import public_item_row
    return {"items": [public_item_row(row) for row in metadata]}


def discover_search_terms(frappe):
    rows = frappe.db.sql(
        """
        SELECT item.name AS item_code, item.item_name, item.item_group, item.brand,
               COALESCE(barcode.barcode, '') AS barcode
        FROM `tabItem` AS item
        LEFT JOIN `tabItem Barcode` AS barcode ON barcode.parent = item.name AND barcode.parentfield = 'barcodes'
        WHERE item.disabled = 0 AND item.has_variants = 0
        ORDER BY item.name
        LIMIT 80
        """,
        as_dict=True,
    )
    terms = ["KM-00001", "%", "_", "definitely-no-such-item-xyz"]
    if rows:
        first = rows[0]
        for value in (first.get("item_code"), first.get("item_name"), first.get("item_group"), first.get("brand"), first.get("barcode")):
            if value:
                terms.append(str(value))
        terms.append(str(first.get("item_name") or first.get("item_code")).swapcase())
        for row in rows:
            for field in ("item_code", "item_name", "item_group", "brand"):
                value = str(row.get(field) or "")
                if any(ord(char) > 127 for char in value):
                    terms.append(value)
                    break
            else:
                continue
            break
    terms.append("မရှိသောကုန်ပစ္စည်း")
    output = []
    for term in terms:
        if term and term not in output:
            output.append(term)
    return output


def restricted_parity_user(frappe):
    local_part = "parity.stock.user"
    email = local_part + "@example.com"
    suffix = 0
    while frappe.db.exists("User", email):
        suffix += 1
        email = f"{local_part}+{suffix}@example.com"
    roles = [role for role in ("Stock User", "Sales User") if frappe.db.exists("Role", role)]
    if not roles:
        raise ParityFailure("no Stock User or Sales User role exists for restricted search parity")
    doc = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": "Parity",
        "last_name": "Rollback User",
        "enabled": 1,
        "user_type": "System User",
        "roles": [{"role": role} for role in roles],
    })
    doc.flags.no_welcome_mail = True
    doc.flags.ignore_password_policy = True
    doc.insert(ignore_permissions=True)
    return email, True


def add_temporary_item_user_permission(frappe, user):
    item_codes = frappe.get_list(
        "Item",
        filters={"disabled": 0, "has_variants": 0},
        pluck="name",
        limit_page_length=50,
    )
    if len(item_codes) < 2:
        raise ParityFailure("need at least two non-variant items for allowed/denied search permission parity")
    for item_code in item_codes:
        if frappe.db.exists("User Permission", {"user": user, "allow": "Item", "for_value": item_code}):
            continue
        doc = frappe.get_doc({
            "doctype": "User Permission",
            "user": user,
            "allow": "Item",
            "for_value": item_code,
            "apply_to_all_doctypes": 1,
        })
        doc.insert(ignore_permissions=True)
        denied_item = next((candidate for candidate in item_codes if candidate != item_code), None)
        if not denied_item:
            raise ParityFailure("could not choose a distinct denied item for search permission parity")
        return item_code, denied_item
    raise ParityFailure("could not create a unique rollback-only Item User Permission for search parity")


def search_payload_item_codes(payload):
    return {
        str(row.get("item_code") or row.get("name") or "")
        for row in (payload.get("items", []) if isinstance(payload, dict) else [])
        if isinstance(row, dict)
    }


def evaluate_search_permission_probe(allowed_payload, denied_payload, allowed_item, denied_item):
    failures = []
    if str(allowed_item) not in search_payload_item_codes(allowed_payload):
        failures.append("allowed_item_not_returned")
    if str(denied_item) in search_payload_item_codes(denied_payload):
        failures.append("denied_item_returned")
    return failures


def run_search_parity(frappe, args):
    import khitmee_ops.api.search as search_module
    warehouse = resolve_warehouse(frappe, "Shop 1")
    price_list = args.price_list
    mismatches = []
    terms = discover_search_terms(frappe)
    for term in terms:
        new_payload = search_module.search_items(q=term, warehouse=warehouse, price_list=price_list, limit=15)
        reference_payload = reference_search_items(search_module, term, warehouse, price_list, limit=15)
        differences = compare_search_results(new_payload, reference_payload)
        mismatches.extend({**difference, "term": term} for difference in differences)
        emit("PARITY_SEARCH_CASE", {
            "term": term,
            "new_count": len(new_payload.get("items", [])),
            "reference_count": len(reference_payload.get("items", [])),
            "differences": len(differences),
        })
        for difference in differences[:5]:
            emit("PARITY_SEARCH_MISMATCH", {"term": term, **difference})

    permission_probe = {"calls": 0, "user": None, "temporary_user": False}
    original_permitted = search_module._permitted_item_codes

    def spy_permitted(item_codes):
        permission_probe["calls"] += 1
        return original_permitted(item_codes)

    frappe.db.sql("SAVEPOINT khitmee_perf_parity_user")
    try:
        restricted_user, temporary_user = restricted_parity_user(frappe)
        restriction_item, denied_item = add_temporary_item_user_permission(frappe, restricted_user)
        permission_probe["user"] = restricted_user
        permission_probe["temporary_user"] = temporary_user
        permission_probe["restriction_item"] = restriction_item
        permission_probe["denied_item"] = denied_item
        frappe.set_user(restricted_user)
        search_module._permitted_item_codes = spy_permitted
        try:
            allowed_payload = search_module.search_items(q=restriction_item, warehouse=warehouse, price_list=price_list, limit=5)
            denied_payload = search_module.search_items(q=denied_item, warehouse=warehouse, price_list=price_list, limit=5)
        finally:
            search_module._permitted_item_codes = original_permitted
    finally:
        search_module._permitted_item_codes = original_permitted
        frappe.db.sql("ROLLBACK TO SAVEPOINT khitmee_perf_parity_user")
        frappe.set_user("Administrator")

    permission_failures = evaluate_search_permission_probe(
        allowed_payload,
        denied_payload,
        permission_probe.get("restriction_item"),
        permission_probe.get("denied_item"),
    )
    permission_probe["allowed_returned"] = str(permission_probe.get("restriction_item")) in search_payload_item_codes(allowed_payload)
    permission_probe["denied_returned"] = str(permission_probe.get("denied_item")) in search_payload_item_codes(denied_payload)
    permission_probe["assertion_failures"] = permission_failures
    permission_ok = permission_probe["calls"] > 0 and not permission_failures
    emit("PARITY_SEARCH_PERMISSION", {**permission_probe, "ok": permission_ok})
    if permission_probe["calls"] <= 0:
        mismatches.append({"kind": "permission_filter_not_invoked"})
    mismatches.extend({"kind": failure} for failure in permission_failures)
    emit("PARITY_SEARCH_SUMMARY", {"terms": len(terms), "mismatches": len(mismatches)})
    if mismatches:
        raise ParityFailure(f"search parity failed: mismatches={len(mismatches)}")


def capture_gl(frappe, voucher_type, voucher_no):
    fields = [
        "account", "debit", "credit", "debit_in_account_currency", "credit_in_account_currency",
        "account_currency", "cost_center", "party_type", "party", "against_voucher_type", "is_cancelled",
    ]
    rows = query_rows(frappe, "SELECT * FROM `tabGL Entry` WHERE voucher_type=%s AND voucher_no=%s", (voucher_type, voucher_no), fields)
    debit_index = fields.index("debit")
    credit_index = fields.index("credit")
    return rows, {
        "count": len(rows),
        "debit_sum": round(sum(as_number(row[debit_index]) or 0.0 for row in rows), 6),
        "credit_sum": round(sum(as_number(row[credit_index]) or 0.0 for row in rows), 6),
    }


def capture_sle(frappe, voucher_type, voucher_no):
    fields = [
        "item_code", "warehouse", "actual_qty", "qty_after_transaction", "valuation_rate",
        "stock_value", "stock_value_difference", "is_cancelled",
    ]
    rows = query_rows(frappe, "SELECT * FROM `tabStock Ledger Entry` WHERE voucher_type=%s AND voucher_no=%s", (voucher_type, voucher_no), fields)
    qty_index = fields.index("actual_qty")
    value_index = fields.index("stock_value_difference")
    return rows, {
        "count": len(rows),
        "actual_qty_sum": round(sum(as_number(row[qty_index]) or 0.0 for row in rows), 6),
        "stock_value_difference_sum": round(sum(as_number(row[value_index]) or 0.0 for row in rows), 6),
    }


def bin_rows(frappe, pairs):
    output = {}
    for item_code, warehouse in sorted(set(pairs)):
        row = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": warehouse},
            ["actual_qty", "projected_qty", "reserved_qty", "valuation_rate", "stock_value"],
            as_dict=True,
        ) or {}
        output[(item_code, warehouse)] = tuple(normalize_for_compare(row.get(field) or 0) for field in ("actual_qty", "projected_qty", "reserved_qty", "valuation_rate", "stock_value"))
    return output


def bin_deltas(before, after):
    deltas = {}
    for key in sorted(set(before) | set(after), key=str):
        left = before.get(key) or (0, 0, 0, 0, 0)
        right = after.get(key) or (0, 0, 0, 0, 0)
        deltas[key] = tuple(round((as_number(right[i]) or 0.0) - (as_number(left[i]) or 0.0), 9) for i in range(5))
    return {
        "count": len(deltas),
        "actual_qty_net": round(sum(row[0] for row in deltas.values()), 9),
        "stock_value_net": round(sum(row[4] for row in deltas.values()), 9),
        "rows": sorted(deltas.items(), key=lambda item: str(item[0])),
    }


def capture_sale_invoice(frappe, name, bin_before):
    doc = frappe.get_doc("Sales Invoice", name)
    doc_fields = ["grand_total", "rounded_total", "paid_amount", "change_amount", "outstanding_amount", "net_total", "total_taxes_and_charges", "discount_amount"]
    payment_fields = ["mode_of_payment", "amount", "account"]
    gl, gl_summary = capture_gl(frappe, "Sales Invoice", name)
    sle, sle_summary = capture_sle(frappe, "Sales Invoice", name)
    pairs = [(item.get("item_code"), item.get("warehouse") or doc.set_warehouse) for item in doc.items]
    return {
        "doc": {field: normalize_for_compare(doc.get(field)) for field in doc_fields},
        "items": sorted(tuple(normalize_for_compare(item.get(field)) for field in ("item_code", "warehouse", "qty", "stock_qty", "rate", "amount")) for item in doc.items),
        "payments": sorted(tuple(normalize_for_compare(row.get(field)) for field in payment_fields) for row in doc.payments),
        "gl": gl,
        "gl_summary": gl_summary,
        "sle": sle,
        "sle_summary": sle_summary,
        "bin": bin_deltas(bin_before, bin_rows(frappe, pairs)),
    }


def capture_stock_entry(frappe, name, bin_before):
    doc = frappe.get_doc("Stock Entry", name)
    doc_fields = ["total_amount", "total_outgoing_value", "total_incoming_value", "posting_date"]
    gl, gl_summary = capture_gl(frappe, "Stock Entry", name)
    sle, sle_summary = capture_sle(frappe, "Stock Entry", name)
    pairs = []
    for item in doc.items:
        if item.get("s_warehouse"):
            pairs.append((item.get("item_code"), item.get("s_warehouse")))
        if item.get("t_warehouse"):
            pairs.append((item.get("item_code"), item.get("t_warehouse")))
    return {
        "doc": {field: normalize_for_compare(doc.get(field)) for field in doc_fields},
        "items": sorted(tuple(normalize_for_compare(item.get(field)) for field in ("item_code", "s_warehouse", "t_warehouse", "qty", "transfer_qty", "valuation_rate", "amount")) for item in doc.items),
        "gl": gl,
        "gl_summary": gl_summary,
        "sle": sle,
        "sle_summary": sle_summary,
        "bin": bin_deltas(bin_before, bin_rows(frappe, pairs)),
    }


class CommitGuard:
    def __init__(self, frappe):
        self.frappe = frappe
        self.calls = 0
        self.original = None

    def __enter__(self):
        self.original = self.frappe.db.commit

        def guarded_commit(*args, **kwargs):
            self.calls += 1
            raise ParityFailure("commit guard tripped during rollback-only parity verification")

        self.frappe.db.commit = guarded_commit
        return self

    def __exit__(self, exc_type, exc, tb):
        self.frappe.db.commit = self.original
        return False


def voucher_result_name(result):
    if isinstance(result, dict):
        return result.get("voucher_no")
    return getattr(result, "name", None)


def clear_document_cache(frappe):
    cache = getattr(getattr(frappe, "local", None), "document_cache", None)
    if hasattr(cache, "clear"):
        cache.clear()


def capture_with_rollback(frappe, label, pairs, build_document, capture_document):
    try:
        before = bin_rows(frappe, pairs)
        result = build_document()
        name = voucher_result_name(result)
        if not name:
            raise ParityFailure(f"{label} did not return a voucher name")
        snapshot = capture_document(frappe, name, before)
        return name, snapshot
    finally:
        frappe.db.rollback()
        clear_document_cache(frappe)


def choose_sale_inputs(frappe, warehouse, seed, lines=3, override=False):
    rng = random.Random(seed)
    rows = frappe.db.sql(
        "SELECT name, stock_uom FROM `tabItem` WHERE disabled=0 AND has_variants=0 AND is_stock_item=1 ORDER BY name LIMIT 200",
        as_dict=True,
    )
    if len(rows) < lines:
        raise ParityFailure("not enough stock items for voucher parity carts")
    chosen = rng.sample(rows, lines)
    items = []
    for row in chosen:
        uom = row.stock_uom or "Nos"
        alternate_uom = frappe.db.get_value("UOM Conversion Detail", {"parent": row.name}, "uom")
        if alternate_uom and alternate_uom != uom and rng.randrange(2):
            uom = alternate_uom
        item = {"item_code": row.name, "qty": rng.randint(1, 4), "uom": uom}
        if override:
            item["rate"] = round(rng.uniform(1.01, 99.99), rng.choice((2, 3)))
        items.append(item)
    customer = frappe.db.get_value("Customer", {"disabled": 0}, "name", order_by="name")
    price_list = frappe.db.get_value("Price List", {"enabled": 1, "selling": 1}, "name", order_by="name")
    payment_mode = frappe.db.get_value("Mode of Payment", {"enabled": 1, "type": "Cash"}, "name") or frappe.db.get_value("Mode of Payment", {"enabled": 1}, "name")
    if not customer or not price_list:
        raise ParityFailure("customer or selling price list missing for voucher parity")
    return {"warehouse": warehouse, "customer": customer, "price_list": price_list, "payment_mode": payment_mode, "items": items}


def build_new_sale(frappe, vouchers, inputs, override=False, client_voucher_id="parity-sale"):
    return lambda: vouchers.submit_sale(
        client_voucher_id,
        inputs["warehouse"],
        inputs["customer"],
        inputs["price_list"],
        json.dumps(inputs["items"]),
        payment_mode=inputs.get("payment_mode"),
        printer_type="voucher",
        override_rate=override,
    )


def build_classic_sale(frappe, vouchers, inputs, override=False):
    cleaned = vouchers._validate_items(inputs["items"])

    def create():
        warehouse_row = vouchers._validate_warehouse(inputs["warehouse"], "Warehouse")
        vouchers._validate_sale_inputs(inputs["customer"], inputs["price_list"], inputs.get("payment_mode"))
        invoice_items = []
        for row in cleaned:
            item = {"item_code": row["item_code"], "qty": row["qty"], "uom": row["uom"], "warehouse": inputs["warehouse"]}
            if override and row["rate"] is not None:
                item["rate"] = row["rate"]
            invoice_items.append(item)
        doc = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": inputs["customer"],
            "company": warehouse_row.company,
            "posting_date": frappe.utils.nowdate(),
            "posting_time": frappe.utils.nowtime(),
            "set_posting_time": 1,
            "update_stock": 1,
            "set_warehouse": inputs["warehouse"],
            "selling_price_list": inputs["price_list"],
            "is_pos": 1 if inputs.get("payment_mode") else 0,
            "items": invoice_items,
        })
        if inputs.get("payment_mode"):
            doc.append("payments", {"mode_of_payment": inputs["payment_mode"], "amount": 0})
        doc.flags.ignore_pos_profile = True
        if override:
            doc.flags.ignore_pricing_rule = True
        doc.set_missing_values()
        doc.calculate_taxes_and_totals()
        if inputs.get("payment_mode"):
            payment_amount = vouchers._sales_invoice_payable_total(doc)
            for payment in doc.payments:
                payment.amount = payment_amount
        if hasattr(doc, "set_paid_amount"):
            doc.set_paid_amount()
        if hasattr(doc, "set_account_for_mode_of_payment"):
            doc.set_account_for_mode_of_payment()
        doc.insert()
        doc.submit()
        return doc
    return create


def sale_pairs(inputs):
    return [(row["item_code"], inputs["warehouse"]) for row in inputs["items"]]


def build_new_transfer(frappe, vouchers, inputs, client_voucher_id="parity-transfer"):
    return lambda: vouchers.submit_transfer(
        client_voucher_id,
        inputs["source"],
        inputs["target"],
        "direct",
        outgoing_stock_entry=None,
        items=json.dumps(inputs["items"]),
        printer_type="voucher",
    )


def build_classic_transfer(frappe, vouchers, inputs):
    cleaned = vouchers._validate_items(inputs["items"])

    def create():
        source_row = vouchers._validate_warehouse(inputs["source"], "Source Warehouse")
        vouchers._validate_warehouse(inputs["target"], "Target Warehouse")
        doc = frappe.get_doc({
            "doctype": "Stock Entry",
            "stock_entry_type": "Material Transfer",
            "purpose": "Material Transfer",
            "company": source_row.company,
            "posting_date": frappe.utils.nowdate(),
            "posting_time": frappe.utils.nowtime(),
            "set_posting_time": 1,
            "from_warehouse": inputs["source"],
            "to_warehouse": inputs["target"],
            "add_to_transit": 0,
            "items": [
                {"item_code": row["item_code"], "qty": row["qty"], "uom": row["uom"], "s_warehouse": inputs["source"], "t_warehouse": inputs["target"]}
                for row in cleaned
            ],
        })
        doc.insert()
        doc.submit()
        return doc
    return create


def transfer_pairs(inputs):
    pairs = []
    for row in inputs["items"]:
        pairs.append((row["item_code"], inputs["source"]))
        pairs.append((row["item_code"], inputs["target"]))
    return pairs


def count_names(frappe, doctype, names):
    if not names:
        return 0
    placeholders = ", ".join(["%s"] * len(names))
    return int(frappe.db.sql(f"SELECT COUNT(*) FROM `tab{doctype}` WHERE name IN ({placeholders})", list(names))[0][0])


def count_idempotency_keys(frappe, keys):
    if not keys:
        return 0
    placeholders = ", ".join(["%s"] * len(keys))
    return int(frappe.db.sql(f"SELECT COUNT(*) FROM `tabKhitmee Idempotency Log` WHERE idempotency_key IN ({placeholders})", list(keys))[0][0])


def run_voucher_parity(frappe, args):
    import khitmee_ops.api.vouchers as vouchers
    from khitmee_ops.api.payloads import idempotency_key, request_fingerprint

    frappe.set_user("Administrator")
    shop = resolve_warehouse(frappe, "Shop 1")
    shop2 = resolve_warehouse(frappe, "Shop 2")
    dist = resolve_warehouse(frappe, "Dist Hub 1")
    differences = []
    try:
        tax_templates_available = int(frappe.db.count("Sales Taxes and Charges Template"))
    except Exception:
        tax_templates_available = None
    sale_names = []
    transfer_names = []
    idempotency_keys = []

    with CommitGuard(frappe):
        pos_inputs = choose_sale_inputs(frappe, shop, args.seed + 1, lines=3, override=False)
        override_inputs = choose_sale_inputs(frappe, shop, args.seed + 2, lines=4, override=True)
        credit_inputs = choose_sale_inputs(frappe, shop, args.seed + 3, lines=2, override=False)
        credit_inputs["payment_mode"] = None
        for label, inputs, override in (
            ("pos_cash_multiline", pos_inputs, False),
            ("price_override_rounding", override_inputs, True),
            ("credit_sale", credit_inputs, False),
        ):
            pairs = sale_pairs(inputs)
            client_voucher_id = f"parity-{label}-{args.seed}"
            idempotency_keys.append(idempotency_key("sale", client_voucher_id, user=frappe.session.user))
            new_name, new_snapshot = capture_with_rollback(
                frappe, label + "-new", pairs, build_new_sale(frappe, vouchers, inputs, override, client_voucher_id), capture_sale_invoice,
            )
            classic_name, classic_snapshot = capture_with_rollback(
                frappe, label + "-classic", pairs, build_classic_sale(frappe, vouchers, inputs, override), capture_sale_invoice,
            )
            sale_names.extend((new_name, classic_name))
            case_differences = compare_tree(new_snapshot, classic_snapshot)
            differences.extend({**difference, "case": label} for difference in case_differences)
            emit("PARITY_VOUCHER_CASE", {
                "case": label,
                "differences": len(case_differences),
                "gl_count": new_snapshot["gl_summary"]["count"],
                "sle_count": new_snapshot["sle_summary"]["count"],
                "bin_count": new_snapshot["bin"]["count"],
                "gl_debit_sum": new_snapshot["gl_summary"]["debit_sum"],
                "gl_credit_sum": new_snapshot["gl_summary"]["credit_sum"],
                "sle_actual_qty_sum": new_snapshot["sle_summary"]["actual_qty_sum"],
                "bin_actual_qty_net": new_snapshot["bin"]["actual_qty_net"],
                "bin_stock_value_net": new_snapshot["bin"]["stock_value_net"],
                "uoms": sorted({str(item.get("uom") or "") for item in inputs["items"]}),
                "tax_total": new_snapshot["doc"].get("total_taxes_and_charges"),
                "tax_templates_available": tax_templates_available,
            })

        transfer_items = choose_sale_inputs(frappe, dist, args.seed + 4, lines=2, override=False)["items"]
        transfer_inputs = {"source": dist, "target": shop2, "items": transfer_items}
        pairs = transfer_pairs(transfer_inputs)
        transfer_client_id = f"parity-transfer-{args.seed}"
        idempotency_keys.append(idempotency_key("transfer", transfer_client_id, user=frappe.session.user))
        new_name, new_snapshot = capture_with_rollback(
            frappe, "transfer-new", pairs, build_new_transfer(frappe, vouchers, transfer_inputs, transfer_client_id), capture_stock_entry,
        )
        classic_name, classic_snapshot = capture_with_rollback(
            frappe, "transfer-classic", pairs, build_classic_transfer(frappe, vouchers, transfer_inputs), capture_stock_entry,
        )
        transfer_names.extend((new_name, classic_name))
        case_differences = compare_tree(new_snapshot, classic_snapshot)
        differences.extend({**difference, "case": "direct_transfer"} for difference in case_differences)
        emit("PARITY_VOUCHER_CASE", {
            "case": "direct_transfer",
            "differences": len(case_differences),
            "gl_count": new_snapshot["gl_summary"]["count"],
            "sle_count": new_snapshot["sle_summary"]["count"],
            "bin_count": new_snapshot["bin"]["count"],
            "gl_debit_sum": new_snapshot["gl_summary"]["debit_sum"],
            "gl_credit_sum": new_snapshot["gl_summary"]["credit_sum"],
            "sle_actual_qty_sum": new_snapshot["sle_summary"]["actual_qty_sum"],
            "bin_actual_qty_net": new_snapshot["bin"]["actual_qty_net"],
            "bin_stock_value_net": new_snapshot["bin"]["stock_value_net"],
        })

        forced_key = idempotency_key("sale", "parity-forced-exception", user=frappe.session.user)
        idempotency_keys.append(forced_key)
        try:
            try:
                vouchers._run_idempotent(
                    "parity-forced-exception", "sale", "Sales Invoice",
                    request_fingerprint(items=[]),
                    lambda: (_ for _ in ()).throw(RuntimeError("forced parity exception")),
                )
                raise ParityFailure("forced idempotency exception did not raise")
            except RuntimeError as exc:
                if str(exc) != "forced parity exception":
                    raise
            probe = vouchers._acquire_idempotency_lock(forced_key)
            if probe is None:
                raise ParityFailure("idempotency lock was not released after a forced exception")
            vouchers._release_lock(probe)
        finally:
            frappe.db.rollback()
        emit("PARITY_IDEMPOTENCY_LOCK", {"released_after_forced_exception": True})

        persisted_sales = count_names(frappe, "Sales Invoice", sale_names)
        persisted_transfers = count_names(frappe, "Stock Entry", transfer_names)
        persisted_keys = count_idempotency_keys(frappe, idempotency_keys)
        emit("PARITY_VOUCHER_ROLLBACK", {
            "persisted_sales_invoices": persisted_sales,
            "persisted_stock_entries": persisted_transfers,
            "persisted_idempotency_rows": persisted_keys,
        })
        if persisted_sales or persisted_transfers or persisted_keys:
            differences.append({"kind": "rollback_persistence", "sales": persisted_sales, "transfers": persisted_transfers, "idempotency": persisted_keys})

    emit("PARITY_VOUCHER_SUMMARY", {"differences": len(differences), "commits": 0})
    if differences:
        for difference in differences[:20]:
            emit("PARITY_VOUCHER_DIFF", difference)
        raise ParityFailure(f"voucher parity failed: differences={len(differences)}")


def persisted_table_counts(frappe):
    counts = {}
    for table in PERSISTED_TABLES:
        counts[table] = int(frappe.db.sql(f"SELECT COUNT(*) FROM `{table}`")[0][0])
    return counts


def compare_persisted_table_counts(before, after):
    deltas = {}
    missing = []
    for table in PERSISTED_TABLES:
        if table not in before or table not in after:
            missing.append(table)
            continue
        deltas[table] = int(after[table]) - int(before[table])
    persisted_writes = sum(abs(delta) for delta in deltas.values())
    return {
        "before": {table: before.get(table) for table in PERSISTED_TABLES},
        "after": {table: after.get(table) for table in PERSISTED_TABLES},
        "deltas": deltas,
        "missing_tables": missing,
        "persisted_writes": persisted_writes,
        "ok": not missing and persisted_writes == 0,
    }


def capture_fresh_persisted_table_counts(site, connection_hook=None):
    """Count rows through a new post-rollback connection; tests may inject the hook."""
    if connection_hook is not None:
        return connection_hook(site)
    frappe = None
    try:
        frappe, _fresh_site = connect_frappe(site)
        return persisted_table_counts(frappe)
    finally:
        if frappe is not None:
            frappe.destroy()


def connect_frappe(site=None):
    import frappe
    if not site:
        configs = sorted(Path.cwd().glob("*/site_config.json"))
        if len(configs) != 1:
            raise RuntimeError(f"expected exactly one site below {Path.cwd()}, found {len(configs)}")
        site = configs[0].parent.name
    lowered = site.lower()
    if lowered == "khitmee.localhost" or lowered.endswith(".khitmee.localhost"):
        raise RuntimeError(f"refusing live-stack site {site}")
    frappe.init(site=site, sites_path=str(Path.cwd()))
    frappe.connect()
    if getattr(frappe.conf, "db_host", None) != "db":
        frappe.destroy()
        raise RuntimeError("unexpected database host; perf_parity only runs in the isolated khitmee-perf backend")
    frappe.set_user("Administrator")
    return frappe, site


def selected_sections(only):
    sections = set(only or ["all"])
    if "all" in sections:
        return set(ALL_SECTIONS)
    return sections


def merge_case_stats(target, addition):
    for field in ("executed_cases", "skipped_cases", "failed_cases"):
        target[field] = target.get(field, 0) + int((addition or {}).get(field, 0))
    return target


def assert_live_bench_db(frappe):
    if not getattr(frappe, "db", None):
        raise ParityFailure("--require-db requested but no Frappe database connection is available")
    rows = frappe.db.sql("SELECT 1")
    if not rows:
        raise ParityFailure("--require-db requested but the bench database did not answer SELECT 1")
    return True


def run_inside_backend(args, fresh_connection_hook=None):
    frappe = None
    try:
        frappe, site = connect_frappe(args.site)
        emit("PARITY_START", {"site": site, "profile": args.profile or "unspecified", "seed": args.seed})
        require_db = bool(getattr(args, "require_db", False))
        if require_db:
            assert_live_bench_db(frappe)
        sections = selected_sections(args.only)
        verification_stats = {"executed_cases": 0, "skipped_cases": 0, "failed_cases": 0}
        section_timings = {}
        run_start = time.monotonic()
        before_counts = persisted_table_counts(frappe)
        if "gp" in sections:
            t0 = time.monotonic()
            merge_case_stats(verification_stats, run_gp_parity(frappe, args))
            section_timings["gp"] = time.monotonic() - t0
        if "sb" in sections:
            t0 = time.monotonic()
            run_sb_parity(frappe, args)
            section_timings["sb"] = time.monotonic() - t0
            merge_case_stats(verification_stats, {"executed_cases": 1})
        if "search" in sections:
            t0 = time.monotonic()
            run_search_parity(frappe, args)
            section_timings["search"] = time.monotonic() - t0
            merge_case_stats(verification_stats, {"executed_cases": 1})
        if "voucher" in sections:
            t0 = time.monotonic()
            run_voucher_parity(frappe, args)
            section_timings["voucher"] = time.monotonic() - t0
            merge_case_stats(verification_stats, {"executed_cases": 1})
        emit("PARITY_SECTION_TIMINGS", format_section_timings(section_timings, time.monotonic() - run_start))

        if require_db:
            require_db_ok = (
                verification_stats["executed_cases"] > 0
                and verification_stats["skipped_cases"] == 0
                and verification_stats["failed_cases"] == 0
            )
            emit("PARITY_REQUIRE_DB_CHECK", {**verification_stats, "ok": require_db_ok})
            if not require_db_ok:
                raise ParityFailure(
                    "required live database verification did not execute cleanly: "
                    f"executed_cases={verification_stats['executed_cases']} "
                    f"skipped_cases={verification_stats['skipped_cases']} "
                    f"failed_cases={verification_stats['failed_cases']}"
                )

        frappe.db.rollback()
        frappe.destroy()
        frappe = None
        after_counts = capture_fresh_persisted_table_counts(site, connection_hook=fresh_connection_hook)
        persisted = compare_persisted_table_counts(before_counts, after_counts)
        emit("PARITY_PERSISTED_WRITE_CHECK", persisted)
        if not persisted["ok"]:
            raise ParityFailure(
                "rollback-only parity persisted writes: "
                f"persisted_writes={persisted['persisted_writes']} missing_tables={len(persisted['missing_tables'])}"
            )

        payload = {
            "site": site,
            "sections": sorted(sections),
            "persisted_writes": persisted["persisted_writes"],
        }
        if sections == set(ALL_SECTIONS):
            emit("PARITY_PASS", {**payload, "partial": False})
        else:
            emit("PARITY_PARTIAL_PASS", {**payload, "partial": True})
        return 0
    except Exception as exc:
        if frappe is not None:
            try:
                frappe.db.rollback()
            except Exception:
                pass
        emit("PARITY_FAIL", {"error": str(exc), "type": type(exc).__name__})
        return 1
    finally:
        if frappe is not None:
            try:
                frappe.db.rollback()
            except Exception:
                pass
            frappe.destroy()
        try:
            import resource

            emit("PARITY_PEAK_RSS", {"ru_maxrss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)})
        except Exception:
            pass


BENCH_PYTHON ="/home/frappe/frappe-bench/env/bin/python"
BENCH_SITES = "/home/frappe/frappe-bench/sites"
SIDECAR_LABEL = "khitmee.perf.parity-sidecar=1"


def _strip_host_only_args(argv):
    """Drop host-runner flags so the inner process only sees suite arguments."""
    stripped = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--runner", "--sidecar-memory"):
            skip_next = True
            continue
        if arg.startswith("--runner=") or arg.startswith("--sidecar-memory="):
            continue
        stripped.append(arg)
    return stripped


def _parse_container_uid_gid(user_spec, default_uid=1000, default_gid=1000):
    text = str(user_spec or "").strip()
    if not text:
        return default_uid, default_gid
    parts = text.split(":", 1)
    try:
        uid = int(parts[0])
        gid = int(parts[1]) if len(parts) > 1 and parts[1] else uid
        return uid, gid
    except ValueError:
        return default_uid, default_gid


SHARED_SYSTEM_DIRS = frozenset({"/", "/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp"})


def _validate_dedicated_host_dir(host_dir, description, *, is_posix, stat_fn=None):
    raw_posix = Path(host_dir).as_posix().rstrip("/") or "/"
    resolved = Path(host_dir).resolve(strict=False)
    resolved_posix = resolved.as_posix().rstrip("/") or "/"
    if raw_posix in SHARED_SYSTEM_DIRS or resolved_posix in SHARED_SYSTEM_DIRS or resolved == resolved.parent:
        raise OracleCacheDriftError(
            f"refusing shared/system directory {resolved} as {description}; use a dedicated subdirectory"
        )

    posix = (os.name != "nt") if is_posix is None else bool(is_posix)
    if posix and (resolved.exists() or stat_fn is not None):
        st_fn = stat_fn if stat_fn is not None else os.stat
        try:
            mode = st_fn(resolved).st_mode
        except OSError:
            mode = 0
        if mode & stat.S_ISVTX:
            raise OracleCacheDriftError(
                f"refusing sticky directory {resolved} as {description}; use a dedicated subdirectory"
            )
        if mode & stat.S_IWOTH:
            raise OracleCacheDriftError(
                f"refusing world-writable directory {resolved} as {description}; use a dedicated subdirectory"
            )
    return resolved


def prepare_sidecar_oracle_cache_dir(
    host_cache,
    target_uid=1000,
    target_gid=1000,
    *,
    is_posix=None,
    geteuid=None,
    chown_fn=None,
    chmod_fn=None,
    access_fn=None,
    stat_fn=None,
):
    """Ensure host oracle cache dir (and entries/) is writable by the sidecar container UID."""
    cache_path = _validate_dedicated_host_dir(
        host_cache,
        "oracle cache dir",
        is_posix=is_posix,
        stat_fn=stat_fn,
    )
    entries_path = cache_path / "entries"
    cache_path.mkdir(parents=True, exist_ok=True)
    _validate_dedicated_host_dir(
        cache_path,
        "oracle cache dir",
        is_posix=is_posix,
        stat_fn=stat_fn,
    )
    entries_path.mkdir(parents=True, exist_ok=True)
    posix = (os.name != "nt") if is_posix is None else bool(is_posix)
    if not posix:
        return str(cache_path)

    euid_fn = geteuid if geteuid is not None else getattr(os, "geteuid", lambda: target_uid)
    chown = chown_fn if chown_fn is not None else getattr(os, "chown", None)
    chmod = chmod_fn if chmod_fn is not None else os.chmod
    access = access_fn if access_fn is not None else os.access

    targets = [cache_path, entries_path]
    manifest_file = cache_path / "manifest.json"
    if manifest_file.is_file():
        targets.append(manifest_file)

    try:
        caller_uid = euid_fn()
        if caller_uid == 0 and chown is not None:
            for item in targets:
                chown(item, target_uid, target_gid)
                chmod(item, 0o640 if item.is_file() else 0o750)
        elif caller_uid != target_uid:
            raise OracleCacheDriftError(
                f"refusing to chmod world-writable oracle cache dir {cache_path} for sidecar UID {target_uid} "
                f"(caller UID {caller_uid}); run as UID {target_uid} or root"
            )
    except OSError as exc:
        raise OracleCacheDriftError(
            f"failed to make oracle cache dir {cache_path} writable by UID {target_uid}: {exc}"
        )

    if not access(cache_path, os.W_OK) or not access(entries_path, os.W_OK):
        raise OracleCacheDriftError(
            f"oracle cache dir {cache_path} is not writable for sidecar UID {target_uid}"
        )
    return str(cache_path)


def prepare_sidecar_oracle_manifest_dir(
    manifest_path,
    cache_dir,
    target_uid=1000,
    target_gid=1000,
    *,
    is_posix=None,
    geteuid=None,
    chown_fn=None,
    chmod_fn=None,
    stat_fn=None,
):
    """Validate and prepare a dedicated non-sticky parent dir for --oracle-manifest."""
    mpath = Path(manifest_path).resolve(strict=False)
    cpath = Path(cache_dir).resolve(strict=False)
    try:
        inside = mpath.is_relative_to(cpath)
    except AttributeError:
        inside = str(mpath).startswith(str(cpath) + os.sep)
    if inside:
        raise OracleCacheDriftError(
            f"--oracle-manifest must be rooted outside --oracle-cache-dir ({mpath})"
        )

    manifest_dir = mpath.parent
    raw_parent_posix = Path(manifest_path).parent.as_posix().rstrip("/") or "/"
    resolved_parent_posix = manifest_dir.as_posix().rstrip("/") or "/"
    if (
        raw_parent_posix in SHARED_SYSTEM_DIRS
        or resolved_parent_posix in SHARED_SYSTEM_DIRS
        or manifest_dir == manifest_dir.parent
    ):
        raise OracleCacheDriftError(
            f"refusing shared/system directory {manifest_dir} as --oracle-manifest parent; "
            "use a dedicated subdirectory (e.g. /tmp/khitmee-oracle-manifest/manifest.json)"
        )

    posix = (os.name != "nt") if is_posix is None else bool(is_posix)
    if posix:
        st_fn = stat_fn if stat_fn is not None else os.stat
        if manifest_dir.exists() or stat_fn is not None:
            try:
                mode = st_fn(manifest_dir).st_mode
            except OSError:
                mode = 0
            if mode & stat.S_ISVTX:
                raise OracleCacheDriftError(
                    f"refusing sticky directory {manifest_dir} as --oracle-manifest parent; "
                    "use a dedicated subdirectory"
                )

    manifest_dir.mkdir(parents=True, exist_ok=True)
    if posix:
        euid_fn = geteuid if geteuid is not None else getattr(os, "geteuid", lambda: target_uid)
        chown = chown_fn if chown_fn is not None else getattr(os, "chown", None)
        chmod = chmod_fn if chmod_fn is not None else os.chmod
        try:
            caller_uid = euid_fn()
            if caller_uid == 0 and chown is not None:
                chown(manifest_dir, target_uid, target_gid)
                chmod(manifest_dir, 0o750)
            elif caller_uid != target_uid:
                raise OracleCacheDriftError(
                    f"refusing to chmod world-writable oracle manifest dir {manifest_dir} for sidecar UID {target_uid} "
                    f"(caller UID {caller_uid}); run as UID {target_uid} or root"
                )
        except OSError as exc:
            raise OracleCacheDriftError(
                f"failed to prepare oracle manifest dir {manifest_dir}: {exc}"
            )
    return str(manifest_dir), mpath.name


def build_sidecar_command(
    backend,
    image,
    network,
    memory,
    inner_argv,
    name,
    env=(),
    oracle_cache_mount=None,
    oracle_manifest_mount=None,
):
    """Ephemeral oracle runner sharing the backend's image, mounts and network.

    The stock ERPNext Gross Profit oracle caches full Stock Ledger history per
    (item, warehouse) for the lifetime of one report run, which exceeds the
    2gb profile's 576 MiB backend cgroup.  Running it in a separate, bounded
    container keeps the oracle byte-for-byte stock ERPNext while the
    production-shaped backend limits stay honest.  Swap is pinned equal to
    memory so an undersized cap fails fast with OOM instead of thrashing.
    ``env`` carries the backend's own environment (e.g. the PYTHONPATH that
    exposes the bind-mounted khitmee_ops app).
    """
    env_args = []
    for entry in env:
        if entry.split("=", 1)[0] in ("HOSTNAME", "HOME"):
            continue
        env_args += ["-e", entry]
    mount_args = []
    if oracle_cache_mount:
        host_dir, container_dir = oracle_cache_mount
        mount_args += ["-v", f"{host_dir}:{container_dir}"]
    if oracle_manifest_mount:
        host_mdir, container_mdir = oracle_manifest_mount
        mount_args += ["-v", f"{host_mdir}:{container_mdir}"]
    return [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--label", SIDECAR_LABEL,
        "--network", network,
        "--volumes-from", backend,
        *mount_args,
        "--memory", memory,
        "--memory-swap", memory,
        *env_args,
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-w", BENCH_SITES,
        "--entrypoint", BENCH_PYTHON,
        image,
        "-", "--inside-backend", *inner_argv,
    ]


def _inspect_backend(backend, template):
    return subprocess.check_output(
        ["docker", "inspect", "-f", template, backend],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _rewrite_cli_flag(argv, flag_name, new_value):
    rewritten = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token == flag_name:
            skip_next = True
            continue
        if token.startswith(flag_name + "="):
            continue
        rewritten.append(token)
    rewritten.extend([flag_name, new_value])
    return rewritten


def build_inner_python_source(parity_path=None):
    """Build the stdin payload with fast_verify embedded for container execution."""
    parity_file = Path(parity_path or __file__)
    parity_source = parity_file.read_text(encoding="utf-8")
    verifier_source = parity_file.with_name("fast_verify.py").read_text(encoding="utf-8")
    bootstrap = (
        "import sys as _khitmee_sys, types as _khitmee_types\n"
        "_fast_verify_module = _khitmee_types.ModuleType('fast_verify')\n"
        "_fast_verify_module.__file__ = '<embedded fast_verify.py>'\n"
        "_fast_verify_source = " + repr(verifier_source) + "\n"
        "exec(compile(_fast_verify_source, '<embedded fast_verify.py>', 'exec'), _fast_verify_module.__dict__)\n"
        "_khitmee_sys.modules['fast_verify'] = _fast_verify_module\n"
    )
    return (bootstrap + "\n" + parity_source).encode("utf-8")


def host_exec(argv, runner="sidecar", sidecar_memory="1536m"):
    parsed_host = build_parser().parse_args(argv)
    if parsed_host.oracle_manifest and not parsed_host.oracle_cache_dir:
        print("refusing parity run: --oracle-manifest requires --oracle-cache-dir", file=sys.stderr)
        return 3
    if parsed_host.oracle_cache_dir and runner == "exec":
        print(
            "refusing parity run: --oracle-cache-dir cannot be combined with --runner exec "
            "(bind-mounting host oracle cache requires --runner sidecar)",
            file=sys.stderr,
        )
        return 3

    backend = f"{PROJECT}-backend-1"
    try:
        project = _inspect_backend(backend, '{{ index .Config.Labels "com.docker.compose.project" }}')
    except (OSError, subprocess.CalledProcessError):
        print(f"perf parity requires the isolated {backend} container", file=sys.stderr)
        return 3
    if project != PROJECT or project == FORBIDDEN_PROJECT:
        print(f"refusing parity run: {backend} compose project is {project!r}, expected {PROJECT!r}", file=sys.stderr)
        return 3
    try:
        source = build_inner_python_source()
    except OSError as exc:
        print(f"could not build embedded parity/comparator payload: {exc}", file=sys.stderr)
        return 3
    inner_argv = _strip_host_only_args(argv)
    if runner == "exec":
        command = [
            "docker", "exec", "-i", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-w", BENCH_SITES, backend,
            BENCH_PYTHON, "-", "--inside-backend", *inner_argv,
        ]
    else:
        try:
            image = _inspect_backend(backend, "{{.Config.Image}}")
            networks = _inspect_backend(
                backend, "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}"
            ).split()
            env = json.loads(_inspect_backend(backend, "{{json .Config.Env}}") or "[]")
            container_user = _inspect_backend(backend, "{{.Config.User}}")
        except (OSError, subprocess.CalledProcessError, ValueError):
            print(f"could not inspect {backend} image/network for the oracle sidecar", file=sys.stderr)
            return 3
        network = f"{PROJECT}_default" if f"{PROJECT}_default" in networks else (networks[0] if networks else "")
        if not image or not network or not network.startswith(PROJECT):
            print(f"refusing sidecar: unexpected image {image!r} or network {network!r}", file=sys.stderr)
            return 3
        name = f"{PROJECT}-parity-{int(time.time())}"
        oracle_cache_mount = None
        oracle_manifest_mount = None
        target_uid, target_gid = _parse_container_uid_gid(container_user)
        if parsed_host.oracle_cache_dir:
            try:
                host_cache = prepare_sidecar_oracle_cache_dir(
                    parsed_host.oracle_cache_dir,
                    target_uid=target_uid,
                    target_gid=target_gid,
                )
            except OracleCacheDriftError as exc:
                print(str(exc), file=sys.stderr)
                return 3
            container_cache = "/tmp/khitmee-oracle-cache"
            oracle_cache_mount = (host_cache, container_cache)
            inner_argv = _rewrite_cli_flag(inner_argv, "--oracle-cache-dir", container_cache)
        if parsed_host.oracle_manifest:
            try:
                manifest_dir_str, manifest_name = prepare_sidecar_oracle_manifest_dir(
                    parsed_host.oracle_manifest,
                    parsed_host.oracle_cache_dir,
                    target_uid=target_uid,
                    target_gid=target_gid,
                )
            except OracleCacheDriftError as exc:
                print(f"refusing parity run: {exc}", file=sys.stderr)
                return 3
            container_mdir = "/tmp/khitmee-oracle-manifest"
            oracle_manifest_mount = (manifest_dir_str, container_mdir)
            inner_argv = _rewrite_cli_flag(inner_argv, "--oracle-manifest", f"{container_mdir}/{manifest_name}")
        command = build_sidecar_command(
            backend,
            image,
            network,
            sidecar_memory,
            inner_argv,
            name,
            env=env,
            oracle_cache_mount=oracle_cache_mount,
            oracle_manifest_mount=oracle_manifest_mount,
        )
    return subprocess.run(command, input=source).returncode


def build_parser():
    parser = argparse.ArgumentParser(description="Run the maintained khitmee-perf live parity suite")
    parser.add_argument("--profile", choices=("2gb", "4gb"), default=None)
    parser.add_argument("--site", default=None)
    parser.add_argument("--price-list", default="Standard Selling")
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--custom-from-date", "--gp-from-date", dest="custom_from_date", default=None, help="optional custom Gross Profit range start")
    parser.add_argument("--custom-to-date", "--gp-to-date", dest="custom_to_date", default=None, help="optional custom Gross Profit range end")
    parser.add_argument("--require-db", action="store_true", help="fail unless the live bench DB executes all verification cases without skips or failures")
    parser.add_argument("--only", action="append", choices=("gp", "sb", "search", "voucher", "all"), help="run one section; repeatable")
    parser.add_argument(
        "--runner",
        choices=("sidecar", "exec"),
        default="sidecar",
        help="sidecar (default): bounded throwaway container so the stock oracle cannot starve the 576 MiB backend; exec: legacy docker exec into the backend",
    )
    parser.add_argument("--sidecar-memory", default="1536m", help="memory (and swap) cap for the oracle sidecar")
    parser.add_argument(
        "--gp-1y",
        choices=("warehouse", "all", "none"),
        default="warehouse",
        help="1y stock-oracle cases (~30 min each): warehouse (default, 2 cases), all (4, for nightly), none",
    )
    parser.add_argument(
        "--oracle-cache-dir",
        default=None,
        help="optional hash-pinned stock-oracle cache directory (fails closed on DB/source/precision/manifest drift)",
    )
    parser.add_argument(
        "--oracle-manifest",
        default=None,
        help="optional external trust-root manifest path rooted outside --oracle-cache-dir",
    )
    parser.add_argument("--inside-backend", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.inside_backend:
        return run_inside_backend(args)
    return host_exec(argv, runner=args.runner, sidecar_memory=args.sidecar_memory)


if __name__ == "__main__":
    sys.exit(main())
