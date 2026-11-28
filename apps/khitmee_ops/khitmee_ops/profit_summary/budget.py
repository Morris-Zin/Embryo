"""Single owner of the request-wide profit work budget."""

from __future__ import annotations

from typing import Literal, TypedDict

try:  # pragma: no cover - offline tests install a stub before importing
	import frappe
except ImportError:  # pragma: no cover
	frappe = None


class BudgetInput(TypedDict):
	mode: Literal["summary", "detail"]
	detail_input_rows: int
	clean_work_rows: int
	dirty_work_rows: int
	combined_live_rows: int
	clean_replay_invoices: int
	largest_replay_family_rows: int
	estimated_output_rows: int
	dirty_days_count: int
	missing_days_count: int
	has_blocking_repost: bool


class BudgetDecision(TypedDict):
	allowed: bool
	outcome: Literal["ok", "pending", "budget_exceeded"]
	reason: str
	counts: dict[str, int]


DEFAULT_SYNC_ROW_BUDGET = 35_000
DEFAULT_MAX_OUTPUT_ROWS = 40_000
DEFAULT_MAX_REPLAY_INVOICES = 1_000
DEFAULT_MAX_REPLAY_FAMILY_ROWS = 5_000
DEFAULT_MAX_DIRTY_SUMMARY_DAYS = 7


def MAX_SAFE_ABS_SUM(precision: int):
	"""Normative additive magnitude cap: min(10**11, 10**(14 - precision))."""
	return min(10**11, 10 ** (14 - int(precision)))


def _cap(filters, name, default):
	try:
		return int((filters or {}).get(name) or default)
	except (TypeError, ValueError):
		return default


def _counts_as_ints(counts: BudgetInput) -> dict[str, int]:
	return {
		"detail_input_rows": int(counts.get("detail_input_rows", 0)),
		"clean_work_rows": int(counts.get("clean_work_rows", 0)),
		"dirty_work_rows": int(counts.get("dirty_work_rows", 0)),
		"combined_live_rows": int(counts.get("combined_live_rows", 0)),
		"clean_replay_invoices": int(counts.get("clean_replay_invoices", 0)),
		"largest_replay_family_rows": int(counts.get("largest_replay_family_rows", 0)),
		"estimated_output_rows": int(counts.get("estimated_output_rows", 0)),
		"dirty_days_count": int(counts.get("dirty_days_count", 0)),
		"missing_days_count": int(counts.get("missing_days_count", 0)),
		"has_blocking_repost": int(bool(counts.get("has_blocking_repost"))),
	}


def evaluate_work_budget(filters: dict, counts: BudgetInput) -> BudgetDecision:
	"""Apply the frozen disjoint summary/detail budget decision tree."""
	filters = filters or {}
	sync_row_budget = _cap(filters, "sync_row_budget", DEFAULT_SYNC_ROW_BUDGET)
	max_output_rows = _cap(filters, "max_output_rows", DEFAULT_MAX_OUTPUT_ROWS)
	max_replay_invoices = _cap(filters, "max_replay_invoices", DEFAULT_MAX_REPLAY_INVOICES)
	max_replay_family_rows = _cap(filters, "max_replay_family_rows", DEFAULT_MAX_REPLAY_FAMILY_ROWS)
	max_dirty_summary_days = _cap(filters, "max_dirty_summary_days", DEFAULT_MAX_DIRTY_SUMMARY_DAYS)
	counts_dict = _counts_as_ints(counts)

	outcome = "ok"
	reason = "within_budget"
	if counts.get("mode") == "detail":
		if int(counts.get("detail_input_rows", 0)) > sync_row_budget:
			outcome, reason = "budget_exceeded", "detail_input_rows_exceeds_sync_row_budget"
		elif int(counts.get("estimated_output_rows", 0)) > max_output_rows:
			outcome, reason = "budget_exceeded", "estimated_output_rows_exceeds_max_output_rows"
		elif int(counts.get("largest_replay_family_rows", 0)) > max_replay_family_rows:
			outcome, reason = "budget_exceeded", "largest_replay_family_rows_exceeds_cap"
	else:
		if int(counts.get("clean_work_rows", 0)) > sync_row_budget:
			outcome, reason = "budget_exceeded", "clean_work_rows_exceeds_sync_row_budget"
		elif int(counts.get("clean_replay_invoices", 0)) > max_replay_invoices:
			outcome, reason = "budget_exceeded", "clean_replay_invoices_exceeds_cap"
		elif int(counts.get("largest_replay_family_rows", 0)) > max_replay_family_rows:
			outcome, reason = "budget_exceeded", "largest_replay_family_rows_exceeds_cap"
		elif int(counts.get("estimated_output_rows", 0)) > max_output_rows:
			outcome, reason = "budget_exceeded", "estimated_output_rows_exceeds_max_output_rows"
		elif counts.get("has_blocking_repost"):
			outcome, reason = "pending", "blocking_repost_item_valuation"
		elif int(counts.get("dirty_days_count", 0)) + int(counts.get("missing_days_count", 0)) > max_dirty_summary_days:
			outcome, reason = "pending", "too_many_dirty_or_missing_summary_days"
		elif int(counts.get("combined_live_rows", 0)) > sync_row_budget:
			outcome, reason = "pending", "combined_live_rows_exceeds_sync_row_budget"

	return {
		"allowed": outcome == "ok",
		"outcome": outcome,
		"reason": reason,
		"counts": counts_dict,
	}


def _tree_lft_rgt(doctype: str, name: str):
	if frappe is None or not hasattr(frappe, "get_cached_value"):
		return None
	lft_rgt = frappe.get_cached_value(doctype, name, ["lft", "rgt"])
	if (
		lft_rgt
		and len(lft_rgt) == 2
		and lft_rgt[0] is not None
		and lft_rgt[1] is not None
		and int(lft_rgt[1]) > int(lft_rgt[0]) + 1
	):
		return int(lft_rgt[0]), int(lft_rgt[1])
	return None


def build_detail_preflight_query(filters: dict, cap: int | None = None):
	"""Build the bounded LIMIT(cap+1) Detail input-row probe.

	The probe counts every submitted non-opening Sales Invoice Item in the complete
	requested date population. It intentionally does not exclude clean-day Set A rows,
	so Summary fallback and direct Detail share one non-bypassable input ceiling.
	Active Sales Person / Payment Term grouping expands through their child tables,
	and packed bundle components are counted as additional input rows.
	"""
	filters = filters or {}
	cap = int(cap or _cap(filters, "sync_row_budget", DEFAULT_SYNC_ROW_BUDGET)) + 1
	params = [filters.get("company"), filters.get("from_date"), filters.get("to_date")]
	where = [
		"`si`.`company` = %s",
		"`si`.`docstatus` = 1",
		"`si`.`is_return` IN (0, 1)",
		"`si`.`is_opening` != 'Yes'",
		"`si`.`posting_date` BETWEEN %s AND %s",
	]
	filter_joins = []
	sales_invoice = filters.get("sales_invoice")
	if sales_invoice:
		# Stock Gross Profit honours a selected invoice; count only its family.
		where.append("(`si`.`name` = %s OR `si`.`return_against` = %s)")
		params.extend([sales_invoice, sales_invoice])
	warehouse = filters.get("warehouse")
	if warehouse:
		lft_rgt = _tree_lft_rgt("Warehouse", warehouse)
		if lft_rgt:
			where.append(
				"`sii`.`warehouse` IN ("
				"SELECT `w`.`name` FROM `tabWarehouse` AS `w` WHERE `w`.`lft` >= %s AND `w`.`rgt` <= %s"
				")"
			)
			params.extend([lft_rgt[0], lft_rgt[1]])
		else:
			where.append("`sii`.`warehouse` = %s")
			params.append(warehouse)
	customer = filters.get("customer")
	if customer:
		where.append("`si`.`customer` = %s")
		params.append(customer)
	item_code = filters.get("item_code")
	if item_code:
		where.append("`sii`.`item_code` = %s")
		params.append(item_code)
	brand = filters.get("brand")
	if brand:
		where.append("`sii`.`brand` = %s")
		params.append(brand)
	item_group = filters.get("item_group")
	if item_group:
		filter_joins.append("INNER JOIN `tabItem` AS `item` ON `item`.`name` = `sii`.`item_code`")
		lft_rgt = _tree_lft_rgt("Item Group", item_group)
		if lft_rgt:
			where.append(
				"`item`.`item_group` IN ("
				"SELECT `ig`.`name` FROM `tabItem Group` AS `ig` WHERE `ig`.`lft` >= %s AND `ig`.`rgt` <= %s"
				")"
			)
			params.extend([lft_rgt[0], lft_rgt[1]])
		else:
			where.append("`item`.`item_group` = %s")
			params.append(item_group)
	sales_person = filters.get("sales_person")
	sales_team_join = (
		"LEFT JOIN `tabSales Team` AS `st` ON `st`.`parent` = `si`.`name` AND `st`.`parenttype` = 'Sales Invoice'"
	)
	if sales_person:
		# Match ERPNext apply_common_filters: exact-name EXISTS predicate independent of Sales Person grouping.
		where.append(
			"EXISTS (SELECT 1 FROM `tabSales Team` AS `st_f` "
			"WHERE `st_f`.`parent` = `si`.`name` "
			"AND `st_f`.`parenttype` = 'Sales Invoice' "
			"AND `st_f`.`sales_person` = %s)"
		)
		params.append(sales_person)

	group_joins = []
	group_by = str(filters.get("group_by") or "").lower()
	if group_by == "sales person":
		group_joins.append(sales_team_join)
	if group_by == "payment term":
		group_joins.append("LEFT JOIN `tabPayment Schedule` AS `ps` ON `ps`.`parent` = `si`.`name` AND `ps`.`parenttype` = 'Sales Invoice'")

	si_index_hint = " FORCE INDEX (`khitmee_si_co_ds_ret_date`)" if not (sales_invoice or customer) else ""
	sii_index_hint = " FORCE INDEX (`khitmee_sii_parent_warehouse`)" if (warehouse and not item_code) else ""
	base_join_sql = "\n\t\t".join([*filter_joins, *group_joins])
	packed_join_sql = "\n\t\t".join(filter_joins)
	base_select = f"""
		SELECT `sii`.`name` AS `input_row`
		FROM `tabSales Invoice` AS `si`{si_index_hint}
		STRAIGHT_JOIN `tabSales Invoice Item` AS `sii`{sii_index_hint} ON `sii`.`parent` = `si`.`name`
		{base_join_sql}
		WHERE {' AND '.join(where)}
	"""
	# ERPNext's invoice tree expands the entire packed group for each matching
	# invoice line. The packed item's source detail ID is used for valuation.
	packed_select = f"""
		SELECT `pi`.`name` AS `input_row`
		FROM `tabSales Invoice` AS `si`{si_index_hint}
		STRAIGHT_JOIN `tabSales Invoice Item` AS `sii`{sii_index_hint} ON `sii`.`parent` = `si`.`name`
		STRAIGHT_JOIN `tabPacked Item` AS `pi` ON `pi`.`parent` = `si`.`name`
			AND `pi`.`parenttype` = 'Sales Invoice'
			AND `pi`.`parent_item` = `sii`.`item_code`
			AND `pi`.`docstatus` = 1
		{packed_join_sql}
		WHERE {' AND '.join(where)}
	"""
	sql = f"""
		SELECT COUNT(*) AS `input_rows`
		FROM (
			SELECT `input_row` FROM (
				{base_select}
				UNION ALL
				{packed_select}
			) AS `khitmee_detail_input`
			LIMIT %s
		) AS `khitmee_bounded_detail_input`
	"""
	return sql, [*params, *params, cap]


def preflight_detail_input_rows(filters: dict, cap: int | None = None) -> int:
	"""Execute the bounded detail preflight and return at most cap + 1."""
	if frappe is None:
		raise RuntimeError("detail budget preflight requires Frappe")
	sql, params = build_detail_preflight_query(filters, cap)
	rows = frappe.db.sql(sql, params)
	if not rows:
		return 0
	row = rows[0]
	if hasattr(row, "get"):
		return int(row.get("input_rows") or 0)
	return int(row[0] or 0)
