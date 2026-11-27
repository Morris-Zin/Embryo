# Copyright (c) 2026, Khitmee and contributors
# License: GNU General Public License v3

import importlib
import inspect
import json
import math
import re
import sys
from collections import OrderedDict
from typing import Literal, TypedDict

try:
	import frappe
	from frappe.utils import add_days, cint, flt, formatdate

	from erpnext.accounts.report.gross_profit import gross_profit as standard_gross_profit
	from erpnext.accounts.report.utils import validate_date_range
except ImportError:  # pragma: no cover - lets pure-Python SQL/calculation tests run outside a bench
	frappe = None
	standard_gross_profit = None
	validate_date_range = None

	def add_days(value, days):
		from datetime import datetime, timedelta

		if isinstance(value, str):
			parsed = datetime.fromisoformat(value)
			result = parsed + timedelta(days=days)
			return result.date().isoformat() if len(value) <= 10 else result.isoformat(sep=" ")
		return value + timedelta(days=days)

	def cint(value):
		return int(value or 0)

	def flt(value, precision=None):
		value = float(value or 0)
		return round(value, precision) if precision is not None else value

	def formatdate(value, format_string=None):
		return str(value)


class RowContribution(TypedDict):
	sales_invoice: str
	item_row: str
	posting_date: str
	warehouse: str
	is_return: int
	return_against: str | None
	update_stock: int
	delivered_by_supplier: int
	valuation_source: Literal[
		"sle",
		"bundle_sle",
		"missing_detail_zero",
		"average_rate",
		"last_purchase_rate",
		"dropship",
		"dn_valuation",
	]
	qty: float
	base_amount: float
	buying_amount: float
	query_phase: Literal[0, 1]
	first_seen_key: str
	is_precision_safe: bool


class ContributionBatch(TypedDict):
	contributions: list[RowContribution]
	replay_reasons_by_invoice: dict[str, str]
	currency_precision: int
	float_precision: int
	rounding_method: str


SI_IDX = "khitmee_si_co_ds_ret_date"
SII_IDX = "khitmee_sii_parent_warehouse"
SII_JOIN = "JOIN `tabSales Invoice Item` ON"


def inject_sii_force_index(sql, index_name=SII_IDX):
	"""Inject the Sales Invoice Item join hint, failing closed if upstream SQL changes shape."""
	join_count = sql.count(SII_JOIN)
	if join_count != 1:
		raise RuntimeError(
			f"Expected exactly one Sales Invoice Item join in invoice SQL, found {join_count}"
		)
	return sql.replace(
		SII_JOIN,
		f"JOIN `tabSales Invoice Item` FORCE INDEX (`{index_name}`) ON",
	)


_NAMED_QUERY_PARAM = re.compile(r"%\((param\d+)\)s")


def positionalize_named_params(sql, params):
	if not isinstance(params, dict):
		return sql, list(params or [])

	ordered = []

	def replace(match):
		ordered.append(params[match.group(1)])
		return "%s"

	return _NAMED_QUERY_PARAM.sub(replace, sql), ordered


def posting_time_sort_value(value):
	if hasattr(value, "total_seconds"):
		return value.total_seconds()
	if hasattr(value, "hour"):
		return value.hour * 3600 + value.minute * 60 + value.second
	parts = str(value or "0:0:0").split(":")
	try:
		return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
	except (IndexError, TypeError, ValueError):
		return 0


def posting_date_sort_value(value):
	if hasattr(value, "date") and hasattr(value, "hour"):
		value = value.date()
	if hasattr(value, "isoformat"):
		return value.isoformat()
	return str(value or "")


def merge_posting_rows_descending(*row_groups):
	rows = [row for group in row_groups for row in (group or [])]
	rows.sort(
		key=lambda row: (
			posting_date_sort_value(row.get("posting_date")),
			posting_time_sort_value(row.get("posting_time")),
		),
		reverse=True,
	)
	return rows


def posting_time_microseconds(value):
	"""Return microseconds since midnight for canonical first-seen ordering keys."""
	if hasattr(value, "total_seconds"):
		return round(value.total_seconds() * 1_000_000)
	if hasattr(value, "hour"):
		return (
			(value.hour * 3600 + value.minute * 60 + value.second) * 1_000_000
			+ getattr(value, "microsecond", 0)
		)
	parts = str(value or "0:0:0").split(":")
	try:
		seconds = float(parts[2])
		return round((int(parts[0]) * 3600 + int(parts[1]) * 60 + seconds) * 1_000_000)
	except (IndexError, TypeError, ValueError):
		return 0


def posting_time_key_digits(value):
	"""Encode time of day as sortable HHMMSSffffff digits for first-seen keys."""
	posting_time_us = posting_time_microseconds(value)
	hours, remainder = divmod(posting_time_us, 3_600_000_000)
	minutes, remainder = divmod(remainder, 60_000_000)
	seconds, microseconds = divmod(remainder, 1_000_000)
	return f"{hours:02d}{minutes:02d}{seconds:02d}{microseconds:06d}"


def first_seen_key_for_row(row, query_phase):
	"""Build the frozen sortable phase key: ``phase|YYYY-MM-DD|TTTTTTTTTTTT``."""
	posting_date = posting_date_sort_value(row.get("posting_date"))
	return f"{int(query_phase)}|{posting_date}|{posting_time_key_digits(row.get('posting_time'))}"


def invoice_descending_order_key(row):
	return (
		posting_date_sort_value(row.get("posting_date")),
		posting_time_microseconds(row.get("posting_time")),
		str(row.get("parent") or row.get("invoice") or ""),
	)


def sort_invoice_rows_descending(rows):
	"""Apply the export-safe posting date/time/invoice tie-break to one query phase."""
	return sorted(rows or [], key=invoice_descending_order_key, reverse=True)


def mark_query_phase(rows, query_phase):
	for row in rows or []:
		row["_khitmee_query_phase"] = int(query_phase)
		row["_khitmee_first_seen_key"] = first_seen_key_for_row(row, query_phase)
	return rows


COVERING_DRIVE_ANCHOR = (
	"FROM `tabSales Invoice` JOIN `tabSales Invoice Item` ON "
	"`tabSales Invoice Item`.`parent`=`tabSales Invoice`.`name` "
)
COVERING_DRIVE_FROM = (
	"FROM `tabSales Invoice` AS `si2` FORCE INDEX (`khitmee_si_co_ds_ret_date`) "
	"INNER JOIN `tabSales Invoice Item` AS `sii2` FORCE INDEX (`khitmee_sii_parent_warehouse`) "
	"ON `sii2`.`parent` = `si2`.`name` "
	"STRAIGHT_JOIN `tabSales Invoice Item` ON `tabSales Invoice Item`.`name` = `sii2`.`name` "
	"STRAIGHT_JOIN `tabSales Invoice` ON `tabSales Invoice`.`name` = `tabSales Invoice Item`.`parent` "
)
COVERING_DRIVE_ORDER_BY = re.compile(
	r"ORDER\s+BY\s+`tabSales Invoice`\.`posting_date`\s+DESC\s*,\s*"
	r"`tabSales Invoice`\.`posting_time`\s+DESC\s*;?\s*$",
	flags=re.IGNORECASE,
)


def build_warehouse_covering_drive(
	warehouses,
	company,
	from_date,
	to_date,
	return_kind="normal",
	include_returned_invoices=False,
):
	warehouses = list(warehouses or [None])
	warehouse_placeholders = ", ".join(["%s"] * len(warehouses))
	if return_kind == "return":
		is_return_predicate = "(`si2`.`is_return` = 1 AND `si2`.`return_against` IS NOT NULL)"
	elif return_kind == "unlinked_return":
		is_return_predicate = "(`si2`.`is_return` = 1 AND `si2`.`return_against` IS NULL)"
	elif include_returned_invoices:
		is_return_predicate = (
			"(`si2`.`is_return` = 0 OR (`si2`.`is_return` = 1 AND `si2`.`return_against` IS NULL))"
		)
	else:
		is_return_predicate = "`si2`.`is_return` = 0"

	clause = """
		`si2`.`company` = %s
		AND `si2`.`docstatus` = 1
		AND `si2`.`is_opening` != 'Yes'
		AND {is_return_predicate}
		AND `si2`.`posting_date` BETWEEN %s AND %s
		AND `sii2`.`warehouse` IN ({warehouse_placeholders})
	""".format(is_return_predicate=is_return_predicate, warehouse_placeholders=warehouse_placeholders)
	return " ".join(clause.split()), [company, from_date, to_date, *warehouses]


def inject_warehouse_covering_drive(sql, clause):
	anchor_count = sql.count(COVERING_DRIVE_ANCHOR)
	if anchor_count != 1:
		raise RuntimeError(
			f"Expected exactly one invoice covering-drive anchor, found {anchor_count}"
		)
	sql = sql.replace(COVERING_DRIVE_ANCHOR, COVERING_DRIVE_FROM, 1)

	marker = " ORDER BY "
	position = sql.rfind(marker)
	if position < 0:
		raise RuntimeError("Expected invoice SQL to contain ORDER BY for warehouse covering-drive injection")
	sql = sql[:position] + " AND " + clause + sql[position:]

	match = COVERING_DRIVE_ORDER_BY.search(sql)
	if not match:
		raise RuntimeError("Expected invoice SQL to end with Sales Invoice posting date/time ORDER BY")
	terminator = ";" if ";" in match.group(0) else ""
	return (
		sql[: match.start()]
		+ "ORDER BY `si2`.`posting_date` DESC, `si2`.`posting_time` DESC"
		+ terminator
	)


def build_warehouse_semijoin_clause(*args, **kwargs):
	return build_warehouse_covering_drive(*args, **kwargs)


def inject_warehouse_semijoin(sql, clause):
	return inject_warehouse_covering_drive(sql, clause)


def append_tiebreaker_orderby(sql):
	if not re.search(r"\bORDER\s+BY\b", sql, flags=re.IGNORECASE):
		raise RuntimeError("Expected invoice SQL to contain ORDER BY before adding tie-breakers")
	suffix = "`tabSales Invoice`.`name` DESC, `tabSales Invoice Item`.`idx` ASC"
	terminator = ";" if sql.rstrip().endswith(";") else ""
	body = sql.rstrip().removesuffix(";").rstrip()
	if suffix in body:
		return body + terminator
	return body + ", " + suffix + terminator


def batched(values, size):
	"""Yield deterministic query batches without loading more data than the batch itself."""
	values = list(values)
	for start in range(0, len(values), size):
		yield values[start : start + size]


def normalize_sle_key(item_code, warehouse, voucher_type, voucher_no, voucher_detail_no):
	return tuple(str(value or "") for value in (item_code, warehouse, voucher_type, voucher_no, voucher_detail_no))


def build_matched_sle_query(keys, company):
	"""Build one bounded MariaDB query for exact line SLE matches.

	Each OR group carries the full five-column identity ERPNext uses when it scans a
	preloaded pair list: item, warehouse, voucher type, voucher number and voucher detail.
	No unkeyed company/item/warehouse history rows are returned.
	"""
	clauses = []
	params = [company]
	for key in keys:
		item_code, warehouse, voucher_type, voucher_no, voucher_detail_no = key
		clauses.append(
			"(`item_code` = %s AND `warehouse` = %s AND `voucher_type` = %s "
			"AND `voucher_no` = %s AND `voucher_detail_no` = %s)"
		)
		params.extend([item_code, warehouse, voucher_type, voucher_no, voucher_detail_no])

	sql = """
		SELECT
			`item_code`,
			`warehouse`,
			`voucher_type`,
			`voucher_no`,
			`voucher_detail_no`,
			`stock_value`,
			`actual_qty` AS `qty`,
			`posting_datetime`,
			`creation`
		FROM `tabStock Ledger Entry`
		WHERE `company` = %s
			AND `is_cancelled` = 0
			AND ({clauses})
		ORDER BY `posting_datetime` DESC, `creation` DESC
	""".format(clauses=" OR ".join(clauses))
	return sql, params


def build_sales_invoice_voucher_sle_query(voucher_nos, company):
	placeholders = ", ".join(["%s"] * len(voucher_nos))
	sql = """
		SELECT
			`item_code`,
			`warehouse`,
			`voucher_type`,
			`voucher_no`,
			`voucher_detail_no`,
			`stock_value`,
			`stock_value_difference`,
			`actual_qty`,
			`posting_datetime`,
			`creation`
		FROM `tabStock Ledger Entry`
		WHERE `voucher_no` IN ({placeholders})
			AND `voucher_type` = 'Sales Invoice'
			AND `company` = %s
			AND `is_cancelled` = 0
	""".format(placeholders=placeholders)
	return sql, [*voucher_nos, company]


def _voucher_sle_value(row, index, fieldname):
	if hasattr(row, "get"):
		return row.get(fieldname)
	return row[index]


def _voucher_sle_key(row):
	return normalize_sle_key(
		_voucher_sle_value(row, 0, "item_code"),
		_voucher_sle_value(row, 1, "warehouse"),
		_voucher_sle_value(row, 2, "voucher_type"),
		_voucher_sle_value(row, 3, "voucher_no"),
		_voucher_sle_value(row, 4, "voucher_detail_no"),
	)


def _voucher_sle_ordering(row):
	return (
		_voucher_sle_value(row, 8, "posting_datetime"),
		_voucher_sle_value(row, 9, "creation"),
	)


# ERPNext rounds each SLE's stock_value independently, so the derived predecessor
# ``stock_value - stock_value_difference`` can be off from the stored predecessor by
# ~1e-6.  That never matters except when the buying amount sits on a currency
# rounding half-way point (1.2125 derived vs 1.212499 stored): then the shortcut
# flips flt() by one unit.  Such keys fetch the real predecessor row instead.
ROUNDING_AMBIGUITY_MARGIN = 1e-5


def is_rounding_ambiguous(amount, precision) -> bool:
	"""True when ``amount`` is within the shortcut's error of a half-unit boundary."""
	if precision is None:
		return False
	scale = 10 ** int(precision)
	scaled = abs(flt(amount)) * scale
	distance_units = abs(scaled - math.floor(scaled) - 0.5)
	return distance_units / scale < ROUNDING_AMBIGUITY_MARGIN


def process_sales_invoice_sle_rows(rows, requested_keys, currency_precision=None):
	"""Process one voucher-scoped SLE batch without materializing unneeded row dicts.

	Keys whose derived predecessor is unsafe (near-zero stock value, or a buying
	amount on a rounding half-way point) are returned in ``near_zero_keys`` /
	``ambiguous_keys`` for the exact predecessor fetch.
	"""
	requested_keys = list(dict.fromkeys(requested_keys))
	requested_key_set = set(requested_keys)
	latest_rows = {}
	latest_ordering = {}
	seen_ordering_by_pair = {}
	tie_pairs = set()

	for row in rows or []:
		if isinstance(row, tuple):
			key = normalize_sle_key(row[0], row[1], row[2], row[3], row[4])
			ordering = (row[8], row[9])
		else:
			key = _voucher_sle_key(row)
			ordering = _voucher_sle_ordering(row)
		pair = (key[0], key[1])
		if ordering in seen_ordering_by_pair.setdefault(pair, set()):
			tie_pairs.add(pair)
		else:
			seen_ordering_by_pair[pair].add(ordering)

		if key in requested_key_set and (
			key not in latest_rows or ordering > latest_ordering[key]
		):
			latest_rows[key] = row
			latest_ordering[key] = ordering

	matched = {}
	predecessors = {}
	near_zero_keys = []
	ambiguous_keys = []
	pair_exists = set()
	for key in requested_keys:
		row = latest_rows.get(key)
		if row is None or (key[0], key[1]) in tie_pairs:
			continue

		if isinstance(row, tuple):
			stock_value = row[5]
			stock_value_difference = row[6]
			actual_qty = row[7]
		else:
			stock_value = _voucher_sle_value(row, 5, "stock_value")
			stock_value_difference = _voucher_sle_value(row, 6, "stock_value_difference")
			actual_qty = _voucher_sle_value(row, 7, "actual_qty")
		previous_stock_value = flt(stock_value) - flt(stock_value_difference)
		pair_exists.add((key[0], key[1]))
		if abs(previous_stock_value) < 1e-4:
			near_zero_keys.append(key)
			continue
		if is_rounding_ambiguous(stock_value_difference, currency_precision):
			ambiguous_keys.append(key)
			continue

		matched[key] = {"stock_value": stock_value, "qty": actual_qty}
		predecessors[key] = {"stock_value": round(previous_stock_value, 9)}

	return {
		"matched": matched,
		"predecessors": predecessors,
		"near_zero_keys": near_zero_keys,
		"ambiguous_keys": ambiguous_keys,
		"tie_pairs": tie_pairs,
		"pair_exists": pair_exists,
		"matched_keys": set(matched) | set(near_zero_keys) | set(ambiguous_keys),
		"orderings_by_pair": seen_ordering_by_pair,
	}


def build_pair_exists_query(pairs, company):
	"""Cheap existence query preserving ERPNext's non-empty-pair-miss returns-zero edge case.

	Only call this for pairs not already resolved by exact SLE matches or the cache;
	known pairs must never trigger redundant SELECT DISTINCT probes.
	"""
	clauses = []
	params = [company]
	for item_code, warehouse in pairs:
		clauses.append("(`item_code` = %s AND `warehouse` = %s)")
		params.extend([item_code, warehouse])

	sql = """
		SELECT DISTINCT `item_code`, `warehouse`
		FROM `tabStock Ledger Entry`
		WHERE `company` = %s
			AND `is_cancelled` = 0
			AND ({clauses})
	""".format(clauses=" OR ".join(clauses))
	return sql, params


def build_tie_detection_query(matched_rows, company):
	"""Detect ambiguous (item, warehouse, posting_datetime, creation) tie groups.

	When more than one non-cancelled SLE shares the exact ordering tuple, MariaDB's
	tie order is unspecified and our bounded LIMIT-1 predecessor probe cannot guarantee
	upstream parity. Those pairs must be delegated to the ordered-list calculation.
	"""
	clauses = []
	params = [company]
	for row in matched_rows:
		clauses.append(
			"(`item_code` = %s AND `warehouse` = %s AND `posting_datetime` = %s AND `creation` = %s)"
		)
		params.extend(
			[row.get("item_code"), row.get("warehouse"), row.get("posting_datetime"), row.get("creation")]
		)

	sql = """
		SELECT `item_code`, `warehouse`, `posting_datetime`, `creation`, COUNT(*) AS `tie_count`
		FROM `tabStock Ledger Entry`
		WHERE `company` = %s
			AND `is_cancelled` = 0
			AND ({clauses})
		GROUP BY `item_code`, `warehouse`, `posting_datetime`, `creation`
		HAVING COUNT(*) > 1
	""".format(clauses=" OR ".join(clauses))
	return sql, params


def build_pair_history_query(item_code, warehouse, company):
	"""Full ordered SLE history for one pair, matching upstream get_stock_ledger_entries."""
	sql = """
		SELECT
			`item_code`,
			`warehouse`,
			`voucher_type`,
			`voucher_no`,
			`voucher_detail_no`,
			`stock_value`,
			`actual_qty` AS `qty`,
			`posting_datetime`,
			`creation`
		FROM `tabStock Ledger Entry`
		WHERE `company` = %s
			AND `is_cancelled` = 0
			AND `item_code` = %s
			AND `warehouse` = %s
		ORDER BY `posting_datetime` DESC, `creation` DESC
	"""
	return sql, [company, item_code, warehouse]


def build_predecessor_query(matched, key_order, company):
	"""Build a MariaDB 11.8 UNION query returning at most one predecessor per matched line.

	MariaDB supports ORDER BY + LIMIT inside parenthesized UNION branches. Each branch is
	correlated by literal key values and stops at the first older non-cancelled SLE in
	ERPNext's ordering (`posting_datetime DESC, creation DESC`). The result set therefore
	contains only predecessors, never the pair's full history.
	"""
	branches = []
	params = []
	for index, key in enumerate(key_order):
		row = matched[key]
		item_code, warehouse, _voucher_type, _voucher_no, _voucher_detail_no = key
		branches.append(
			f"""
			(SELECT
				{index} AS `khitmee_key_id`,
				`stock_value`,
				`actual_qty` AS `qty`,
				`posting_datetime`,
				`creation`
			FROM `tabStock Ledger Entry`
			WHERE `company` = %s
				AND `item_code` = %s
				AND `warehouse` = %s
				AND `is_cancelled` = 0
				AND (
					`posting_datetime` < %s
					OR (`posting_datetime` = %s AND `creation` < %s)
				)
			ORDER BY `posting_datetime` DESC, `creation` DESC
			LIMIT 1)
			"""
		)
		params.extend(
			[
				company,
				item_code,
				warehouse,
				row.get("posting_datetime"),
				row.get("posting_datetime"),
				row.get("creation"),
			]
		)

	return " UNION ALL ".join(branches), params


def build_previous_sle_batch_query(candidates):
	"""Build a bounded MariaDB UNION query for plain-item get_previous_sle lookups."""
	branches = []
	params = []
	for index, candidate in enumerate(candidates or ()):
		branches.append(
			f"""
			(SELECT
				{index} AS `khitmee_idx`,
				`stock_queue`,
				`valuation_rate`
			FROM `tabStock Ledger Entry`
			WHERE `is_cancelled` = 0
				AND `item_code` = %s
				AND `warehouse` = %s
				AND `posting_datetime` <= %s
			ORDER BY `posting_datetime` DESC, `creation` DESC
			LIMIT 1)
			"""
		)
		params.extend([candidate["item_code"], candidate["warehouse"], candidate["posting_datetime"]])

	return " UNION ALL ".join(branches), params


def build_last_valuation_rate_batch_query(candidates):
	"""Build a bounded MariaDB UNION query for get_valuation_rate last-SLE fallbacks."""
	branches = []
	params = []
	for index, candidate in enumerate(candidates or ()):
		branches.append(
			f"""
			(SELECT
				{index} AS `khitmee_idx`,
				`valuation_rate`
			FROM `tabStock Ledger Entry`
			WHERE `item_code` = %s
				AND `warehouse` = %s
				AND `valuation_rate` >= 0
				AND `is_cancelled` = 0
				AND NOT (`voucher_no` = %s AND `voucher_type` = %s)
				AND `posting_datetime` <= %s
			ORDER BY `posting_datetime` DESC, `creation` DESC
			LIMIT 1)
			"""
		)
		params.extend(
			[
				candidate["item_code"],
				candidate["warehouse"],
				candidate["voucher_no"],
				candidate["voucher_type"],
				candidate["posting_datetime"],
			]
		)

	return " UNION ALL ".join(branches), params


def build_sle_window_query(item_codes, warehouse, company, window_start, window_end):
	"""Bounded ascending SLE window scan for one warehouse and an item-code batch."""
	placeholders = ", ".join(["%s"] * len(item_codes))
	sql = """
		SELECT item_code, warehouse, voucher_type, voucher_no, voucher_detail_no,
			stock_value, actual_qty AS qty, posting_datetime, creation
		FROM `tabStock Ledger Entry`
		WHERE item_code IN ({placeholders}) AND warehouse = %s
			AND posting_datetime >= %s AND posting_datetime < %s
			AND is_cancelled = 0 AND company = %s
		ORDER BY item_code, warehouse, posting_datetime, creation
	""".format(placeholders=placeholders)
	return sql, [*item_codes, warehouse, window_start, window_end, company]


def sle_window_tuple_key(row):
	return normalize_sle_key(row[0], row[1], row[2], row[3], row[4])


def sle_window_tuple_ordering(row):
	return (row[7], row[8])


def sle_window_tuple_to_dict(row):
	return {
		"item_code": row[0],
		"warehouse": row[1],
		"voucher_type": row[2],
		"voucher_no": row[3],
		"voucher_detail_no": row[4],
		"stock_value": row[5],
		"qty": row[6],
		"posting_datetime": row[7],
		"creation": row[8],
	}


def boundary_tuple_ordering(row):
	return (row[2], row[3])


def boundary_tuple_to_dict(row):
	return {
		"item_code": row[0],
		"warehouse": row[1],
		"posting_datetime": row[2],
		"creation": row[3],
		"is_cancelled": row[4],
		"company": row[5],
		"stock_value": row[6],
	}


def assemble_sle_window_tuples(keys, window_rows, boundary_row):
	keys = list(dict.fromkeys(keys))
	requested_keys = set(keys)
	window_rows = list(window_rows or [])
	tie = False

	for previous_row, current_row in zip(window_rows, window_rows[1:]):
		if sle_window_tuple_ordering(previous_row) == sle_window_tuple_ordering(current_row):
			tie = True
			break

	if window_rows and boundary_row is not None:
		boundary_ordering = (
			boundary_row.get("posting_datetime") if hasattr(boundary_row, "get") else boundary_row[2],
			boundary_row.get("creation") if hasattr(boundary_row, "get") else boundary_row[3],
		)
		if sle_window_tuple_ordering(window_rows[0]) == boundary_ordering:
			tie = True

	if tie:
		return {
			"matched": {},
			"predecessors": {},
			"tie": True,
			"pair_exists": bool(window_rows) or boundary_row is not None,
		}

	last_index_by_key = {}
	for index, row in enumerate(window_rows):
		key = sle_window_tuple_key(row)
		if key in requested_keys:
			last_index_by_key[key] = index

	matched = {}
	predecessors = {}
	for key, index in last_index_by_key.items():
		matched[key] = window_rows[index]
		predecessors[key] = window_rows[index - 1] if index > 0 else boundary_row

	return {
		"matched": matched,
		"predecessors": predecessors,
		"tie": False,
		"pair_exists": bool(window_rows) or boundary_row is not None,
	}


def build_sle_boundary_max_query(item_codes, warehouse, window_start):
	"""Loose-index-scan boundary timestamp probe; intentionally company/cancellation agnostic."""
	placeholders = ", ".join(["%s"] * len(item_codes))
	sql = """
		SELECT item_code, warehouse, MAX(posting_datetime) AS pd
		FROM `tabStock Ledger Entry`
		WHERE item_code IN ({placeholders}) AND warehouse = %s
			AND posting_datetime < %s
		GROUP BY item_code, warehouse
	""".format(placeholders=placeholders)
	return sql, [*item_codes, warehouse, window_start]


def build_sle_boundary_rows_query(boundary_points):
	"""Fetch every row at the boundary max timestamp so company/cancellation filtering is exact."""
	clauses = []
	params = []
	for item_code, warehouse, posting_datetime in boundary_points:
		clauses.append("(%s, %s, %s)")
		params.extend([item_code, warehouse, posting_datetime])

	sql = """
		SELECT item_code, warehouse, posting_datetime, creation, is_cancelled, company, stock_value
		FROM `tabStock Ledger Entry`
		WHERE (item_code, warehouse, posting_datetime) IN ({clauses})
	""".format(clauses=", ".join(clauses))
	return sql, params


def build_sle_boundary_fallback_query(item_code, warehouse, posting_datetime, company):
	"""Fetch the newest live same-company boundary row and one extra row for tie detection."""
	sql = """
		SELECT item_code, warehouse, posting_datetime, creation, is_cancelled, company, stock_value
		FROM `tabStock Ledger Entry`
		WHERE company = %s
			AND is_cancelled = 0
			AND item_code = %s
			AND warehouse = %s
			AND posting_datetime < %s
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 2
	"""
	return sql, [company, item_code, warehouse, posting_datetime]


def build_voucher_detail_exists_query(voucher_detail_nos, company):
	placeholders = ", ".join(["%s"] * len(voucher_detail_nos))
	sql = """
		SELECT DISTINCT voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE company = %s AND is_cancelled = 0 AND voucher_detail_no IN ({placeholders})
	""".format(placeholders=placeholders)
	return sql, [company, *voucher_detail_nos]


def sle_ordering_tuple(row):
	return (row.get("posting_datetime"), row.get("creation"))


def same_sle_ordering(left, right):
	return bool(left is not None and right is not None and sle_ordering_tuple(left) == sle_ordering_tuple(right))


def is_live_company_sle(row, company):
	return cint(row.get("is_cancelled")) == 0 and row.get("company") == company


def choose_live_boundary_row(rows_at_max, company):
	"""Choose the newest live same-company row at a max timestamp and expose exact ties."""
	live_rows = [row for row in rows_at_max if is_live_company_sle(row, company)]
	if not live_rows:
		return None, False

	max_ordering = max(sle_ordering_tuple(row) for row in live_rows)
	newest_rows = [row for row in live_rows if sle_ordering_tuple(row) == max_ordering]
	return newest_rows[0], len(newest_rows) > 1


def choose_fallback_boundary_row(fallback_rows):
	"""Choose the first descending fallback row; a second identical ordering tuple means a tie."""
	if not fallback_rows:
		return None, False
	return fallback_rows[0], len(fallback_rows) > 1 and same_sle_ordering(
		fallback_rows[0], fallback_rows[1]
	)


def choose_live_boundary_tuple(rows_at_max, company):
	live_rows = [row for row in rows_at_max if cint(row[4]) == 0 and row[5] == company]
	if not live_rows:
		return None, False

	max_ordering = max(boundary_tuple_ordering(row) for row in live_rows)
	newest_rows = [row for row in live_rows if boundary_tuple_ordering(row) == max_ordering]
	return newest_rows[0], len(newest_rows) > 1


def choose_fallback_boundary_tuple(fallback_rows):
	if not fallback_rows:
		return None, False
	return fallback_rows[0], len(fallback_rows) > 1 and boundary_tuple_ordering(
		fallback_rows[0]
	) == boundary_tuple_ordering(fallback_rows[1])


def assemble_sle_window(keys, window_rows, boundary_row):
	"""Assemble exact upstream first-descending matches from an ascending pair window."""
	keys = list(dict.fromkeys(keys))
	requested_keys = set(keys)
	window_rows = list(window_rows or [])
	tie = False

	for previous_row, current_row in zip(window_rows, window_rows[1:]):
		if same_sle_ordering(previous_row, current_row):
			tie = True
			break

	if window_rows and boundary_row is not None and same_sle_ordering(window_rows[0], boundary_row):
		tie = True

	if tie:
		return {
			"matched": {},
			"predecessors": {},
			"tie": True,
			"pair_exists": bool(window_rows) or boundary_row is not None,
		}

	last_index_by_key = {}
	for index, row in enumerate(window_rows):
		key = normalize_sle_key(
			row.get("item_code"),
			row.get("warehouse"),
			row.get("voucher_type"),
			row.get("voucher_no"),
			row.get("voucher_detail_no"),
		)
		if key in requested_keys:
			last_index_by_key[key] = index

	matched = {}
	predecessors = {}
	for key, index in last_index_by_key.items():
		matched[key] = window_rows[index]
		predecessors[key] = window_rows[index - 1] if index > 0 else boundary_row

	return {
		"matched": matched,
		"predecessors": predecessors,
		"tie": False,
		"pair_exists": bool(window_rows) or boundary_row is not None,
	}


def buying_amount_from_sle_pair(qty, matched_sle, predecessor, average_rate):
	"""ERPNext Gross Profit's exact matched-SLE buying amount decision tree."""
	if matched_sle is None:
		return 0.0

	previous_stock_value = flt(predecessor.get("stock_value")) if predecessor else 0.0
	if previous_stock_value:
		return (
			abs(previous_stock_value - flt(matched_sle.get("stock_value")))
			* flt(qty)
			/ abs(flt(matched_sle.get("qty")))
		)

	rate = average_rate() if callable(average_rate) else average_rate
	return flt(qty) * flt(rate)


def effective_dn_detail(row):
	"""dn_detail only applies when the invoice itself does not update stock."""
	if row.get("update_stock"):
		return None
	return row.get("dn_detail") or None


def extract_stock_ledger_key(row, item_code=None, warehouse=None):
	"""Derive the SLE identity exactly as Gross Profit does after its dn_detail rewrite."""
	dn_detail = effective_dn_detail(row)
	if not (row.get("update_stock") or dn_detail):
		return None

	resolved_warehouse = warehouse or row.get("delivered_warehouse") or row.get("warehouse")
	resolved_item = item_code or row.get("item_code")
	if not resolved_item or not resolved_warehouse:
		return None

	parenttype = row.get("parenttype")
	parent = row.get("invoice") or row.get("parent")
	if dn_detail:
		parenttype = "Delivery Note"
		parent = row.get("delivery_note")

	voucher_detail_no = dn_detail if dn_detail else row.get("item_row")
	if not parenttype or not parent or not voucher_detail_no:
		return None

	return normalize_sle_key(resolved_item, resolved_warehouse, parenttype, parent, voucher_detail_no)


def _group_wise_columns():
	return frappe._dict(
		{
			"invoice": [
				"invoice_or_item",
				"customer",
				"customer_group",
				"customer_name",
				"posting_date",
				"item_code",
				"item_name",
				"item_group",
				"brand",
				"description",
				"warehouse",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
				"project",
			],
			"item_code": [
				"item_code",
				"item_name",
				"brand",
				"description",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"warehouse": [
				"warehouse",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"brand": [
				"brand",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"item_group": [
				"item_group",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"customer": [
				"customer",
				"customer_group",
				"customer_name",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"customer_group": [
				"customer_group",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"sales_person": [
				"sales_person",
				"allocated_amount",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"project": ["project", "base_amount", "buying_amount", "gross_profit", "gross_profit_percent"],
			"cost_center": [
				"cost_center",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"territory": [
				"territory",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"monthly": [
				"monthly",
				"qty",
				"base_rate",
				"buying_rate",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
			"payment_term": [
				"payment_term",
				"base_amount",
				"buying_amount",
				"gross_profit",
				"gross_profit_percent",
			],
		}
	)


def _cached_single_value(doctype, fieldname):
	return frappe.get_cached_value(doctype, doctype, fieldname)


def format_invoice_rows_inplace(columns, gross_profit_data, filters, group_wise_columns, data):
	"""Format Invoice-mode rows in place with upstream Gross Profit's exact output shape."""
	column_names = standard_gross_profit.get_column_names()

	columns[0]["fieldname"] = "sales_invoice"
	columns[0]["options"] = "Item"
	columns[0]["width"] = 300
	supplier_master_name = _cached_single_value("Buying Settings", "supp_master_name")
	customer_master_name = _cached_single_value("Selling Settings", "cust_master_name")
	if supplier_master_name == "Supplier Name" and customer_master_name == "Customer Name":
		del columns[4]
		columns.insert(1, columns.pop(4))
	else:
		del columns[5]
		columns.insert(1, columns.pop(5))

	total_base_amount = 0
	total_buying_amount = 0
	invoice_columns = group_wise_columns.get("invoice")
	for src in gross_profit_data.si_list:
		if src.indent == 1:
			total_base_amount += src.base_amount or 0.0
			total_buying_amount += src.buying_amount or 0.0

		row_values = {
			"indent": src.indent,
			"parent_invoice": src.parent_invoice,
			"currency": filters.currency,
		}
		for col in invoice_columns:
			row_values[column_names[col]] = src.get(col)
		src.clear()
		src.update(row_values)
		data.append(src)

	gross_profit_data.si_list.clear()
	total_gross_profit = flt(
		total_base_amount + abs(total_buying_amount)
		if total_buying_amount < 0
		else total_base_amount - total_buying_amount,
	)
	currency_precision = cint(frappe.db.get_default("currency_precision")) or 3
	data.append(
		frappe._dict(
			{
				"sales_invoice": "Total",
				"qty": None,
				"avg._selling_rate": None,
				"valuation_rate": None,
				"selling_amount": total_base_amount,
				"buying_amount": total_buying_amount,
				"gross_profit": total_gross_profit,
				"gross_profit_%": flt(
					(total_gross_profit / abs(total_base_amount)) * 100.0,
					currency_precision,
				)
				if total_base_amount
				else 0,
				"currency": filters.currency,
			}
		)
	)


class _ReportDict(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


def _copy_report_filters(filters):
	copied = dict(filters or {})
	if frappe is not None and hasattr(frappe, "_dict"):
		return frappe._dict(copied)
	return _ReportDict(copied)


def _normalise_invoice_ids(invoice_ids):
	if invoice_ids is None:
		return None
	return tuple(dict.fromkeys(str(invoice) for invoice in invoice_ids if invoice))


def _render_detail_result(gross_profit_data, filters):
	data = []
	group_wise_columns = _group_wise_columns()
	columns = standard_gross_profit.get_columns(group_wise_columns, filters)

	if filters.group_by == "Invoice":
		format_invoice_rows_inplace(columns, gross_profit_data, filters, group_wise_columns, data)
	else:
		standard_gross_profit.get_data_when_not_grouped_by_invoice(
			gross_profit_data, filters, group_wise_columns, data
		)

	return columns, data


def _enforce_detail_budget_if_requested(filters, invoice_ids, enforce_budget):
	"""Invoke Worker 2's optional detail preflight without ever routing through query_profit."""
	if not enforce_budget:
		return
	try:
		budget = sys.modules.get("khitmee_ops.profit_summary.budget") or importlib.import_module(
			"khitmee_ops.profit_summary.budget"
		)
	except (ImportError, ModuleNotFoundError):
		return

	enforce = getattr(budget, "enforce_detail_budget", None)
	if enforce:
		enforce(filters, invoice_ids=invoice_ids)
		return

	preflight_rows = getattr(budget, "preflight_detail_input_rows", None)
	evaluate = getattr(budget, "evaluate_work_budget", None)
	if preflight_rows and evaluate:
		detail_input_rows = preflight_rows(filters)
		counts = {
			"mode": "detail",
			"detail_input_rows": detail_input_rows,
			"clean_work_rows": 0,
			"dirty_work_rows": 0,
			"combined_live_rows": 0,
			"clean_replay_invoices": 0,
			"largest_replay_family_rows": 0,
			"estimated_output_rows": detail_input_rows,
			"dirty_days_count": 0,
			"missing_days_count": 0,
			"has_blocking_repost": False,
		}
		decision = evaluate(filters, counts)
		if decision.get("outcome") != "ok":
			validation_error = getattr(frappe, "ValidationError", Exception)
			raise validation_error(f"Detail budget exceeded: {decision.get('reason') or decision.get('outcome')}")
		return

	preflight = getattr(budget, "preflight_detail_budget", None)
	if preflight:
		preflight(filters, invoice_ids=invoice_ids)


def run_detail_gross_profit(
	filters: dict, *, invoice_ids: list[str] | tuple[str, ...] | None = None, enforce_budget: bool = False
) -> tuple[list[dict], list]:
	"""Run the low-level detail report directly; this never delegates to ``query_profit``."""
	if standard_gross_profit is None:
		raise ImportError("Fast Gross Profit must run inside a bench with ERPNext installed")

	report_filters = _copy_report_filters(filters)
	validate_date_range(report_filters)
	scoped_invoice_ids = _normalise_invoice_ids(invoice_ids)
	_enforce_detail_budget_if_requested(report_filters, scoped_invoice_ids, enforce_budget)
	if not report_filters.get("currency"):
		report_filters.currency = frappe.get_cached_value(
			"Company", report_filters.company, "default_currency"
		)

	gross_profit_data = FastGrossProfitGenerator(report_filters, invoice_ids=scoped_invoice_ids)
	return _render_detail_result(gross_profit_data, report_filters)


def _default_rounding_method():
	return "Banker's Rounding (legacy)"


def _empty_contribution_batch(filters):
	filters = filters or {}
	return {
		"contributions": [],
		"replay_reasons_by_invoice": {},
		"currency_precision": cint(filters.get("currency_precision") or 3),
		"float_precision": cint(filters.get("float_precision") or 2),
		"rounding_method": filters.get("rounding_method") or _default_rounding_method(),
	}


def _precision_matches(value, precision):
	numeric = float(value or 0)
	return flt(numeric, precision) == numeric


def _get_rounding_method():
	try:
		return frappe.db.get_default("rounding_method") or _default_rounding_method()
	except Exception:
		return _default_rounding_method()


def _fetch_linked_return_originals(invoices, company):
	"""Find visible invoices with any submitted linked return, globally and in bounded chunks."""
	invoices = sorted({str(invoice) for invoice in invoices if invoice})
	if not invoices:
		return set()

	linked_originals = set()
	for invoice_chunk in batched(invoices, 1000):
		placeholders = ", ".join(["%s"] * len(invoice_chunk))
		rows = frappe.db.sql(
			f"""
			SELECT DISTINCT return_against
			FROM `tabSales Invoice`
			WHERE company = %s
				AND docstatus = 1
				AND is_return = 1
				AND return_against IN ({placeholders})
			""",
			[company, *invoice_chunk],
			as_dict=True,
		)
		for row in rows:
			linked_originals.add(str(row.get("return_against") if hasattr(row, "get") else row[0]))
	return linked_originals


def _valuation_source_for_row(generator, row):
	source = row.get("_khitmee_valuation_source")
	if not source:
		source = getattr(generator, "_valuation_source_by_row_id", {}).get(id(row))
	return source or "missing_detail_zero"


def _replay_reason_for_invoice(invoice, rows, linked_return_originals):
	if invoice in linked_return_originals:
		return "linked_return_original"
	if any(cint(row.get("is_return")) and (row.get("return_against") or row.get("_khitmee_query_phase") == 0) for row in rows):
		return "linked_return"
	if any(not cint(row.get("update_stock")) for row in rows):
		return "non_update_stock"
	if any(cint(row.get("delivered_by_supplier")) for row in rows):
		return "dropship"

	sources = [row.get("_contribution_valuation_source") for row in rows]
	if "missing_detail_zero" in sources:
		return "missing_sle_zero"
	if any(source in {"average_rate", "last_purchase_rate", "dn_valuation"} for source in sources):
		return "request_dependent_valuation"
	return None


LINE_SNAPSHOT_FIELD = "_khitmee_line_values"


def row_line_values(row):
	"""Return (qty, base_amount, buying_amount) for the line itself.

	Prefers the snapshot taken before stock group aggregation mutated the
	group's first row into the group total.
	"""
	snapshot = row.get(LINE_SNAPSHOT_FIELD)
	if snapshot is not None:
		return snapshot
	return row.get("qty"), row.get("base_amount"), row.get("buying_amount")


def _build_contribution_batch(generator, linked_return_originals):
	currency_precision = cint(getattr(generator, "currency_precision", 0)) or 3
	float_precision = cint(getattr(generator, "float_precision", 0)) or 2
	contributions = []
	rows_by_invoice = {}

	for row in generator.si_list:
		if not row.get("item_row") or not row.get("parent"):
			continue

		line_qty, line_base_amount, line_buying_amount = row_line_values(row)
		qty = float(line_qty or 0)
		base_amount = float(line_base_amount or 0)
		buying_amount = float(line_buying_amount or 0)
		query_phase =cint(row.get("_khitmee_query_phase")) if row.get("_khitmee_query_phase") is not None else 1
		valuation_source = _valuation_source_for_row(generator, row)
		row["_contribution_valuation_source"] = valuation_source
		posting_date = posting_date_sort_value(row.get("posting_date"))
		sales_invoice = str(row.get("parent"))
		contribution = {
			"sales_invoice": sales_invoice,
			"item_row": str(row.get("item_row")),
			"posting_date": posting_date,
			"warehouse": str(row.get("warehouse") or ""),
			"is_return": cint(row.get("is_return")),
			"return_against": row.get("return_against") or None,
			"update_stock": cint(row.get("update_stock")),
			"delivered_by_supplier": cint(row.get("delivered_by_supplier")),
			"valuation_source": valuation_source,
			"qty": qty,
			"base_amount": base_amount,
			"buying_amount": buying_amount,
			"query_phase": query_phase,
			"first_seen_key": row.get("_khitmee_first_seen_key")
			or first_seen_key_for_row(row, query_phase),
			"is_precision_safe": _precision_matches(qty, float_precision)
			and _precision_matches(base_amount, currency_precision)
			and _precision_matches(buying_amount, currency_precision),
		}
		contributions.append(contribution)
		rows_by_invoice.setdefault(sales_invoice, []).append(row)

	replay_reasons_by_invoice = {}
	for invoice, rows in rows_by_invoice.items():
		reason = _replay_reason_for_invoice(invoice, rows, linked_return_originals)
		if reason:
			replay_reasons_by_invoice[invoice] = reason

	return {
		"contributions": contributions,
		"replay_reasons_by_invoice": replay_reasons_by_invoice,
		"currency_precision": currency_precision,
		"float_precision": float_precision,
		"rounding_method": _get_rounding_method(),
	}


def collect_profit_contributions(
	filters: dict, *, invoice_ids: list[str] | tuple[str, ...] | None = None
) -> ContributionBatch:
	"""Extract line-level, post-return profit contributions with two-phase provenance."""
	scoped_invoice_ids = _normalise_invoice_ids(invoice_ids)
	if scoped_invoice_ids == ():
		return _empty_contribution_batch(filters)

	if standard_gross_profit is None:
		raise ImportError("Fast Gross Profit must run inside a bench with ERPNext installed")

	extraction_filters = _copy_report_filters(filters)
	extraction_filters.group_by = "Monthly"
	generator = FastGrossProfitGenerator(extraction_filters, invoice_ids=scoped_invoice_ids)
	invoices = {row.get("parent") for row in generator.si_list if row.get("parent")}
	linked_return_originals = _fetch_linked_return_originals(invoices, extraction_filters.get("company"))
	return _build_contribution_batch(generator, linked_return_originals)


def _truthy_export_flag(value):
	if isinstance(value, str):
		return value.strip().lower() not in {"", "0", "false", "no", "off"}
	return bool(value)


def _is_native_background_export_invocation():
	frame = inspect.currentframe()
	try:
		frame = frame.f_back if frame else None
		while frame:
			if frame.f_code.co_name == "run_export_query_job":
				return True
			frame = frame.f_back
	finally:
		del frame

	form_dict = getattr(frappe, "form_dict", None) or {}
	return (
		form_dict.get("cmd") == "frappe.desk.query_report.export_query"
		and _truthy_export_flag(form_dict.get("export_in_background"))
	)


def execute(filters=None):
	if standard_gross_profit is None:
		raise ImportError("Fast Gross Profit must run inside a bench with ERPNext installed")

	if _is_native_background_export_invocation():
		raise frappe.PermissionError(
			"Background email export is disabled for Fast Gross Profit; please use Export Profit CSV."
		)

	if not filters:
		filters = frappe._dict()
	elif isinstance(filters, dict) and not hasattr(filters, "__getattr__"):
		filters = frappe._dict(filters)

	validate_date_range(filters)
	filters.currency = frappe.get_cached_value("Company", filters.company, "default_currency")

	from khitmee_ops.profit_summary import query_profit

	result = query_profit(filters)
	mode = result.get("mode")
	if mode in {"summary", "detail"}:
		return result["columns"], result["data"]
	if mode in {"pending", "budget_exceeded"}:
		return result["columns"], [], result.get("message") or result.get("message_html") or ""
	raise RuntimeError(f"Fast Gross Profit query returned unknown mode: {mode!r}")


_BaseGrossProfitGenerator = (
	standard_gross_profit.GrossProfitGenerator if standard_gross_profit is not None else object
)


class FastGrossProfitGenerator(_BaseGrossProfitGenerator):
	"""Gross Profit generator with bounded SLE and Packed Item reads.

	The class deliberately inherits ERPNext's invoice query, permissions, return folding,
	bundle proration, currency precision and grouping logic. Only the two unbounded data
	accesses are replaced.
	"""

	def __init__(self, filters=None, *, invoice_ids=None):
		self._matched_sles = {}
		self._sle_predecessors = {}
		self._sle_pair_exists = {}
		self._tie_pairs = set()
		self._tie_pair_history = {}
		self._dn_item_stock_qty = {}
		self._submitted_delivery_note_items = None
		self._linked_dn_parents = set()
		self._linked_dn_parent_invoices = set()
		self._last_purchase_rate_cache = {}
		self._warehouse_scope = None
		self._active_repost_item_valuations = None
		self._scoped_invoice_ids = _normalise_invoice_ids(invoice_ids)
		self._valuation_source_by_row_id = {}
		super().__init__(filters)

	def process(self):
		self._preload_delivery_note_item_quantities()
		self._preload_stock_ledger_data()

		self.grouped = {}
		self.grouped_data = []
		self.invoice_item_amounts = {}

		self.currency_precision = cint(frappe.db.get_default("currency_precision")) or 3
		self.float_precision = cint(frappe.db.get_default("float_precision")) or 2

		grouped_by_invoice = self.filters.get("group_by") == "Invoice"
		track_grouped_amounts = not grouped_by_invoice
		if grouped_by_invoice:
			buying_amount = 0
			base_amount = 0

		for row in reversed(self.si_list):
			sales_invoice_item = row.item_row

			if self.filters.get("group_by") == "Monthly":
				row.monthly = formatdate(row.posting_date, "MMM YYYY")

			if self.skip_row(row):
				continue

			row.base_amount = flt(row.base_net_amount, self.currency_precision)

			product_bundles = []
			if row.update_stock:
				product_bundles = self.product_bundles.get(row.parenttype, {}).get(row.parent, frappe._dict())
			elif row.dn_detail:
				product_bundles = self.product_bundles.get("Delivery Note", {}).get(
					row.delivery_note, frappe._dict()
				)
				row.item_row = row.dn_detail
				if product_bundles and not row.parent:
					product_bundle = product_bundles.get(row.parent_invoice)
					if product_bundle:
						for packed_item in product_bundle:
							if (
								packed_item.get("item_code") == row.item_code
								and packed_item.get("parent_detail_docname") == row.item_row
							):
								row.warehouse = packed_item.warehouse
								row.base_amount = packed_item.base_amount

			if grouped_by_invoice and row.indent == 0.0:
				row.buying_amount = buying_amount
				row.base_amount = base_amount
				buying_amount = 0
				base_amount = 0
			elif row.is_debit_note:
				if not grouped_by_invoice:
					row.qty = 0
				row.buying_amount = 0
				self._remember_valuation_source(row, "missing_detail_zero")
			elif row.item_code in product_bundles:
				row.buying_amount = flt(
					self.get_buying_amount_from_product_bundle(row, product_bundles[row.item_code]),
					self.currency_precision,
				)
			elif row.item_row in self.bundle_deliveries:
				row.buying_amount = flt(
					self.get_buying_amount_from_delivered_bundles(row), self.currency_precision
				)
			else:
				row.buying_amount = flt(self.get_buying_amount(row, row.item_code), self.currency_precision)

			if flt(row.qty):
				row.buying_rate = (
					flt(row.buying_amount / flt(row.qty), self.float_precision)
					if not row.delivered_by_supplier
					else None
				)
				row.base_rate = flt(row.base_amount / flt(row.qty), self.float_precision)
			else:
				if self.is_not_invoice_row(row):
					row.buying_rate, row.base_rate = 0.0, 0.0

			if self.is_not_invoice_row(row):
				self.update_return_invoices(row, sales_invoice_item)

			if grouped_by_invoice and row.indent == 1.0:
				buying_amount += row.buying_amount
				base_amount += row.base_amount

			row.gross_profit = flt(
				row.base_amount + abs(row.buying_amount)
				if row.buying_amount < 0
				else row.base_amount - row.buying_amount,
				self.currency_precision,
			)
			if row.base_amount:
				row.gross_profit_percent = flt(
					(row.gross_profit / abs(row.base_amount)) * 100.0,
					self.currency_precision,
				)
			else:
				row.gross_profit_percent = 0.0

			if track_grouped_amounts:
				group_key = row.get(standard_gross_profit.scrub(self.filters.group_by))
				group_rows = self.grouped.get(group_key)
				if group_rows is None:
					# Stock get_average_rate_based_on_group_by() folds every row of a
					# group into the group's first row *in place*.  Snapshot that one
					# row's line values so line-level consumers (profit contributions)
					# never read a group total.  One tuple per group, not per row.
					row[LINE_SNAPSHOT_FIELD] = (row.qty, row.base_amount, row.buying_amount)
					group_rows = self.grouped[group_key] = []
				group_rows.append(row)
				self.invoice_item_amounts[sales_invoice_item] = frappe._dict(
					base_amount=row.base_amount, buying_amount=row.buying_amount
				)

		if self.grouped:
			self.get_average_rate_based_on_group_by()

		for attr in ("_matched_sles", "_sle_predecessors", "_tie_pair_history"):
			cache = getattr(self, attr, None)
			if hasattr(cache, "clear"):
				cache.clear()

	def _hints_enabled(self):
		return not (
			self.filters.get("sales_invoice")
			or self.filters.get("item_code")
			or getattr(self, "_scoped_invoice_ids", None) is not None
		)

	def _invoice_scope_criterion(self, SI):
		scoped_invoice_ids = getattr(self, "_scoped_invoice_ids", None)
		if scoped_invoice_ids is None:
			return None

		if scoped_invoice_ids:
			chunks = list(batched(scoped_invoice_ids, 1000))
		else:
			chunks = [(None,)]

		criterion = None
		for invoice_chunk in chunks:
			chunk_criterion = SI.name.isin(invoice_chunk)
			criterion = chunk_criterion if criterion is None else criterion | chunk_criterion
		return criterion

	def apply_common_filters(self, query, SI, SII, ST, Item):
		warehouse_filter = self.filters.get("warehouse")
		if warehouse_filter:
			self.filters.warehouse = None
			try:
				query = super().apply_common_filters(query, SI, SII, ST, Item)
			finally:
				self.filters.warehouse = warehouse_filter

			if self._warehouse_scope and self._warehouse_scope[0] == warehouse_filter:
				scope = self._warehouse_scope[1]
			else:
				lft, rgt = frappe.get_cached_value("Warehouse", warehouse_filter, ["lft", "rgt"])
				scope = frappe.db.sql_list(
					"SELECT name FROM `tabWarehouse` WHERE lft >= %s AND rgt <= %s",
					(lft, rgt),
				)
				self._warehouse_scope = (warehouse_filter, scope)
			query = query.where(SII.warehouse.isin(scope or [None]))
		else:
			query = super().apply_common_filters(query, SI, SII, ST, Item)

		invoice_scope_criterion = self._invoice_scope_criterion(SI)
		if invoice_scope_criterion is not None:
			query = query.where(invoice_scope_criterion)
		return query

	def _warehouse_semijoin_enabled(self):
		return (
			bool(self.filters.get("warehouse"))
			and self._hints_enabled()
			and getattr(self, "_scoped_invoice_ids", None) is None
		)

	def _upstream_prepare_invoice_query(self):
		return super().prepare_invoice_query()

	def prepare_invoice_query(self):
		query = self._upstream_prepare_invoice_query()
		if frappe is not None and hasattr(frappe, "qb") and hasattr(query, "select"):
			query = query.select(frappe.qb.DocType("Sales Invoice").return_against)
		use_si_hint = (
			self._hints_enabled()
			and not self._warehouse_semijoin_enabled()
			and getattr(self, "_scoped_invoice_ids", None) is None
		)
		return query.force_index(SI_IDX) if use_si_hint else query

	def _run_invoice_query(self, query, semijoin_kind=None):
		from frappe.query_builder.utils import prepare_query

		sql, params = prepare_query(query)
		if semijoin_kind and self._warehouse_semijoin_enabled():
			scope = self._warehouse_scope[1] if self._warehouse_scope else [None]
			clause, clause_params = build_warehouse_covering_drive(
				scope or [None],
				self.filters.company,
				self.filters.get("from_date"),
				self.filters.get("to_date"),
				return_kind=semijoin_kind,
				include_returned_invoices=False
				if semijoin_kind in {"normal", "unlinked_return"}
				else bool(self.filters.get("include_returned_invoices")),
			)
			sql = inject_warehouse_covering_drive(sql, clause)
			sql, ordered_params = positionalize_named_params(sql, params)
			ordered_params.extend(clause_params)
			return frappe.db.sql(sql, ordered_params, as_dict=True)

		if self._hints_enabled():
			sql = inject_sii_force_index(sql)
		return frappe.db.sql(sql, params, as_dict=True)

	def load_invoice_items(self):
		SalesInvoice = frappe.qb.DocType("Sales Invoice")
		invoice_query = self.prepare_invoice_query().where(SalesInvoice.is_return == 0)
		self.si_list = sort_invoice_rows_descending(
			self._run_invoice_query(invoice_query, semijoin_kind="normal")
		)

		if self.filters.include_returned_invoices:
			unlinked_return_query = self.prepare_invoice_query().where(
				(SalesInvoice.is_return == 1) & SalesInvoice.return_against.isnull()
			)
			unlinked_return_rows = self._run_invoice_query(
				unlinked_return_query, semijoin_kind="unlinked_return"
			)
			if unlinked_return_rows:
				self.si_list = sort_invoice_rows_descending(
					merge_posting_rows_descending(self.si_list, unlinked_return_rows)
				)

		mark_query_phase(self.si_list, 1)
		self.prepare_vouchers_to_ignore()

		ret_invoice_query = self.prepare_invoice_query().where(
			(SalesInvoice.is_return == 1) & SalesInvoice.return_against.isnotnull()
		)
		if self.vouchers_to_ignore:
			ret_invoice_query = ret_invoice_query.where(
				SalesInvoice.return_against.notin(self.vouchers_to_ignore)
			)

		ret_rows = mark_query_phase(
			sort_invoice_rows_descending(self._run_invoice_query(ret_invoice_query, semijoin_kind="return")),
			0,
		)
		if ret_rows:
			self.si_list.extend(ret_rows)

	def prepare_vouchers_to_ignore(self):
		self.vouchers_to_ignore = tuple(dict.fromkeys(row["parent"] for row in self.si_list))

	def _has_submitted_delivery_note_items(self):
		if getattr(self, "_submitted_delivery_note_items", None) is None:
			self._submitted_delivery_note_items = bool(
				frappe.db.sql("SELECT 1 FROM `tabDelivery Note Item` WHERE docstatus = 1 LIMIT 1")
			)
		return self._submitted_delivery_note_items

	def _get_linked_delivery_note_parents(self, invoices):
		invoices = sorted(set(invoices or []))
		if not invoices or not self._has_submitted_delivery_note_items():
			return []
		if not hasattr(self, "_linked_dn_parents"):
			self._linked_dn_parents = set()
		if not hasattr(self, "_linked_dn_parent_invoices"):
			self._linked_dn_parent_invoices = set()

		pending = [invoice for invoice in invoices if invoice not in self._linked_dn_parent_invoices]
		for invoice_chunk in batched(pending, 1000):
			placeholders = ", ".join(["%s"] * len(invoice_chunk))
			rows = frappe.db.sql(
				f"""
				SELECT DISTINCT parent
				FROM `tabDelivery Note Item`
				WHERE docstatus = 1 AND against_sales_invoice IN ({placeholders})
				""",
				invoice_chunk,
				as_dict=True,
			)
			self._linked_dn_parents.update(
				row.get("parent") if hasattr(row, "get") else row[0] for row in rows
			)
			self._linked_dn_parent_invoices.update(invoice_chunk)
		return sorted(parent for parent in self._linked_dn_parents if parent)

	def _build_returned_invoice_query(self, parent_chunk):
		si = frappe.qb.DocType("Sales Invoice")
		si_item = frappe.qb.DocType("Sales Invoice Item")
		invoice_query = frappe.qb.from_(si)
		if self._hints_enabled():
			invoice_query = invoice_query.force_index(SI_IDX)

		query = (
			invoice_query.inner_join(si_item)
			.on(si.name == si_item.parent)
			.select(
				si.name,
				si_item.item_code,
				si_item.sales_invoice_item,
				si_item.stock_qty.as_("qty"),
				si_item.base_net_amount.as_("base_amount"),
				si.return_against,
			)
			.where(
				(si.docstatus == 1)
				& (si.is_return == 1)
				& si.posting_date.between(self.filters.from_date, self.filters.to_date)
			)
			.where(si.return_against.isin(parent_chunk))
		)

		if self.filters.company:
			query = query.where(si.company == self.filters.company)
		return query

	def get_returned_invoice_items(self):
		parents = sorted({row.parent for row in self.si_list if row.get("parent")})
		if not parents:
			self.returned_invoices = frappe._dict()
			self.legacy_returned_invoices = frappe._dict()
			return

		returned_invoices = []
		for parent_chunk in batched(parents, 1000):
			returned_invoices.extend(
				self._run_invoice_query(self._build_returned_invoice_query(parent_chunk)) or []
			)

		self.returned_invoices = frappe._dict()
		self.legacy_returned_invoices = frappe._dict()
		for inv in returned_invoices:
			invoice_returns = (
				self.returned_invoices if inv.sales_invoice_item else self.legacy_returned_invoices
			)
			invoice_returns.setdefault(inv.return_against, frappe._dict()).setdefault(
				inv.sales_invoice_item or inv.item_code, []
			).append(inv)

	def load_product_bundle(self):
		"""Load Packed Item only for bundle-selling invoices and their linked Delivery Notes."""
		self.product_bundles = {}
		invoices = sorted({row.parent for row in self.si_list if row.get("parent")})
		if not invoices:
			return {}

		packed_parent_items = set(
			frappe.db.sql_list(
				"SELECT DISTINCT parent_item FROM `tabPacked Item` "
				"WHERE parenttype = 'Sales Invoice' AND docstatus = 1"
			)
		)
		bundle_invoices = sorted(
			{
				row.parent
				for row in self.si_list
				if row.get("parent") and row.get("item_code") in packed_parent_items
			}
		)

		delivery_notes = {row.delivery_note for row in self.si_list if row.get("delivery_note")}
		if self._has_submitted_delivery_note_items():
			delivery_notes.update(self._get_linked_delivery_note_parents(invoices))
		delivery_notes = sorted(delivery_notes)

		if not bundle_invoices and not delivery_notes:
			return {}

		clauses = []
		params = []
		if bundle_invoices:
			placeholders = ", ".join(["%s"] * len(bundle_invoices))
			clauses.append(f"(parenttype = 'Sales Invoice' AND parent IN ({placeholders}))")
			params.extend(bundle_invoices)
		if delivery_notes:
			placeholders = ", ".join(["%s"] * len(delivery_notes))
			clauses.append(f"(parenttype = 'Delivery Note' AND parent IN ({placeholders}))")
			params.extend(delivery_notes)

		rows = frappe.db.sql(
			"""
			SELECT
				parenttype,
				parent,
				parent_item,
				item_code,
				warehouse,
				(-1 * qty) AS total_qty,
				rate,
				(rate * qty) AS base_amount,
				parent_detail_docname,
				serial_and_batch_bundle
			FROM `tabPacked Item`
			WHERE docstatus = 1 AND ({clauses})
			""".format(clauses=" OR ".join(clauses)),
			params,
			as_dict=True,
		)

		for row in rows:
			self.product_bundles.setdefault(row.parenttype, frappe._dict()).setdefault(
				row.parent, frappe._dict()
			).setdefault(row.parent_item, []).append(row)
		return self.product_bundles

	def _product_bundles_for_row(self, row, dn_detail=None):
		if row.get("update_stock"):
			return self.product_bundles.get(row.get("parenttype"), {}).get(row.get("parent"), frappe._dict())
		dn_detail = dn_detail if dn_detail is not None else effective_dn_detail(row)
		if dn_detail:
			return self.product_bundles.get("Delivery Note", {}).get(row.get("delivery_note"), frappe._dict())
		return frappe._dict()

	def _preload_delivery_note_item_quantities(self):
		self._dn_item_stock_qty = {}
		dn_details = sorted(
			{
				row.get("dn_detail")
				for row in self.si_list
				if row.get("dn_detail")
			}
		)
		if not dn_details:
			return

		placeholders = ", ".join(["%s"] * len(dn_details))
		rows = frappe.db.sql(
			f"""
			SELECT name, stock_qty
			FROM `tabDelivery Note Item`
			WHERE name IN ({placeholders})
			""",
			dn_details,
			as_dict=True,
		)
		self._dn_item_stock_qty = {row.name: row.stock_qty for row in rows}

	def get_delivery_notes(self):
		self.delivery_notes = frappe._dict()
		all_invoices = sorted({row.parent for row in self.si_list if row.get("parent")})
		if not self._get_linked_delivery_note_parents(all_invoices):
			return

		placeholders = ", ".join(["%s"] * len(all_invoices))
		rows = frappe.db.sql(
			f"""
			SELECT
				si_detail,
				SUM(stock_qty * incoming_rate) AS total_incoming_value,
				SUM(stock_qty) AS total_qty
			FROM `tabDelivery Note Item`
			WHERE docstatus = 1
				AND against_sales_invoice IN ({placeholders})
				AND si_detail IS NOT NULL
				AND si_detail != ''
			GROUP BY si_detail
			""",
			all_invoices,
			as_dict=True,
		)
		for entry in rows:
			self.delivery_notes[entry.si_detail] = entry

	def load_bundle_deliveries(self):
		if not self.product_bundles.get("Delivery Note"):
			self.bundle_deliveries = {}
			return
		return super().load_bundle_deliveries()

	def group_items_by_invoice(self):
		"""Turn the line list into invoice headers followed by their contiguous child rows."""
		grouped = OrderedDict()
		product_bundles = self.product_bundles.get("Sales Invoice", {})

		for row in self.si_list:
			bucket = grouped.get(row.parent)
			if bucket is None:
				bucket = [self.get_invoice_row(row)]
				grouped[row.parent] = bucket
			bucket.append(
				row.update(
					{"indent": 1.0, "parent_invoice": row.parent, "invoice_or_item": row.item_code}
				)
			)

			if bundled_items := product_bundles.get(row.parent, {}).get(row.item_code):
				for item in bundled_items:
					bucket.append(self.get_bundle_item_row(row, item))

		self.si_list.clear()
		for items in grouped.values():
			self.si_list.extend(items)

	def get_billed_share_of_bundle(self, row):
		"""Share of the delivered bundle qty billed by this invoice row; packed item qty is the delivered qty."""
		dn_detail = row.get("dn_detail")
		if not dn_detail:
			return 1

		delivered_qty = flt(self._dn_item_stock_qty.get(dn_detail))
		return flt(row.get("qty")) / delivered_qty if delivered_qty else 1

	def _fetch_last_purchase_rate(self, item_code, row):
		return super().get_last_purchase_rate(item_code, row)

	def get_last_purchase_rate(self, item_code, row):
		key = (item_code, row.get("project"), row.get("cost_center"))
		if key not in self._last_purchase_rate_cache:
			self._last_purchase_rate_cache[key] = self._fetch_last_purchase_rate(item_code, row)
		return self._last_purchase_rate_cache[key]

	def _remember_valuation_source(self, row, source):
		if not hasattr(self, "_valuation_source_by_row_id"):
			self._valuation_source_by_row_id = {}
		self._valuation_source_by_row_id[id(row)] = source
		row["_khitmee_valuation_source"] = source

	def _aggregate_bundle_valuation_sources(self, sources):
		sources = [source for source in sources if source]
		if sources and all(source == "sle" for source in sources):
			return "bundle_sle"
		if not sources:
			return "missing_detail_zero"
		for fallback in ("missing_detail_zero", "average_rate", "last_purchase_rate", "dn_valuation", "dropship"):
			if fallback in sources:
				return fallback
		return sources[0]

	def _row_value(self, row, fieldname, default=None):
		return row.get(fieldname, default) if hasattr(row, "get") else getattr(row, fieldname, default)

	def _classify_tie_history_sle_source(self, row, item_code, warehouse, parenttype, parent, item_row):
		history = self._pair_history(item_code, warehouse)
		for index, sle in enumerate(history):
			if not (
				self._row_value(sle, "voucher_type") == parenttype
				and self._row_value(sle, "voucher_no") == parent
				and self._row_value(sle, "voucher_detail_no") == item_row
			):
				continue
			predecessor = history[index + 1] if len(history) > index + 1 else None
			previous_stock_value = flt(self._row_value(predecessor, "stock_value")) if predecessor else 0.0
			return "sle" if previous_stock_value else "average_rate"
		return "missing_detail_zero"

	def _classify_sle_valuation_source(self, row, item_code):
		warehouse = row.get("delivered_warehouse") or row.get("warehouse")
		parenttype = row.get("parenttype")
		parent = row.get("invoice") or row.get("parent")
		item_row = row.get("item_row")
		if row.get("dn_detail"):
			parenttype, parent, item_row = "Delivery Note", row.get("delivery_note"), row.get("dn_detail")

		if (str(item_code), str(warehouse)) in self._tie_pairs:
			return self._classify_tie_history_sle_source(
				row, item_code, warehouse, parenttype, parent, item_row
			)

		key = normalize_sle_key(item_code, warehouse, parenttype, parent, item_row)
		matched = self._matched_sles.get(key)
		if matched is None:
			return "missing_detail_zero"
		predecessor = self._sle_predecessors.get(key)
		previous_stock_value = flt(predecessor.get("stock_value")) if predecessor else 0.0
		return "sle" if previous_stock_value else "average_rate"

	def _buying_amount_and_source(self, row, item_code):
		if row.get("delivered_by_supplier"):
			buying_amount = self.get_drop_ship_buying_amount(row)
			if buying_amount is not None:
				return buying_amount, "dropship"

		if item_code in self.non_stock_items and (row.get("project") or row.get("cost_center")):
			item_rate = self.get_last_purchase_rate(item_code, row)
			return flt(row.get("qty")) * item_rate, "last_purchase_rate"

		my_sle = self.get_stock_ledger_entries(item_code, row.get("delivered_warehouse") or row.get("warehouse"))
		if (row.get("update_stock") or row.get("dn_detail")) and my_sle:
			parenttype = row.get("parenttype")
			parent = row.get("invoice") or row.get("parent")
			if row.get("dn_detail"):
				parenttype, parent = "Delivery Note", row.get("delivery_note")
			buying_amount = self.calculate_buying_amount_from_sle(
				row, my_sle, parenttype, parent, row.get("item_row"), item_code
			)
			return buying_amount, self._classify_sle_valuation_source(row, item_code)

		if row.get("item_row") and self.delivery_notes.get(row.get("item_row")):
			dn = self.delivery_notes[row.get("item_row")]
			if flt(dn.get("total_qty")):
				return (
					flt(row.get("qty")) * flt(dn.get("total_incoming_value")) / flt(dn.get("total_qty")),
					"dn_valuation",
				)
			return flt(row.get("qty")) * self.get_average_buying_rate(row, item_code), "average_rate"

		if row.get("sales_order") and row.get("so_detail"):
			incoming_amount = self.get_buying_amount_from_so_dn(
				row.get("sales_order"), row.get("so_detail"), item_code
			)
			if incoming_amount:
				return flt(row.get("qty")) * incoming_amount, "dn_valuation"

		return flt(row.get("qty")) * self.get_average_buying_rate(row, item_code), "average_rate"

	def get_buying_amount(self, row, item_code):
		buying_amount, valuation_source = self._buying_amount_and_source(row, item_code)
		self._remember_valuation_source(row, valuation_source)
		return buying_amount

	def get_buying_amount_from_product_bundle(self, row, product_bundle):
		buying_amount = 0.0
		billed_share = self.get_billed_share_of_bundle(row)
		component_sources = []
		for packed_item in product_bundle:
			if packed_item.get("parent_detail_docname") != row.get("item_row"):
				continue
			packed_item_row = row.copy()
			packed_item_row["warehouse"] = packed_item.get("warehouse")
			packed_item_row["delivered_warehouse"] = None
			packed_item_row["qty"] = packed_item.get("total_qty") * -1 * billed_share
			packed_item_row["serial_and_batch_bundle"] = packed_item.get("serial_and_batch_bundle")
			buying_amount += self.get_buying_amount(packed_item_row, packed_item.get("item_code"))
			component_sources.append(self._valuation_source_by_row_id.get(id(packed_item_row)))

		self._remember_valuation_source(row, self._aggregate_bundle_valuation_sources(component_sources))
		return flt(buying_amount, self.currency_precision)

	def get_buying_amount_from_delivered_bundles(self, row):
		buying_amount = 0.0
		delivery_sources = []
		for delivered_row, packed_items in self.bundle_deliveries[row.get("item_row")]:
			delivery = row.copy()
			delivery.update(
				{
					"dn_detail": delivered_row.get("name"),
					"delivery_note": delivered_row.get("parent"),
					"item_row": delivered_row.get("name"),
					"qty": delivered_row.get("stock_qty"),
				}
			)
			buying_amount += self.get_buying_amount_from_product_bundle(delivery, packed_items)
			delivery_sources.append(self._valuation_source_by_row_id.get(id(delivery)))

		self._remember_valuation_source(row, self._aggregate_bundle_valuation_sources(delivery_sources))
		return buying_amount

	def load_non_stock_items(self):
		self.non_stock_items = set(frappe.get_all("Item", filters={"is_stock_item": 0}, pluck="name"))

	def _collect_stock_ledger_keys(self):
		keys = []
		seen = set()

		def add(row, item_code=None, warehouse=None):
			key = extract_stock_ledger_key(row, item_code=item_code, warehouse=warehouse)
			if key and key not in seen:
				seen.add(key)
				keys.append(key)

		for row in self.si_list:
			# Resolve the effective dn_detail exactly once; both the bundle preload
			# below and the later buying-amount calculation must use the same value.
			dn_detail = effective_dn_detail(row)
			add(row, row.get("item_code"))

			product_bundles = self._product_bundles_for_row(row, dn_detail)
			if bundle := product_bundles.get(row.get("item_code")):
				for packed_item in bundle:
					if packed_item.get("parent_detail_docname") == (dn_detail or row.get("item_row")):
						add(row, packed_item.item_code, packed_item.warehouse)

			if row.get("item_row") in self.bundle_deliveries:
				for delivered_row, packed_items in self.bundle_deliveries[row.get("item_row")]:
					delivery = dict(row)
					delivery.update(
						{
							"update_stock": 0,
							"dn_detail": delivered_row.name,
							"delivery_note": delivered_row.parent,
							"item_row": delivered_row.name,
						}
					)
					for packed_item in packed_items:
						add(delivery, packed_item.item_code, packed_item.warehouse)

		return keys

	def _preload_stock_ledger_data(self):
		keys = self._collect_stock_ledger_keys()
		if not (self.filters.get("from_date") and self.filters.get("to_date")):
			for chunk in batched(keys, 100):
				self._fetch_sle_chunk(chunk)
			self._preload_average_buying_rates()
			return

		si_keys = [key for key in keys if key[2] == "Sales Invoice"]
		legacy_keys = [key for key in keys if key[2] != "Sales Invoice"]
		for chunk in batched(legacy_keys, 100):
			self._fetch_sle_chunk(chunk)

		if si_keys:
			if self._has_active_repost_item_valuations():
				self._preload_sales_invoice_sle_windows(si_keys)
			else:
				self._preload_sales_invoice_sle_by_voucher(si_keys)

		self._preload_average_buying_rates()

	def _fetch_item_valuation_metadata(self, item_codes):
		meta_by_item = {}
		items = sorted({str(code) for code in item_codes if code})
		for chunk in batched(items, 1000):
			placeholders = ", ".join(["%s"] * len(chunk))
			rows = frappe.db.sql(
				f"""
				SELECT `name`, `has_serial_no`, `has_batch_no`, `valuation_method`, `valuation_rate`, `standard_rate`
				FROM `tabItem`
				WHERE `name` IN ({placeholders})
				""",
				list(chunk),
				as_dict=True,
			)
			for row in rows or []:
				meta_by_item[str(row.get("name"))] = row
		return meta_by_item

	def _row_needs_average_buying_rate(self, row, item_code):
		if not item_code or row.get("delivered_by_supplier") or row.get("is_debit_note"):
			return False
		non_stock_items = getattr(self, "non_stock_items", ()) or ()
		if item_code in non_stock_items and (row.get("project") or row.get("cost_center")):
			return False

		warehouse = row.get("delivered_warehouse") or row.get("warehouse")
		if not warehouse:
			return False
		pair = (str(item_code), str(warehouse))
		if pair in (getattr(self, "_tie_pairs", None) or ()):
			return False

		dn_detail = effective_dn_detail(row)
		if row.get("update_stock") or dn_detail:
			if (getattr(self, "_sle_pair_exists", None) or {}).get(pair):
				key = extract_stock_ledger_key(row, item_code=item_code, warehouse=warehouse)
				matched = (getattr(self, "_matched_sles", None) or {}).get(key)
				if matched is None:
					return False
				predecessor = (getattr(self, "_sle_predecessors", None) or {}).get(key)
				return not (predecessor and flt(predecessor.get("stock_value")))

		delivery_notes = getattr(self, "delivery_notes", None) or {}
		dn = delivery_notes.get(row.get("item_row")) if row.get("item_row") else None
		if dn:
			return not flt(dn.get("total_qty"))
		if row.get("sales_order") and row.get("so_detail"):
			return False
		return True

	def _collect_average_rate_pairs(self):
		pairs = []
		bundle_deliveries = getattr(self, "bundle_deliveries", None) or {}
		for row in reversed(getattr(self, "si_list", None) or ()):
			if row.get("is_debit_note"):
				continue
			dn_detail = effective_dn_detail(row)
			product_bundles = self._product_bundles_for_row(row, dn_detail) if getattr(self, "product_bundles", None) else {}
			if row.item_code in product_bundles:
				for packed_item in product_bundles[row.item_code]:
					if packed_item.get("parent_detail_docname") != (dn_detail or row.get("item_row")):
						continue
					packed_row = dict(row)
					packed_row["warehouse"] = packed_item.get("warehouse")
					packed_row["delivered_warehouse"] = None
					packed_row["serial_and_batch_bundle"] = packed_item.get("serial_and_batch_bundle")
					if self._row_needs_average_buying_rate(packed_row, packed_item.get("item_code")):
						pairs.append((packed_row, packed_item.get("item_code")))
			elif row.get("item_row") in bundle_deliveries:
				for delivered_row, packed_items in bundle_deliveries[row.get("item_row")]:
					for packed_item in packed_items:
						if packed_item.get("parent_detail_docname") != delivered_row.get("name"):
							continue
						packed_row = dict(row)
						packed_row["update_stock"] = 0
						packed_row["dn_detail"] = delivered_row.get("name")
						packed_row["delivery_note"] = delivered_row.get("parent")
						packed_row["item_row"] = delivered_row.get("name")
						packed_row["warehouse"] = packed_item.get("warehouse")
						packed_row["delivered_warehouse"] = None
						packed_row["serial_and_batch_bundle"] = packed_item.get("serial_and_batch_bundle")
						if self._row_needs_average_buying_rate(packed_row, packed_item.get("item_code")):
							pairs.append((packed_row, packed_item.get("item_code")))
			elif self._row_needs_average_buying_rate(row, row.get("item_code")):
				pairs.append((row, row.get("item_code")))
		return pairs

	def _preload_average_buying_rates(self):
		"""Batch plain-item get_average_buying_rate lookups in exact stock order."""
		if frappe is None or not getattr(frappe, "db", None) or standard_gross_profit is None:
			return
		pairs = self._collect_average_rate_pairs()
		if not pairs:
			return

		try:
			from erpnext.stock.utils import _get_fifo_lifo_rate, get_combine_datetime
		except ImportError:
			return

		if not hasattr(self, "average_buying_rate"):
			self.average_buying_rate = {}

		company = getattr(self.filters, "company", None)
		company_method = (
			frappe.get_cached_value("Company", company, "valuation_method") if company else None
		) or frappe.db.get_single_value("Stock Settings", "valuation_method") or "FIFO"
		item_meta = self._fetch_item_valuation_metadata(item_code for _row, item_code in pairs)

		candidates_by_key = {}
		for row, item_code in pairs:
			if (
				row.get("serial_and_batch_bundle")
				or row.get("serial_no")
				or row.get("batch_no")
				or row.get("name")
				or row.get("creation")
				or row.get("sle")
			):
				continue
			warehouse = row.get("warehouse")
			posting_date = row.get("posting_date")
			voucher_no = row.get("parent")
			voucher_type = row.get("parenttype") or "Sales Invoice"
			if not (warehouse and posting_date and voucher_no):
				continue
			meta = item_meta.get(str(item_code))
			if not meta or cint(meta.get("has_serial_no")) or cint(meta.get("has_batch_no")):
				continue
			valuation_method = meta.get("valuation_method") or company_method
			if valuation_method not in {"FIFO", "LIFO", "Moving Average"}:
				continue

			key = (
				item_code,
				warehouse,
				posting_date,
				row.get("posting_time"),
				row.get("serial_and_batch_bundle"),
			)
			if key in self.average_buying_rate or key in candidates_by_key:
				continue
			candidates_by_key[key] = {
				"key": key,
				"item_code": item_code,
				"warehouse": warehouse,
				"posting_datetime": get_combine_datetime(posting_date, row.get("posting_time")),
				"qty": row.get("qty") or 0,
				"voucher_no": voucher_no,
				"voucher_type": voucher_type,
				"valuation_method": valuation_method,
				"meta": meta,
			}

		candidates = list(candidates_by_key.values())
		if not candidates:
			return

		unresolved = []
		for chunk in batched(candidates, 100):
			chunk_list = list(chunk)
			sql, params = build_previous_sle_batch_query(chunk_list)
			prev_by_idx = {
				cint(row.get("khitmee_idx")): row
				for row in frappe.db.sql(sql, params, as_dict=True) or []
			}
			for idx, candidate in enumerate(chunk_list):
				prev_sle = prev_by_idx.get(idx)
				in_rate = None
				if candidate["valuation_method"] in {"FIFO", "LIFO"}:
					if prev_sle:
						stock_queue = json.loads(prev_sle.get("stock_queue", "[]") or "[]")
						if stock_queue:
							in_rate = _get_fifo_lifo_rate(
								stock_queue, candidate["qty"], candidate["valuation_method"]
							)
				elif prev_sle:
					in_rate = prev_sle.get("valuation_rate")

				if in_rate is not None:
					self.average_buying_rate[candidate["key"]] = flt(in_rate)
				else:
					unresolved.append(candidate)

		for chunk in batched(unresolved, 100):
			chunk_list = list(chunk)
			sql, params = build_last_valuation_rate_batch_query(chunk_list)
			last_vr_by_idx = {
				cint(row.get("khitmee_idx")): row.get("valuation_rate")
				for row in frappe.db.sql(sql, params, as_dict=True) or []
			}
			for idx, candidate in enumerate(chunk_list):
				if idx in last_vr_by_idx and last_vr_by_idx[idx] is not None:
					self.average_buying_rate[candidate["key"]] = flt(last_vr_by_idx[idx])
					continue
				meta = candidate["meta"]
				fallback_rate = meta.get("valuation_rate") or meta.get("standard_rate")
				if fallback_rate:
					self.average_buying_rate[candidate["key"]] = flt(fallback_rate)

	def _has_active_repost_item_valuations(self):
		if self._active_repost_item_valuations is None:
			self._active_repost_item_valuations = bool(
				frappe.db.sql(
					"SELECT 1 FROM `tabRepost Item Valuation` "
					"WHERE status NOT IN ('Completed', 'Skipped') LIMIT 1"
				)
			)
		return self._active_repost_item_valuations

	def _drop_pair_matches(self, pair):
		for key in list(self._matched_sles):
			if (key[0], key[1]) == pair:
				self._matched_sles.pop(key, None)
				self._sle_predecessors.pop(key, None)

	def _preload_sales_invoice_sle_by_voucher(self, si_keys):
		si_keys = list(dict.fromkeys(si_keys))
		voucher_nos = sorted({key[3] for key in si_keys})
		keys_by_voucher = {}
		for key in si_keys:
			keys_by_voucher.setdefault(key[3], []).append(key)

		matched_keys = set()
		tied_keys = set()
		near_zero_keys = []
		voucher_tie_pairs = set()
		seen_ordering_by_pair = {}
		pending_matches = {}
		pending_predecessors = {}
		pending_pair_exists = set()

		for voucher_chunk in batched(voucher_nos, 1000):
			chunk_keys = []
			for voucher_no in voucher_chunk:
				chunk_keys.extend(keys_by_voucher.get(voucher_no, ()))
			chunk_keys = list(dict.fromkeys(chunk_keys))
			if not chunk_keys:
				continue

			sql, params = build_sales_invoice_voucher_sle_query(voucher_chunk, self.filters.company)
			rows = frappe.db.sql(sql, params, as_dict=False)
			processed = process_sales_invoice_sle_rows(
				rows, chunk_keys, getattr(self, "currency_precision", None)
			)

			for pair, orderings in processed["orderings_by_pair"].items():
				seen_orderings = seen_ordering_by_pair.setdefault(pair, set())
				if seen_orderings.intersection(orderings):
					voucher_tie_pairs.add(pair)
				seen_orderings.update(orderings)
			voucher_tie_pairs.update(processed["tie_pairs"])

			for key, matched_row in processed["matched"].items():
				pending_matches[key] = matched_row
				pending_predecessors[key] = processed["predecessors"].get(key)
			pending_pair_exists.update(processed["pair_exists"])
			near_zero_keys.extend(processed["near_zero_keys"])
			near_zero_keys.extend(processed["ambiguous_keys"])

		for pair in voucher_tie_pairs:
			self._tie_pairs.add(pair)
			self._drop_pair_matches(pair)
		tied_keys.update(
			key for key in si_keys if (key[0], key[1]) in voucher_tie_pairs
		)

		for key, matched_row in pending_matches.items():
			if key in tied_keys:
				continue
			self._matched_sles[key] = matched_row
			self._sle_predecessors[key] = pending_predecessors.get(key)
			matched_keys.add(key)

		for pair in pending_pair_exists:
			if pair not in voucher_tie_pairs:
				self._sle_pair_exists[pair] = True

		near_zero_keys = [key for key in near_zero_keys if key not in tied_keys]
		for chunk in batched(list(dict.fromkeys(near_zero_keys)), 100):
			self._fetch_sle_chunk(chunk)
			matched_keys.update(chunk)

		leftover_keys = [
			key for key in si_keys if key not in matched_keys and key not in tied_keys
		]
		self._process_leftover_sales_invoice_keys(leftover_keys)

	def _preload_sales_invoice_sle_windows(self, si_keys):
		window_start = f"{self.filters.get('from_date')} 00:00:00"
		window_end = f"{add_days(self.filters.get('to_date'), 1)} 00:00:00"

		pair_keys = {}
		warehouse_items = {}
		for key in si_keys:
			pair = (key[0], key[1])
			pair_keys.setdefault(pair, []).append(key)
			warehouse_items.setdefault(key[1], set()).add(key[0])

		matched_keys = set()
		tied_keys = set()
		for warehouse in sorted(warehouse_items):
			for item_codes in batched(sorted(warehouse_items[warehouse]), 1000):
				batch_matched, batch_tied = self._preload_sle_window_batch(
					warehouse, item_codes, pair_keys, window_start, window_end
				)
				matched_keys.update(batch_matched)
				tied_keys.update(batch_tied)

		leftover_keys = [
			key for key in si_keys if key not in matched_keys and key not in tied_keys
		]
		self._process_leftover_sales_invoice_keys(leftover_keys)

	def _preload_sle_window_batch(self, warehouse, item_codes, pair_keys, window_start, window_end):
		sql, params = build_sle_window_query(
			item_codes, warehouse, self.filters.company, window_start, window_end
		)
		window_rows = frappe.db.sql(sql, params, as_dict=False)
		window_rows_by_pair = {}
		for row in window_rows:
			pair = (str(row[0]), str(row[1]))
			window_rows_by_pair.setdefault(pair, []).append(row)

		sql, params = build_sle_boundary_max_query(item_codes, warehouse, window_start)
		boundary_max_rows = frappe.db.sql(sql, params, as_dict=False)
		max_posting_by_pair = {}
		boundary_points = []
		for row in boundary_max_rows:
			pair = (str(row[0]), str(row[1]))
			posting_datetime = row[2]
			max_posting_by_pair[pair] = posting_datetime
			if posting_datetime is not None:
				boundary_points.append((pair[0], pair[1], posting_datetime))

		rows_at_max_by_pair = {}
		for point_chunk in batched(boundary_points, 500):
			if not point_chunk:
				continue
			sql, params = build_sle_boundary_rows_query(point_chunk)
			for row in frappe.db.sql(sql, params, as_dict=False):
				pair = (str(row[0]), str(row[1]))
				rows_at_max_by_pair.setdefault(pair, []).append(row)

		matched_keys = set()
		tied_keys = set()
		for item_code in item_codes:
			pair = (item_code, warehouse)
			keys_for_pair = pair_keys.get(pair, [])
			boundary_row, boundary_tie = self._resolve_boundary_row(
				pair,
				max_posting_by_pair.get(pair),
				rows_at_max_by_pair.get(pair, []),
			)
			assembly = assemble_sle_window_tuples(
				keys_for_pair,
				window_rows_by_pair.get(pair, []),
				boundary_row,
			)

			if assembly["pair_exists"]:
				self._sle_pair_exists[pair] = True

			if boundary_tie or assembly["tie"]:
				self._tie_pairs.add(pair)
				tied_keys.update(keys_for_pair)
				for key in keys_for_pair:
					self._matched_sles.pop(key, None)
					self._sle_predecessors.pop(key, None)
				continue

			for key, matched_row in assembly["matched"].items():
				self._matched_sles[key] = (
					sle_window_tuple_to_dict(matched_row) if not hasattr(matched_row, "get") else matched_row
				)
				predecessor = assembly["predecessors"].get(key)
				if predecessor is not None and not hasattr(predecessor, "get"):
					predecessor = (
						boundary_tuple_to_dict(predecessor)
						if len(predecessor) == 7
						else sle_window_tuple_to_dict(predecessor)
					)
				self._sle_predecessors[key] = predecessor
				matched_keys.add(key)

		return matched_keys, tied_keys

	def _resolve_boundary_row(self, pair, max_posting_datetime, rows_at_max):
		if max_posting_datetime is None:
			return None, False

		boundary_row, tie = choose_live_boundary_tuple(rows_at_max, self.filters.company)
		if boundary_row is not None:
			return boundary_tuple_to_dict(boundary_row), tie

		sql, params = build_sle_boundary_fallback_query(
			pair[0], pair[1], max_posting_datetime, self.filters.company
		)
		fallback_rows = frappe.db.sql(sql, params, as_dict=False)
		boundary_row, tie = choose_fallback_boundary_tuple(fallback_rows)
		return (boundary_tuple_to_dict(boundary_row), tie) if boundary_row is not None else (None, tie)

	def _process_leftover_sales_invoice_keys(self, leftover_keys):
		if not leftover_keys:
			return

		keys_by_detail = {}
		for key in leftover_keys:
			keys_by_detail.setdefault(key[4], []).append(key)

		found_details = set()
		for detail_chunk in batched(sorted(keys_by_detail), 500):
			sql, params = build_voucher_detail_exists_query(detail_chunk, self.filters.company)
			found_details.update(
				str(row.get("voucher_detail_no"))
				for row in frappe.db.sql(sql, params, as_dict=True)
			)

		outside_keys = []
		for voucher_detail_no, keys in keys_by_detail.items():
			if voucher_detail_no in found_details:
				outside_keys.extend(keys)
			else:
				for key in keys:
					self._matched_sles[key] = None

		for chunk in batched(outside_keys, 100):
			self._fetch_sle_chunk(chunk)

		unresolved_pairs = {(key[0], key[1]) for key in leftover_keys}
		self._fetch_pair_existence(unresolved_pairs)

	def _fetch_pair_existence(self, pairs):
		# Skip pairs already resolved by exact matches or earlier probes; only truly
		# unresolved missing pairs pay for the indexed existence check.
		pairs = sorted({tuple(pair) for pair in pairs} - set(self._sle_pair_exists))
		if not pairs:
			return
		sql, params = build_pair_exists_query(pairs, self.filters.company)
		found = {(row.item_code, row.warehouse) for row in frappe.db.sql(sql, params, as_dict=True)}
		for pair in pairs:
			self._sle_pair_exists[pair] = pair in found

	def _detect_timestamp_ties(self, matched_rows):
		if not matched_rows:
			return
		sql, params = build_tie_detection_query(matched_rows, self.filters.company)
		for row in frappe.db.sql(sql, params, as_dict=True):
			if cint(row.tie_count) > 1:
				self._tie_pairs.add((str(row.item_code), str(row.warehouse)))

	def _fetch_sle_chunk(self, keys):
		keys = list(dict.fromkeys(keys))
		if not keys:
			return

		sql, params = build_matched_sle_query(keys, self.filters.company)
		rows = frappe.db.sql(sql, params, as_dict=True)

		matched = {}
		key_order = []
		keys_set = set(keys)
		for row in rows:
			key = normalize_sle_key(
				row.item_code, row.warehouse, row.voucher_type, row.voucher_no, row.voucher_detail_no
			)
			if key in keys_set and key not in matched:
				matched[key] = row
				key_order.append(key)

		for key in keys:
			self._matched_sles[key] = matched.get(key)

		# Pairs with exact SLE matches in this batch provably exist; mark them now so
		# the existence probe below only runs for unresolved missing pairs.
		for key in matched:
			self._sle_pair_exists[(key[0], key[1])] = True
		pairs = sorted({(key[0], key[1]) for key in keys})
		self._fetch_pair_existence(pairs)

		if not matched:
			return

		self._detect_timestamp_ties(list(matched.values()))

		sql, params = build_predecessor_query(matched, key_order, self.filters.company)
		for row in frappe.db.sql(sql, params, as_dict=True):
			key = key_order[cint(row.khitmee_key_id)]
			self._sle_predecessors[key] = row

	def _pair_history(self, item_code, warehouse):
		pair = (str(item_code), str(warehouse))
		if pair not in self._tie_pair_history:
			sql, params = build_pair_history_query(item_code, warehouse, self.filters.company)
			self._tie_pair_history[pair] = frappe.db.sql(sql, params, as_dict=True)
		return self._tie_pair_history[pair]

	def get_stock_ledger_entries(self, item_code, warehouse):
		"""Return the pair existence signal ERPNext's control flow needs.

		The returned list is not SLE history; calculate_buying_amount_from_sle below uses
		the exact bounded rows. Empty means the pair truly has no non-cancelled SLE, so the
		inherited DN/SO/average fallbacks continue to match stock ERPNext. Pairs with
		ambiguous timestamp ties return their full ordered history instead so the
		inherited ordered-list calculation keeps 100% tie-handling parity.
		"""
		if not (item_code and warehouse):
			return []

		pair = (str(item_code), str(warehouse))
		if pair in self._tie_pairs:
			return self._pair_history(item_code, warehouse)

		if pair not in self._sle_pair_exists:
			self._fetch_pair_existence([pair])

		return [True] if self._sle_pair_exists.get(pair) else []

	def _upstream_calculate_buying_amount_from_sle(
		self, row, my_sle, parenttype, parent, item_row, item_code
	):
		return super().calculate_buying_amount_from_sle(
			row, my_sle, parenttype, parent, item_row, item_code
		)

	def calculate_buying_amount_from_sle(self, row, my_sle, parenttype, parent, item_row, item_code):
		warehouse = row.get("delivered_warehouse") or row.get("warehouse")
		pair = (str(item_code), str(warehouse))
		if pair in self._tie_pairs:
			# Ambiguous (posting_datetime, creation) ties need upstream's ordered-list
			# tie handling; use the full ordered history for this pair.
			return self._upstream_calculate_buying_amount_from_sle(
				row, self._pair_history(item_code, warehouse), parenttype, parent, item_row, item_code
			)

		key = normalize_sle_key(item_code, warehouse, parenttype, parent, item_row)
		if key not in self._matched_sles:
			self._fetch_sle_chunk([key])
			if pair in self._tie_pairs:
				return self._upstream_calculate_buying_amount_from_sle(
					row, self._pair_history(item_code, warehouse), parenttype, parent, item_row, item_code
				)

		matched = self._matched_sles.get(key)
		predecessor = self._sle_predecessors.get(key)
		return buying_amount_from_sle_pair(
			row.qty,
			matched,
			predecessor,
			lambda: self.get_average_buying_rate(row, item_code),
		)
