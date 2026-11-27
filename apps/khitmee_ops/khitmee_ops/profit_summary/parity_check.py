"""Independent Profit Summary Oracle parity verification callable."""

import importlib
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict


class ParityCheckResult(TypedDict):
	passed: bool
	executed_cases: int
	skipped_cases: int
	failed_cases: int
	mismatches: list[dict]
	exit_code: int


QUERY_EXECUTE_NAMES = ("query_profit", "execute", "run_profit_summary", "get_profit_summary", "run")
_GP_NUMERIC_FIELDS = frozenset(
	{
		"qty",
		"avg._selling_rate",
		"valuation_rate",
		"selling_amount",
		"base_amount",
		"buying_amount",
		"gross_profit",
		"gross_profit_%",
	}
)
_TOTAL_ROW_FIELDS = (
	"sales_invoice",
	"item_code",
	"invoice_or_item",
	"warehouse",
	"monthly",
	"month",
	"posting_period",
)
_COLUMN_METADATA_FIELDS = ("fieldname", "label", "fieldtype", "options", "width")


def _scalar(value):
	if isinstance(value, Decimal):
		return float(value)
	if hasattr(value, "isoformat"):
		return value.isoformat()
	if isinstance(value, float):
		return round(value, 9)
	return value


def _column_fieldnames(columns):
	return [col.get("fieldname") if isinstance(col, dict) else None for col in (columns or [])]


def _row_as_dict(columns, row, index):
	if isinstance(row, dict):
		result = dict(row)
	else:
		result = dict(zip(_column_fieldnames(columns), row))
	result["_row_index"] = index
	return result


def _is_total_row(row, group_field=None):
	if not isinstance(row, dict):
		return False
	if group_field and row.get(group_field) == "Total":
		return True
	if any(row.get(field) == "Total" for field in _TOTAL_ROW_FIELDS):
		return True
	first_key = next((key for key in row if not str(key).startswith("_")), None)
	return first_key is not None and row.get(first_key) == "Total"


def _as_float(value):
	if value is None:
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _compare_field_maps(fast_row, upstream_row, tolerance, location):
	mismatches = []
	fields = sorted((set(fast_row) | set(upstream_row)) - {"_row_index"}, key=str)
	for field in fields:
		if field in _GP_NUMERIC_FIELDS:
			fast_raw = fast_row.get(field)
			upstream_raw = upstream_row.get(field)
			if (fast_raw is None) != (upstream_raw is None):
				mismatches.append(
					{
						"kind": "null_numeric",
						"field": field,
						"location": location,
						"fast": _scalar(fast_raw),
						"upstream": _scalar(upstream_raw),
						"tolerance": tolerance,
					}
				)
				continue
			fast_num = _as_float(fast_raw)
			upstream_num = _as_float(upstream_raw)
			if fast_num is None or upstream_num is None:
				if fast_num != upstream_num:
					mismatches.append(
						{
							"kind": "numeric_field",
							"field": field,
							"location": location,
							"fast": _scalar(fast_raw),
							"upstream": _scalar(upstream_raw),
							"diff": None,
							"tolerance": tolerance,
						}
					)
				continue
			diff = abs(fast_num - upstream_num)
			if diff > tolerance + 1e-12:
				mismatches.append(
					{
						"kind": "numeric_field",
						"field": field,
						"location": location,
						"fast": _scalar(fast_raw),
						"upstream": _scalar(upstream_raw),
						"diff": diff,
						"tolerance": tolerance,
					}
				)
			continue
		fast_val = _scalar(fast_row.get(field))
		upstream_val = _scalar(upstream_row.get(field))
		if fast_val != upstream_val:
			mismatches.append(
				{
					"kind": "exact_field" if field == "currency" else "identity_field",
					"field": field,
					"location": location,
					"fast": fast_val,
					"upstream": upstream_val,
				}
			)
	return mismatches


def _compare_gross_profit_rows_fallback(
	fast_columns,
	fast_data,
	upstream_columns,
	upstream_data,
	*,
	tolerance=1e-7,
	check_column_metadata=True,
	compare_group_order=False,
	order_keys=None,
):
	"""Self-contained oracle comparator used inside bench containers without tools/."""
	fast_rows = [_row_as_dict(fast_columns, row, i) for i, row in enumerate(fast_data or [])]
	upstream_rows = [_row_as_dict(upstream_columns, row, i) for i, row in enumerate(upstream_data or [])]
	mismatches = []

	fast_fields = _column_fieldnames(fast_columns)
	upstream_fields = _column_fieldnames(upstream_columns)
	group_field = fast_fields[0] if fast_fields else None
	if fast_fields != upstream_fields:
		mismatches.append({"kind": "columns", "fast": fast_fields, "upstream": upstream_fields})
	if check_column_metadata and fast_columns and upstream_columns:
		if len(fast_columns) != len(upstream_columns):
			mismatches.append(
				{"kind": "column_count", "fast": len(fast_columns), "upstream": len(upstream_columns)}
			)
		for col_idx, (fast_col, upstream_col) in enumerate(zip(fast_columns, upstream_columns)):
			fcol = fast_col if isinstance(fast_col, dict) else {}
			ucol = upstream_col if isinstance(upstream_col, dict) else {}
			for field in _COLUMN_METADATA_FIELDS:
				if field not in fcol and field not in ucol:
					continue
				if _scalar(fcol.get(field)) != _scalar(ucol.get(field)):
					mismatches.append(
						{
							"kind": "column_metadata",
							"column_index": col_idx,
							"field": field,
							"fast": _scalar(fcol.get(field)),
							"upstream": _scalar(ucol.get(field)),
						}
					)
	if len(fast_rows) != len(upstream_rows):
		mismatches.append({"kind": "row_count", "fast": len(fast_rows), "upstream": len(upstream_rows)})

	fast_totals = [row for row in fast_rows if _is_total_row(row, group_field)]
	upstream_totals = [row for row in upstream_rows if _is_total_row(row, group_field)]
	if len(fast_totals) != 1 or len(upstream_totals) != 1:
		if fast_rows or upstream_rows:
			mismatches.append(
				{"kind": "total_row_count", "fast": len(fast_totals), "upstream": len(upstream_totals)}
			)
	else:
		fast_tot_idx = fast_rows.index(fast_totals[0])
		up_tot_idx = upstream_rows.index(upstream_totals[0])
		if fast_tot_idx != len(fast_rows) - 1:
			mismatches.append(
				{"kind": "total_row_not_final", "side": "fast", "position": fast_tot_idx}
			)
		if up_tot_idx != len(upstream_rows) - 1:
			mismatches.append(
				{"kind": "total_row_not_final", "side": "upstream", "position": up_tot_idx}
			)
		mismatches.extend(_compare_field_maps(fast_totals[0], upstream_totals[0], tolerance, "total"))

	fast_body = [row for row in fast_rows if not _is_total_row(row, group_field)]
	upstream_body = [row for row in upstream_rows if not _is_total_row(row, group_field)]
	if not compare_group_order:
		fast_body = sorted(fast_body, key=lambda r: str(_scalar(r.get(group_field) or "")))
		upstream_body = sorted(upstream_body, key=lambda r: str(_scalar(r.get(group_field) or "")))
	for index, (fast_row, upstream_row) in enumerate(zip(fast_body, upstream_body)):
		mismatches.extend(_compare_field_maps(fast_row, upstream_row, tolerance, f"group-row:{index}"))
	return mismatches


def _load_comparator():
	try:
		from tools.perf_parity import compare_gross_profit_rows

		return compare_gross_profit_rows
	except ImportError:
		repo_root = Path(__file__).resolve().parents[4]
		if str(repo_root) not in sys.path:
			sys.path.insert(0, str(repo_root))
		try:
			from tools.perf_parity import compare_gross_profit_rows

			return compare_gross_profit_rows
		except ImportError:
			return _compare_gross_profit_rows_fallback


def _resolve_profit_summary_execute():
	package_names = []
	if __package__:
		package_names.append(__package__)
	package_names.extend(("khitmee_ops.profit_summary", "khitmee_ops.khitmee_ops.profit_summary"))
	errors = []
	for package_name in dict.fromkeys(package_names):
		try:
			package = importlib.import_module(package_name)
		except Exception as exc:
			errors.append(f"{package_name}: {type(exc).__name__}: {exc}")
			continue
		query_profit = getattr(package, "query_profit", None)
		if callable(query_profit):
			return query_profit
		try:
			query_module = importlib.import_module(f"{package_name}.query")
		except Exception as exc:
			errors.append(f"{package_name}.query: {type(exc).__name__}: {exc}")
			continue
		for attribute in QUERY_EXECUTE_NAMES:
			execute = getattr(query_module, attribute, None)
			if callable(execute):
				return execute
		errors.append(f"{package_name}.query: no callable query_profit/execute/run_profit_summary/get_profit_summary/run")
	raise RuntimeError("Profit Summary query implementation is unavailable; " + "; ".join(errors))


def _stock_oracle_execute():
	from erpnext.accounts.report.gross_profit.gross_profit import execute

	return execute


def _frappe_dict(frappe_module: Any, filters: dict):
	if hasattr(frappe_module, "_dict"):
		return frappe_module._dict(dict(filters))
	return dict(filters)


def _normalize_execute_result(result):
	if isinstance(result, (list, tuple)) and len(result) >= 2:
		return result[0], result[1]
	if isinstance(result, dict):
		columns = result.get("columns")
		rows = result.get("result", result.get("data"))
		if columns is not None and rows is not None:
			return columns, rows
	raise RuntimeError("report execute result did not contain columns and rows")


def _parse_iso_date(value) -> date:
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value))


def _stratified_single_day_ranges(from_date: str, to_date: str, sample_days: int) -> list[tuple[str, str]]:
	start = _parse_iso_date(from_date)
	end = _parse_iso_date(to_date)
	if end < start:
		raise ValueError(f"to_date {to_date!r} precedes from_date {from_date!r}")
	if sample_days <= 0:
		return []

	span = (end - start).days + 1
	offsets = []
	if sample_days >= 1:
		offsets.append(0)
	if sample_days >= 2:
		offsets.append(span - 1)
	if sample_days >= 3:
		for index in range(1, sample_days - 1):
			offsets.append(round(index * (span - 1) / (sample_days - 1)))

	unique_offsets = []
	for offset in offsets:
		if offset not in unique_offsets:
			unique_offsets.append(offset)
	if len(unique_offsets) < sample_days:
		for offset in range(span):
			if len(unique_offsets) >= sample_days:
				break
			if offset not in unique_offsets:
				unique_offsets.append(offset)
	while len(unique_offsets) < sample_days:
		unique_offsets.append(unique_offsets[len(unique_offsets) % span])

	ranges = []
	for offset in sorted(unique_offsets[:sample_days]):
		day = start + timedelta(days=offset)
		ranges.append((day.isoformat(), day.isoformat()))
	return ranges


def _case_ranges(from_date: str, to_date: str, sample_days: int | None) -> list[tuple[str, str, str]]:
	ranges = [("full", from_date, to_date)]
	if sample_days and sample_days > 0:
		for index, (sample_from, sample_to) in enumerate(_stratified_single_day_ranges(from_date, to_date, sample_days), start=1):
			ranges.append((f"sample-{index}", sample_from, sample_to))
	return ranges


def _planned_case_count(group_by_list: tuple[str, ...], sample_days: int | None) -> int:
	ranges_per_mode = 1 + (sample_days if sample_days and sample_days > 0 else 0)
	return len(group_by_list) * 2 * ranges_per_mode


def _finish_result(executed_cases: int, skipped_cases: int, failed_cases: int, mismatches: list[dict]) -> ParityCheckResult:
	passed = bool(executed_cases > 0 and skipped_cases == 0 and failed_cases == 0 and not mismatches)
	return {
		"passed": passed,
		"executed_cases": executed_cases,
		"skipped_cases": skipped_cases,
		"failed_cases": failed_cases,
		"mismatches": mismatches,
		"exit_code": 0 if passed else 1,
	}


def _unavailable_result(kind: str, diagnostic: str, planned_cases: int) -> ParityCheckResult:
	return _finish_result(
		0,
		planned_cases,
		0,
		[{"kind": kind, "diagnostic": diagnostic}],
	)


def run_profit_parity_check(
	company: str,
	from_date: str,
	to_date: str,
	group_by_list: tuple[str, ...] = ("Warehouse", "Monthly"),
	sample_days: int | None = None,
) -> ParityCheckResult:
	"""Run exact Profit Summary versus stock Gross Profit parity checks.

	The Oracle is always stock ``erpnext.accounts.report.gross_profit``.  Every
	case is compared at ``tolerance=1e-7`` with order-aware grouped comparison.
	"""
	if not company:
		raise ValueError("company is required")
	if not group_by_list:
		raise ValueError("group_by_list must contain at least one grouping")
	if sample_days is not None and sample_days < 0:
		raise ValueError("sample_days must be positive or None")

	if _parse_iso_date(to_date) < _parse_iso_date(from_date):
		raise ValueError(f"to_date {to_date!r} precedes from_date {from_date!r}")
	ranges = _case_ranges(from_date, to_date, sample_days)
	planned_cases = _planned_case_count(tuple(group_by_list), sample_days)

	try:
		import frappe
	except Exception as exc:
		return _unavailable_result(
			"bench_unavailable",
			f"run_profit_parity_check requires an initialized bench Frappe module: {type(exc).__name__}: {exc}",
			planned_cases,
		)

	if not getattr(frappe, "db", None):
		return _unavailable_result(
			"bench_unavailable",
			"run_profit_parity_check requires a live bench database connection (frappe.db is unset)",
			planned_cases,
		)

	try:
		profit_summary_execute = _resolve_profit_summary_execute()
		stock_execute = _stock_oracle_execute()
		compare_gross_profit_rows = _load_comparator()
	except Exception as exc:
		return _unavailable_result(
			"verification_unavailable",
			f"could not load Profit Summary, stock Oracle, or comparator: {type(exc).__name__}: {exc}",
			planned_cases,
		)

	executed_cases = 0
	failed_cases = 0
	mismatches = []
	for group_by in tuple(group_by_list):
		for include_returned in (0, 1):
			for range_label, case_from_date, case_to_date in ranges:
				case = f"{group_by}-ret{include_returned}-{range_label}-{case_from_date}-{case_to_date}"
				filters = {
					"company": company,
					"from_date": case_from_date,
					"to_date": case_to_date,
					"group_by": group_by,
					"include_returned_invoices": include_returned,
				}
				try:
					fast_result = profit_summary_execute(_frappe_dict(frappe, filters))
					fast_columns, fast_rows = _normalize_execute_result(fast_result)
					stock_columns, stock_rows = _normalize_execute_result(
						stock_execute(_frappe_dict(frappe, filters))
					)
					fast_mode = fast_result.get("mode") if isinstance(fast_result, dict) else None
					if fast_mode in {"pending", "budget_exceeded"}:
						case_mismatches = [
							{
								"kind": "non_oracle_mode",
								"mode": fast_mode,
								"message": fast_result.get("message") if isinstance(fast_result, dict) else None,
							}
						]
					else:
						case_mismatches = compare_gross_profit_rows(
							fast_columns,
							fast_rows,
							stock_columns,
							stock_rows,
							tolerance=1e-7,
							check_column_metadata=True,
							compare_group_order=group_by in {"Invoice", "Warehouse", "Monthly"},
						)
				except Exception as exc:
					case_mismatches = [
						{
							"kind": "execution_error",
							"error": str(exc),
							"error_type": type(exc).__name__,
						}
					]

				executed_cases += 1
				if case_mismatches:
					failed_cases += 1
				mismatches.extend(
					{
						**mismatch,
						"case": case,
						"group_by": group_by,
						"include_returned_invoices": include_returned,
						"from_date": case_from_date,
						"to_date": case_to_date,
					}
					for mismatch in case_mismatches
				)

	return _finish_result(executed_cases, 0, failed_cases, mismatches)
