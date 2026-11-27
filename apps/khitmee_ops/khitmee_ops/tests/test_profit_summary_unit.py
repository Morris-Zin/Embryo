import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))


def install_frappe_stub():
	frappe = sys.modules.get("frappe") or types.ModuleType("frappe")
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
	frappe.session = SimpleNamespace(user="administrator@example.com")
	frappe.has_permission = lambda *args, **kwargs: True
	frappe.get_all = lambda *args, **kwargs: []
	frappe.get_roles = lambda user=None: ["Accounts User"]
	frappe.set_user = lambda user: setattr(frappe.session, "user", user)
	frappe.permissions = SimpleNamespace(
		has_permission=lambda *args, **kwargs: True,
		can_export=lambda *args, **kwargs: True,
		build_match_conditions=lambda doctype, user=None: "",
	)
	frappe.db = SimpleNamespace(
		sql=lambda *args, **kwargs: [],
		sql_list=lambda *args, **kwargs: [],
		get_value=lambda *args, **kwargs: None,
		get_default=lambda name: {"currency_precision": 3, "float_precision": 2}.get(name),
		commit=lambda: None,
		rollback=lambda: None,
	)

	utils = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")
	utils.nowdate = lambda: "2026-02-01"
	utils.cint = lambda value: int(value or 0)
	utils.add_days = lambda value, days: value
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	return frappe


FRAPPE = install_frappe_stub()

from khitmee_ops.profit_summary import rebuilder, replay, store
from khitmee_ops.profit_summary import query
from khitmee_ops.profit_summary.budget import MAX_SAFE_ABS_SUM, build_detail_preflight_query, evaluate_work_budget
from khitmee_ops.profit_summary.eligibility import (
	authorize_profit_request,
	company_only_match_conditions,
	evaluate_summary_eligibility,
)
from khitmee_ops.setup import indexes


def contribution(
	sales_invoice="SI-1",
	item_row="ROW-1",
	posting_date="2026-01-01",
	warehouse="W1",
	qty=1.0,
	base_amount=100.0,
	buying_amount=60.0,
	query_phase=1,
	first_seen_key=None,
	**overrides,
):
	row = {
		"sales_invoice": sales_invoice,
		"item_row": item_row,
		"posting_date": posting_date,
		"warehouse": warehouse,
		"is_return": 0,
		"return_against": None,
		"update_stock": 1,
		"delivered_by_supplier": 0,
		"valuation_source": "sle",
		"qty": qty,
		"base_amount": base_amount,
		"buying_amount": buying_amount,
		"query_phase": query_phase,
		"first_seen_key": first_seen_key or f"{query_phase}|{posting_date}|100000000000",
		"is_precision_safe": True,
	}
	row.update(overrides)
	return row


def batch(rows, reasons=None, currency_precision=3):
	return {
		"contributions": rows,
		"replay_reasons_by_invoice": reasons or {},
		"currency_precision": currency_precision,
		"float_precision": 2,
		"rounding_method": "Banker's Rounding (legacy)",
	}


def empty_shop_row(company="C", posting_date="2026-01-01", warehouse="W1"):
	return {
		"company": company,
		"posting_date": posting_date,
		"warehouse": warehouse,
		"normal_has_rows": 0,
		"normal_qty": "0",
		"normal_base_amount": "0",
		"normal_buying_amount": "0",
		"normal_abs_qty_sum": "0",
		"normal_abs_base_sum": "0",
		"normal_abs_buying_sum": "0",
		"normal_first_seen_key": None,
		"unlinked_return_has_rows": 0,
		"unlinked_return_qty": "0",
		"unlinked_return_base_amount": "0",
		"unlinked_return_buying_amount": "0",
		"unlinked_return_abs_qty_sum": "0",
		"unlinked_return_abs_base_sum": "0",
		"unlinked_return_abs_buying_sum": "0",
		"unlinked_return_first_seen_key": None,
	}


def budget_counts(mode="summary", **overrides):
	counts = {
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
	counts.update(overrides)
	return counts


class TestSetAPartition(unittest.TestCase):
	def test_positive_sle_invoice_is_set_a_and_unlinked_return_uses_separate_bucket(self):
		rows = [
			contribution("GOOD", "R1", warehouse="W1"),
			contribution(
				"UNLINKED",
				"R2",
				warehouse="W1",
				qty=-1.0,
				base_amount=-90.0,
				buying_amount=-50.0,
				is_return=1,
				return_against=None,
			),
		]

		shop_rows, replay_rows, flags = rebuilder.partition_day_contributions(batch(rows), "C", "2026-01-01")

		self.assertEqual(replay_rows, [])
		self.assertEqual(len(shop_rows), 1)
		self.assertEqual(shop_rows[0]["normal_has_rows"], 1)
		self.assertEqual(shop_rows[0]["unlinked_return_has_rows"], 1)
		self.assertTrue(shop_rows[0]["normal_first_seen_key"].startswith("1|2026-01-01|"))
		self.assertTrue(flags["is_additive_safe"])

	def test_set_b_rejections_cover_all_non_positive_or_request_dependent_rules(self):
		cases = [
			({"valuation_source": "missing_detail_zero"}, "missing_sle_zero"),
			({"valuation_source": "last_purchase_rate"}, "request_dependent_valuation"),
			({"valuation_source": "average_rate"}, "request_dependent_valuation"),
			({"update_stock": 0}, "non_update_stock"),
			({"delivered_by_supplier": 1}, "dropship"),
			({"is_return": 1, "return_against": "ORIG"}, "linked_return"),
			({"is_precision_safe": False}, "request_dependent_valuation"),
		]
		for overrides, reason in cases:
			with self.subTest(reason=reason):
				rows = [contribution("BAD", **overrides)]
				_shop_rows, replay_rows, _flags = rebuilder.partition_day_contributions(batch(rows), "C", "2026-01-01")
				self.assertEqual(replay_rows, [{"company": "C", "posting_date": "2026-01-01", "sales_invoice": "BAD", "reason": reason}])

	def test_global_linked_original_is_set_b_even_when_own_rows_are_positive_sle(self):
		rows = [contribution("ORIG")]
		_shop_rows, replay_rows, _flags = rebuilder.partition_day_contributions(
			batch(rows, {"ORIG": "linked_return_original"}), "C", "2026-01-01"
		)
		self.assertEqual(replay_rows[0]["reason"], "linked_return_original")


class TestBudget(unittest.TestCase):
	def test_detail_budget_decision_tree(self):
		self.assertEqual(evaluate_work_budget({}, budget_counts("detail"))["outcome"], "ok")
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("detail", detail_input_rows=35_001))["outcome"], "budget_exceeded"
		)
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("detail", estimated_output_rows=40_001))["outcome"], "budget_exceeded"
		)
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("detail", largest_replay_family_rows=5_001))["outcome"], "budget_exceeded"
		)

	def test_summary_budget_clean_caps_then_pending_rules(self):
		self.assertEqual(evaluate_work_budget({}, budget_counts("summary"))["outcome"], "ok")
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("summary", clean_work_rows=35_001))["outcome"], "budget_exceeded"
		)
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("summary", clean_replay_invoices=1_001))["outcome"],
			"budget_exceeded",
		)
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("summary", has_blocking_repost=True))["outcome"], "pending"
		)
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("summary", dirty_days_count=5, missing_days_count=3))["outcome"], "pending"
		)
		self.assertEqual(
			evaluate_work_budget({}, budget_counts("summary", combined_live_rows=35_001))["outcome"], "pending"
		)

	def test_detail_preflight_sql_is_bounded_and_counts_child_and_bundle_expansion(self):
		sql, params = build_detail_preflight_query(
			{"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-31", "group_by": "Sales Person", "warehouse": "W"},
			cap=35_000,
		)
		self.assertIn("LIMIT %s", sql)
		self.assertIn("tabSales Team", sql)
		self.assertIn("tabPacked Item", sql)
		self.assertIn("tabSales Invoice Item", sql)
		self.assertEqual(params[-1], 35_001)

		sql, _params = build_detail_preflight_query(
			{"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-31", "group_by": "Payment Term"}
		)
		self.assertIn("tabPayment Schedule", sql)


class TestPrecisionAndOrdering(unittest.TestCase):
	def test_max_safe_abs_sum_formula(self):
		self.assertEqual(MAX_SAFE_ABS_SUM(3), 100_000_000_000)
		self.assertEqual(MAX_SAFE_ABS_SUM(6), 100_000_000)

	def test_large_float_loss_and_boundary_are_rejected_before_grouping(self):
		filters = {"group_by": "Warehouse", "from_date": "2026-01-01", "to_date": "2026-01-31"}
		rows = [contribution("BIG", item_row="R0", base_amount=68_719_476_736.0)]
		rows.extend(
			contribution("BIG", item_row=f"R{index}", base_amount=0.000001) for index in range(1, 9)
		)
		result = query.aggregate_summary_rows([], rows, filters, currency_precision=6, warehouse_scope=set())
		self.assertTrue(result["cap_exceeded"])

		boundary = [contribution("BOUNDARY", base_amount=100_000_000.000001)]
		result = query.aggregate_summary_rows([], boundary, filters, currency_precision=6, warehouse_scope=set())
		self.assertTrue(result["cap_exceeded"])

	def test_mixed_sign_group_inside_absolute_cap_is_admitted(self):
		filters = {"group_by": "Warehouse", "from_date": "2026-01-01", "to_date": "2026-01-31"}
		rows = [
			contribution("A", item_row="R1", base_amount=40_000_000.0),
			contribution("B", item_row="R2", base_amount=-40_000_000.0, qty=-1.0),
		]
		result = query.aggregate_summary_rows([], rows, filters, currency_precision=6, warehouse_scope=set())
		self.assertFalse(result["cap_exceeded"])
		self.assertTrue(result["row_precision_safe"])

	def test_row_level_seven_digit_precision_is_rejected_at_precision_six(self):
		filters = {"group_by": "Warehouse", "from_date": "2026-01-01", "to_date": "2026-01-31"}
		result = query.aggregate_summary_rows(
			[], [contribution(qty=1.0000001)], filters, currency_precision=6, warehouse_scope=set()
		)
		self.assertFalse(result["row_precision_safe"])

	def test_phase_zero_cross_month_emits_feb_before_jan(self):
		filters = {"group_by": "Monthly", "from_date": "2026-01-01", "to_date": "2026-02-28", "currency": "USD"}
		rows = [
			contribution("ORD", posting_date="2026-01-01", query_phase=1, first_seen_key="1|2026-01-01|100000000000"),
			contribution(
				"RET",
				posting_date="2026-02-02",
				qty=-1.0,
				base_amount=-100.0,
				buying_amount=-60.0,
				query_phase=0,
				first_seen_key="0|2026-02-02|100000000000",
				is_return=1,
				return_against="ORD",
			),
		]
		aggregated = query.aggregate_summary_rows([], rows, filters, warehouse_scope=set())
		data = query.format_summary_groups(aggregated["groups"], filters)
		self.assertEqual([row["monthly"] for row in data[:-1]], ["Feb 2026", "Jan 2026"])

	def test_equal_timestamp_tie_breaks_by_emitted_group_id_ascending(self):
		filters = {"group_by": "Warehouse", "from_date": "2026-01-01", "to_date": "2026-01-31", "currency": "USD"}
		key = "1|2026-01-01|100000000000"
		rows = [
			contribution("B", item_row="R1", warehouse="W-B", first_seen_key=key),
			contribution("A", item_row="R2", warehouse="W-A", first_seen_key=key),
		]
		aggregated = query.aggregate_summary_rows([], rows, filters, warehouse_scope=set())
		data = query.format_summary_groups(aggregated["groups"], filters)
		self.assertEqual([row["warehouse"] for row in data[:-1]], ["W-A", "W-B"])

	def test_monthly_warehouse_descendants_are_filtered_before_grouping_and_blank_rules_hold(self):
		filters = {"group_by": "Monthly", "from_date": "2026-01-01", "to_date": "2026-03-31", "warehouse": "PARENT"}
		rows = [
			contribution("CHILD", posting_date="2026-01-01", warehouse="CHILD"),
			contribution("OUT", posting_date="2026-02-01", warehouse="OUTSIDE"),
			contribution("BLANK", posting_date="2026-03-01", warehouse=""),
		]
		aggregated = query.aggregate_summary_rows([], rows, filters, warehouse_scope={"PARENT", "CHILD"})
		self.assertEqual(list(aggregated["groups"]), ["Jan 2026"])

		unfiltered = dict(filters, warehouse=None)
		aggregated = query.aggregate_summary_rows([], rows, unfiltered, warehouse_scope=set())
		self.assertEqual(set(aggregated["groups"]), {"Jan 2026", "Feb 2026", "Mar 2026"})

		warehouse_filters = dict(unfiltered, group_by="Warehouse")
		aggregated = query.aggregate_summary_rows([], rows, warehouse_filters, warehouse_scope=set())
		self.assertEqual(set(aggregated["groups"]), {"CHILD", "OUTSIDE"})
		self.assertNotIn("", aggregated["groups"])

	def test_float_precision_governs_qty_and_abs_qty_cap_independently(self):
		from datetime import timedelta

		filters = {"group_by": "Warehouse", "from_date": "2026-01-01", "to_date": "2026-01-31", "currency": "USD"}
		# float_precision=1 -> max qty abs cap is 10**13; float_precision=6 -> cap is 10**8 (100_000_000).
		large_qty_rows = [
			contribution("A", item_row="R1", qty=80_000_000.0, base_amount=10.0, buying_amount=5.0),
			contribution("B", item_row="R2", qty=30_000_000.0, base_amount=10.0, buying_amount=5.0),
		]
		res_p2 = query.aggregate_summary_rows(
			[], large_qty_rows, filters, currency_precision=6, float_precision=2, warehouse_scope=set()
		)
		self.assertFalse(res_p2["cap_exceeded"])
		res_p6 = query.aggregate_summary_rows(
			[], large_qty_rows, filters, currency_precision=2, float_precision=6, warehouse_scope=set()
		)
		self.assertTrue(res_p6["cap_exceeded"])

		fractional_qty = [contribution(qty=1.123, base_amount=10.1234, buying_amount=5.1234)]
		safe_p3 = query.aggregate_summary_rows(
			[], fractional_qty, filters, currency_precision=6, float_precision=3, warehouse_scope=set()
		)
		self.assertTrue(safe_p3["row_precision_safe"])
		formatted = query.format_summary_groups(
			safe_p3["groups"], filters, currency_precision=6, float_precision=3
		)
		self.assertEqual(formatted[0]["qty"], 1.123)

		unsafe_p2 = query.aggregate_summary_rows(
			[], fractional_qty, filters, currency_precision=6, float_precision=2, warehouse_scope=set()
		)
		self.assertFalse(unsafe_p2["row_precision_safe"])

		shop_rows, _replay_rows, day_flags = rebuilder.partition_day_contributions(
			{
				"contributions": [
					{
						"sales_invoice": "SI-1",
						"item_row": "R1",
						"posting_date": "2026-01-01",
						"posting_time": "10:00:00",
						"warehouse": "W1",
						"is_return": 0,
						"return_against": None,
						"update_stock": 1,
						"delivered_by_supplier": 0,
						"valuation_source": "sle",
						"qty": 60_000_000.0,
						"base_amount": 1.0,
						"buying_amount": 1.0,
						"query_phase": 1,
						"first_seen_key": "1|2026-01-01|100000000000",
						"is_precision_safe": True,
					},
					{
						"sales_invoice": "SI-2",
						"item_row": "R2",
						"posting_date": "2026-01-01",
						"posting_time": "10:00:01",
						"warehouse": "W1",
						"is_return": 0,
						"return_against": None,
						"update_stock": 1,
						"delivered_by_supplier": 0,
						"valuation_source": "sle",
						"qty": 60_000_000.0,
						"base_amount": 1.0,
						"buying_amount": 1.0,
						"query_phase": 1,
						"first_seen_key": "1|2026-01-01|100001000000",
						"is_precision_safe": True,
					},
				],
				"currency_precision": 2,
				"float_precision": 6,
			},
			"C",
			"2026-01-01",
		)
		self.assertEqual(len(shop_rows), 1)
		self.assertFalse(day_flags["is_additive_safe"])
		self.assertEqual(
			rebuilder.make_first_seen_key(1, "2026-01-02", timedelta(hours=9, minutes=8, seconds=7, microseconds=654321)),
			"1|2026-01-02|090807654321",
		)

	def test_detail_preflight_expands_group_warehouse_subquery_and_leaf_equals(self):
		base_filters = {"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-31", "warehouse": "W-PARENT"}
		with patch.object(FRAPPE, "get_cached_value", return_value=(1, 10), create=True):
			sql_group, params_group = build_detail_preflight_query(base_filters, cap=100)
		self.assertIn("FROM `tabWarehouse` AS `w` WHERE `w`.`lft` >= %s AND `w`.`rgt` <= %s", sql_group)
		self.assertEqual(params_group, ["C", "2026-01-01", "2026-01-31", 1, 10, "C", "2026-01-01", "2026-01-31", 1, 10, 101])

		with patch.object(FRAPPE, "get_cached_value", return_value=(2, 3), create=True):
			sql_leaf, params_leaf = build_detail_preflight_query(base_filters, cap=100)
		self.assertIn("`sii`.`warehouse` = %s", sql_leaf)
		self.assertEqual(params_leaf, ["C", "2026-01-01", "2026-01-31", "W-PARENT", "C", "2026-01-01", "2026-01-31", "W-PARENT", 101])

	def test_get_summary_columns_wraps_plain_dict_and_fallback_matches_stock_metadata(self):
		seen = {}

		class FakeStandard:
			@staticmethod
			def get_columns(group_wise, filters):
				seen["group_by_attr"] = filters.group_by
				seen["group_by_dict"] = filters.get("group_by")
				return [{"fieldname": "monthly"}]

		fake_fgp = types.SimpleNamespace(
			standard_gross_profit=FakeStandard(),
			_group_wise_columns=lambda: {"monthly": ["monthly"]},
		)
		with patch.object(query, "_fast_gross_profit_module", return_value=fake_fgp):
			cols = query.get_summary_columns({"group_by": "Monthly"})
		self.assertEqual(cols, [{"fieldname": "monthly"}])
		self.assertEqual(seen, {"group_by_attr": "Monthly", "group_by_dict": "Monthly"})

		wh_cols = query._fallback_columns("Warehouse")
		self.assertEqual(
			wh_cols[0],
			{"fieldname": "warehouse", "label": "Warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 100},
		)
		self.assertEqual(wh_cols[1]["width"], 80)
		self.assertEqual(wh_cols[4]["options"], "currency")
		self.assertEqual(wh_cols[-1], {"fieldname": "currency", "label": "Currency", "fieldtype": "Link", "options": "Currency", "hidden": 1})

	def test_format_summary_groups_uses_flt_and_matches_stock_total_math(self):
		flt_calls = []

		def fake_flt(val, precision=None):
			flt_calls.append(precision)
			return round(float(val), precision) if precision is not None else float(val)

		utils_mod = types.ModuleType("frappe.utils")
		utils_mod.flt = fake_flt
		filters = {"group_by": "Warehouse", "from_date": "2026-01-01", "to_date": "2026-01-31", "currency": "USD"}
		rows = [
			contribution("A", item_row="R1", warehouse="W1", qty=3.0, base_amount=10.004, buying_amount=6.001),
			contribution("B", item_row="R2", warehouse="W2", qty=2.0, base_amount=20.004, buying_amount=12.001),
		]
		agg = query.aggregate_summary_rows([], rows, filters, currency_precision=2, float_precision=2, warehouse_scope=set())
		with patch.dict(sys.modules, {"frappe.utils": utils_mod}):
			data = query.format_summary_groups(agg["groups"], filters, currency_precision=2, float_precision=2)

		self.assertIn(2, flt_calls)
		self.assertIn(None, flt_calls)
		# Per-group rounded values sum into Total (10.0 + 20.0 = 30.0, 6.0 + 12.0 = 18.0)
		self.assertEqual(data[-1]["warehouse"], "Total")
		self.assertEqual(data[-1]["selling_amount"], 30.0)
		self.assertEqual(data[-1]["buying_amount"], 18.0)
		self.assertEqual(data[-1]["gross_profit"], 12.0)
		self.assertEqual(data[-1]["gross_profit_%"], 40.0)


class TestTriggersAndStore(unittest.TestCase):
	def test_trigger_ddl_names_conditions_and_dirty_range_insert_are_frozen(self):
		self.assertEqual(
			indexes.REQUIRED_RIV_TRIGGERS,
			(
				"khitmee_riv_after_insert_dirty_range",
				"khitmee_riv_after_update_dirty_range",
				"khitmee_riv_before_delete_dirty_range",
			),
		)
		insert_sql = indexes.REPOST_TRIGGER_DDLS["khitmee_riv_after_insert_dirty_range"]
		update_sql = indexes.REPOST_TRIGGER_DDLS["khitmee_riv_after_update_dirty_range"]
		delete_sql = indexes.REPOST_TRIGGER_DDLS["khitmee_riv_before_delete_dirty_range"]
		self.assertIn("AFTER INSERT ON `tabRepost Item Valuation`", insert_sql)
		self.assertIn("NEW.`docstatus` = 1 AND NEW.`status` = 'Completed'", insert_sql)
		self.assertIn("NOT (OLD.`docstatus` = 1 AND OLD.`status` = 'Completed')", update_sql)
		self.assertIn("BEFORE DELETE ON `tabRepost Item Valuation`", delete_sql)
		self.assertIn("OLD.`docstatus` = 1 AND OLD.`status` = 'Completed'", delete_sql)
		for sql in (insert_sql, update_sql, delete_sql):
			self.assertIn("INSERT INTO `tabKhitmee Profit Dirty Range`", sql)
			self.assertIn("UUID(), NOW(6), NOW(6), 'Administrator'", sql)
			self.assertIn("'Repost Item Valuation'", sql)

	def test_trigger_verification_queries_active_information_schema_every_time(self):
		class TriggerDB:
			def __init__(self):
				self.calls = 0

			def sql(self, sql, params=None, **kwargs):
				self.calls += 1
				return [("khitmee_riv_after_insert_dirty_range",), ("khitmee_riv_after_update_dirty_range",), ("khitmee_riv_before_delete_dirty_range",)]

		db = TriggerDB()
		with patch.object(indexes.frappe, "db", db):
			self.assertEqual(indexes.verify_repost_triggers_installed(), (True, []))
			self.assertEqual(indexes.verify_repost_triggers_installed(), (True, []))
		self.assertEqual(db.calls, 2)

	def test_summary_schema_ddl_has_exact_decimal_widths_blank_sentinel_and_indexes(self):
		daily = indexes.PROFIT_SUMMARY_TABLE_DDLS["tabDaily Profit by Shop"]
		self.assertIn("`warehouse` varchar(140) NOT NULL DEFAULT ''", daily)
		self.assertIn("`normal_qty` decimal(18, 6) NOT NULL", daily)
		self.assertIn("`normal_abs_qty_sum` decimal(20, 6) NOT NULL", daily)
		self.assertIn("UNIQUE KEY `khitmee_daily_profit_company_date_warehouse_unique` (`company`, `posting_date`, `warehouse`)", daily)
		state = indexes.PROFIT_SUMMARY_TABLE_DDLS["tabKhitmee Profit Day State"]
		self.assertIn("`repost_set_digest` bigint unsigned NOT NULL DEFAULT 0", state)
		dirty = indexes.PROFIT_SUMMARY_TABLE_DDLS["tabKhitmee Profit Dirty Range"]
		self.assertIn("KEY `khitmee_profit_dirty_company_processed_from` (`company`, `processed`, `from_date`)", dirty)

	def test_repost_queries_filter_docstatus_and_required_statuses(self):
		class DB:
			def __init__(self):
				self.statements = []

			def sql(self, sql, params=None, **kwargs):
				self.statements.append(sql)
				if "MAX(`modified`)" in sql:
					return [(0, None, 0)]
				return [(None,)]

		db = DB()
		with patch.object(store.frappe, "db", db):
			self.assertEqual(store.has_blocking_repost("C", "2026-01-01"), (False, None))
			self.assertEqual(store.get_completed_repost_signature("C", "2026-01-01"), (0, None, 0))
		blocking_sql, signature_sql = db.statements
		self.assertIn("`docstatus` = 1", blocking_sql)
		self.assertIn("`status` IN ('Queued', 'In Progress', 'Failed')", blocking_sql)
		self.assertIn("`status` = 'Completed'", signature_sql)
		self.assertIn("BIT_XOR(CRC32(CONCAT(`name`, '|', UNIX_TIMESTAMP(`modified`))))", signature_sql)

	def test_ordered_dirty_day_bump_enforces_ascending_lock_order(self):
		locked = []
		bumped = []
		with patch.object(store, "get_day_state", side_effect=lambda company, day, for_update=False: locked.append((company, day))), patch.object(
			store, "bump_dirty_day", side_effect=lambda company, day: bumped.append((company, day))
		):
			store.bump_dirty_days_ordered([("Z", "2026-01-02"), ("A", "2026-01-03"), ("A", "2026-01-01")])
		self.assertEqual(locked, [("A", "2026-01-01"), ("A", "2026-01-03"), ("Z", "2026-01-02")])
		self.assertEqual(bumped, locked)

	def test_publish_aborts_without_daily_writes_when_trigger_removed_mid_run(self):
		class DB:
			def __init__(self):
				self.statements = []
				self.rollback_calls = 0

			def sql(self, sql, params=None, **kwargs):
				self.statements.append(sql)
				return []

			def rollback(self):
				self.rollback_calls += 1

			def commit(self):
				pass

		db = DB()
		claim = {"company": "C", "posting_date": "2026-01-01", "claim_token": "token", "claim_expires_at": "", "dirty_generation": 2}
		day_state = {"claim_token": "token", "status": "rebuilding", "dirty_generation": 2, "built_generation": 0}
		flags = {"calc_version": 1, "currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)", "is_additive_safe": True}
		with patch.object(store.frappe, "db", db), patch.object(store, "get_day_state", return_value=day_state), patch.object(
			store, "has_blocking_repost", return_value=(False, None)
		), patch.object(store, "verify_repost_triggers_installed", return_value=(False, ["khitmee_riv_after_insert_dirty_range"])):
			result = store.publish_day_snapshot(claim, 2, (0, None, 0), [], [], flags)
		self.assertFalse(result["published"])
		self.assertFalse(any("DELETE FROM `tabDaily Profit by Shop`" in sql for sql in db.statements))
		self.assertFalse(any("INSERT INTO `tabDaily Profit by Shop`" in sql for sql in db.statements))
		self.assertEqual(db.rollback_calls, 1)

	def test_publish_supersedes_on_cas_token_mismatch_without_writes(self):
		class DB:
			def __init__(self):
				self.statements = []

			def sql(self, sql, params=None, **kwargs):
				self.statements.append(sql)
				return []

			def rollback(self):
				pass

			def commit(self):
				pass

		db = DB()
		claim = {"company": "C", "posting_date": "2026-01-01", "claim_token": "old", "claim_expires_at": "", "dirty_generation": 2}
		day_state = {"claim_token": "new", "status": "rebuilding", "dirty_generation": 2, "built_generation": 0}
		flags = {"calc_version": 1, "currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)", "is_additive_safe": True}
		with patch.object(store.frappe, "db", db), patch.object(store, "get_day_state", return_value=day_state):
			result = store.publish_day_snapshot(claim, 2, (0, None, 0), [], [], flags)
		self.assertTrue(result["superseded"])
		self.assertFalse(any("DELETE FROM" in sql for sql in db.statements))

	def test_deadlock_publish_retries_whole_tx3(self):
		claim = {"company": "C", "posting_date": "2026-01-01", "claim_token": "token", "claim_expires_at": "", "dirty_generation": 2}
		flags = {"calc_version": 1, "currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)", "is_additive_safe": True}
		calls = []

		def flaky(*args):
			calls.append(args)
			if len(calls) == 1:
				raise Exception(1213, "deadlock")
			return {"published": True}

		with patch.object(store, "_publish_day_snapshot_once", side_effect=flaky), patch.object(store, "_rollback") as rollback:
			result = store.publish_day_snapshot(claim, 2, (0, None, 0), [], [], flags)
		self.assertEqual(result, {"published": True})
		self.assertEqual(len(calls), 2)
		rollback.assert_called_once()


class TestClaimCursorEligibilityAndHooks(unittest.TestCase):
	def test_claim_sql_expires_stale_claims_locks_in_order_and_uses_five_minute_expiry(self):
		class DB:
			def __init__(self):
				self.statements = []
				self.commits = 0

			def sql(self, sql, params=None, as_dict=False, **kwargs):
				self.statements.append((sql, params))
				if "LIMIT 1" in sql and "FOR UPDATE" in sql:
					return [{"company": "C", "posting_date": "2026-01-01"}]
				if "FROM `tabKhitmee Profit Day State`" in sql and "FOR UPDATE" in sql:
					return [{"company": "C", "posting_date": "2026-01-01", "status": "dirty", "dirty_generation": 2}]
				if "FROM `tabKhitmee Profit Day State`" in sql and "SELECT `company`" not in sql:
					return [{"company": "C", "posting_date": "2026-01-01", "status": "rebuilding", "dirty_generation": 2, "claim_expires_at": "2026-01-01 00:05:00"}]
				if "MIN(`posting_date`)" in sql:
					return [(None,)]
				return []

			def commit(self):
				self.commits += 1

			def rollback(self):
				pass

		db = DB()
		with patch.object(store.frappe, "db", db):
			claim = store.claim_next_dirty_day("C")
		self.assertIsNotNone(claim)
		self.assertEqual(claim["company"], "C")
		joined = "\n".join(sql for sql, _params in db.statements)
		self.assertIn("claim_expires_at` < NOW(6)", joined)
		self.assertIn("ORDER BY `profit_day`.`company` ASC, `profit_day`.`posting_date` ASC", joined)
		self.assertIn("DATE_ADD(NOW(6), INTERVAL 5 MINUTE)", joined)
		self.assertEqual(db.commits, 1)

	def test_dirty_range_cursor_resumes_after_cursor_date_in_ascending_batches(self):
		dirty_range = {
			"name": "DR-1",
			"company": "C",
			"from_date": "2026-01-01",
			"cursor_date": "2026-01-10",
			"processed": 0,
			"source_doctype": "Stock Entry",
			"source_docname": "SE-1",
		}
		bumped = []
		advanced = []
		with patch.object(rebuilder, "_today", return_value="2026-02-01"), patch.object(
			store, "get_unprocessed_dirty_ranges", return_value=[dirty_range]
		), patch.object(store, "bump_dirty_days_ordered", side_effect=lambda keys: bumped.extend(keys)), patch.object(
			store, "advance_dirty_range_cursor", side_effect=lambda name, cursor, processed: advanced.append((name, cursor, processed))
		):
			count = rebuilder.expand_dirty_ranges(batch_days=30)
		self.assertEqual(count, 21)
		self.assertEqual(bumped[0], ("C", "2026-01-11"))
		self.assertEqual(bumped[-1], ("C", "2026-01-31"))
		self.assertEqual(advanced, [("DR-1", "2026-01-31", True)])

	def test_dirty_range_completion_can_mark_equal_cursor_processed(self):
		dirty_range = {
			"name": "DR-DONE",
			"company": "C",
			"from_date": "2026-01-01",
			"cursor_date": "2026-01-31",
			"processed": 0,
			"source_doctype": "Stock Entry",
			"source_docname": "SE-1",
		}
		advanced = []
		with patch.object(rebuilder, "_today", return_value="2026-02-01"), patch.object(
			store, "get_unprocessed_dirty_ranges", return_value=[dirty_range]
		), patch.object(store, "advance_dirty_range_cursor", side_effect=lambda name, cursor, processed: advanced.append((name, cursor, processed))):
			count = rebuilder.expand_dirty_ranges(batch_days=30)
		self.assertEqual(count, 0)
		self.assertEqual(advanced, [("DR-DONE", "2026-01-31", True)])

	def test_company_only_match_condition_proof_is_strict(self):
		self.assertTrue(company_only_match_conditions("", "C"))
		self.assertTrue(company_only_match_conditions("`tabSales Invoice`.`company` = 'C'", "C"))
		self.assertTrue(company_only_match_conditions("company = \"C\"", "C"))
		self.assertFalse(company_only_match_conditions("`tabSales Invoice`.`company` = 'Other'", "C"))
		self.assertFalse(company_only_match_conditions("`tabSales Invoice`.`company` = 'C' AND warehouse = 'W'", "C"))
		self.assertFalse(company_only_match_conditions("`tabSales Invoice Item`.`warehouse` = ''", "C"))

	def test_strict_summary_filter_allowlist_rejects_dimensions_and_bad_grouping(self):
		filters = {"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-31", "group_by": "Warehouse"}
		self.assertTrue(evaluate_summary_eligibility(filters)["eligible"])
		self.assertFalse(evaluate_summary_eligibility(dict(filters, cost_center="CC"))["eligible"])
		self.assertFalse(evaluate_summary_eligibility(dict(filters, group_by="Item Code"))["eligible"])

	def test_can_export_runs_under_bound_user_and_restores_principal(self):
		seen_users = []

		def can_export(doctype, raise_exception=False):
			seen_users.append(FRAPPE.session.user)
			return True

		def set_user(user):
			FRAPPE.session.user = user

		FRAPPE.session.user = "original@example.com"
		with patch.object(FRAPPE.permissions, "can_export", side_effect=can_export), patch.object(FRAPPE, "set_user", side_effect=set_user):
			authorize_profit_request(
				{"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-31"},
				user="requester@example.com",
				require_export=True,
			)
		self.assertEqual(seen_users, ["requester@example.com"])
		self.assertEqual(FRAPPE.session.user, "original@example.com")

	def test_sales_invoice_hook_includes_linked_original_day_and_stock_hook_writes_compact_range(self):
		passed_keys = []
		doc = {"doctype": "Sales Invoice", "company": "C", "posting_date": "2026-01-03", "is_return": 1, "return_against": "ORIG"}
		with patch.object(store.frappe.db, "get_value", return_value="2026-01-01", create=True), patch.object(
			store, "bump_dirty_days_ordered", side_effect=lambda keys: passed_keys.extend(keys)
		):
			store.on_sales_invoice_change(doc)
		self.assertEqual(set(passed_keys), {("C", "2026-01-03"), ("C", "2026-01-01")})

		recorded = []
		with patch.object(store, "record_dirty_range", side_effect=lambda *args: recorded.append(args)):
			store.on_stock_voucher_change({"doctype": "Purchase Invoice", "company": "C", "posting_date": "2026-01-02", "name": "PI-1", "update_stock": 1})
			store.on_stock_voucher_change({"doctype": "Purchase Invoice", "company": "C", "posting_date": "2026-01-02", "name": "PI-2", "update_stock": 0})
		self.assertEqual(recorded, [("C", "2026-01-02", "Purchase Invoice", "PI-1")])

	def test_build_match_conditions_falls_back_to_reportview(self):
		from khitmee_ops.profit_summary import eligibility

		reportview = types.ModuleType("frappe.desk.reportview")
		reportview.build_match_conditions = lambda doctype: "`tabSales Invoice`.`company` = 'C'"
		desk_pkg = types.ModuleType("frappe.desk")
		desk_pkg.reportview = reportview

		class MinimalFrappe:
			permissions = types.SimpleNamespace()

		with patch.object(eligibility, "frappe", MinimalFrappe()), patch.dict(
			sys.modules, {"frappe.desk": desk_pkg, "frappe.desk.reportview": reportview}
		):
			self.assertEqual(
				eligibility.build_match_conditions("Sales Invoice", user="u@example.com"),
				"`tabSales Invoice`.`company` = 'C'",
			)

	def test_store_and_rebuilder_in_predicates_are_batched_to_one_thousand(self):
		chunk_sizes = []

		class ChunkDB:
			def sql(self, sql, params=None, as_dict=False):
				chunk_sizes.append(len(params) - 1)
				if "return_against" in sql:
					return [("ORIG-1",)] if len(params) - 1 == 5 else []
				if "tabDaily Profit by Shop" in sql:
					return [{"company": "C", "posting_date": params[1], "warehouse": "W1"}]
				return [{"company": "C", "posting_date": params[1], "sales_invoice": "SI-R", "reason": "linked_return"}]

		dates = [f"2026-{(idx // 28) + 1:02d}-{(idx % 28) + 1:02d}" for idx in range(1005)]
		invoices = [f"SI-{idx:04d}" for idx in range(1005)]
		db = ChunkDB()
		with patch.object(store.frappe, "db", db), patch.object(rebuilder.frappe, "db", db):
			shop_rows = store.get_shop_day_rows("C", dates)
			replay_rows = store.get_replay_candidates("C", dates)
			originals = rebuilder.find_globally_linked_originals("C", invoices)

		self.assertEqual(chunk_sizes, [1000, 5, 1000, 5, 1000, 5])
		self.assertEqual(len(shop_rows), 2)
		self.assertEqual(len(replay_rows), 2)
		self.assertEqual(originals, {"ORIG-1"})


class TestCleanOnlyQueryIntegration(unittest.TestCase):
	def test_clean_past_only_stored_reads_dirty_replacement_and_future_live_days(self):
		states = {
			"2026-01-01": {
				"status": "clean",
				"built_generation": 2,
				"dirty_generation": 2,
				"repost_completed_count": 0,
				"repost_watermark": None,
				"repost_set_digest": 0,
				"calc_version": 1,
				"currency_precision": 3,
				"float_precision": 2,
				"rounding_method": "Banker's Rounding (legacy)",
				"is_additive_safe": 1,
			},
			"2026-01-02": {
				"status": "clean",
				"built_generation": 2,
				"dirty_generation": 3,
				"repost_completed_count": 0,
				"repost_watermark": None,
				"repost_set_digest": 0,
				"calc_version": 1,
				"currency_precision": 3,
				"float_precision": 2,
				"rounding_method": "Banker's Rounding (legacy)",
				"is_additive_safe": 1,
			},
		}
		flags = {"calc_version": 1, "currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)", "is_additive_safe": True}
		with patch.object(store, "get_day_states", return_value=states), patch.object(
			store, "get_completed_repost_signatures_for_dates", return_value={"2026-01-01": (0, None, 0), "2026-01-02": (0, None, 0)}
		), patch.object(store, "has_blocking_repost", return_value=(False, None)), patch.object(
			store, "get_active_dirty_ranges", return_value=[]
		):
			partition = query.partition_request_days("C", "2026-01-01", "2026-01-04", today="2026-01-03", expected_flags=flags)
		self.assertEqual(partition["clean_past_days"], ["2026-01-01"])
		self.assertEqual(partition["dirty_past_days"], ["2026-01-02"])
		self.assertEqual(partition["missing_days"], [])
		self.assertEqual(partition["live_days"], ["2026-01-02", "2026-01-03", "2026-01-04"])
		self.assertEqual(partition["future_days"], ["2026-01-04"])

	def test_repost_signatures_for_dates_accumulates_prefix_and_replay_cap_fails_fast(self):
		rows = [
			{"posting_date": "2026-01-01", "modified": "2026-01-01 10:00:00", "entry_crc": 11},
			{"posting_date": "2026-01-03", "modified": "2026-01-03 12:00:00", "entry_crc": 22},
		]

		class SigDB:
			def sql(self, sql, params=None, as_dict=False):
				return rows

		with patch.object(store.frappe, "db", SigDB()):
			sigs = store.get_completed_repost_signatures_for_dates(
				"C", ["2026-01-01", "2026-01-02", "2026-01-03"]
			)
		self.assertEqual(sigs["2026-01-01"], (1, "2026-01-01 10:00:00", 11))
		self.assertEqual(sigs["2026-01-02"], (1, "2026-01-01 10:00:00", 11))
		self.assertEqual(sigs["2026-01-03"], (2, "2026-01-03 12:00:00", 11 ^ 22))

		filters = {"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-02", "group_by": "Warehouse"}
		partition = {
			"clean_past_days": ["2026-01-01"],
			"dirty_past_days": [],
			"missing_days": [],
			"live_days": [],
			"future_days": [],
			"has_blocking_repost": False,
			"day_states": {},
		}
		replay_rows = [{"sales_invoice": f"SI-{i}"} for i in range(1001)]
		with patch.object(query, "authorize_profit_request"), patch.object(
			store, "verify_repost_triggers_installed", return_value=(True, [])
		), patch.object(
			query,
			"evaluate_summary_eligibility",
			return_value={"eligible": True, "reason": "eligible", "match_conditions_safe": True, "has_blocking_repost": False, "dirty_days": [], "replay_invoices": 0},
		), patch.object(query, "partition_request_days", return_value=partition), patch.object(
			store, "get_shop_day_rows", return_value=[]
		), patch.object(store, "get_replay_candidates", return_value=replay_rows), patch.object(
			replay, "collect_replay_contributions", side_effect=AssertionError("must fail fast before replay collect")
		):
			result = query.query_profit(filters)
		self.assertEqual(result["mode"], "budget_exceeded")
		self.assertEqual(result["message"], "clean_replay_invoices_exceeds_cap")
		self.assertEqual(result["eligibility"]["replay_invoices"], 1001)

	def test_query_reads_stored_tables_only_for_clean_days_and_collects_live_range(self):
		filters = {"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-04", "group_by": "Warehouse", "include_returned_invoices": 1, "currency": "USD"}
		partition = {
			"clean_past_days": ["2026-01-01"],
			"dirty_past_days": ["2026-01-02"],
			"missing_days": [],
			"live_days": ["2026-01-02", "2026-01-03", "2026-01-04"],
			"future_days": ["2026-01-04"],
			"has_blocking_repost": False,
			"day_states": {},
		}
		stored = empty_shop_row()
		stored.update(
			{
				"normal_has_rows": 1,
				"normal_qty": "1",
				"normal_base_amount": "100",
				"normal_buying_amount": "60",
				"normal_abs_qty_sum": "1",
				"normal_abs_base_sum": "100",
				"normal_abs_buying_sum": "60",
				"normal_first_seen_key": "1|2026-01-01|100000000000",
			}
		)
		read_dates = {}
		collect_calls = []

		class FakeFast:
			standard_gross_profit = None

			def collect_profit_contributions(self, live_filters, *, invoice_ids=None):
				collect_calls.append((dict(live_filters), invoice_ids))
				day = live_filters["from_date"]
				return batch([contribution("LIVE", posting_date=day, first_seen_key=f"1|{day}|100000000000")])

		def fake_shop_rows(company, dates):
			read_dates["shop"] = list(dates)
			return [stored] if dates else []

		def fake_replay_rows(company, dates):
			read_dates["replay"] = list(dates)
			return []

		with patch.object(query, "authorize_profit_request", return_value=None), patch.object(
			query, "evaluate_summary_eligibility",
			return_value={"eligible": True, "reason": "eligible", "match_conditions_safe": True, "has_blocking_repost": False, "dirty_days": [], "replay_invoices": 0},
		), patch.object(store, "verify_repost_triggers_installed", return_value=(True, [])), patch.object(
			store, "current_day_flags", return_value={"calc_version": 1, "currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)", "is_additive_safe": True}
		), patch.object(query, "partition_request_days", return_value=partition), patch.object(
			store, "get_shop_day_rows", side_effect=fake_shop_rows
		), patch.object(store, "get_replay_candidates", side_effect=fake_replay_rows), patch.object(
			query, "_fast_gross_profit_module", return_value=FakeFast()
		), patch.object(query, "_site_today", return_value="2026-01-03"):
			result = query.query_profit(filters)

		self.assertEqual(result["mode"], "summary")
		self.assertEqual(read_dates["shop"], ["2026-01-01"])
		self.assertEqual(read_dates["replay"], ["2026-01-01"])
		self.assertEqual(len(collect_calls), 1)
		self.assertEqual(collect_calls[0][0]["from_date"], "2026-01-02")
		self.assertEqual(collect_calls[0][0]["to_date"], "2026-01-04")
		self.assertIn("W1", [row.get("warehouse") for row in result["data"]])
		self.assertEqual(result["data"][-1]["qty"], None)
		self.assertEqual(result["data"][-1]["avg._selling_rate"], None)
		self.assertEqual(result["data"][-1]["valuation_rate"], None)

	def test_live_set_b_invoice_ids_are_union_replayed_and_not_double_counted(self):
		filters = {"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-01", "group_by": "Warehouse", "currency": "USD"}
		partition = {
			"clean_past_days": [],
			"dirty_past_days": ["2026-01-01"],
			"missing_days": [],
			"live_days": ["2026-01-01"],
			"future_days": [],
			"has_blocking_repost": False,
			"day_states": {},
		}
		replay_calls = []

		class FakeFast:
			standard_gross_profit = None

			def collect_profit_contributions(self, live_filters, *, invoice_ids=None):
				if invoice_ids is not None:
					replay_calls.append(tuple(invoice_ids))
					return batch([contribution("BAD", update_stock=0)])
				return batch([contribution("BAD", update_stock=0)], {"BAD": "non_update_stock"})

		fake_fast = FakeFast()
		with patch.object(query, "authorize_profit_request", return_value=None), patch.object(
			query, "evaluate_summary_eligibility",
			return_value={"eligible": True, "reason": "eligible", "match_conditions_safe": True, "has_blocking_repost": False, "dirty_days": [], "replay_invoices": 0},
		), patch.object(store, "verify_repost_triggers_installed", return_value=(True, [])), patch.object(
			store, "current_day_flags", return_value={"calc_version": 1, "currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)", "is_additive_safe": True}
		), patch.object(query, "partition_request_days", return_value=partition), patch.object(
			store, "get_shop_day_rows", return_value=[]
		), patch.object(store, "get_replay_candidates", return_value=[]), patch.object(
			query, "_fast_gross_profit_module", return_value=fake_fast
		), patch.object(replay, "_fast_gross_profit_module", return_value=fake_fast), patch.object(
			query, "_site_today", return_value="2026-01-02"
		):
			result = query.query_profit(filters)

		self.assertEqual(result["mode"], "summary")
		self.assertEqual(replay_calls, [("BAD",)])
		self.assertEqual(result["data"][0]["selling_amount"], 100.0)
		self.assertEqual(result["eligibility"]["replay_invoices"], 1)

	def test_query_fails_closed_to_pending_when_trigger_is_missing_on_active_db(self):
		filters = {"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-31", "group_by": "Warehouse"}
		with patch.object(query, "authorize_profit_request", return_value=None), patch.object(
			query, "evaluate_summary_eligibility",
			return_value={"eligible": True, "reason": "eligible", "match_conditions_safe": True, "has_blocking_repost": False, "dirty_days": [], "replay_invoices": 0},
		), patch.object(store, "verify_repost_triggers_installed", return_value=(False, ["khitmee_riv_after_insert_dirty_range"])):
			result = query.query_profit(filters)
		self.assertEqual(result["mode"], "pending")
		self.assertIn("missing_repost_triggers", result["eligibility"]["reason"])


class TestPr12ReviewFixes(unittest.TestCase):
	FLAGS = {"calc_version": 1, "currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)", "is_additive_safe": True}

	def test_blocking_repost_covers_live_and_missing_days(self):
		# Codex P1: a today-only / no-state request skipped the repost check entirely.
		with patch.object(store, "get_day_states", return_value={}), patch.object(
			store, "has_blocking_repost", return_value=(True, "2026-01-03")
		) as blocking, patch.object(store, "get_active_dirty_ranges", return_value=[]):
			partition = query.partition_request_days("C", "2026-01-03", "2026-01-03", today="2026-01-03", expected_flags=self.FLAGS)
		blocking.assert_called_once_with("C", "2026-01-03")
		self.assertTrue(partition["has_blocking_repost"])

	def test_repost_after_requested_days_does_not_block(self):
		with patch.object(store, "get_day_states", return_value={}), patch.object(
			store, "has_blocking_repost", return_value=(False, None)
		), patch.object(store, "get_active_dirty_ranges", return_value=[]):
			partition = query.partition_request_days("C", "2026-01-01", "2026-01-02", today="2026-01-05", expected_flags=self.FLAGS)
		self.assertFalse(partition["has_blocking_repost"])
		self.assertEqual(partition["missing_days"], ["2026-01-01", "2026-01-02"])

	def test_too_many_missing_days_returns_pending_before_live_extraction(self):
		# Codex/Devin: oversized requests must not extract live contributions first.
		filters = {"company": "C", "from_date": "2025-01-01", "to_date": "2025-12-31", "group_by": "Warehouse"}
		days = [f"2025-01-{day:02d}" for day in range(1, 31)]
		partition = {
			"clean_past_days": [], "dirty_past_days": [], "missing_days": days, "live_days": days,
			"future_days": [], "has_blocking_repost": False, "day_states": {},
		}
		with patch.object(query, "partition_request_days", return_value=partition), patch.object(
			query, "_collect_live_batches", side_effect=AssertionError("must not extract live rows")
		), patch.object(store, "get_replay_candidates", return_value=[]), patch.object(
			store, "get_shop_day_rows", side_effect=AssertionError("must not read stored rows")
		):
			result = query._query_summary_in_snapshot(filters, {"eligible": True})
		self.assertEqual(result["mode"], "pending")
		self.assertEqual(result["message"], "too_many_dirty_or_missing_summary_days")

	def test_replay_cap_wins_over_dirty_day_pending(self):
		# Devin: rebuilding dirty days cannot fix a replay-cap breach, so report it first.
		filters = {"company": "C", "from_date": "2025-01-01", "to_date": "2025-01-31", "group_by": "Warehouse"}
		days = [f"2025-01-{day:02d}" for day in range(1, 31)]
		partition = {
			"clean_past_days": ["2025-01-31"], "dirty_past_days": days, "missing_days": [], "live_days": days,
			"future_days": [], "has_blocking_repost": False, "day_states": {},
		}
		replay_rows = [{"sales_invoice": f"SI-{i}"} for i in range(1001)]
		with patch.object(query, "partition_request_days", return_value=partition), patch.object(
			store, "get_replay_candidates", return_value=replay_rows
		), patch.object(query, "_collect_live_batches", side_effect=AssertionError("must not extract")):
			result = query._query_summary_in_snapshot(filters, {"eligible": True})
		self.assertEqual(result["mode"], "budget_exceeded")
		self.assertEqual(result["message"], "clean_replay_invoices_exceeds_cap")

	def test_expansion_honours_deadline(self):
		ranges = [{"name": "R1", "company": "C", "from_date": "2026-01-01", "cursor_date": None}]
		with patch.object(store, "get_unprocessed_dirty_ranges", return_value=ranges), patch.object(
			rebuilder, "_today", return_value="2026-02-01"
		), patch.object(store, "bump_dirty_days_ordered", side_effect=AssertionError("expanded past deadline")), patch.object(
			rebuilder.time, "monotonic", return_value=500.0
		):
			self.assertEqual(rebuilder.expand_dirty_ranges(deadline=100.0), 0)

	def test_backfill_starts_at_fiscal_year_start(self):
		ensured = []
		with patch.object(rebuilder, "_today", return_value="2026-01-08"), patch.object(
			rebuilder, "_company_names", return_value=["C"]
		), patch.object(store, "current_day_flags", return_value=self.FLAGS), patch.object(
			store, "get_history_start", return_value="2026-01-05"
		), patch.object(rebuilder, "_fiscal_year_start", return_value="2026-01-03"), patch.object(
			store, "get_day_states", return_value={}
		), patch.object(store, "ensure_day_state", side_effect=lambda company, day: ensured.append(day)), patch.object(
			store, "get_stale_flag_days", return_value=[]
		):
			rebuilder.register_missing_history()
		self.assertEqual(ensured[0], "2026-01-03")
		self.assertEqual(ensured[-1], "2026-01-07")

	def test_export_rejects_input_over_cap_before_extraction(self):
		# Codex P1: never materialize an unbounded export in the long worker.
		from khitmee_ops.profit_summary import budget, export_service

		with patch.object(budget, "preflight_detail_input_rows", return_value=export_service.MAX_EXPORT_INPUT_ROWS + 1), patch.object(
			export_service, "run_detail_gross_profit", side_effect=AssertionError("must not extract")
		):
			with self.assertRaises(export_service.frappe.ValidationError):
				export_service.write_profit_csv({"company": "C", "group_by": "Invoice"}, "unused.csv")

	def test_precision_beyond_storage_scale_is_not_additive_safe(self):
		# Codex P2: decimal(18, 6) columns would silently round 7+ decimal values.
		row = {
			"sales_invoice": "SI-1", "posting_date": "2026-01-01", "warehouse": "W", "qty": 1, "base_amount": 1,
			"buying_amount": 1, "update_stock": 1, "valuation_source": "sle", "is_precision_safe": True,
		}
		batch = {"contributions": [row], "currency_precision": 7, "float_precision": 2}
		with patch.object(store, "current_day_flags", side_effect=lambda safe=True: {**self.FLAGS, "is_additive_safe": safe}):
			_shop, _replay, flags = rebuilder.partition_day_contributions(batch, "C", "2026-01-01")
		self.assertFalse(flags["is_additive_safe"])

	def test_high_precision_site_goes_to_detail_not_pending(self):
		with patch.object(query, "authorize_profit_request"), patch.object(
			store, "verify_repost_triggers_installed", return_value=(True, [])
		), patch.object(
			query, "evaluate_summary_eligibility", return_value={"eligible": True, "reason": "eligible"}
		), patch.object(store, "current_day_flags", return_value={**self.FLAGS, "float_precision": 7}), patch.object(
			query, "_query_summary_in_snapshot", side_effect=AssertionError("must not use stored summary")
		), patch.object(query, "_run_detail", return_value={"mode": "detail"}) as detail:
			query.query_profit({"company": "C", "from_date": "2026-01-01", "to_date": "2026-01-31", "group_by": "Monthly"})
		self.assertEqual(detail.call_args.kwargs["message"], "precision_exceeds_summary_storage")

	def test_preflight_scopes_selected_invoice_family(self):
		sql, params = build_detail_preflight_query(
			{"company": "C", "from_date": "2026-01-01", "to_date": "2026-12-31", "sales_invoice": "SI-9"}, cap=10
		)
		self.assertIn("(`si`.`name` = %s OR `si`.`return_against` = %s)", sql)
		self.assertEqual(params.count("SI-9"), 4)

	def test_clean_report_without_replay_uses_site_precision(self):
		# Devin: the empty replay batch's placeholder precision must not win.
		with patch.object(store, "current_day_flags", return_value={**self.FLAGS, "currency_precision": 2, "float_precision": 4}):
			self.assertEqual(query._batch_metadata([])[:2], (2, 4))

	def test_same_day_linked_return_becomes_replay_candidate(self):
		replay_rows = [{"company": "C", "posting_date": "2026-01-02", "sales_invoice": "SI-ORIG", "reason": "linked_return_original"}]
		with patch.object(rebuilder, "find_same_day_linked_returns", return_value=["SI-RET"]) as finder:
			rows = rebuilder.add_same_day_linked_return_candidates(replay_rows, "C", "2026-01-02")
		finder.assert_called_once_with("C", "2026-01-02", ["SI-ORIG"])
		self.assertEqual(
			[(row["sales_invoice"], row["reason"]) for row in rows],
			[("SI-ORIG", "linked_return_original"), ("SI-RET", "linked_return")],
		)

	def test_register_missing_history_fills_gaps_and_restales_flag_days(self):
		ensured, bumped = [], []
		with patch.object(rebuilder, "_today", return_value="2026-01-05"), patch.object(
			rebuilder, "_company_names", return_value=["C"]
		), patch.object(store, "current_day_flags", return_value=self.FLAGS), patch.object(
			store, "get_history_start", return_value="2026-01-01"
		), patch.object(store, "get_day_states", return_value={"2026-01-02": {}}), patch.object(
			store, "ensure_day_state", side_effect=lambda company, day: ensured.append(day)
		), patch.object(store, "get_stale_flag_days", return_value=["2026-01-02"]), patch.object(
			store, "bump_dirty_days_ordered", side_effect=lambda keys: bumped.extend(keys)
		):
			result = rebuilder.register_missing_history()
		self.assertEqual(ensured, ["2026-01-01", "2026-01-03", "2026-01-04"])
		self.assertEqual(bumped, [("C", "2026-01-02")])
		self.assertEqual(result, {"registered_days": 3, "restaled_days": 1})

	def test_scheduled_slices_stop_at_time_budget(self):
		claims = iter([{"company": "C", "posting_date": f"2026-01-0{i}"} for i in range(1, 9)])
		clock = iter([0.0, 0.0, 50.0, 130.0, 999.0])
		with patch.object(rebuilder, "_get_lock", return_value=True), patch.object(rebuilder, "_release_lock"), patch.object(
			rebuilder, "expand_dirty_ranges"
		), patch.object(store, "claim_next_dirty_day", side_effect=lambda company=None: next(claims)), patch.object(
			rebuilder, "rebuild_claimed_day"
		), patch.object(rebuilder, "_trim_heap"), patch.object(rebuilder.time, "monotonic", side_effect=lambda: next(clock)):
			processed = rebuilder.process_dirty_slices(max_slices=50, time_budget_seconds=120)
		self.assertEqual(processed, 2)

	def test_report_roles_gate_profit_access(self):
		import khitmee_ops.profit_summary.eligibility as eligibility_module

		fake = SimpleNamespace(
			get_all=lambda doctype, filters=None, fields=None: [{"role": "Accounts Manager"}],
			get_roles=lambda user=None: ["Sales User"],
		)
		with patch.object(eligibility_module, "_require_frappe", return_value=fake):
			self.assertFalse(eligibility_module._report_role_permitted("u"))
		fake.get_roles = lambda user=None: ["Accounts Manager"]
		with patch.object(eligibility_module, "_require_frappe", return_value=fake):
			self.assertTrue(eligibility_module._report_role_permitted("u"))
		with patch.object(eligibility_module, "_require_frappe", return_value=SimpleNamespace()):
			self.assertFalse(eligibility_module._report_role_permitted("u"))

	def test_csv_cells_neutralize_formulas_but_keep_numbers(self):
		from khitmee_ops.profit_summary import export_service

		self.assertEqual(export_service._csv_value("=HYPERLINK(\"x\")"), "'=HYPERLINK(\"x\")")
		self.assertEqual(export_service._csv_value("@SUM(A1)"), "'@SUM(A1)")
		self.assertEqual(export_service._csv_value("-12.5"), "-12.5")
		self.assertEqual(export_service._csv_value("Shop 1"), "Shop 1")
		self.assertEqual(export_service._csv_value(-3), -3)

	def test_reconcile_reports_coverage(self):
		from khitmee_ops.profit_summary import reconcile

		with patch.object(store, "get_reconcile_inputs", return_value=(["2026-01-01"], [], [])), patch.object(
			store, "get_active_dirty_ranges", return_value=[]
		):
			result = reconcile.reconcile_with_coverage("C", "2026-01-01", "2026-01-03")
		self.assertEqual((result["clean_days"], result["total_days"], result["drifts"]), (1, 3, []))
		# Devin: clean days under an active dirty range are not coverage.
		with patch.object(store, "get_reconcile_inputs", return_value=(["2026-01-01", "2026-01-02"], [], [])), patch.object(
			store, "get_active_dirty_ranges", return_value=[{"effective_date": "2026-01-02"}]
		):
			result = reconcile.reconcile_with_coverage("C", "2026-01-01", "2026-01-03")
		self.assertEqual(result["clean_days"], 1)


if __name__ == "__main__":
	unittest.main()
