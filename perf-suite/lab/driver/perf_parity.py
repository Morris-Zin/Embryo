#!/usr/bin/env python3
"""Maintained live parity suite for the isolated khitmee-perf stack.

The module is importable on a plain host Python: comparison helpers stay pure and
Frappe is imported lazily only inside the perf backend container.
"""
import argparse
import json
import random
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import financial_verifier

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
COLUMN_METADATA_FIELDS = ("fieldname", "label", "fieldtype", "options", "width")
ORDER_KEY_FIELDS = ("query_phase", "posting_date", "posting_time")
DEFAULT_NUMERIC_TOLERANCE = Decimal("0.0000001")
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


def decimal_numbers_close(left, right, tolerance=DEFAULT_NUMERIC_TOLERANCE):
    """Decimal numeric comparison for report evidence.

    Null is never numeric zero, invalid numeric input never passes, and the
    tolerance is an explicit decimal compatibility mode rather than a rounding
    license.  This avoids binary float cancellation around posting units.
    """
    tolerance_decimal, tolerance_error = financial_verifier.decimal_value(tolerance)
    if tolerance_error or tolerance_decimal is None or tolerance_decimal < 0:
        raise ValueError("numeric comparison tolerance must be a non-negative decimal")
    left_decimal, left_error = financial_verifier.decimal_value(left)
    right_decimal, right_error = financial_verifier.decimal_value(right)
    if left_error or right_error:
        return False, None
    if left_decimal is None or right_decimal is None:
        return left_decimal == right_decimal, Decimal(0) if left_decimal == right_decimal else None
    diff = abs(left_decimal - right_decimal)
    return diff <= tolerance_decimal, diff


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


def column_metadata(columns):
    metadata = []
    for column in columns or []:
        if isinstance(column, dict):
            metadata.append({field: column.get(field) for field in COLUMN_METADATA_FIELDS})
        else:
            metadata.append({field: None for field in COLUMN_METADATA_FIELDS})
    return metadata


def compare_column_metadata(fast_columns, upstream_columns):
    fast_metadata = column_metadata(fast_columns)
    upstream_metadata = column_metadata(upstream_columns)
    if financial_verifier.canonical_json(fast_metadata) == financial_verifier.canonical_json(upstream_metadata):
        return []
    differences = []
    for index in range(max(len(fast_metadata), len(upstream_metadata))):
        fast_column = fast_metadata[index] if index < len(fast_metadata) else None
        upstream_column = upstream_metadata[index] if index < len(upstream_metadata) else None
        if financial_verifier.canonical_json(fast_column) == financial_verifier.canonical_json(upstream_column):
            continue
        differences.append({
            "kind": "column_metadata",
            "column_index": index,
            "fast": fast_column,
            "upstream": upstream_column,
        })
    if not differences:
        differences.append({"kind": "column_metadata", "fast_count": len(fast_metadata), "upstream_count": len(upstream_metadata)})
    return differences


def row_as_dict(columns, row, index):
    if isinstance(row, dict):
        result = dict(row)
    else:
        result = dict(zip(column_fieldnames(columns), row))
    result["_row_index"] = index
    return result


def is_total_row(row):
    return any(
        row.get(field) == "Total"
        for field in ("sales_invoice", "item_code", "invoice_or_item", "warehouse", "monthly", "month")
    )


def total_row_position_mismatches(rows, side):
    mismatches = []
    total_indexes = [index for index, row in enumerate(rows) if is_total_row(row)]
    for index in total_indexes:
        if index != len(rows) - 1:
            mismatches.append({"kind": "total_row_position", "side": side, "index": index, "expected": len(rows) - 1})
    return mismatches


def ordering_component(value):
    if value is None:
        return "null"
    if hasattr(value, "total_seconds"):
        number, error = financial_verifier.decimal_value(str(value.total_seconds()))
        if not error and number is not None:
            return "seconds:" + financial_verifier.decimal_text(number)
    if hasattr(value, "isoformat"):
        return "iso:" + value.isoformat()
    try:
        return financial_verifier.canonical_json(value)
    except financial_verifier.VerificationInputError:
        return "str:" + str(value)


def tie_order_key(row):
    return tuple(ordering_component(row.get(field)) for field in ORDER_KEY_FIELDS)


def tie_order_runs(rows):
    runs = []
    for row in rows:
        key = tie_order_key(row)
        if not runs or runs[-1] != key:
            runs.append(key)
    return runs


def fragmented_tie_bucket_mismatches(rows, side):
    mismatches = []
    previous_key = None
    closed_keys = set()
    for index, row in enumerate(rows):
        key = tie_order_key(row)
        if key != previous_key:
            if key in closed_keys:
                mismatches.append({"kind": "tie_bucket_fragmented", "side": side, "row": index, "key": list(key)})
            if previous_key is not None:
                closed_keys.add(previous_key)
            previous_key = key
    return mismatches


def rows_with_inherited_invoice_order_keys(rows):
    inherited = []
    current_values = {field: None for field in ORDER_KEY_FIELDS}
    for row in rows:
        copied = dict(row)
        if row_indent(row) == 0:
            for field in ORDER_KEY_FIELDS:
                current_values[field] = copied.get(field)
        else:
            for field in ORDER_KEY_FIELDS:
                if copied.get(field) is None:
                    copied[field] = current_values.get(field)
        inherited.append(copied)
    return inherited


def compare_tie_aware_order(fast_rows, upstream_rows):
    """Compare runs of distinct ordering keys without sorting away real order.

    Rows with equal `(query_phase, posting_date, posting_time)` form a tie
    bucket and may permute inside that bucket.  Distinct buckets must occur in
    the same run order in reference and candidate output.
    """
    mismatches = []
    mismatches.extend(fragmented_tie_bucket_mismatches(fast_rows, "fast"))
    mismatches.extend(fragmented_tie_bucket_mismatches(upstream_rows, "upstream"))
    fast_runs = tie_order_runs(fast_rows)
    upstream_runs = tie_order_runs(upstream_rows)
    if fast_runs != upstream_runs:
        first_difference = None
        for index, (fast_key, upstream_key) in enumerate(zip(fast_runs, upstream_runs)):
            if fast_key != upstream_key:
                first_difference = {"index": index, "fast": list(fast_key), "upstream": list(upstream_key)}
                break
        mismatches.append({
            "kind": "tie_order",
            "fast_run_count": len(fast_runs),
            "upstream_run_count": len(upstream_runs),
            "first_difference": first_difference,
            "fast_runs": [list(key) for key in fast_runs[:12]],
            "upstream_runs": [list(key) for key in upstream_runs[:12]],
        })
    return mismatches


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
            close, diff = decimal_numbers_close(fast_row.get(field), upstream_row.get(field), tolerance)
            if not close:
                mismatches.append({
                    "kind": "numeric_field", "field": field, "location": location,
                    "fast": scalar(fast_row.get(field)), "upstream": scalar(upstream_row.get(field)),
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


def compare_gross_profit_rows(fast_columns, fast_data, upstream_columns, upstream_data, tolerance=DEFAULT_NUMERIC_TOLERANCE):
    fast_rows = [row_as_dict(fast_columns, row, i) for i, row in enumerate(fast_data or [])]
    upstream_rows = [row_as_dict(upstream_columns, row, i) for i, row in enumerate(upstream_data or [])]
    mismatches = []

    fast_fields = column_fieldnames(fast_columns)
    upstream_fields = column_fieldnames(upstream_columns)
    if fast_fields != upstream_fields:
        mismatches.append({"kind": "columns", "fast": fast_fields, "upstream": upstream_fields})
    mismatches.extend(compare_column_metadata(fast_columns, upstream_columns))
    if len(fast_rows) != len(upstream_rows):
        mismatches.append({"kind": "row_count", "fast": len(fast_rows), "upstream": len(upstream_rows)})

    mismatches.extend(total_row_position_mismatches(fast_rows, "fast"))
    mismatches.extend(total_row_position_mismatches(upstream_rows, "upstream"))
    fast_totals = [row for row in fast_rows if is_total_row(row)]
    upstream_totals = [row for row in upstream_rows if is_total_row(row)]
    if len(fast_totals) != 1 or len(upstream_totals) != 1:
        if fast_rows or upstream_rows:
            mismatches.append({"kind": "total_row_count", "fast": len(fast_totals), "upstream": len(upstream_totals)})
    else:
        if fast_rows.index(fast_totals[0]) != upstream_rows.index(upstream_totals[0]):
            mismatches.append({"kind": "total_row_position", "fast": fast_rows.index(fast_totals[0]), "upstream": upstream_rows.index(upstream_totals[0])})
        mismatches.extend(compare_gp_row_fields(fast_totals[0], upstream_totals[0], tolerance, "total"))

    invoice_mode = invoice_mode_rows(fast_rows) or invoice_mode_rows(upstream_rows)
    if invoice_mode:
        mismatches.extend({**error, "side": "fast"} for error in validate_invoice_tree(fast_rows))
        mismatches.extend({**error, "side": "upstream"} for error in validate_invoice_tree(upstream_rows))
        fast_ordered_body = rows_with_inherited_invoice_order_keys(row for row in fast_rows if not is_total_row(row))
        upstream_ordered_body = rows_with_inherited_invoice_order_keys(row for row in upstream_rows if not is_total_row(row))
        mismatches.extend(compare_tie_aware_order(fast_ordered_body, upstream_ordered_body))

        fast_invoices, fast_order, fast_duplicates = parse_invoice_rows(fast_rows)
        upstream_invoices, upstream_order, upstream_duplicates = parse_invoice_rows(upstream_rows)
        for key in fast_duplicates:
            mismatches.append({"kind": "duplicate_invoice_key", "side": "fast", "invoice": scalar(key)})
        for key in upstream_duplicates:
            mismatches.append({"kind": "duplicate_invoice_key", "side": "upstream", "invoice": scalar(key)})

        if set(fast_invoices) != set(upstream_invoices):
            mismatches.append({
                "kind": "invoice_keys",
                "missing_fast": sorted((str(key) for key in set(upstream_invoices) - set(fast_invoices)))[:10],
                "missing_upstream": sorted((str(key) for key in set(fast_invoices) - set(upstream_invoices)))[:10],
            })

        posting_dates = [scalar(fast_invoices[key]["header"].get("posting_date")) for key in fast_order]
        for index, (previous, current) in enumerate(zip(posting_dates, posting_dates[1:])):
            if previous is not None and current is not None and str(previous) < str(current):
                mismatches.append({
                    "kind": "invoice_posting_date_order",
                    "side": "fast",
                    "index": index,
                    "previous": previous,
                    "current": current,
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

    group_field = fast_fields[0] if fast_fields else None
    fast_unsorted_body = [row for row in fast_rows if not is_total_row(row)]
    upstream_unsorted_body = [row for row in upstream_rows if not is_total_row(row)]
    mismatches.extend(compare_tie_aware_order(fast_unsorted_body, upstream_unsorted_body))
    fast_body = sorted(fast_unsorted_body, key=lambda row: canonical_group_sort_key(row, group_field))
    upstream_body = sorted(upstream_unsorted_body, key=lambda row: canonical_group_sort_key(row, group_field))
    for index, (fast_row, upstream_row) in enumerate(zip(fast_body, upstream_body)):
        mismatches.extend(compare_gp_row_fields(fast_row, upstream_row, tolerance, f"group-row:{index}"))
    return mismatches


def stock_key(row):
    return (row.get("item_code"), row.get("warehouse"))


def compare_stock_balance_rows(normal_columns, normal_data, ignore_columns, ignore_data, tolerance=DEFAULT_NUMERIC_TOLERANCE):
    normal_rows = [row_as_dict(normal_columns, row, i) for i, row in enumerate(normal_data or [])]
    ignore_rows = [row_as_dict(ignore_columns, row, i) for i, row in enumerate(ignore_data or [])]
    normal_by_key = {stock_key(row): row for row in normal_rows}
    ignore_by_key = {stock_key(row): row for row in ignore_rows}
    mismatches = compare_column_metadata(normal_columns, ignore_columns)
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
            close, diff = decimal_numbers_close(normal_by_key[key].get(field), ignore_by_key[key].get(field), tolerance)
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
        normalized.append({field: row.get(field) for field in fields})
    return sorted(normalized, key=financial_verifier.canonical_json)


def exact_numeric_sum(rows, field):
    total = Decimal(0)
    for row in rows:
        value, error = financial_verifier.decimal_value(row.get(field))
        if error or value is None:
            raise ParityFailure(f"missing or non-decimal financial capture in {field}: {error or 'null'}")
        total += value
    return total


def exact_numeric_delta(left, right):
    left_number, left_error = financial_verifier.decimal_value(left)
    right_number, right_error = financial_verifier.decimal_value(right)
    if left_error or right_error or left_number is None or right_number is None:
        reason = left_error or right_error or "null bin value"
        raise ParityFailure(f"missing or non-decimal bin capture: {reason}")
    return right_number - left_number


def emit(kind, payload):
    print(kind + " " + json.dumps(payload, default=str, sort_keys=True), flush=True)


class ParityFailure(RuntimeError):
    pass


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


def run_gp_parity(frappe, args):
    mismatches = []
    explain_offenses = []
    cases = []
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
    first_company = company_for_warehouse(frappe, resolve_warehouse(frappe, "Shop 1"))
    cases.append(("empty-range", {
        "company": first_company,
        "from_date": "1999-01-01",
        "to_date": "1999-01-01",
        "warehouse": resolve_warehouse(frappe, "Shop 1"),
        "group_by": "Invoice",
        "include_returned_invoices": 0,
    }))

    captured_semijoin = {}
    for label, filters in cases:
        capture_for_explain = label.startswith("30d-") and filters.get("group_by") == "Invoice" and filters.get("include_returned_invoices") == 1
        if capture_for_explain:
            fast_columns, fast_data, fast_seconds, captured = run_fast_gross_profit_capture_semijoin(frappe, filters)
            logical = "Shop 1" if "Shop_1" in label else "Dist Hub 1"
            for sql, params in captured:
                captured_semijoin.setdefault((logical, semijoin_query_label(sql)), (sql, params))
        else:
            fast_columns, fast_data, fast_seconds = run_gross_profit(frappe, "fast", filters)
        upstream_columns, upstream_data, upstream_seconds = run_gross_profit(frappe, "upstream", filters)
        case_mismatches = compare_gross_profit_rows(fast_columns, fast_data, upstream_columns, upstream_data)
        mismatches.extend({**mismatch, "case": label} for mismatch in case_mismatches)
        emit("PARITY_GP_CASE", {
            "case": label,
            "fast_rows": len(fast_data or []),
            "upstream_rows": len(upstream_data or []),
            "fast_seconds": round(fast_seconds, 3),
            "upstream_seconds": round(upstream_seconds, 3),
            "mismatches": len(case_mismatches),
        })
        for mismatch in case_mismatches[:10]:
            emit("PARITY_GP_MISMATCH", {"case": label, **mismatch})

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
    emit("PARITY_GP_SUMMARY", {"mismatches": len(mismatches), "explain_offenses": len(explain_offenses)})
    if mismatches or explain_offenses:
        raise ParityFailure(f"Fast Gross Profit parity failed: mismatches={len(mismatches)} explain_offenses={len(explain_offenses)}")


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


DETAIL_TABLE_ROLES = (
    ("items", "item"),
    ("packed_items", "packed_item"),
    ("supplied_items", "supplied_item"),
)
FINANCIAL_DETAIL_ROLES = frozenset(role for _table, role in DETAIL_TABLE_ROLES)
CHILD_PARENT_LINK_FIELDS = {
    "packed_item": ("parent_detail_docname", "parent_item"),
    "supplied_item": ("reference_name", "main_item_code"),
}


def document_child_rows(doc, table_field):
    rows = getattr(doc, table_field, None)
    if rows is None and hasattr(doc, "get"):
        rows = doc.get(table_field)
    return list(rows or [])


def document_detail_registry(doc):
    """Map random child row names to stable child role/index keys.

    Independent submissions intentionally generate different child-row IDs.  The
    stable key preserves linkage instead of ignoring it: a row linked to the
    second item is `item:2`, while a product-bundle component is for example
    `packed_item:1`.  Unknown, duplicate, or cross-table-broken child IDs are
    input failures rather than normalized away.
    """
    registry = {}
    for table_field, role in DETAIL_TABLE_ROLES:
        for index, child in enumerate(document_child_rows(doc, table_field), 1):
            detail_name = child.get("name") if hasattr(child, "get") else None
            if not detail_name:
                raise ParityFailure(f"{table_field}[{index}] has no child row name")
            if detail_name in registry:
                raise ParityFailure(f"duplicate voucher detail row name {detail_name!r}")
            registry[detail_name] = {
                "key": f"{role}:{index}",
                "role": role,
                "index": index,
                "table_field": table_field,
                "row": child,
            }
    for detail in list(registry.values()):
        link_fields = CHILD_PARENT_LINK_FIELDS.get(detail["role"])
        if link_fields is None:
            continue
        link_field, parent_item_field = link_fields
        parent_detail = detail["row"].get(link_field)
        if not parent_detail:
            continue
        parent = registry.get(parent_detail)
        if parent is None or parent["role"] != "item":
            raise ParityFailure(
                f"{detail['role']} detail {detail['key']} has broken parent link {parent_detail!r}"
            )
        expected_parent_item = detail["row"].get(parent_item_field)
        actual_parent_item = parent["row"].get("item_code")
        if expected_parent_item and actual_parent_item != expected_parent_item:
            raise ParityFailure(
                f"{detail['role']} detail {detail['key']} is cross-linked to parent item "
                f"{actual_parent_item!r}, expected {expected_parent_item!r}"
            )
    return registry


def document_detail_ordinals(doc):
    return {name: detail["key"] for name, detail in document_detail_registry(doc).items()}


def ordinalize_voucher_detail_rows(rows, detail_ordinals):
    if detail_ordinals is None:
        return rows
    output = []
    for row in rows:
        copied = dict(row)
        detail_no = copied.pop("voucher_detail_no", None)
        if detail_no in (None, ""):
            copied["voucher_detail_ordinal"] = None
        elif detail_no not in detail_ordinals:
            raise ParityFailure(f"financial row references unknown voucher detail {detail_no!r}")
        else:
            copied["voucher_detail_ordinal"] = detail_ordinals[detail_no]
        output.append(copied)
    return output


def validate_gl_detail_links(rows, detail_registry):
    details_by_key = {detail["key"]: detail for detail in detail_registry.values()}
    for row in rows:
        detail_key = row.get("voucher_detail_ordinal")
        if detail_key in (None, ""):
            continue
        detail = details_by_key.get(detail_key)
        if detail is None or detail["role"] not in FINANCIAL_DETAIL_ROLES:
            raise ParityFailure(f"GL row has broken voucher detail link {detail_key!r}")


def detail_child_warehouses(doc, voucher_type, detail, actual_qty):
    child = detail["row"]
    if voucher_type == "Stock Entry":
        qty, error = financial_verifier.decimal_value(actual_qty)
        if error or qty is None:
            raise ParityFailure(f"cannot validate stock detail link with invalid qty {actual_qty!r}")
        warehouses = []
        if qty <= 0 and child.get("s_warehouse"):
            warehouses.append(child.get("s_warehouse"))
        if qty >= 0 and child.get("t_warehouse"):
            warehouses.append(child.get("t_warehouse"))
        return warehouses
    warehouses = []
    for field in ("warehouse", "target_warehouse", "rejected_warehouse"):
        warehouse = child.get(field)
        if warehouse and warehouse not in warehouses:
            warehouses.append(warehouse)
    if not warehouses and hasattr(doc, "get"):
        warehouse = doc.get("set_warehouse")
        if warehouse:
            warehouses.append(warehouse)
    if not warehouses:
        warehouse = getattr(doc, "set_warehouse", None)
        if warehouse:
            warehouses.append(warehouse)
    return warehouses


def validate_sle_detail_links(rows, detail_registry, doc, voucher_type):
    details_by_key = {detail["key"]: detail for detail in detail_registry.values()}
    for row in rows:
        detail_key = row.get("voucher_detail_ordinal")
        if detail_key in (None, ""):
            raise ParityFailure(f"{voucher_type} stock ledger row has no voucher detail link")
        detail = details_by_key.get(detail_key)
        if detail is None:
            raise ParityFailure(f"stock ledger row has broken voucher detail link {detail_key!r}")
        if detail["role"] not in FINANCIAL_DETAIL_ROLES:
            raise ParityFailure(f"stock ledger row links non-stock child role {detail_key!r}")
        child = detail["row"]
        child_item_code = child.get("item_code") or child.get("rm_item_code")
        if row.get("item_code") != child_item_code:
            raise ParityFailure(
                f"stock ledger row item {row.get('item_code')!r} is cross-linked to {detail_key} item {child_item_code!r}"
            )
        warehouses = detail_child_warehouses(doc, voucher_type, detail, row.get("actual_qty"))
        if not warehouses:
            if detail["role"] == "supplied_item":
                continue
            raise ParityFailure(f"stock detail {detail_key} has no warehouse for link validation")
        if row.get("warehouse") not in warehouses:
            raise ParityFailure(
                f"stock ledger row warehouse {row.get('warehouse')!r} is cross-linked to {detail_key} warehouses {warehouses!r}"
            )


def capture_gl(frappe, voucher_type, voucher_no, detail_ordinals=None):
    fields = [
        "account", "debit", "credit", "debit_in_account_currency", "credit_in_account_currency",
        "account_currency", "cost_center", "party_type", "party", "against_voucher_type", "is_cancelled", "voucher_detail_no",
    ]
    rows = query_rows(frappe, "SELECT * FROM `tabGL Entry` WHERE voucher_type=%s AND voucher_no=%s", (voucher_type, voucher_no), fields)
    rows = ordinalize_voucher_detail_rows(rows, detail_ordinals)
    return rows, {
        "count": len(rows),
        "debit_sum": exact_numeric_sum(rows, "debit"),
        "credit_sum": exact_numeric_sum(rows, "credit"),
    }


def capture_sle(frappe, voucher_type, voucher_no, detail_ordinals=None):
    fields = [
        "item_code", "warehouse", "actual_qty", "qty_after_transaction", "valuation_rate",
        "stock_value", "stock_value_difference", "is_cancelled", "voucher_detail_no",
    ]
    rows = query_rows(frappe, "SELECT * FROM `tabStock Ledger Entry` WHERE voucher_type=%s AND voucher_no=%s", (voucher_type, voucher_no), fields)
    rows = ordinalize_voucher_detail_rows(rows, detail_ordinals)
    return rows, {
        "count": len(rows),
        "actual_qty_sum": exact_numeric_sum(rows, "actual_qty"),
        "stock_value_difference_sum": exact_numeric_sum(rows, "stock_value_difference"),
    }


BIN_FIELDS = ("actual_qty", "projected_qty", "reserved_qty", "valuation_rate", "stock_value")


def bin_rows(frappe, pairs):
    output = {}
    for item_code, warehouse in sorted(set(pairs)):
        row = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": warehouse},
            list(BIN_FIELDS),
            as_dict=True,
        ) or {}
        if row:
            output[(item_code, warehouse)] = tuple(row.get(field) for field in BIN_FIELDS)
    return output


def bin_deltas(before, after):
    rows = []
    zero = (Decimal(0),) * len(BIN_FIELDS)
    for key in sorted(set(before) | set(after), key=str):
        left = before.get(key) if key in before else None
        right = after.get(key) if key in after else None
        left_values = left if left is not None else zero
        right_values = right if right is not None else zero
        delta = tuple(exact_numeric_delta(left_values[index], right_values[index]) for index in range(len(BIN_FIELDS)))
        rows.append({
            "item_code": key[0],
            "warehouse": key[1],
            "before": left,
            "after": right,
            "delta": delta,
        })
    return {
        "count": len(rows),
        "actual_qty_net": sum((row["delta"][0] for row in rows), Decimal(0)),
        "stock_value_net": sum((row["delta"][4] for row in rows), Decimal(0)),
        "rows": rows,
    }


def capture_sale_invoice(frappe, name, bin_before):
    doc = frappe.get_doc("Sales Invoice", name)
    doc_fields = ["grand_total", "rounded_total", "paid_amount", "change_amount", "outstanding_amount", "net_total", "total_taxes_and_charges", "discount_amount"]
    payment_fields = ["mode_of_payment", "amount", "account"]
    detail_registry = document_detail_registry(doc)
    detail_ordinals = {detail_name: detail["key"] for detail_name, detail in detail_registry.items()}
    gl, gl_summary = capture_gl(frappe, "Sales Invoice", name, detail_ordinals)
    sle, sle_summary = capture_sle(frappe, "Sales Invoice", name, detail_ordinals)
    validate_gl_detail_links(gl, detail_registry)
    validate_sle_detail_links(sle, detail_registry, doc, "Sales Invoice")
    pairs = [(item.get("item_code"), item.get("warehouse") or doc.set_warehouse) for item in doc.items]
    return {
        "doc": {field: doc.get(field) for field in doc_fields},
        "items": sorted(tuple(item.get(field) for field in ("item_code", "warehouse", "qty", "stock_qty", "rate", "amount")) for item in doc.items),
        "payments": sorted(tuple(row.get(field) for field in payment_fields) for row in doc.payments),
        "gl": gl,
        "gl_summary": gl_summary,
        "sle": sle,
        "sle_summary": sle_summary,
        "bin": bin_deltas(bin_before, bin_rows(frappe, pairs)),
    }


def capture_stock_entry(frappe, name, bin_before):
    doc = frappe.get_doc("Stock Entry", name)
    doc_fields = ["total_amount", "total_outgoing_value", "total_incoming_value", "posting_date"]
    detail_registry = document_detail_registry(doc)
    detail_ordinals = {detail_name: detail["key"] for detail_name, detail in detail_registry.items()}
    gl, gl_summary = capture_gl(frappe, "Stock Entry", name, detail_ordinals)
    sle, sle_summary = capture_sle(frappe, "Stock Entry", name, detail_ordinals)
    validate_gl_detail_links(gl, detail_registry)
    validate_sle_detail_links(sle, detail_registry, doc, "Stock Entry")
    pairs = []
    for item in doc.items:
        if item.get("s_warehouse"):
            pairs.append((item.get("item_code"), item.get("s_warehouse")))
        if item.get("t_warehouse"):
            pairs.append((item.get("item_code"), item.get("t_warehouse")))
    return {
        "doc": {field: doc.get(field) for field in doc_fields},
        "items": sorted(tuple(item.get(field) for field in ("item_code", "s_warehouse", "t_warehouse", "qty", "transfer_qty", "valuation_rate", "amount")) for item in doc.items),
        "gl": gl,
        "gl_summary": gl_summary,
        "sle": sle,
        "sle_summary": sle_summary,
        "bin": bin_deltas(bin_before, bin_rows(frappe, pairs)),
    }


def purchase_item_rows(doc):
    return sorted(tuple(item.get(field) for field in ("item_code", "warehouse", "qty", "stock_qty", "rate", "amount")) for item in doc.items)


def purchase_item_pairs(doc):
    return [(item.get("item_code"), item.get("warehouse") or doc.get("set_warehouse")) for item in doc.items if item.get("warehouse") or doc.get("set_warehouse")]


def capture_purchase_receipt(frappe, name, bin_before):
    doc = frappe.get_doc("Purchase Receipt", name)
    doc_fields = ["grand_total", "rounded_total", "net_total", "total_taxes_and_charges", "discount_amount", "posting_date"]
    detail_registry = document_detail_registry(doc)
    detail_ordinals = {detail_name: detail["key"] for detail_name, detail in detail_registry.items()}
    gl, gl_summary = capture_gl(frappe, "Purchase Receipt", name, detail_ordinals)
    sle, sle_summary = capture_sle(frappe, "Purchase Receipt", name, detail_ordinals)
    validate_gl_detail_links(gl, detail_registry)
    validate_sle_detail_links(sle, detail_registry, doc, "Purchase Receipt")
    return {
        "doc": {field: doc.get(field) for field in doc_fields},
        "items": purchase_item_rows(doc),
        "gl": gl,
        "gl_summary": gl_summary,
        "sle": sle,
        "sle_summary": sle_summary,
        "bin": bin_deltas(bin_before, bin_rows(frappe, purchase_item_pairs(doc))),
    }


def capture_purchase_invoice(frappe, name, bin_before):
    doc = frappe.get_doc("Purchase Invoice", name)
    doc_fields = ["grand_total", "rounded_total", "outstanding_amount", "net_total", "total_taxes_and_charges", "discount_amount", "posting_date"]
    detail_registry = document_detail_registry(doc)
    detail_ordinals = {detail_name: detail["key"] for detail_name, detail in detail_registry.items()}
    gl, gl_summary = capture_gl(frappe, "Purchase Invoice", name, detail_ordinals)
    sle, sle_summary = capture_sle(frappe, "Purchase Invoice", name, detail_ordinals)
    validate_gl_detail_links(gl, detail_registry)
    validate_sle_detail_links(sle, detail_registry, doc, "Purchase Invoice")
    return {
        "doc": {field: doc.get(field) for field in doc_fields},
        "items": purchase_item_rows(doc),
        "gl": gl,
        "gl_summary": gl_summary,
        "sle": sle,
        "sle_summary": sle_summary,
        "bin": bin_deltas(bin_before, bin_rows(frappe, purchase_item_pairs(doc))),
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
    savepoint = "khitmee_perf_parity_case"
    frappe.db.sql(f"SAVEPOINT {savepoint}")
    try:
        before = bin_rows(frappe, pairs)
        result = build_document()
        name = voucher_result_name(result)
        if not name:
            raise ParityFailure(f"{label} did not return a voucher name")
        snapshot = capture_document(frappe, name, before)
        return name, snapshot
    finally:
        frappe.db.sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
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


RETRY_KIND_DOCTYPES = {
    "sale": "Sales Invoice",
    "transfer": "Stock Entry",
    "purchase_receipt": "Purchase Receipt",
    "purchase_invoice": "Purchase Invoice",
}
RETRY_DOCUMENT_FIELDS = ("name", "docstatus")
RETRY_GL_FIELDS = ("name", "voucher_no", "account", "debit", "credit", "party_type", "party", "voucher_detail_no", "is_cancelled")
RETRY_STOCK_FIELDS = ("name", "voucher_no", "voucher_detail_no", "item_code", "warehouse", "actual_qty", "stock_value_difference", "is_cancelled")
RETRY_IDEMPOTENCY_FIELDS = ("idempotency_key", "request_fingerprint", "reference_doctype", "reference_name")


def financial_effect_counts(frappe, doctype):
    if doctype not in set(RETRY_KIND_DOCTYPES.values()):
        raise ParityFailure(f"unsupported retry evidence doctype {doctype!r}")
    return {
        "documents": int(frappe.db.sql(f"SELECT COUNT(*) FROM `tab{doctype}`")[0][0]),
        "gl_entries": int(frappe.db.sql("SELECT COUNT(*) FROM `tabGL Entry`")[0][0]),
        "stock_entries": int(frappe.db.sql("SELECT COUNT(*) FROM `tabStock Ledger Entry`")[0][0]),
    }


def financial_effect_count_deltas(before, after):
    missing = sorted((set(before or {}) | set(after or {})) - {"documents", "gl_entries", "stock_entries"})
    if not before or not after or missing or any(field not in before or field not in after for field in ("documents", "gl_entries", "stock_entries")):
        raise ParityFailure("committed retry evidence requires complete before/after financial effect counts")
    return {field: int(after[field]) - int(before[field]) for field in ("documents", "gl_entries", "stock_entries")}


def retry_voucher_rows(frappe, doctype, voucher_names):
    names = sorted(set(name for name in voucher_names if name))
    if not names:
        raise ParityFailure("retry evidence collection requires at least one voucher name")
    placeholders = ", ".join(["%s"] * len(names))
    documents = query_rows(
        frappe,
        f"SELECT * FROM `tab{doctype}` WHERE name IN ({placeholders})",
        names,
        RETRY_DOCUMENT_FIELDS,
    )
    gl_entries = query_rows(
        frappe,
        f"SELECT * FROM `tabGL Entry` WHERE voucher_type=%s AND voucher_no IN ({placeholders})",
        [doctype, *names],
        RETRY_GL_FIELDS,
    )
    stock_entries = query_rows(
        frappe,
        f"SELECT * FROM `tabStock Ledger Entry` WHERE voucher_type=%s AND voucher_no IN ({placeholders})",
        [doctype, *names],
        RETRY_STOCK_FIELDS,
    )
    return documents, gl_entries, stock_entries


def idempotency_entries_for_vouchers(frappe, doctype, voucher_names):
    names = sorted(set(name for name in voucher_names if name))
    if not names:
        return []
    placeholders = ", ".join(["%s"] * len(names))
    return query_rows(
        frappe,
        f"SELECT * FROM `tabKhitmee Idempotency Log` WHERE reference_doctype=%s AND reference_name IN ({placeholders})",
        [doctype, *names],
        RETRY_IDEMPOTENCY_FIELDS,
    )


def purchase_invoice_receipt_links(frappe, voucher_names):
    names = sorted(set(name for name in voucher_names if name))
    if not names:
        return []
    placeholders = ", ".join(["%s"] * len(names))
    rows = query_rows(
        frappe,
        f"SELECT * FROM `tabPurchase Invoice Item` WHERE parent IN ({placeholders}) AND purchase_receipt IS NOT NULL",
        names,
        ("parent", "purchase_receipt", "pr_detail", "name"),
    )
    return [
        {
            "purchase_invoice": row.get("parent"),
            "purchase_receipt": row.get("purchase_receipt"),
            "pr_detail": row.get("pr_detail"),
            "purchase_invoice_item": row.get("name"),
        }
        for row in rows
    ]


def collect_retry_evidence(
    frappe,
    *,
    kind,
    client_voucher_id,
    first_voucher,
    retry_voucher,
    before_counts,
    after_counts,
    mode=financial_verifier.COMMITTED_RETRY,
    links=None,
):
    """Collect replayable persisted retry evidence for a seasonal fixture step.

    The caller must run one synchronized operation, capture `financial_effect_counts`
    before and after it, and pass the first and retry voucher names.  Global count
    deltas are intentionally required rather than inferred from the voucher rows:
    deriving them from the rows being checked would make the reconciliation circular.
    """
    doctype = RETRY_KIND_DOCTYPES.get(kind)
    if not doctype:
        raise ParityFailure(f"unsupported retry evidence kind {kind!r}")
    documents, gl_entries, stock_entries = retry_voucher_rows(frappe, doctype, (first_voucher, retry_voucher))
    evidence = {
        "mode": mode,
        "client_voucher_id": client_voucher_id,
        "first_voucher": first_voucher,
        "retry_voucher": retry_voucher,
        "documents": documents,
        "gl_entries": gl_entries,
        "stock_entries": stock_entries,
        "count_deltas": financial_effect_count_deltas(before_counts, after_counts),
    }
    if kind in {"sale", "transfer"}:
        evidence["idempotency_entries"] = idempotency_entries_for_vouchers(frappe, doctype, (first_voucher, retry_voucher))
    if kind == "purchase_invoice":
        evidence["links"] = links if links is not None else purchase_invoice_receipt_links(frappe, (first_voucher, retry_voucher))
    return evidence


def compare_exact_voucher_snapshots(reference_snapshot, candidate_snapshot, kind):
    return financial_verifier.compare_voucher_snapshots(
        reference_snapshot,
        candidate_snapshot,
        kind=kind,
        reference_source="erpnext-independent-reference",
        candidate_source="khitmee-candidate",
    )


def capture_verified_rollback_case(frappe, kind, label, pairs, build_document, capture_document, client_voucher_id):
    """Capture one rollback-only case and independently reconcile its rollback.

    This is transactional fixture evidence only.  It intentionally returns
    `proves_committed_retry=False`; committed retry proof requires the seasonal
    runner's post-commit lost-response path and `collect_retry_evidence` with
    committed before/after counts.
    """
    doctype = RETRY_KIND_DOCTYPES[kind]
    before_counts = financial_effect_counts(frappe, doctype)
    name, snapshot = capture_with_rollback(frappe, label, pairs, build_document, capture_document)
    after_counts = financial_effect_counts(frappe, doctype)
    evidence = collect_retry_evidence(
        frappe,
        kind=kind,
        client_voucher_id=client_voucher_id,
        first_voucher=name,
        retry_voucher=name,
        before_counts=before_counts,
        after_counts=after_counts,
        mode=financial_verifier.ROLLBACK_FIXTURE,
    )
    verdict = financial_verifier.reconcile_retry_evidence(evidence, kind=kind)
    return name, snapshot, evidence, verdict


def emit_negative_control_evidence(differences):
    diagnostics = financial_verifier.run_negative_controls(precision=3)
    emit("PARITY_FINANCIAL_NEGATIVE_CONTROLS", {
        "ok": diagnostics["ok"],
        "controls": [
            {"name": control["name"], "caught": control["caught"], "difference_kinds": control["difference_kinds"]}
            for control in diagnostics["controls"]
        ],
    })
    if not diagnostics["ok"]:
        differences.append({"kind": "financial_verifier_negative_control_failed"})


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
        emit_negative_control_evidence(differences)
        emit("PARITY_FINANCIAL_RETRY_SCOPE", {
            "mode": financial_verifier.ROLLBACK_FIXTURE,
            "proves_committed_retry": False,
            "reason": "normal perf parity is rollback-only; committed retry evidence must come from a seasonal fixture run",
        })
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
            new_name, new_snapshot, new_rollback_evidence, new_rollback_verdict = capture_verified_rollback_case(
                frappe, "sale", label + "-new", pairs, build_new_sale(frappe, vouchers, inputs, override, client_voucher_id), capture_sale_invoice, client_voucher_id,
            )
            classic_client_id = f"parity-{label}-classic-{args.seed}"
            classic_name, classic_snapshot, classic_rollback_evidence, classic_rollback_verdict = capture_verified_rollback_case(
                frappe, "sale", label + "-classic", pairs, build_classic_sale(frappe, vouchers, inputs, override), capture_sale_invoice, classic_client_id,
            )
            sale_names.extend((new_name, classic_name))
            comparison = compare_exact_voucher_snapshots(classic_snapshot, new_snapshot, "sale")
            case_differences = comparison["differences"]
            differences.extend({**difference, "case": label} for difference in case_differences)
            for side, rollback_verdict in (("candidate", new_rollback_verdict), ("reference", classic_rollback_verdict)):
                if not rollback_verdict["ok"]:
                    differences.append({"kind": "rollback_fixture_reconciliation", "case": label, "side": side, "differences": rollback_verdict["differences"]})
                if rollback_verdict["proves_committed_retry"]:
                    differences.append({"kind": "rollback_fixture_misclassified_as_committed_retry", "case": label, "side": side})
            emit("PARITY_VOUCHER_CASE", {
                "case": label,
                "differences": len(case_differences),
                "reference_snapshot_sha256": comparison["reference"]["snapshot_sha256"],
                "candidate_snapshot_sha256": comparison["candidate"]["snapshot_sha256"],
                "independent_sources": comparison["independent_sources"],
                "rollback_fixture": {
                    "candidate_ok": new_rollback_verdict["ok"],
                    "reference_ok": classic_rollback_verdict["ok"],
                    "candidate_proves_committed_retry": new_rollback_verdict["proves_committed_retry"],
                    "reference_proves_committed_retry": classic_rollback_verdict["proves_committed_retry"],
                    "candidate_count_deltas": new_rollback_verdict["count_deltas"],
                    "reference_count_deltas": classic_rollback_verdict["count_deltas"],
                    "candidate_evidence_sha256": financial_verifier.evidence_sha256(new_rollback_evidence),
                    "reference_evidence_sha256": financial_verifier.evidence_sha256(classic_rollback_evidence),
                },
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
        new_name, new_snapshot, new_rollback_evidence, new_rollback_verdict = capture_verified_rollback_case(
            frappe, "transfer", "transfer-new", pairs, build_new_transfer(frappe, vouchers, transfer_inputs, transfer_client_id), capture_stock_entry, transfer_client_id,
        )
        classic_client_id = f"parity-transfer-classic-{args.seed}"
        classic_name, classic_snapshot, classic_rollback_evidence, classic_rollback_verdict = capture_verified_rollback_case(
            frappe, "transfer", "transfer-classic", pairs, build_classic_transfer(frappe, vouchers, transfer_inputs), capture_stock_entry, classic_client_id,
        )
        transfer_names.extend((new_name, classic_name))
        comparison = compare_exact_voucher_snapshots(classic_snapshot, new_snapshot, "transfer")
        case_differences = comparison["differences"]
        differences.extend({**difference, "case": "direct_transfer"} for difference in case_differences)
        for side, rollback_verdict in (("candidate", new_rollback_verdict), ("reference", classic_rollback_verdict)):
            if not rollback_verdict["ok"]:
                differences.append({"kind": "rollback_fixture_reconciliation", "case": "direct_transfer", "side": side, "differences": rollback_verdict["differences"]})
            if rollback_verdict["proves_committed_retry"]:
                differences.append({"kind": "rollback_fixture_misclassified_as_committed_retry", "case": "direct_transfer", "side": side})
        emit("PARITY_VOUCHER_CASE", {
            "case": "direct_transfer",
            "differences": len(case_differences),
            "reference_snapshot_sha256": comparison["reference"]["snapshot_sha256"],
            "candidate_snapshot_sha256": comparison["candidate"]["snapshot_sha256"],
            "independent_sources": comparison["independent_sources"],
            "rollback_fixture": {
                "candidate_ok": new_rollback_verdict["ok"],
                "reference_ok": classic_rollback_verdict["ok"],
                "candidate_proves_committed_retry": new_rollback_verdict["proves_committed_retry"],
                "reference_proves_committed_retry": classic_rollback_verdict["proves_committed_retry"],
                "candidate_count_deltas": new_rollback_verdict["count_deltas"],
                "reference_count_deltas": classic_rollback_verdict["count_deltas"],
                "candidate_evidence_sha256": financial_verifier.evidence_sha256(new_rollback_evidence),
                "reference_evidence_sha256": financial_verifier.evidence_sha256(classic_rollback_evidence),
            },
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
        frappe.db.sql("SAVEPOINT khitmee_perf_parity_lock")
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
            frappe.db.sql("ROLLBACK TO SAVEPOINT khitmee_perf_parity_lock")
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


def run_inside_backend(args, fresh_connection_hook=None):
    frappe = None
    try:
        frappe, site = connect_frappe(args.site)
        emit("PARITY_START", {"site": site, "profile": args.profile or "unspecified", "seed": args.seed})
        sections = selected_sections(args.only)
        before_counts = persisted_table_counts(frappe)
        if "gp" in sections:
            run_gp_parity(frappe, args)
        if "sb" in sections:
            run_sb_parity(frappe, args)
        if "search" in sections:
            run_search_parity(frappe, args)
        if "voucher" in sections:
            run_voucher_parity(frappe, args)

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


def bundled_parity_source(path=None):
    path = Path(path or __file__)
    source = path.read_bytes()
    verifier_source = path.with_name("financial_verifier.py").read_bytes()
    # docker exec receives only this script on stdin.  Install the exact sibling
    # verifier source as an in-memory module before perf_parity's import runs so
    # the backend executes the same reviewed comparison code as the host.
    prefix = (
        "import types as _khitmee_types, sys as _khitmee_sys\n"
        f"_khitmee_verifier_source = {verifier_source!r}\n"
        "_khitmee_verifier = _khitmee_types.ModuleType('financial_verifier')\n"
        "exec(compile(_khitmee_verifier_source, 'financial_verifier.py', 'exec'), _khitmee_verifier.__dict__)\n"
        "_khitmee_sys.modules['financial_verifier'] = _khitmee_verifier\n"
    ).encode("utf-8")
    return prefix + source


def host_exec(argv):
    backend = f"{PROJECT}-backend-1"
    try:
        project = subprocess.check_output(
            ["docker", "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.project" }}', backend],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        print(f"perf parity requires the isolated {backend} container", file=sys.stderr)
        return 3
    if project != PROJECT or project == FORBIDDEN_PROJECT:
        print(f"refusing parity run: {backend} compose project is {project!r}, expected {PROJECT!r}", file=sys.stderr)
        return 3
    source = bundled_parity_source()
    command = [
        "docker", "exec", "-i", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-w", "/home/frappe/frappe-bench/sites", backend,
        "/home/frappe/frappe-bench/env/bin/python", "-", "--inside-backend", *argv,
    ]
    return subprocess.run(command, input=source).returncode


def build_parser():
    parser = argparse.ArgumentParser(description="Run the maintained khitmee-perf live parity suite")
    parser.add_argument("--profile", choices=("2gb", "4gb"), default=None)
    parser.add_argument("--site", default=None)
    parser.add_argument("--price-list", default="Standard Selling")
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--only", action="append", choices=("gp", "sb", "search", "voucher", "all"), help="run one section; repeatable")
    parser.add_argument("--inside-backend", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.inside_backend:
        return run_inside_backend(args)
    return host_exec(argv)


if __name__ == "__main__":
    sys.exit(main())
