"""Summary query engine with clean-day stored reads and Detail fallback."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, TypedDict

try:  # pragma: no cover - offline tests install a stub before importing
	import frappe
except ImportError:  # pragma: no cover
	frappe = None

from khitmee_ops.profit_summary import replay, store
from khitmee_ops.profit_summary.budget import (
	MAX_SAFE_ABS_SUM,
	evaluate_work_budget,
	preflight_detail_input_rows,
)
from khitmee_ops.profit_summary.eligibility import authorize_profit_request, evaluate_summary_eligibility


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
		"sle", "bundle_sle", "missing_detail_zero", "average_rate", "last_purchase_rate", "dropship", "dn_valuation"
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


class QueryProfitResult(TypedDict):
	mode: Literal["summary", "detail", "pending", "budget_exceeded"]
	columns: list[dict]
	data: list
	message: str | None
	eligibility: dict
	meta: dict


MONTH_ABBREVIATIONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FIRST_KEY_RE = re.compile(r"^[01]\|\d{4}-\d{2}-\d{2}\|\d{12}")


def _as_date_string(value):
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	return str(value or "")[:10]


def _site_today():
	try:
		from frappe.utils import nowdate

		return nowdate()
	except Exception:
		return date.today().isoformat()


def _add_days(value, days):
	parsed = datetime.fromisoformat(_as_date_string(value)).date()
	return (parsed + timedelta(days=days)).isoformat()


def _date_range(from_date, to_date):
	result = []
	current = _as_date_string(from_date)
	end = _as_date_string(to_date)
	while current <= end:
		result.append(current)
		current = _add_days(current, 1)
	return result


def _month_label(posting_date):
	parsed = datetime.fromisoformat(_as_date_string(posting_date)).date()
	return f"{MONTH_ABBREVIATIONS[parsed.month - 1]} {parsed.year:04d}"


def _decimal(value) -> Decimal:
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value or 0))


class _AttrDict(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as error:
			raise AttributeError(key) from error


def _float_decimal(value: Decimal, precision: int):
	quantum = Decimal("1").scaleb(-int(precision))
	return float(value.quantize(quantum))


def _flt_value(value, precision: int | None = None):
	flt_fn = None
	if frappe is not None:
		try:
			from frappe.utils import flt as frappe_flt

			flt_fn = frappe_flt
		except Exception:
			flt_fn = None
	if callable(flt_fn):
		return float(flt_fn(float(value or 0), precision))
	if precision is None:
		return float(value or 0)
	return _float_decimal(Decimal(str(value or 0)), precision)


def _precision_safe_number(value, precision: int) -> bool:
	try:
		number = _decimal(value)
		quantum = Decimal("1").scaleb(-int(precision))
		return number == number.quantize(quantum)
	except Exception:
		return False


def _canonical_first_seen_key(row) -> str:
	key = str(row.get("first_seen_key") or "")
	if _FIRST_KEY_RE.match(key):
		return key
	phase = int(row.get("query_phase") if row.get("query_phase") in (0, 1) else 1)
	posting_date = _as_date_string(row.get("posting_date"))
	digits = re.sub(r"\D", "", str(row.get("posting_time") or ""))[:12].ljust(12, "0")
	return f"{phase}|{posting_date}|{digits}"


def _empty_budget_counts(mode):
	return {
		"mode": mode,
		"detail_input_rows": 0,
		"clean_work_rows": 0,
		"dirty_work_rows": 0,
		"combined_live_rows": 0,
		"clean_replay_invoices": 0,
		"largest_replay_family_rows": 0,
		"estimated_output_rows": 0,
		"dirty_days_count": 0,
		"missing_days_count": 0,
		"has_blocking_repost": False,
	}


def _meta_from_decision(decision, counts):
	return {
		"budget_outcome": decision.get("outcome"),
		"detail_input_rows": int(counts.get("detail_input_rows", 0)),
		"combined_live_rows": int(counts.get("combined_live_rows", 0)),
		"dirty_days_count": int(counts.get("dirty_days_count", 0)),
		"missing_days_count": int(counts.get("missing_days_count", 0)),
	}


def _result(mode, columns=None, data=None, message=None, eligibility=None, meta=None) -> QueryProfitResult:
	return {
		"mode": mode,
		"columns": columns or [],
		"data": data or [],
		"message": message,
		"eligibility": eligibility
		or {
			"eligible": False,
			"reason": "uninitialized",
			"match_conditions_safe": False,
			"has_blocking_repost": False,
			"dirty_days": [],
			"replay_invoices": 0,
		},
		"meta": meta
		or {
			"budget_outcome": "pending",
			"detail_input_rows": 0,
			"combined_live_rows": 0,
			"dirty_days_count": 0,
			"missing_days_count": 0,
		},
	}


def _fast_gross_profit_module():
	from khitmee_ops.khitmee_ops.report.fast_gross_profit import fast_gross_profit

	return fast_gross_profit


def _fallback_columns(group_by):
	group_field = "warehouse" if group_by == "Warehouse" else "monthly"
	group_label = "Warehouse" if group_by == "Warehouse" else "Monthly"
	group_column = (
		{"fieldname": "warehouse", "label": group_label, "fieldtype": "Link", "options": "Warehouse", "width": 100}
		if group_field == "warehouse"
		else {"fieldname": "monthly", "label": group_label, "fieldtype": "Data", "width": 100}
	)
	return [
		group_column,
		{"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "width": 80},
		{"fieldname": "avg._selling_rate", "label": "Avg. Selling Rate", "fieldtype": "Currency", "options": "currency", "width": 100},
		{"fieldname": "valuation_rate", "label": "Valuation Rate", "fieldtype": "Currency", "options": "currency", "width": 100},
		{"fieldname": "selling_amount", "label": "Selling Amount", "fieldtype": "Currency", "options": "currency", "width": 100},
		{"fieldname": "buying_amount", "label": "Buying Amount", "fieldtype": "Currency", "options": "currency", "width": 100},
		{"fieldname": "gross_profit", "label": "Gross Profit", "fieldtype": "Currency", "options": "currency", "width": 100},
		{"fieldname": "gross_profit_%", "label": "Gross Profit Percent", "fieldtype": "Percent", "width": 100},
		{"fieldname": "currency", "label": "Currency", "fieldtype": "Link", "options": "Currency", "hidden": 1},
	]


def get_summary_columns(filters: dict):
	wrapped = filters
	if isinstance(filters, dict) and not hasattr(filters, "group_by"):
		frappe_dict = getattr(frappe, "_dict", None) if frappe is not None else None
		wrapped = frappe_dict(filters) if callable(frappe_dict) else _AttrDict(filters)
	try:
		fast_module = _fast_gross_profit_module()
		standard = getattr(fast_module, "standard_gross_profit", None)
		if standard is not None:
			return standard.get_columns(fast_module._group_wise_columns(), wrapped)
	except Exception:
		pass
	return _fallback_columns((filters or {}).get("group_by"))


def _normalize_datetime_value(value):
	if value is None:
		return None
	if isinstance(value, datetime):
		return value.isoformat(sep=" ")
	return str(value)


def partition_request_days(company, from_date, to_date, *, today=None, expected_flags=None) -> dict:
	"""Split the request into verified clean past days and replacement live days."""
	today = _as_date_string(today or _site_today())
	expected_flags = expected_flags or store.current_day_flags()
	states = store.get_day_states(company, _as_date_string(from_date), _as_date_string(to_date))
	clean_past_days = []
	dirty_past_days = []
	missing_days = []
	live_days = []
	future_days = []
	blocking_repost = False

	for posting_date in _date_range(from_date, to_date):
		if posting_date >= today:
			live_days.append(posting_date)
			if posting_date > today:
				future_days.append(posting_date)
			continue
		day_state = states.get(posting_date)
		if not day_state:
			missing_days.append(posting_date)
			live_days.append(posting_date)
			continue

		repost_sig = store.get_completed_repost_signature(company, posting_date)
		blocked, _blocking_date = store.has_blocking_repost(company, posting_date)
		blocking_repost = blocking_repost or blocked
		stored_sig = (
			int(day_state.get("repost_completed_count") or 0),
			_normalize_datetime_value(day_state.get("repost_watermark")),
			int(day_state.get("repost_set_digest") or 0),
		)
		current_sig = (int(repost_sig[0]), _normalize_datetime_value(repost_sig[1]), int(repost_sig[2]))
		clean = (
			day_state.get("status") == "clean"
			and int(day_state.get("built_generation") or 0) == int(day_state.get("dirty_generation") or 0)
			and int(day_state.get("calc_version") or 0) == int(expected_flags["calc_version"])
			and int(day_state.get("currency_precision") or 0) == int(expected_flags["currency_precision"])
			and int(day_state.get("float_precision") or 0) == int(expected_flags["float_precision"])
			and day_state.get("rounding_method") == expected_flags["rounding_method"]
			and bool(day_state.get("is_additive_safe"))
			and stored_sig == current_sig
			and not blocked
			and not store.has_active_dirty_range_covering(company, posting_date)
		)
		if clean:
			clean_past_days.append(posting_date)
		else:
			dirty_past_days.append(posting_date)
			live_days.append(posting_date)

	return {
		"clean_past_days": clean_past_days,
		"dirty_past_days": dirty_past_days,
		"missing_days": missing_days,
		"live_days": live_days,
		"future_days": future_days,
		"has_blocking_repost": blocking_repost,
		"day_states": states,
	}


def _contiguous_ranges(days):
	ordered = sorted(days or [])
	if not ordered:
		return []
	ranges = []
	start = previous = ordered[0]
	for day in ordered[1:]:
		if day == _add_days(previous, 1):
			previous = day
			continue
		ranges.append((start, previous))
		start = previous = day
	ranges.append((start, previous))
	return ranges


def expand_warehouse_scope(warehouse) -> set[str]:
	"""Expand a warehouse filter to descendants; the blank sentinel is never in scope."""
	if not warehouse:
		return set()
	if frappe is None:
		return {str(warehouse)}
	try:
		lft_rgt = frappe.get_cached_value("Warehouse", warehouse, ["lft", "rgt"])
		if lft_rgt:
			rows = frappe.db.sql_list(
				"SELECT `name` FROM `tabWarehouse` WHERE `lft` >= %s AND `rgt` <= %s",
				(lft_rgt[0], lft_rgt[1]),
			)
			return {str(row) for row in rows or [] if row}
	except Exception:
		pass
	return {str(warehouse)}


def _new_group(group_id, posting_date=None):
	return {
		"group_id": group_id,
		"qty": Decimal("0"),
		"base_amount": Decimal("0"),
		"buying_amount": Decimal("0"),
		"abs_qty_sum": Decimal("0"),
		"abs_base_sum": Decimal("0"),
		"abs_buying_sum": Decimal("0"),
		"first_seen_key": None,
		"has_rows": False,
		"posting_date": posting_date,
	}


def _add_to_group(group, qty, base_amount, buying_amount, first_seen_key) -> None:
	group["has_rows"] = True
	group["qty"] += qty
	group["base_amount"] += base_amount
	group["buying_amount"] += buying_amount
	group["abs_qty_sum"] += abs(qty)
	group["abs_base_sum"] += abs(base_amount)
	group["abs_buying_sum"] += abs(buying_amount)
	if first_seen_key and (group["first_seen_key"] is None or first_seen_key < group["first_seen_key"]):
		group["first_seen_key"] = first_seen_key


def _group_id_for_row(group_by, warehouse, posting_date):
	if group_by == "Warehouse":
		return str(warehouse or "")
	return _month_label(posting_date)


def aggregate_summary_rows(
	stored_rows,
	contributions,
	filters,
	*,
	currency_precision=3,
	float_precision=2,
	warehouse_scope=None,
) -> dict:
	"""Aggregate stored and live/replay rows using exact Python Decimal arithmetic.

	Returns aggregate groups plus certification flags. No output rows are emitted here;
	this separation lets callers fall back to Detail before formatting when either a
	row-level precision check or the group absolute-magnitude envelope is breached.
	"""
	filters = filters or {}
	group_by = filters.get("group_by")
	include_unlinked = bool(filters.get("include_returned_invoices"))
	warehouse_scope = warehouse_scope if warehouse_scope is not None else expand_warehouse_scope(filters.get("warehouse"))
	warehouse_filtered = bool(filters.get("warehouse"))
	groups = {}
	row_precision_safe = True
	input_row_count = 0

	def warehouse_allowed(warehouse):
		warehouse = str(warehouse or "")
		if group_by == "Warehouse" and warehouse == "":
			return False
		if warehouse_filtered and warehouse not in warehouse_scope:
			return False
		return True

	def bucket_values(row, prefix):
		return (
			_decimal(row.get(f"{prefix}_qty")),
			_decimal(row.get(f"{prefix}_base_amount")),
			_decimal(row.get(f"{prefix}_buying_amount")),
			_decimal(row.get(f"{prefix}_abs_qty_sum")),
			_decimal(row.get(f"{prefix}_abs_base_sum")),
			_decimal(row.get(f"{prefix}_abs_buying_sum")),
			row.get(f"{prefix}_first_seen_key"),
		)

	for stored in stored_rows or []:
		warehouse = str(stored.get("warehouse") or "")
		if not warehouse_allowed(warehouse):
			continue
		group_id = _group_id_for_row(group_by, warehouse, stored.get("posting_date"))
		for prefix, has_field in (("normal", "normal_has_rows"), ("unlinked_return", "unlinked_return_has_rows")):
			if prefix == "unlinked_return" and not include_unlinked:
				continue
			if not int(stored.get(has_field) or 0):
				continue
			qty, base_amount, buying_amount, abs_qty, abs_base, abs_buying, first_key = bucket_values(stored, prefix)
			group = groups.setdefault(group_id, _new_group(group_id, stored.get("posting_date")))
			group["has_rows"] = True
			group["qty"] += qty
			group["base_amount"] += base_amount
			group["buying_amount"] += buying_amount
			group["abs_qty_sum"] += abs_qty
			group["abs_base_sum"] += abs_base
			group["abs_buying_sum"] += abs_buying
			if first_key and (group["first_seen_key"] is None or str(first_key) < group["first_seen_key"]):
				group["first_seen_key"] = str(first_key)
			input_row_count += 1

	for row in contributions or []:
		posting_date = _as_date_string(row.get("posting_date"))
		if posting_date < _as_date_string(filters.get("from_date")) or posting_date > _as_date_string(filters.get("to_date")):
			continue
		warehouse = str(row.get("warehouse") or "")
		if not warehouse_allowed(warehouse):
			continue
		input_row_count += 1
		if not row.get("is_precision_safe") or not (
			_precision_safe_number(row.get("qty"), float_precision)
			and _precision_safe_number(row.get("base_amount"), currency_precision)
			and _precision_safe_number(row.get("buying_amount"), currency_precision)
		):
			row_precision_safe = False
		group_id = _group_id_for_row(group_by, warehouse, posting_date)
		group = groups.setdefault(group_id, _new_group(group_id, posting_date))
		_add_to_group(
			group,
			_decimal(row.get("qty")),
			_decimal(row.get("base_amount")),
			_decimal(row.get("buying_amount")),
			_canonical_first_seen_key(row),
		)

	qty_cap = Decimal(str(MAX_SAFE_ABS_SUM(float_precision)))
	money_cap = Decimal(str(MAX_SAFE_ABS_SUM(currency_precision)))
	cap_exceeded = any(
		group["abs_qty_sum"] > qty_cap
		or max(group["abs_base_sum"], group["abs_buying_sum"]) > money_cap
		for group in groups.values()
	)
	return {
		"groups": groups,
		"row_precision_safe": row_precision_safe,
		"cap_exceeded": cap_exceeded,
		"input_row_count": input_row_count,
		"estimated_output_rows": len(groups) + 1,
	}


def format_summary_groups(groups, filters, *, currency_precision=3, float_precision=2) -> list:
	"""Format rows with stock fieldnames, Total shape, and deterministic group order."""
	filters = filters or {}
	group_by = filters.get("group_by")
	group_field = "warehouse" if group_by == "Warehouse" else "monthly"
	ordered = sorted(
		(groups or {}).values(),
		key=lambda group: (group.get("first_seen_key") or "9|9999-12-31|999999999999", str(group.get("group_id") or "")),
	)
	data = []
	total_base = 0.0
	total_buying = 0.0
	currency = filters.get("currency")

	for group in ordered:
		qty = group["qty"]
		base_amount = group["base_amount"]
		buying_amount = group["buying_amount"]
		gross_profit = base_amount + abs(buying_amount) if buying_amount < 0 else base_amount - buying_amount
		qty_val = _flt_value(qty, float_precision)
		selling_val = _flt_value(base_amount, currency_precision)
		buying_val = _flt_value(buying_amount, currency_precision)
		gross_profit_val = _flt_value(gross_profit, currency_precision)
		row = {
			group_field: group["group_id"],
			"qty": qty_val,
			"avg._selling_rate": _flt_value(selling_val / qty_val, float_precision) if qty_val else 0.0,
			"valuation_rate": _flt_value(buying_val / qty_val, float_precision) if qty_val else 0.0,
			"selling_amount": selling_val,
			"buying_amount": buying_val,
			"gross_profit": gross_profit_val,
			"gross_profit_%": _flt_value((gross_profit_val / abs(selling_val)) * 100.0, currency_precision)
			if selling_val
			else 0.0,
			"currency": currency,
		}
		data.append(row)
		total_base += selling_val
		total_buying += buying_val

	total_gross = _flt_value(total_base + abs(total_buying) if total_buying < 0 else total_base - total_buying)
	data.append(
		{
			group_field: "Total",
			"qty": None,
			"avg._selling_rate": None,
			"valuation_rate": None,
			"selling_amount": total_base,
			"buying_amount": total_buying,
			"gross_profit": total_gross,
			"gross_profit_%": _flt_value((total_gross / abs(total_base)) * 100.0, currency_precision)
			if total_base
			else 0.0,
			"currency": currency,
		}
	)
	return data


def _collect_live_batches(filters, live_days):
	batches = []
	for start, end in _contiguous_ranges(live_days):
		live_filters = dict(filters or {})
		live_filters["from_date"] = start
		live_filters["to_date"] = end
		batches.append(_fast_gross_profit_module().collect_profit_contributions(live_filters))
	return batches


def _batch_metadata(batches):
	for batch in batches or []:
		if batch:
			return (
				int(batch.get("currency_precision") or 3),
				int(batch.get("float_precision") or 2),
				batch.get("rounding_method") or store.DEFAULT_ROUNDING_METHOD,
			)
	flags = store.current_day_flags()
	return flags["currency_precision"], flags["float_precision"], flags["rounding_method"]


def _run_detail(filters, eligibility, message=None) -> QueryProfitResult:
	detail_input_rows = preflight_detail_input_rows(filters)
	counts = _empty_budget_counts("detail")
	counts["detail_input_rows"] = detail_input_rows
	counts["estimated_output_rows"] = detail_input_rows
	decision = evaluate_work_budget(filters, counts)
	meta = _meta_from_decision(decision, counts)
	if decision["outcome"] != "ok":
		return _result("budget_exceeded", message=decision["reason"], eligibility=eligibility, meta=meta)
	columns, data = _fast_gross_profit_module().run_detail_gross_profit(filters)
	return _result("detail", columns=columns, data=data, message=message, eligibility=eligibility, meta=meta)


def _begin_repeatable_read_snapshot():
	"""Open the read snapshot used for day-state, stored-table, and live extraction reads."""
	if frappe is None:
		return
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
	frappe.db.sql("START TRANSACTION")


def _end_repeatable_read_snapshot():
	if frappe is None:
		return
	rollback = getattr(frappe.db, "rollback", None)
	if callable(rollback):
		rollback()


def _live_set_b_invoice_ids(live_batches) -> set[str]:
	"""Certify live invoices and return the Set B complement that must be replayed."""
	from khitmee_ops.profit_summary.rebuilder import classify_invoice_rows

	rows_by_invoice = {}
	reasons = {}
	for batch in live_batches or []:
		if not batch:
			continue
		reasons.update(batch.get("replay_reasons_by_invoice") or {})
		for row in batch.get("contributions") or []:
			if row.get("sales_invoice"):
				rows_by_invoice.setdefault(row["sales_invoice"], []).append(row)
	result = set()
	for invoice, rows in rows_by_invoice.items():
		is_set_a, _reason = classify_invoice_rows(rows, reasons.get(invoice))
		if not is_set_a:
			result.add(invoice)
	return result


def _query_summary_in_snapshot(filters: dict, eligibility: dict) -> QueryProfitResult:
	company = filters.get("company")
	expected_flags = store.current_day_flags()
	day_partition = partition_request_days(
		company,
		filters.get("from_date"),
		filters.get("to_date"),
		expected_flags=expected_flags,
	)
	eligibility = dict(eligibility)
	eligibility["has_blocking_repost"] = bool(day_partition["has_blocking_repost"])
	eligibility["dirty_days"] = list(day_partition["dirty_past_days"])

	clean_days = list(day_partition["clean_past_days"])
	live_days = list(day_partition["live_days"])
	stored_rows = store.get_shop_day_rows(company, clean_days)
	clean_replay_rows = store.get_replay_candidates(company, clean_days)
	clean_replay_ids = replay.extract_replay_invoice_ids(clean_replay_rows)

	live_batches = _collect_live_batches(filters, live_days)
	live_set_b_ids = _live_set_b_invoice_ids(live_batches)
	replay_ids = sorted(set(clean_replay_ids) | set(live_set_b_ids))
	eligibility["replay_invoices"] = len(replay_ids)
	replay_batch = replay.collect_replay_contributions(filters, replay_ids)

	all_batches = [*live_batches, replay_batch]
	live_contributions_all = [row for batch in live_batches for row in (batch or {}).get("contributions", [])]
	live_contributions = [row for row in live_contributions_all if row.get("sales_invoice") not in live_set_b_ids]
	replay_contributions = list((replay_batch or {}).get("contributions") or [])
	all_contributions = [*live_contributions, *replay_contributions]
	for row in all_contributions:
		if row.get("query_phase") not in (0, 1):
			row["query_phase"] = 0 if int(row.get("is_return") or 0) == 1 and row.get("return_against") else 1
	currency_precision, float_precision, _rounding_method = _batch_metadata(all_batches)

	today = _site_today()
	today_or_future = set(_date_range(today, filters.get("to_date"))) if filters.get("to_date") >= today else set()
	clean_replay_id_set = set(clean_replay_ids)
	clean_replay_work_rows = sum(
		1 for row in replay_contributions if row.get("sales_invoice") in clean_replay_id_set
	)
	live_replay_contributions = [
		row for row in replay_contributions if row.get("sales_invoice") not in clean_replay_id_set
	]
	live_work_contributions = [*live_contributions, *live_replay_contributions]
	today_future_rows = sum(
		1 for row in live_work_contributions if _as_date_string(row.get("posting_date")) in today_or_future
	)
	dirty_rows = sum(
		1 for row in live_work_contributions if _as_date_string(row.get("posting_date")) not in today_or_future
	)

	warehouse_scope = expand_warehouse_scope(filters.get("warehouse"))
	aggregation = aggregate_summary_rows(
		stored_rows,
		all_contributions,
		filters,
		currency_precision=currency_precision,
		float_precision=float_precision,
		warehouse_scope=warehouse_scope,
	)
	counts = _empty_budget_counts("summary")
	counts.update(
		{
			"clean_work_rows": today_future_rows + clean_replay_work_rows,
			"dirty_work_rows": dirty_rows,
			"combined_live_rows": len(live_work_contributions) + clean_replay_work_rows,
			"clean_replay_invoices": len(clean_replay_ids),
			"largest_replay_family_rows": max(
				replay.largest_replay_family_rows(replay_batch),
				*(replay.largest_replay_family_rows(batch) for batch in live_batches),
				0,
			),
			"estimated_output_rows": int(aggregation["estimated_output_rows"]),
			"dirty_days_count": len(day_partition["dirty_past_days"]),
			"missing_days_count": len(day_partition["missing_days"]),
			"has_blocking_repost": bool(day_partition["has_blocking_repost"]),
		}
	)
	decision = evaluate_work_budget(filters, counts)
	meta = _meta_from_decision(decision, counts)
	if decision["outcome"] != "ok":
		return _result(decision["outcome"], message=decision["reason"], eligibility=eligibility, meta=meta)
	if not aggregation["row_precision_safe"] or aggregation["cap_exceeded"]:
		reason = "precision_or_additive_magnitude_breach"
		return _run_detail(filters, eligibility, message=reason)

	columns = get_summary_columns(filters)
	data = format_summary_groups(
		aggregation["groups"],
		filters,
		currency_precision=currency_precision,
		float_precision=float_precision,
	)
	return _result("summary", columns=columns, data=data, eligibility=eligibility, meta=meta)


def query_profit(filters: dict, user: str | None = None) -> QueryProfitResult:
	"""Canonical profit entrypoint enforcing auth, freshness, budget, and parity order."""
	filters = dict(filters or {})
	for date_key in ("from_date", "to_date"):
		if filters.get(date_key):
			filters[date_key] = _as_date_string(filters[date_key])
	authorize_profit_request(filters, user)
	triggers_installed, missing_triggers = store.verify_repost_triggers_installed()
	if not triggers_installed:
		return _result(
			"pending",
			message="Profit summary repost triggers are not installed",
			eligibility={
				"eligible": False,
				"reason": "missing_repost_triggers:" + ",".join(missing_triggers),
				"match_conditions_safe": False,
				"has_blocking_repost": False,
				"dirty_days": [],
				"replay_invoices": 0,
			},
		)
	eligibility = evaluate_summary_eligibility(filters, user)

	if not eligibility.get("eligible"):
		_begin_repeatable_read_snapshot()
		try:
			return _run_detail(filters, eligibility, message=eligibility.get("reason"))
		finally:
			_end_repeatable_read_snapshot()

	_begin_repeatable_read_snapshot()
	try:
		return _query_summary_in_snapshot(filters, eligibility)
	finally:
		_end_repeatable_read_snapshot()


__all__ = [
	"MAX_SAFE_ABS_SUM",
	"ContributionBatch",
	"QueryProfitResult",
	"RowContribution",
	"aggregate_summary_rows",
	"format_summary_groups",
	"partition_request_days",
	"query_profit",
]
