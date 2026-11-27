"""Independent Profit Summary Oracle parity verification callable."""

import importlib
import sys
from datetime import date, timedelta
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


def _load_comparator():
	try:
		from tools.perf_parity import compare_gross_profit_rows

		return compare_gross_profit_rows
	except ImportError:
		repo_root = Path(__file__).resolve().parents[4]
		if str(repo_root) not in sys.path:
			sys.path.insert(0, str(repo_root))
		from tools.perf_parity import compare_gross_profit_rows

		return compare_gross_profit_rows


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
