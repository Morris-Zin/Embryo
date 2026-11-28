import csv
import os
import sqlite3
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))

import khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit as fast_gross_profit
from khitmee_ops.khitmee_ops.report.fast_gross_profit import invoice_batches
from khitmee_ops.profit_summary import export_service
from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import (
	FastGrossProfitGenerator,
	SI_IDX,
	SII_IDX,
	SII_JOIN,
	assemble_sle_window,
	assemble_sle_window_tuples,
	append_tiebreaker_orderby,
	build_last_valuation_rate_batch_query,
	build_matched_sle_query,
	build_pair_exists_query,
	build_predecessor_query,
	build_previous_sle_batch_query,
	build_sales_invoice_voucher_sle_query,
	build_sle_boundary_max_query,
	build_sle_window_query,
	build_voucher_detail_exists_query,
	build_warehouse_covering_drive,
	buying_amount_from_sle_pair,
	choose_fallback_boundary_row,
	choose_live_boundary_row,
	collect_profit_contributions,
	extract_stock_ledger_key,
	first_seen_key_for_row,
	format_invoice_rows_inplace,
	inject_sii_force_index,
	inject_warehouse_covering_drive,
	merge_posting_rows_descending,
	normalize_sle_key,
	positionalize_named_params,
	is_rounding_ambiguous,
	process_sales_invoice_sle_rows,
	sle_window_tuple_to_dict,
)


class TestForceIndexInjection(unittest.TestCase):
	def test_inject_sii_force_index_replaces_the_single_upstream_join(self):
		sql = (
			"SELECT `tabSales Invoice`.`name` FROM `tabSales Invoice` "
			f"{SII_JOIN} `tabSales Invoice Item`.`parent`=`tabSales Invoice`.`name`"
		)

		result = inject_sii_force_index(sql)

		self.assertIn(f"JOIN `tabSales Invoice Item` FORCE INDEX (`{SII_IDX}`) ON", result)
		self.assertEqual(result.count("FORCE INDEX"), 1)

	def test_inject_sii_force_index_fails_closed_for_zero_or_duplicate_joins(self):
		with self.assertRaises(RuntimeError):
			inject_sii_force_index("SELECT * FROM `tabSales Invoice`")

		duplicate = f"{SII_JOIN} a {SII_JOIN} b"
		with self.assertRaises(RuntimeError):
			inject_sii_force_index(duplicate)


def sle_row(
	item_code="ITEM-1",
	warehouse="W1",
	voucher_no="SI-1",
	voucher_detail_no="ROW-1",
	posting_datetime="2026-01-10 10:00:00",
	creation="2026-01-10 10:00:01",
	stock_value=100,
	qty=-1,
	is_cancelled=0,
	company="Company",
):
	return {
		"item_code": item_code,
		"warehouse": warehouse,
		"voucher_type": "Sales Invoice",
		"voucher_no": voucher_no,
		"voucher_detail_no": voucher_detail_no,
		"stock_value": stock_value,
		"qty": qty,
		"posting_datetime": posting_datetime,
		"creation": creation,
		"is_cancelled": is_cancelled,
		"company": company,
	}


class TestSetBasedSleWindowHelpers(unittest.TestCase):
	def test_window_predecessor_comes_from_previous_ascending_row(self):
		older = sle_row(voucher_detail_no="OTHER", posting_datetime="2026-01-10 09:00:00", creation="c1")
		matched = sle_row(posting_datetime="2026-01-10 10:00:00", creation="c2")
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")

		result = assemble_sle_window([key], [older, matched], None)

		self.assertFalse(result["tie"])
		self.assertIs(result["matched"][key], matched)
		self.assertIs(result["predecessors"][key], older)
		self.assertTrue(result["pair_exists"])

	def test_first_window_row_uses_boundary_predecessor(self):
		boundary = sle_row(voucher_detail_no="BOUNDARY", posting_datetime="2026-01-09 23:00:00", creation="c0")
		matched = sle_row(posting_datetime="2026-01-10 10:00:00", creation="c2")
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")

		result = assemble_sle_window([key], [matched], boundary)

		self.assertIs(result["predecessors"][key], boundary)

	def test_last_ascending_duplicate_matches_upstream_first_descending_match(self):
		first = sle_row(posting_datetime="2026-01-10 10:00:00", creation="c1", stock_value=90)
		second = sle_row(posting_datetime="2026-01-10 10:00:00", creation="c2", stock_value=80)
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")

		result = assemble_sle_window([key], [first, second], None)

		self.assertIs(result["matched"][key], second)
		self.assertIs(result["predecessors"][key], first)

	def test_adjacent_window_ordering_tie_defers_to_full_pair_history(self):
		left = sle_row(voucher_detail_no="ROW-1", creation="same")
		right = sle_row(voucher_detail_no="ROW-2", creation="same")
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")

		result = assemble_sle_window([key], [left, right], None)

		self.assertTrue(result["tie"])
		self.assertEqual(result["matched"], {})

	def test_boundary_window_ordering_tie_defers_to_full_pair_history(self):
		boundary = sle_row(voucher_detail_no="BOUNDARY", creation="same")
		matched = sle_row(creation="same")
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")

		result = assemble_sle_window([key], [matched], boundary)

		self.assertTrue(result["tie"])
		self.assertEqual(result["predecessors"], {})

	def test_missing_key_is_not_matched_by_unrelated_window_rows(self):
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "MISSING")
		result = assemble_sle_window([key], [sle_row(voucher_detail_no="OTHER")], None)

		self.assertEqual(result["matched"], {})
		self.assertTrue(result["pair_exists"])

	def test_live_boundary_choice_uses_newest_creation_and_detects_exact_tie(self):
		older = sle_row(creation="c1")
		newer = sle_row(creation="c2", stock_value=80)
		row, tie = choose_live_boundary_row([older, newer], "Company")

		self.assertIs(row, newer)
		self.assertFalse(tie)

		row, tie = choose_live_boundary_row([newer, dict(newer)], "Company")
		self.assertTrue(tie)
		self.assertIsNotNone(row)

	def test_cancelled_or_other_company_rows_at_max_are_not_live_boundary_rows(self):
		cancelled = sle_row(is_cancelled=1)
		other_company = sle_row(company="Other")
		row, tie = choose_live_boundary_row([cancelled, other_company], "Company")

		self.assertIsNone(row)
		self.assertFalse(tie)

	def test_all_cancelled_boundary_uses_fallback_row_and_detects_fallback_tie(self):
		fallback = sle_row(voucher_detail_no="OLD", posting_datetime="2026-01-09 12:00:00", creation="c1")
		row, tie = choose_fallback_boundary_row([fallback])

		self.assertIs(row, fallback)
		self.assertFalse(tie)

		row, tie = choose_fallback_boundary_row([fallback, dict(fallback)])
		self.assertIs(row, fallback)
		self.assertTrue(tie)

	def test_window_sql_is_bounded_live_company_ordered_and_has_no_stock_value_difference_shortcut(self):
		sql, params = build_sle_window_query(
			["ITEM-1"], "W1", "Company", "2026-01-01 00:00:00", "2026-02-01 00:00:00"
		)

		self.assertIn("posting_datetime >= %s AND posting_datetime < %s", sql)
		self.assertIn("is_cancelled = 0 AND company = %s", sql)
		self.assertIn("ORDER BY item_code, warehouse, posting_datetime, creation", sql)
		self.assertNotIn("stock_value_difference", sql)
		self.assertEqual(params, ["ITEM-1", "W1", "2026-01-01 00:00:00", "2026-02-01 00:00:00", "Company"])

	def test_boundary_max_scan_intentionally_ignores_company_and_cancellation(self):
		sql, params = build_sle_boundary_max_query(["ITEM-1"], "W1", "2026-01-01 00:00:00")

		self.assertIn("MAX(posting_datetime) AS pd", sql)
		self.assertNotIn("is_cancelled", sql)
		self.assertNotIn("company = %s", sql)
		self.assertEqual(params, ["ITEM-1", "W1", "2026-01-01 00:00:00"])

	def test_voucher_detail_probe_shape_for_leftover_keys(self):
		sql, params = build_voucher_detail_exists_query(["ROW-1", "ROW-2"], "Company")

		self.assertIn("SELECT DISTINCT voucher_detail_no", sql)
		self.assertIn("voucher_detail_no IN (%s, %s)", sql)
		self.assertEqual(params, ["Company", "ROW-1", "ROW-2"])


class TestLeftoverSalesInvoiceKeyWiring(unittest.TestCase):
	def test_outside_window_key_uses_exact_chunk_and_missing_key_is_none(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = type("Filters", (), {"company": "Company"})()
		generator._matched_sles = {}
		generator._sle_pair_exists = {}

		class FakeDB:
			def sql(self, sql, params, as_dict=False):
				if "SELECT DISTINCT voucher_detail_no" in sql:
					return [{"voucher_detail_no": "OUTSIDE-ROW"}]
				return []

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = type("FakeFrappe", (), {"db": FakeDB()})()
		try:
			fetched_chunks = []
			probed_pairs = []
			generator._fetch_sle_chunk = lambda keys: fetched_chunks.append(list(keys))
			generator._fetch_pair_existence = lambda pairs: probed_pairs.append(set(pairs))
			outside_key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "OUTSIDE-ROW")
			missing_key = normalize_sle_key("ITEM-2", "W1", "Sales Invoice", "SI-1", "MISSING-ROW")

			generator._process_leftover_sales_invoice_keys([outside_key, missing_key])
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(fetched_chunks, [[outside_key]])
		self.assertIsNone(generator._matched_sles[missing_key])
		self.assertEqual(probed_pairs, [{(outside_key[0], outside_key[1]), (missing_key[0], missing_key[1])}])


class TestEliminatedNPlusOnes(unittest.TestCase):
	def test_billed_share_uses_preloaded_delivery_note_qty_and_upstream_fallbacks(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._dn_item_stock_qty = {"DN-ROW-1": 4, "ZERO-ROW": 0}

		self.assertEqual(generator.get_billed_share_of_bundle({"qty": 2, "dn_detail": "DN-ROW-1"}), 0.5)
		self.assertEqual(generator.get_billed_share_of_bundle({"qty": 2, "dn_detail": None}), 1)
		self.assertEqual(generator.get_billed_share_of_bundle({"qty": 2, "dn_detail": "MISSING"}), 1)
		self.assertEqual(generator.get_billed_share_of_bundle({"qty": 2, "dn_detail": "ZERO-ROW"}), 1)

	def test_last_purchase_rate_is_memoized_per_item_project_and_cost_center(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._last_purchase_rate_cache = {}
		calls = []

		def fetch(item_code, row):
			calls.append((item_code, row.get("project"), row.get("cost_center")))
			return 12.5

		generator._fetch_last_purchase_rate = fetch
		row = {"project": "P1", "cost_center": "CC1"}

		self.assertEqual(generator.get_last_purchase_rate("ITEM-1", row), 12.5)
		self.assertEqual(generator.get_last_purchase_rate("ITEM-1", dict(row)), 12.5)
		self.assertEqual(len(calls), 1)

		generator.get_last_purchase_rate("ITEM-1", {"project": "P2", "cost_center": "CC1"})
		self.assertEqual(len(calls), 2)


class TestBoundedStockLedgerSQL(unittest.TestCase):
	def test_matched_query_is_exact_keyed_and_bounded(self):
		keys = [
			normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1"),
			normalize_sle_key("ITEM-2", "W2", "Delivery Note", "DN-1", "ROW-2"),
		]

		sql, params = build_matched_sle_query(keys, "Company")

		self.assertEqual(sql.count("`voucher_detail_no` = %s"), 2)
		self.assertIn("`is_cancelled` = 0", sql)
		self.assertIn("ORDER BY `posting_datetime` DESC, `creation` DESC", sql)
		self.assertEqual(len(params), 1 + (5 * len(keys)))
		self.assertEqual(params[0], "Company")
		self.assertNotIn("SELECT *", sql)

	def test_pair_exists_query_supports_nonempty_pair_miss_edge_case(self):
		sql, params = build_pair_exists_query([("ITEM-1", "W1"), ("ITEM-2", "W2")], "Company")

		self.assertIn("SELECT DISTINCT `item_code`, `warehouse`", sql)
		self.assertEqual(sql.count("`item_code` = %s"), 2)
		self.assertEqual(len(params), 1 + 4)

	def test_predecessor_query_returns_at_most_one_row_per_line_using_mariadb_union_syntax(self):
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")
		matched = {
			key: {"posting_datetime": "2026-11-26 10:00:00", "creation": "2026-11-26 10:00:01"}
		}

		sql, params = build_predecessor_query(matched, [key], "Company")

		self.assertIn("(SELECT", sql)
		self.assertIn("ORDER BY `posting_datetime` DESC, `creation` DESC", sql)
		self.assertIn("LIMIT 1", sql)
		self.assertEqual(sql.count("LIMIT 1"), 1)
		self.assertEqual(len(params), 6)
		self.assertNotIn("ROW_NUMBER", sql)


class TestBuyingAmountDecisionTree(unittest.TestCase):
	def test_matched_sle_with_predecessor_uses_stock_value_difference(self):
		matched = {"stock_value": 80, "qty": 2}
		predecessor = {"stock_value": 100}

		result = buying_amount_from_sle_pair(4, matched, predecessor, lambda: 999)

		self.assertEqual(result, 40)

	def test_nonempty_pair_miss_returns_zero(self):
		result = buying_amount_from_sle_pair(4, None, None, lambda: 999)

		self.assertEqual(result, 0.0)

	def test_missing_predecessor_uses_incoming_rate_fallback(self):
		matched = {"stock_value": 80, "qty": 2}

		result = buying_amount_from_sle_pair(4, matched, None, lambda: 12.5)

		self.assertEqual(result, 50.0)

	def test_zero_predecessor_stock_value_uses_incoming_rate_fallback(self):
		matched = {"stock_value": 80, "qty": 2}

		result = buying_amount_from_sle_pair(4, matched, {"stock_value": 0}, lambda: 12.5)

		self.assertEqual(result, 50.0)


class TestStockLedgerKeyExtraction(unittest.TestCase):
	def test_sales_invoice_key(self):
		row = {
			"update_stock": 1,
			"item_code": "ITEM-1",
			"warehouse": "W1",
			"parenttype": "Sales Invoice",
			"parent": "SI-1",
			"item_row": "ROW-1",
		}

		self.assertEqual(
			extract_stock_ledger_key(row),
			("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1"),
		)

	def test_delivery_note_key_uses_dn_detail_rewrite(self):
		row = {
			"update_stock": 0,
			"dn_detail": "DN-ROW-1",
			"delivery_note": "DN-1",
			"item_code": "ITEM-1",
			"warehouse": "W1",
			"parenttype": "Sales Invoice",
			"parent": "SI-1",
			"item_row": "SI-ROW-1",
		}

		self.assertEqual(
			extract_stock_ledger_key(row),
			("ITEM-1", "W1", "Delivery Note", "DN-1", "DN-ROW-1"),
		)


class AttrDict(dict):
	__getattr__ = dict.get

	def update(self, *args, **kwargs):
		super().update(*args, **kwargs)
		return self


class TestWarehouseCoveringDriveSql(unittest.TestCase):
	def test_prepare_invoice_query_suppresses_sales_invoice_hint_only_for_covering_drive(self):
		class Query:
			def __init__(self):
				self.forced = []

			def force_index(self, index_name):
				self.forced.append(index_name)
				return self

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		query = Query()
		generator._upstream_prepare_invoice_query = lambda: query
		generator.filters = AttrDict(warehouse="W1")

		self.assertIs(generator.prepare_invoice_query(), query)
		self.assertEqual(query.forced, [])

		generator.filters = AttrDict()
		self.assertIs(generator.prepare_invoice_query(), query)
		self.assertEqual(query.forced, [SI_IDX])

	def test_covering_drive_execution_suppresses_outer_sii_hint_and_orders_parameters(self):
		base_sql = (
			"SELECT * FROM `tabSales Invoice` "
			"JOIN `tabSales Invoice Item` ON `tabSales Invoice Item`.`parent`=`tabSales Invoice`.`name` "
			"WHERE `tabSales Invoice`.`company`=%(param1)s "
			"ORDER BY `tabSales Invoice`.`posting_date` DESC, `tabSales Invoice`.`posting_time` DESC"
		)
		captured = []

		class FakeDB:
			def sql(self, sql, params=None, as_dict=False):
				captured.append((sql, params, as_dict))
				return []

		utils_module = types.ModuleType("frappe.query_builder.utils")
		utils_module.prepare_query = lambda query: (base_sql, {"param1": "Company"})
		frappe_module = types.ModuleType("frappe")
		qb_module = types.ModuleType("frappe.query_builder")
		qb_module.__path__ = []
		old_modules = {
			name: sys.modules.get(name)
			for name in ("frappe", "frappe.query_builder", "frappe.query_builder.utils")
		}
		old_frappe = fast_gross_profit.frappe
		sys.modules["frappe"] = frappe_module
		sys.modules["frappe.query_builder"] = qb_module
		sys.modules["frappe.query_builder.utils"] = utils_module
		fast_gross_profit.frappe = types.SimpleNamespace(db=FakeDB())
		try:
			generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
			generator.filters = AttrDict(
				warehouse="W1", company="Company", from_date="2026-01-01", to_date="2026-01-31"
			)
			generator._warehouse_scope = ("W1", ["W1"])
			generator._run_invoice_query(object(), semijoin_kind="normal")
		finally:
			fast_gross_profit.frappe = old_frappe
			for name, module in old_modules.items():
				if module is None:
					sys.modules.pop(name, None)
				else:
					sys.modules[name] = module

		sql, params, as_dict = captured[0]
		self.assertIn("STRAIGHT_JOIN `tabSales Invoice Item` ON `tabSales Invoice Item`.`name` = `sii2`.`name`", sql)
		self.assertIn("STRAIGHT_JOIN `tabSales Invoice` ON `tabSales Invoice`.`name` = `tabSales Invoice Item`.`parent`", sql)
		self.assertNotIn("`tabSales Invoice Item`.`name` IN (", sql)
		self.assertNotIn("JOIN `tabSales Invoice Item` FORCE INDEX", sql)
		self.assertIn("FORCE INDEX (`khitmee_si_co_ds_ret_date`)", sql)
		self.assertIn("FORCE INDEX (`khitmee_sii_parent_warehouse`)", sql)
		self.assertNotIn("`tabSales Invoice`.`name` DESC, `tabSales Invoice Item`.`idx` ASC", sql)
		self.assertTrue(sql.endswith("ORDER BY `si2`.`posting_date` DESC, `si2`.`posting_time` DESC"))
		self.assertEqual(params, ["Company", "Company", "2026-01-01", "2026-01-31", "W1"])
		self.assertTrue(as_dict)

	def test_covering_drive_clause_uses_covering_predicates_and_ordered_params(self):
		clause, params = build_warehouse_covering_drive(
			["W1", "W2"], "Company", "2026-01-01", "2026-01-31"
		)

		self.assertNotIn(" IN (SELECT", clause)
		self.assertIn("`si2`.`company` = %s", clause)
		self.assertIn("`si2`.`docstatus` = 1", clause)
		self.assertIn("`si2`.`is_opening` != 'Yes'", clause)
		self.assertIn("`si2`.`is_return` = 0", clause)
		self.assertIn("`si2`.`posting_date` BETWEEN %s AND %s", clause)
		self.assertIn("`sii2`.`warehouse` IN (%s, %s)", clause)
		self.assertEqual(params, ["Company", "2026-01-01", "2026-01-31", "W1", "W2"])

	def test_covering_drive_return_predicates_match_invoice_and_linked_return_queries(self):
		normal, _ = build_warehouse_covering_drive(
			["W1"], "Company", "2026-01-01", "2026-01-31", include_returned_invoices=True
		)
		linked, _ = build_warehouse_covering_drive(
			["W1"], "Company", "2026-01-01", "2026-01-31", return_kind="return"
		)

		self.assertIn("`si2`.`is_return` = 0 OR (`si2`.`is_return` = 1 AND `si2`.`return_against` IS NULL)", normal)
		self.assertIn("`si2`.`is_return` = 1 AND `si2`.`return_against` IS NOT NULL", linked)

	def test_unlinked_return_covering_predicate_is_index_friendly(self):
		unlinked, _ = build_warehouse_covering_drive(
			["W1"], "Company", "2026-01-01", "2026-01-31", return_kind="unlinked_return"
		)

		self.assertIn("`si2`.`is_return` = 1 AND `si2`.`return_against` IS NULL", unlinked)
		self.assertNotIn("`si2`.`is_return` = 0 OR", unlinked)

	def test_split_invoice_result_groups_are_merged_by_posting_datetime_descending(self):
		normal = [
			{"parent": "SI-NORMAL-NEW", "posting_date": "2026-01-03", "posting_time": "9:00:00"},
			{"parent": "SI-NORMAL-OLD", "posting_date": "2026-01-01", "posting_time": "10:00:00"},
		]
		unlinked = [
			{"parent": "SI-RET-NEWER-TIME", "posting_date": "2026-01-03", "posting_time": "10:00:00"},
			{"parent": "SI-RET-MIDDLE", "posting_date": "2026-01-02", "posting_time": "00:00:00"},
		]

		merged = merge_posting_rows_descending(normal, unlinked)

		self.assertEqual(
			[row["parent"] for row in merged],
			["SI-RET-NEWER-TIME", "SI-NORMAL-NEW", "SI-RET-MIDDLE", "SI-NORMAL-OLD"],
		)

	def test_covering_drive_injects_before_order_by_and_rewrites_it_without_tiebreakers(self):
		sql = (
			"SELECT * FROM `tabSales Invoice` "
			"JOIN `tabSales Invoice Item` ON `tabSales Invoice Item`.`parent`=`tabSales Invoice`.`name` "
			"WHERE `docstatus`=%(param1)s "
			"ORDER BY `tabSales Invoice`.`posting_date` DESC,\n `tabSales Invoice`.`posting_time` DESC;  "
		)
		injected = inject_warehouse_covering_drive(sql, "`si2`.`company` = %s")

		self.assertLess(injected.index("`si2`.`company` = %s"), injected.index("ORDER BY `si2`"))
		self.assertTrue(injected.endswith("ORDER BY `si2`.`posting_date` DESC, `si2`.`posting_time` DESC;"))
		self.assertNotIn("`tabSales Invoice`.`name` DESC", injected)
		positional, params = positionalize_named_params(injected, {"param1": 1})
		self.assertIn("`docstatus`=%s", positional)
		self.assertNotIn("%(param1)s", positional)
		self.assertEqual(params, [1])

	def test_covering_drive_injection_fails_closed_when_anchor_or_order_by_shape_changes(self):
		with self.assertRaises(RuntimeError):
			inject_warehouse_covering_drive("SELECT * FROM `tabSales Invoice`", "`si2`.`company` = %s")

		missing_time = (
			"SELECT * FROM `tabSales Invoice` "
			"JOIN `tabSales Invoice Item` ON `tabSales Invoice Item`.`parent`=`tabSales Invoice`.`name` "
			"ORDER BY `tabSales Invoice`.`posting_date` DESC"
		)
		with self.assertRaises(RuntimeError):
			inject_warehouse_covering_drive(missing_time, "`si2`.`company` = %s")

	def test_tiebreaker_helper_remains_deterministic_for_non_covering_sql(self):
		sql = "SELECT * FROM `tabSales Invoice` WHERE `docstatus`=%(param1)s ORDER BY `posting_date` DESC"
		ordered = append_tiebreaker_orderby(sql)

		self.assertTrue(ordered.endswith("`tabSales Invoice`.`name` DESC, `tabSales Invoice Item`.`idx` ASC"))
		positional, params = positionalize_named_params(ordered, {"param1": 1})
		self.assertIn("`docstatus`=%s", positional)
		self.assertEqual(params, [1])


class TestSingleQueryVoucherSleProcessing(unittest.TestCase):
	def test_voucher_query_is_voucher_scoped_tuple_shape(self):
		sql, params = build_sales_invoice_voucher_sle_query(["SI-1", "SI-2"], "Company")

		self.assertIn("`stock_value_difference`", sql)
		self.assertIn("`voucher_no` IN (%s, %s)", sql)
		self.assertIn("`voucher_type` = 'Sales Invoice'", sql)
		self.assertIn("`company` = %s", sql)
		self.assertEqual(params, ["SI-1", "SI-2", "Company"])

	def test_stock_value_difference_is_used_as_compact_predecessor(self):
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")
		row = ("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1", 80, -20, -2, "2026-01-10 10:00:00", "c1")

		result = process_sales_invoice_sle_rows([row], [key])

		self.assertEqual(result["matched"][key], {"stock_value": 80, "qty": -2})
		self.assertEqual(result["predecessors"][key], {"stock_value": 100})
		self.assertEqual(result["near_zero_keys"], [])
		self.assertIn(("ITEM-1", "W1"), result["pair_exists"])

	def test_near_zero_previous_value_is_delegated_to_exact_fallback(self):
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")
		row = ("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1", 0.00005, 0.0, -2, "pd", "c1")

		result = process_sales_invoice_sle_rows([row], [key])

		self.assertEqual(result["matched"], {})
		self.assertEqual(result["near_zero_keys"], [key])

	def test_rounding_half_way_difference_is_delegated_to_exact_fallback(self):
		# Live case ACC-SINV-2026-89530 / KM-00054: stored predecessor 115.901562 and
		# stock_value 114.689063 give 1.212499 -> 1.212, but the derived difference
		# 1.2125 rounds to 1.213.  Only the real predecessor row is exact.
		key = normalize_sle_key("KM-00054", "W1", "Sales Invoice", "SI-1", "ROW-1")
		row = ("KM-00054", "W1", "Sales Invoice", "SI-1", "ROW-1", 114.689063, -1.2125, -1, "pd", "c1")

		result = process_sales_invoice_sle_rows([row], [key], currency_precision=3)

		self.assertEqual(result["matched"], {})
		self.assertEqual(result["ambiguous_keys"], [key])
		self.assertIn(key, result["matched_keys"])
		# Without a precision the legacy compact path is unchanged.
		legacy = process_sales_invoice_sle_rows([row], [key])
		self.assertIn(key, legacy["matched"])

	def test_process_resolves_currency_precision_before_sle_preload(self):
		# The ambiguity routing silently disables itself without a precision, so
		# process() must set it before the SLE preload runs (live miss on 2026-04-06).
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		seen = []
		generator._preload_delivery_note_item_quantities = lambda: None

		def preload():
			seen.append(getattr(generator, "currency_precision", None))
			raise StopIteration

		generator._preload_stock_ledger_data = preload
		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(
			_dict=AttrDict,
			db=types.SimpleNamespace(get_default=lambda key: 3),
		)
		try:
			with self.assertRaises(StopIteration):
				generator.process()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(seen, [3])

	def test_rounding_ambiguity_only_flags_half_way_points(self):
		self.assertTrue(is_rounding_ambiguous(1.2125, 3))
		self.assertTrue(is_rounding_ambiguous(-25.0565, 3))
		self.assertTrue(is_rounding_ambiguous(1.212499, 3))
		self.assertFalse(is_rounding_ambiguous(1.2124, 3))
		self.assertFalse(is_rounding_ambiguous(40.109, 3))
		self.assertFalse(is_rounding_ambiguous(1.2125, None))

	def test_same_batch_ordering_tie_clears_pair_matches(self):
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")
		rows = [
			("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1", 80, -20, -2, "same", "same"),
			("ITEM-1", "W1", "Sales Invoice", "SI-2", "ROW-2", 70, -10, -1, "same", "same"),
		]

		result = process_sales_invoice_sle_rows(rows, [key])

		self.assertIn(("ITEM-1", "W1"), result["tie_pairs"])
		self.assertEqual(result["matched"], {})

	def test_late_tie_after_lazy_fetch_delegates_with_full_pair_history(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._matched_sles = {}
		generator._sle_predecessors = {}
		generator._tie_pairs = set()
		pair = ("ITEM-1", "W1")

		def fetch(keys):
			generator._tie_pairs.add(pair)
			generator._matched_sles[keys[0]] = {"stock_value": 80, "qty": -1}

		delegated = []
		generator._fetch_sle_chunk = fetch
		generator._pair_history = lambda item_code, warehouse: ["full-history"]
		generator._upstream_calculate_buying_amount_from_sle = lambda *args: delegated.append(args) or "delegated"
		row = AttrDict(qty=1, warehouse="W1")

		result = generator.calculate_buying_amount_from_sle(
			row, [True], "Sales Invoice", "SI-1", "ROW-1", "ITEM-1"
		)

		self.assertEqual(result, "delegated")
		self.assertEqual(delegated[0][1], ["full-history"])


class TestTupleWindowFallback(unittest.TestCase):
	def test_tuple_window_assembly_and_compact_conversion(self):
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")
		older = ("ITEM-1", "W1", "Sales Invoice", "SI-0", "ROW-0", 100, -1, "pd0", "c0")
		matched = ("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1", 80, -2, "pd1", "c1")

		result = assemble_sle_window_tuples([key], [older, matched], None)

		self.assertIs(result["matched"][key], matched)
		compact = sle_window_tuple_to_dict(matched)
		self.assertEqual(compact["stock_value"], 80)
		self.assertEqual(compact["qty"], -2)
		self.assertNotIn("stock_value_difference", compact)


class TestInvoiceModeQueryAndMemoryCuts(unittest.TestCase):
	def test_monthly_process_reuses_formatting_only_for_identical_posting_dates(self):
		class ReportRow(AttrDict):
			__setattr__ = dict.__setitem__

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = AttrDict(group_by="Monthly")
		generator._preload_delivery_note_item_quantities = lambda: None
		generator._preload_stock_ledger_data = lambda: None
		generator.skip_row = lambda row: False
		generator.product_bundles = {}
		generator.bundle_deliveries = {}
		generator.get_buying_amount = lambda row, item_code: 4
		generator.update_return_invoices = lambda *_: None
		generator.is_not_invoice_row = lambda row: True
		generator.get_average_rate_based_on_group_by = lambda: None
		generator.si_list = [ReportRow(item_row=f"ROW-{index}", posting_date=day, qty=1, base_net_amount=10)
			for index, day in enumerate(("2024-12-31", "2024-12-25", "2024-12-31"))]
		labels = {"2024-12-31": "Dec 2025", "2024-12-25": "Dec 2024"}
		frappe_stub = types.SimpleNamespace(_dict=AttrDict, db=types.SimpleNamespace(get_default=lambda key: 2))
		stock_stub = types.SimpleNamespace(scrub=str.lower)
		with patch.object(fast_gross_profit, "frappe", frappe_stub), patch.object(
			fast_gross_profit, "standard_gross_profit", stock_stub
		), patch.object(fast_gross_profit, "formatdate", side_effect=lambda day, pattern: labels[day]) as format_day:
			generator.process()
		self.assertEqual([row.monthly for row in generator.si_list], ["Dec 2025", "Dec 2024", "Dec 2025"])
		self.assertEqual([call.args for call in format_day.call_args_list], [
			("2024-12-31", "MMM YYYY"), ("2024-12-25", "MMM YYYY"),
		])

	def test_invoice_process_skips_grouped_and_invoice_item_amount_allocations(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = AttrDict(group_by="Invoice")
		generator._preload_delivery_note_item_quantities = lambda: None
		generator._preload_stock_ledger_data = lambda: None
		generator.skip_row = lambda row: False
		generator.product_bundles = {}
		generator.bundle_deliveries = {}
		generator.get_buying_amount = lambda row, item_code: 60
		generator.update_return_invoices = lambda row, item_row: None
		generator.is_not_invoice_row = lambda row: row.indent != 0.0
		generator.get_average_rate_based_on_group_by = lambda: (_ for _ in ()).throw(
			AssertionError("Invoice mode must not aggregate unused grouped rows")
		)
		child = AttrDict(
			item_row="ROW-1",
			update_stock=1,
			parenttype="Sales Invoice",
			parent="SI-1",
			dn_detail=None,
			is_debit_note=0,
			item_code="ITEM-1",
			qty=2,
			base_net_amount=100,
			delivered_by_supplier=0,
			indent=1.0,
		)
		header = AttrDict(
			item_row=None,
			update_stock=0,
			parenttype="Sales Invoice",
			parent="SI-1",
			dn_detail=None,
			is_debit_note=0,
			item_code=None,
			qty=0,
			base_net_amount=0,
			delivered_by_supplier=0,
			indent=0.0,
		)
		generator.si_list = [header, child]
		generator._matched_sles = {"stale": {}}
		generator._sle_predecessors = {"stale": {}}
		generator._tie_pair_history = {("ITEM-1", "W1"): []}
		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(
			_dict=AttrDict,
			db=types.SimpleNamespace(get_default=lambda key: 2),
		)
		try:
			generator.process()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(generator.grouped, {})
		self.assertEqual(generator.invoice_item_amounts, {})
		self.assertEqual(child.buying_amount, 60)
		self.assertEqual(header.buying_amount, 60)
		self.assertEqual(header.base_amount, 100)
		self.assertEqual(generator._matched_sles, {})
		self.assertEqual(generator._sle_predecessors, {})
		self.assertEqual(generator._tie_pair_history, {})

	def test_delivery_notes_query_is_skipped_when_database_has_no_submitted_delivery_notes(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._submitted_delivery_note_items = None
		generator._linked_dn_parents = set()
		generator._linked_dn_parent_invoices = set()
		generator.si_list = [
			AttrDict(parent="SI-1", update_stock=1),
			AttrDict(parent="SI-2", update_stock=0, dn_detail="DN-ROW-1"),
		]
		queries = []

		class FakeDB:
			def sql(self, sql, *args, **kwargs):
				queries.append(sql)
				return []

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = type("FakeFrappe", (), {"_dict": AttrDict, "db": FakeDB()})()
		try:
			generator.get_delivery_notes()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(generator.delivery_notes, {})
		self.assertEqual(len(queries), 1)
		self.assertIn("SELECT 1 FROM `tabDelivery Note Item` WHERE docstatus = 1 LIMIT 1", queries[0])

	def test_bundle_delivery_query_is_skipped_without_delivery_note_bundles(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.product_bundles = {}

		generator.load_bundle_deliveries()

		self.assertEqual(generator.bundle_deliveries, {})

	def test_invoice_grouping_allocates_headers_lazily_and_keeps_children_contiguous(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.product_bundles = {"Sales Invoice": {}}
		header_calls = []
		generator.get_invoice_row = lambda row: header_calls.append(row.parent) or AttrDict(
			parent=None, indent=0.0, invoice_or_item=row.parent
		)
		first = AttrDict(parent="SI-1", item_code="ITEM-1")
		second = AttrDict(parent="SI-1", item_code="ITEM-2")
		other = AttrDict(parent="SI-2", item_code="ITEM-3")
		generator.si_list = [first, second, other]

		generator.group_items_by_invoice()

		self.assertEqual(header_calls, ["SI-1", "SI-2"])
		self.assertEqual([row.indent for row in generator.si_list], [0.0, 1.0, 1.0, 0.0, 1.0])
		self.assertEqual([row.get("invoice_or_item") for row in generator.si_list], ["SI-1", "ITEM-1", "ITEM-2", "SI-2", "ITEM-3"])

	def test_invoice_formatting_reuses_row_objects_clears_si_list_and_appends_total(self):
		column_names = {
			"invoice_or_item": "sales_invoice",
			"qty": "qty",
			"base_amount": "selling_amount",
			"buying_amount": "buying_amount",
			"gross_profit": "gross_profit",
			"gross_profit_percent": "gross_profit_%",
		}
		old_standard = fast_gross_profit.standard_gross_profit
		old_frappe = fast_gross_profit.frappe

		class FakeDB:
			@staticmethod
			def get_default(key):
				return 2

		class FakeFrappe:
			_dict = AttrDict
			db = FakeDB()

			@staticmethod
			def get_cached_value(doctype, name, fieldname):
				return "Supplier Name" if doctype == "Buying Settings" else "Customer Name"

		fast_gross_profit.standard_gross_profit = type(
			"FakeStandard", (), {"get_column_names": staticmethod(lambda: column_names)}
		)()
		fast_gross_profit.frappe = FakeFrappe
		try:
			header = AttrDict(
				indent=0.0,
				parent_invoice="",
				invoice_or_item="SI-1",
				qty=None,
				base_amount=0,
				buying_amount=0,
				gross_profit=0,
				gross_profit_percent=0,
				base_net_amount=999,
			)
			child = AttrDict(
				indent=1.0,
				parent_invoice="SI-1",
				invoice_or_item="ITEM-1",
				qty=2,
				base_amount=100,
				buying_amount=60,
				gross_profit=40,
				gross_profit_percent=40,
			)
			gross_profit_data = type("Data", (), {"si_list": [header, child]})()
			filters = AttrDict(currency="USD")
			columns = [{"fieldname": str(index)} for index in range(6)]
			data = []

			format_invoice_rows_inplace(
				columns,
				gross_profit_data,
				filters,
				{"invoice": list(column_names)},
				data,
			)
		finally:
			fast_gross_profit.standard_gross_profit = old_standard
			fast_gross_profit.frappe = old_frappe

		self.assertIs(data[0], header)
		self.assertIs(data[1], child)
		self.assertEqual(gross_profit_data.si_list, [])
		self.assertNotIn("base_amount", child)
		self.assertEqual(child["selling_amount"], 100)
		self.assertEqual(data[-1]["sales_invoice"], "Total")
		self.assertEqual(data[-1]["selling_amount"], 100)
		self.assertEqual(data[-1]["buying_amount"], 60)
		self.assertEqual(columns[0]["fieldname"], "sales_invoice")


class TestPass3DeliveryNoteAndBundleScoping(unittest.TestCase):
	def test_preload_delivery_note_quantities_includes_update_stock_rows(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.si_list = [
			AttrDict(dn_detail="DN-ROW-UPDATE", update_stock=1),
			AttrDict(dn_detail="DN-ROW-LINKED", update_stock=0),
			AttrDict(dn_detail=None, update_stock=0),
		]
		captured = []

		class FakeDB:
			def sql(self, sql, params=None, as_dict=False):
				captured.append((sql, list(params or [])))
				return [AttrDict(name="DN-ROW-UPDATE", stock_qty=4), AttrDict(name="DN-ROW-LINKED", stock_qty=2)]

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(db=FakeDB(), _dict=AttrDict)
		try:
			generator._preload_delivery_note_item_quantities()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(captured[0][1], ["DN-ROW-LINKED", "DN-ROW-UPDATE"])
		self.assertEqual(generator._dn_item_stock_qty, {"DN-ROW-UPDATE": 4, "DN-ROW-LINKED": 2})

	def test_get_delivery_notes_uses_all_invoices_and_memoizes_linked_parent_probe(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._submitted_delivery_note_items = None
		generator._linked_dn_parents = set()
		generator._linked_dn_parent_invoices = set()
		generator.si_list = [
			AttrDict(parent="SI-UPDATE", update_stock=1, dn_detail="DN-ROW-UPDATE"),
			AttrDict(parent="SI-LINKED", update_stock=0, dn_detail="DN-ROW-LINKED"),
			AttrDict(parent="SI-PLAIN", update_stock=0, dn_detail=None),
		]
		distinct_probe_calls = []
		grouped_calls = []

		class FakeDB:
			def sql(self, sql, params=None, as_dict=False):
				if sql.startswith("SELECT 1 FROM `tabDelivery Note Item`"):
					return [(1,)]
				if "SELECT DISTINCT parent" in sql:
					distinct_probe_calls.append(list(params or []))
					return [AttrDict(parent="DN-1")]
				if "GROUP BY si_detail" in sql:
					grouped_calls.append(list(params or []))
					return [AttrDict(si_detail="SI-ROW-1", total_incoming_value=40, total_qty=2)]
				raise AssertionError(f"unexpected SQL: {sql}")

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(db=FakeDB(), _dict=AttrDict)
		try:
			generator.get_delivery_notes()
			generator.get_delivery_notes()
		finally:
			fast_gross_profit.frappe = old_frappe

		expected_invoices = ["SI-LINKED", "SI-PLAIN", "SI-UPDATE"]
		self.assertEqual(distinct_probe_calls, [expected_invoices])
		self.assertEqual(grouped_calls, [expected_invoices, expected_invoices])
		self.assertEqual(generator.delivery_notes["SI-ROW-1"].total_incoming_value, 40)

	def test_load_product_bundle_prefilters_invoices_by_packed_parent_item(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._submitted_delivery_note_items = None
		generator._linked_dn_parents = set()
		generator._linked_dn_parent_invoices = set()
		generator.si_list = [
			AttrDict(parent="SI-BUNDLE", item_code="BUNDLE-PARENT", delivery_note=None),
			AttrDict(parent="SI-NORMAL", item_code="PLAIN-ITEM", delivery_note=None),
			AttrDict(parent="SI-DN", item_code="PLAIN-ITEM", delivery_note="DN-DIRECT"),
		]
		captured = {}

		class FakeDB:
			def sql_list(self, sql, params=None):
				captured["parent_item_sql"] = sql
				return ["BUNDLE-PARENT"]

			def sql(self, sql, params=None, as_dict=False):
				if sql.startswith("SELECT 1 FROM `tabDelivery Note Item`"):
					return [(1,)]
				if "SELECT DISTINCT parent" in sql:
					captured["linked_params"] = list(params or [])
					return [AttrDict(parent="DN-LINKED")]
				if "(-1 * qty) AS total_qty" in sql:
					captured["packed_sql"] = sql
					captured["packed_params"] = list(params or [])
					return [
						AttrDict(
							parenttype="Sales Invoice",
							parent="SI-BUNDLE",
							parent_item="BUNDLE-PARENT",
							item_code="PACKED-1",
							warehouse="W1",
							total_qty=-1,
							rate=10,
							base_amount=-10,
							parent_detail_docname="ROW-1",
							serial_and_batch_bundle=None,
						)
					]
				raise AssertionError(f"unexpected SQL: {sql}")

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(db=FakeDB(), _dict=AttrDict)
		try:
			result = generator.load_product_bundle()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertIn("SELECT DISTINCT parent_item FROM `tabPacked Item`", captured["parent_item_sql"])
		self.assertEqual(captured["linked_params"], ["SI-BUNDLE", "SI-DN", "SI-NORMAL"])
		self.assertEqual(
			captured["packed_params"],
			["SI-BUNDLE", "DN-DIRECT", "DN-LINKED"],
		)
		self.assertNotIn("SI-NORMAL", captured["packed_params"])
		self.assertIn("parenttype = 'Sales Invoice' AND parent IN (%s)", captured["packed_sql"])
		self.assertIn("parenttype = 'Delivery Note' AND parent IN (%s, %s)", captured["packed_sql"])
		self.assertEqual(result["Sales Invoice"]["SI-BUNDLE"]["BUNDLE-PARENT"][0].item_code, "PACKED-1")

	def test_load_product_bundle_returns_early_when_no_bundle_invoice_or_delivery_note_exists(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._submitted_delivery_note_items = None
		generator._linked_dn_parents = set()
		generator._linked_dn_parent_invoices = set()
		generator.si_list = [AttrDict(parent="SI-PLAIN", item_code="PLAIN", delivery_note=None)]
		queries = []

		class FakeDB:
			def sql_list(self, sql, params=None):
				return []

			def sql(self, sql, params=None, as_dict=False):
				queries.append(sql)
				return []

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(db=FakeDB(), _dict=AttrDict)
		try:
			result = generator.load_product_bundle()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(result, {})
		self.assertFalse(any("(-1 * qty) AS total_qty" in sql for sql in queries))


class FakeCriterion:
	def __init__(self, kind, value=None):
		self.kind = kind
		self.value = value

	def __and__(self, other):
		return FakeCriterion("and", (self, other))

	def __or__(self, other):
		return FakeCriterion("or", (self, other))


class FakeQBField:
	def __init__(self, name):
		self.name = name

	def between(self, start, end):
		return FakeCriterion("between", (self.name, start, end))

	def isin(self, values):
		return FakeCriterion("isin", (self.name, list(values)))

	def notin(self, values):
		return FakeCriterion("notin", (self.name, list(values)))

	def isnull(self):
		return FakeCriterion("isnull", self.name)

	def isnotnull(self):
		return FakeCriterion("isnotnull", self.name)

	def as_(self, alias):
		return (self.name, alias)

	def __eq__(self, other):
		return FakeCriterion("eq", (self.name, other))


class FakeQBTable:
	def __init__(self, table_name):
		self.table_name = table_name
		self.fields = {}

	def __getattr__(self, fieldname):
		if fieldname not in self.fields:
			self.fields[fieldname] = FakeQBField(fieldname)
		return self.fields[fieldname]


class FakeQBQuery:
	def __init__(self, table):
		self.table = table
		self.wheres = []
		self.force_indexes = []

	def force_index(self, index_name):
		self.force_indexes.append(index_name)
		return self

	def inner_join(self, table):
		return self

	def on(self, condition):
		return self

	def select(self, *fields):
		return self

	def where(self, criterion):
		self.wheres.append(criterion)
		return self


class TestPass3ReturnedInvoiceScoping(unittest.TestCase):
	def test_empty_invoice_scope_skips_returned_invoice_query(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.si_list = []
		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(_dict=AttrDict)
		try:
			generator.get_returned_invoice_items()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(generator.returned_invoices, {})
		self.assertEqual(generator.legacy_returned_invoices, {})

	def test_returned_invoice_query_is_scoped_to_selected_parent_invoices(self):
		tables = {}
		queries = []

		def doc_type(name):
			return tables.setdefault(name, FakeQBTable(name))

		def from_(table):
			query = FakeQBQuery(table)
			queries.append(query)
			return query

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.si_list = [AttrDict(parent="SI-2"), AttrDict(parent="SI-1"), AttrDict(parent=None)]
		generator.filters = AttrDict(company="Company", from_date="2026-01-01", to_date="2026-01-31")
		generator._hints_enabled = lambda: True
		generator._run_invoice_query = lambda query: []

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(
			_dict=AttrDict,
			qb=types.SimpleNamespace(DocType=doc_type, from_=from_),
		)
		try:
			generator.get_returned_invoice_items()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(len(queries), 1)
		self.assertEqual(queries[0].force_indexes, [SI_IDX])
		self.assertEqual(queries[0].wheres[1].kind, "isin")
		self.assertEqual(queries[0].wheres[1].value, ("return_against", ["SI-1", "SI-2"]))


class TestFrozenContributionContracts(unittest.TestCase):
	def test_empty_invoice_scope_short_circuits_without_database_or_bench_access(self):
		batch = collect_profit_contributions({"company": "Company"}, invoice_ids=[])

		self.assertEqual(batch["contributions"], [])
		self.assertEqual(batch["replay_reasons_by_invoice"], {})
		self.assertEqual(batch["currency_precision"], 3)
		self.assertEqual(batch["float_precision"], 2)

	def test_contributions_expose_phases_keys_precision_and_replay_reasons(self):
		main = AttrDict(
			parent="SI-MAIN", item_row="ROW-1", posting_date="2026-01-02", posting_time="10:00:00",
			warehouse="W1", is_return=0, return_against=None, update_stock=1, delivered_by_supplier=0,
			qty=2.0, base_amount=100.0, buying_amount=60.0,
			_khitmee_query_phase=1, _khitmee_valuation_source="sle",
		)
		ret = AttrDict(
			parent="SI-RET", item_row="ROW-R", posting_date="2026-01-03", posting_time="11:00:00",
			warehouse="W1", is_return=1, return_against="SI-ORIGINAL", update_stock=1,
			delivered_by_supplier=0, qty=-1.234, base_amount=-50.0, buying_amount=-30.0,
			_khitmee_query_phase=0, _khitmee_valuation_source="average_rate",
		)
		generator = types.SimpleNamespace(si_list=[main, ret], currency_precision=2, float_precision=1)

		batch = fast_gross_profit._build_contribution_batch(generator, {"SI-ORIGINAL"})

		self.assertEqual([row["query_phase"] for row in batch["contributions"]], [1, 0])
		self.assertEqual(batch["contributions"][0]["first_seen_key"], "1|2026-01-02|100000000000")
		self.assertEqual(batch["contributions"][1]["first_seen_key"], "0|2026-01-03|110000000000")
		self.assertEqual(
			first_seen_key_for_row(
				{"posting_date": "2026-01-02", "posting_time": "10:11:12.123456"}, 1
			),
			"1|2026-01-02|101112123456",
		)
		self.assertTrue(batch["contributions"][0]["is_precision_safe"])
		self.assertFalse(batch["contributions"][1]["is_precision_safe"])
		self.assertEqual(batch["replay_reasons_by_invoice"]["SI-RET"], "linked_return")
		self.assertEqual(batch["currency_precision"], 2)

	def test_contributions_ignore_stock_in_place_group_fold_on_first_row(self):
		# Stock get_average_rate_based_on_group_by() mutates the first row of each
		# group into the group total; contributions must keep the line values.
		first = AttrDict(
			parent="SI-1", item_row="ROW-1", posting_date="2026-11-26", posting_time="08:00:00",
			warehouse="Shop 1", is_return=0, return_against=None, update_stock=1, delivered_by_supplier=0,
			qty=1.0, base_amount=10.0, buying_amount=6.0,
			_khitmee_query_phase=1, _khitmee_valuation_source="sle",
		)
		second = AttrDict(
			parent="SI-2", item_row="ROW-2", posting_date="2026-11-26", posting_time="09:00:00",
			warehouse="Dist Hub 1", is_return=0, return_against=None, update_stock=1, delivered_by_supplier=0,
			qty=4.0, base_amount=40.0, buying_amount=30.0,
			_khitmee_query_phase=1, _khitmee_valuation_source="sle",
		)
		first[fast_gross_profit.LINE_SNAPSHOT_FIELD] = (first.qty, first.base_amount, first.buying_amount)
		# Simulate the stock fold into the group's first row.
		first.qty += second.qty
		first.base_amount += second.base_amount
		first.buying_amount += second.buying_amount
		generator = types.SimpleNamespace(si_list=[first, second], currency_precision=3, float_precision=2)

		batch = fast_gross_profit._build_contribution_batch(generator, set())

		by_row = {row["item_row"]: row for row in batch["contributions"]}
		self.assertEqual((by_row["ROW-1"]["qty"], by_row["ROW-1"]["base_amount"], by_row["ROW-1"]["buying_amount"]), (1.0, 10.0, 6.0))
		self.assertEqual((by_row["ROW-2"]["qty"], by_row["ROW-2"]["base_amount"]), (4.0, 40.0))
		self.assertEqual(sum(row["qty"] for row in batch["contributions"]), 5.0)

	def test_collect_scopes_invoice_ids_and_forces_row_preserving_monthly_extraction(self):
		row = AttrDict(
			parent="SI-1", item_row="ROW-1", posting_date="2026-01-02", posting_time="00:00:00",
			warehouse="", is_return=0, return_against=None, update_stock=1, delivered_by_supplier=0,
			qty=1.0, base_amount=10.0, buying_amount=5.0,
			_khitmee_query_phase=1, _khitmee_valuation_source="sle",
		)
		captured = {}

		class FakeGenerator:
			def __init__(self, filters, invoice_ids=None):
				captured["filters"] = filters
				captured["invoice_ids"] = invoice_ids
				self.si_list = [row]
				self.currency_precision = 2
				self.float_precision = 2

		with patch.object(fast_gross_profit, "standard_gross_profit", object()), patch.object(
			fast_gross_profit, "FastGrossProfitGenerator", FakeGenerator
		), patch.object(fast_gross_profit, "_fetch_linked_return_originals", return_value=set()):
			batch = collect_profit_contributions(
				{"company": "Company", "group_by": "Invoice"}, invoice_ids=["SI-1", "SI-1"]
			)

		self.assertEqual(captured["filters"].group_by, "Monthly")
		self.assertEqual(captured["invoice_ids"], ("SI-1",))
		self.assertEqual(len(batch["contributions"]), 1)

	def test_valuation_source_classification_covers_sle_fallbacks_and_special_branches(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator._valuation_source_by_row_id = {}
		generator._tie_pairs = set()
		generator._matched_sles = {}
		generator._sle_predecessors = {}
		generator._last_purchase_rate_cache = {}
		generator.non_stock_items = set()
		generator.delivery_notes = {}
		generator.get_stock_ledger_entries = lambda item, warehouse: [True]
		generator.calculate_buying_amount_from_sle = lambda *args: 40
		generator.get_average_buying_rate = lambda row, item: 7
		generator.get_drop_ship_buying_amount = lambda row: None
		generator.get_buying_amount_from_so_dn = lambda *args: 0
		row = AttrDict(
			parenttype="Sales Invoice", parent="SI-1", item_row="ROW-1", warehouse="W1",
			update_stock=1, dn_detail=None, qty=2, delivered_by_supplier=0,
		)
		key = normalize_sle_key("ITEM", "W1", "Sales Invoice", "SI-1", "ROW-1")
		generator._matched_sles[key] = {"stock_value": 80, "qty": -2}
		generator._sle_predecessors[key] = {"stock_value": 100}
		generator.get_buying_amount(row, "ITEM")
		self.assertEqual(row["_khitmee_valuation_source"], "sle")

		generator._sle_predecessors[key] = {"stock_value": 0}
		generator.get_buying_amount(row, "ITEM")
		self.assertEqual(row["_khitmee_valuation_source"], "average_rate")

		generator._matched_sles[key] = None
		generator.get_buying_amount(row, "ITEM")
		self.assertEqual(row["_khitmee_valuation_source"], "missing_detail_zero")

		dropship = AttrDict(row, delivered_by_supplier=1)
		generator.get_drop_ship_buying_amount = lambda current: 20
		generator.get_buying_amount(dropship, "ITEM")
		self.assertEqual(dropship["_khitmee_valuation_source"], "dropship")

		non_stock = AttrDict(row, delivered_by_supplier=0, project="P1", cost_center=None)
		generator.non_stock_items = {"ITEM"}
		generator.get_stock_ledger_entries = lambda item, warehouse: []
		generator._fetch_last_purchase_rate = lambda item, current: 11
		generator.get_buying_amount(non_stock, "ITEM")
		self.assertEqual(non_stock["_khitmee_valuation_source"], "last_purchase_rate")

		dn_row = AttrDict(row, project=None, delivery_notes=None)
		generator.non_stock_items = set()
		generator.delivery_notes = {"ROW-1": AttrDict(total_qty=2, total_incoming_value=30)}
		generator.get_buying_amount(dn_row, "ITEM")
		self.assertEqual(dn_row["_khitmee_valuation_source"], "dn_valuation")


class TestTwoPhaseInvoiceOrdering(unittest.TestCase):
	def test_main_rows_are_forward_before_unabsorbed_linked_returns(self):
		queries = []

		class InvoiceQuery:
			def where(self, criterion):
				queries.append(criterion)
				return self

		def doc_type(name):
			return FakeQBTable(name)

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = AttrDict(include_returned_invoices=1)
		prepare_calls = []

		def prepare_invoice_query():
			prepare_calls.append(1)
			return InvoiceQuery()

		generator.prepare_invoice_query = prepare_invoice_query

		def run_query(query, semijoin_kind=None):
			if semijoin_kind == "normal":
				return [AttrDict(parent="SI-A", posting_date="2026-01-01", posting_time="10:00:00")]
			if semijoin_kind == "unlinked_return":
				return [AttrDict(parent="SI-Z", posting_date="2026-01-01", posting_time="10:00:00")]
			return [AttrDict(parent="SI-RET", posting_date="2026-01-04", posting_time="10:00:00")]

		generator._run_invoice_query = run_query
		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(qb=types.SimpleNamespace(DocType=doc_type))
		try:
			generator.load_invoice_items()
		finally:
			fast_gross_profit.frappe = old_frappe

		self.assertEqual(len(prepare_calls), 3)
		self.assertEqual([row.parent for row in generator.si_list], ["SI-A", "SI-Z", "SI-RET"])
		self.assertEqual([row._khitmee_query_phase for row in generator.si_list], [1, 1, 0])
		self.assertTrue(all(row._khitmee_first_seen_key.startswith(f"{row._khitmee_query_phase}|") for row in generator.si_list))

	def test_sort_invoice_rows_descending_uses_ascending_tie_break_and_preserves_item_order(self):
		rows = [
			AttrDict(parent="ACC-SINV-2026-317777", posting_date="2026-11-25", posting_time="10:00:00", item_code="ITEM-1"),
			AttrDict(parent="ACC-SINV-2026-317777", posting_date="2026-11-25", posting_time="10:00:00", item_code="ITEM-2"),
			AttrDict(parent="ACC-SINV-2026-317776", posting_date="2026-11-25", posting_time="10:00:00", item_code="ITEM-A"),
			AttrDict(parent="ACC-SINV-2026-317776", posting_date="2026-11-25", posting_time="10:00:00", item_code="ITEM-B"),
			AttrDict(invoice="ACC-SINV-2026-317778", posting_date="2026-11-25", posting_time="10:00:00.000500", item_code="ITEM-NEWER"),
		]

		ordered = fast_gross_profit.sort_invoice_rows_descending(rows)

		self.assertEqual(
			[(row.get("parent") or row.get("invoice"), row.get("item_code")) for row in ordered],
			[
				("ACC-SINV-2026-317778", "ITEM-NEWER"),
				("ACC-SINV-2026-317776", "ITEM-A"),
				("ACC-SINV-2026-317776", "ITEM-B"),
				("ACC-SINV-2026-317777", "ITEM-1"),
				("ACC-SINV-2026-317777", "ITEM-2"),
			],
		)

	def test_group_items_by_invoice_preserves_ascending_tie_break_and_contiguous_children(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.product_bundles = {}
		generator.get_invoice_row = lambda row: AttrDict(
			indent=0.0, parent_invoice="", invoice_or_item=row.parent, posting_date=row.posting_date
		)
		generator.si_list = fast_gross_profit.sort_invoice_rows_descending(
			[
				AttrDict(parent="SI-2", posting_date="2026-11-25", posting_time="10:00:00", item_code="I-2A"),
				AttrDict(parent="SI-1", posting_date="2026-11-25", posting_time="10:00:00", item_code="I-1A"),
				AttrDict(parent="SI-1", posting_date="2026-11-25", posting_time="10:00:00", item_code="I-1B"),
			]
		)

		generator.group_items_by_invoice()

		self.assertEqual(
			[(row.indent, row.invoice_or_item, row.parent_invoice) for row in generator.si_list],
			[
				(0.0, "SI-1", ""),
				(1.0, "I-1A", "SI-1"),
				(1.0, "I-1B", "SI-1"),
				(0.0, "SI-2", ""),
				(1.0, "I-2A", "SI-2"),
			],
		)

	def test_normalize_sle_key_inline_matches_string_coercion_for_all_types(self):
		class StrSub(str):
			pass

		self.assertEqual(
			normalize_sle_key("ITEM-1", None, 0, False, 42),
			("ITEM-1", "", "", "", "42"),
		)
		key = normalize_sle_key(StrSub("ITEM-1"), "", "Sales Invoice", "SI-1", "ROW-1")
		self.assertEqual(key, ("ITEM-1", "", "Sales Invoice", "SI-1", "ROW-1"))
		self.assertTrue(all(type(part) is str for part in key))


class TestReturnedInvoiceContractFixes(unittest.TestCase):
	def test_return_lookup_preserves_per_invoice_row_order_and_visibility_across_batch_sizes(self):
		parents = [f"SI-{index:04d}" for index in range(260)]
		db = sqlite3.connect(":memory:")
		self.addCleanup(db.close)
		db.executescript(
			"""
			CREATE TABLE invoice (name, customer, posting_order);
			CREATE TABLE returned (name, return_against, item_code, sales_invoice_item, qty, base_amount);
			INSERT INTO returned VALUES
				('R1', 'SI-0000', 'ITEM', 'LINE-1', -1, -10.50),
				('R2', 'SI-0000', 'ITEM', 'LINE-1', -2, -21.00),
				('R3', 'SI-0000', 'ITEM', NULL, -1, -10.25),
				('R4', 'SI-0259', 'ITEM', NULL, -1, -10.75),
				('R5', 'SI-0259', 'ITEM', 'LINE-2', -2, -21.50);
			"""
		)
		db.executemany(
			"INSERT INTO invoice VALUES (?, ?, ?)",
			[(name, "restricted" if index < 259 else "hidden", index) for index, name in enumerate(parents)],
		)

		def run_returns(parent_chunk):
			placeholders = ", ".join("?" for _ in parent_chunk)
			rows = db.execute(
				f"SELECT * FROM returned WHERE return_against IN ({placeholders})",
				parent_chunk,
			)
			fields = [description[0] for description in rows.description]
			return [AttrDict(zip(fields, row)) for row in rows]

		batched = fast_gross_profit.batched
		for visibility, expected_parents in (("%", {"SI-0000", "SI-0259"}), ("restricted", {"SI-0000"})):
			generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
			generator.si_list = [
				AttrDict(parent=name)
				for (name,) in db.execute(
					"SELECT name FROM invoice WHERE customer LIKE ? ORDER BY posting_order DESC", (visibility,)
				)
			]
			generator._build_returned_invoice_query = tuple
			generator._run_invoice_query = run_returns
			with patch.object(fast_gross_profit, "frappe", types.SimpleNamespace(_dict=AttrDict)):
				baseline = None
				for size in (len(parents), 16, 64, 256):
					with self.subTest(visibility=visibility, size=size), patch.object(
						fast_gross_profit, "batched", lambda values, _: batched(values, size)
					):
						generator.get_returned_invoice_items()
						actual = (generator.returned_invoices, generator.legacy_returned_invoices)
						if baseline is None:
							baseline = actual
						self.assertEqual(actual, baseline)
						self.assertEqual(set(actual[0]), expected_parents)
						self.assertEqual(set(actual[1]), expected_parents)
						self.assertEqual([row.name for row in actual[0]["SI-0000"]["LINE-1"]], ["R1", "R2"])
						self.assertEqual([row.name for row in actual[1]["SI-0000"]["ITEM"]], ["R3"])

	def test_returned_invoice_parents_are_fetched_in_chunks_of_one_thousand(self):
		queries = []

		def doc_type(name):
			return FakeQBTable(name)

		def from_(table):
			query = FakeQBQuery(table)
			queries.append(query)
			return query

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.si_list = [AttrDict(parent=f"SI-{index:04d}") for index in range(1001)]
		generator.filters = AttrDict(company="Company", from_date="2026-01-01", to_date="2026-01-31")
		generator._hints_enabled = lambda: False
		generator._run_invoice_query = lambda query: []

		old_frappe = fast_gross_profit.frappe
		old_standard = fast_gross_profit.standard_gross_profit
		fast_gross_profit.frappe = types.SimpleNamespace(
			_dict=AttrDict,
			qb=types.SimpleNamespace(DocType=doc_type, from_=from_),
		)
		fast_gross_profit.standard_gross_profit = types.SimpleNamespace(
			build_match_conditions=lambda doctype: (_ for _ in ()).throw(
				AssertionError("in-date return detail lookup must omit match conditions")
			)
		)
		try:
			generator.get_returned_invoice_items()
		finally:
			fast_gross_profit.frappe = old_frappe
			fast_gross_profit.standard_gross_profit = old_standard

		self.assertEqual(len(queries), 2)
		self.assertEqual(len(queries[0].wheres[1].value[1]), 1000)
		self.assertEqual(len(queries[1].wheres[1].value[1]), 1)


class TestPass3ChunkedVoucherSleProcessing(unittest.TestCase):
	def test_cross_chunk_ordering_tie_matches_all_keys_processing_and_drops_partial_matches(self):
		voucher_count = 1001
		si_keys = []
		for index in range(voucher_count):
			voucher = f"SI-{index:04d}"
			si_keys.append(normalize_sle_key("ITEM-TIE", "W1", "Sales Invoice", voucher, f"TIE-ROW-{index:04d}"))
		first_normal_key = normalize_sle_key("ITEM-OK", "W1", "Sales Invoice", "SI-0000", "OK-ROW-0000")
		last_normal_key = normalize_sle_key("ITEM-OK", "W1", "Sales Invoice", "SI-1000", "OK-ROW-1000")
		si_keys.extend([first_normal_key, last_normal_key])

		tie_first = ("ITEM-TIE", "W1", "Sales Invoice", "SI-0000", "TIE-ROW-0000", 90, -10, -1, "same-pd", "same-creation")
		tie_last = ("ITEM-TIE", "W1", "Sales Invoice", "SI-1000", "TIE-ROW-1000", 80, -10, -1, "same-pd", "same-creation")
		normal_first = ("ITEM-OK", "W1", "Sales Invoice", "SI-0000", "OK-ROW-0000", 90, -10, -1, "pd-1", "c-1")
		normal_last = ("ITEM-OK", "W1", "Sales Invoice", "SI-1000", "OK-ROW-1000", 80, -10, -1, "pd-2", "c-2")
		rows_by_voucher = {
			"SI-0000": [tie_first, normal_first],
			"SI-1000": [tie_last, normal_last],
		}
		query_key_sizes = []

		class FakeDB:
			def sql(self, sql, params=None, as_dict=False):
				vouchers = list(params[:-1])
				query_key_sizes.append(len(vouchers))
				return [row for voucher in vouchers for row in rows_by_voucher.get(voucher, [])]

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = AttrDict(company="Company")
		generator._matched_sles = {}
		generator._sle_predecessors = {}
		generator._sle_pair_exists = {}
		generator._tie_pairs = set()
		leftovers = []
		generator._process_leftover_sales_invoice_keys = lambda keys: leftovers.extend(keys)
		generator._fetch_sle_chunk = lambda keys: (_ for _ in ()).throw(AssertionError("no near-zero fallback expected"))

		old_frappe = fast_gross_profit.frappe
		fast_gross_profit.frappe = types.SimpleNamespace(db=FakeDB(), _dict=AttrDict)
		try:
			generator._preload_sales_invoice_sle_by_voucher(si_keys)
		finally:
			fast_gross_profit.frappe = old_frappe

		all_rows = [tie_first, normal_first, tie_last, normal_last]
		all_keys_result = process_sales_invoice_sle_rows(all_rows, si_keys)
		self.assertIn(("ITEM-TIE", "W1"), all_keys_result["tie_pairs"])
		self.assertIn(("ITEM-TIE", "W1"), generator._tie_pairs)
		self.assertEqual(query_key_sizes, [1000, 1])
		self.assertFalse(any(key[0] == "ITEM-TIE" for key in generator._matched_sles))
		self.assertFalse(any(key[0] == "ITEM-TIE" for key in leftovers))
		self.assertEqual(generator._matched_sles[first_normal_key], all_keys_result["matched"][first_normal_key])
		self.assertEqual(generator._matched_sles[last_normal_key], all_keys_result["matched"][last_normal_key])
		self.assertEqual(generator._sle_predecessors[first_normal_key], all_keys_result["predecessors"][first_normal_key])
		self.assertEqual(generator._sle_predecessors[last_normal_key], all_keys_result["predecessors"][last_normal_key])

	def test_process_sales_invoice_sle_rows_keeps_dict_row_compatibility(self):
		key = normalize_sle_key("ITEM-1", "W1", "Sales Invoice", "SI-1", "ROW-1")
		row = {
			"item_code": "ITEM-1",
			"warehouse": "W1",
			"voucher_type": "Sales Invoice",
			"voucher_no": "SI-1",
			"voucher_detail_no": "ROW-1",
			"stock_value": 80,
			"stock_value_difference": -20,
			"actual_qty": -2,
			"posting_datetime": "pd",
			"creation": "c",
		}

		result = process_sales_invoice_sle_rows([row], [key])

		self.assertEqual(result["matched"][key], {"stock_value": 80, "qty": -2})
		self.assertEqual(result["predecessors"][key], {"stock_value": 100})


class TestReplayIndexAndAverageRatePreload(unittest.TestCase):
	def test_prepare_invoice_query_omits_si_force_index_when_invoice_ids_scoped(self):
		class FakeQuery:
			def __init__(self):
				self.indexes = []

			def select(self, *args):
				return self

			def force_index(self, name):
				self.indexes.append(name)
				return self

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = AttrDict(company="Company", warehouse="W1")
		generator._upstream_prepare_invoice_query = lambda: FakeQuery()

		generator._scoped_invoice_ids = ("SI-1",)
		self.assertFalse(generator._hints_enabled())
		scoped_query = generator.prepare_invoice_query()
		self.assertFalse(generator._warehouse_semijoin_enabled())
		self.assertEqual(scoped_query.indexes, [])

		generator._scoped_invoice_ids = None
		generator.filters["warehouse"] = None
		self.assertTrue(generator._hints_enabled())
		full_query = generator.prepare_invoice_query()
		self.assertEqual(full_query.indexes, [SI_IDX])

	def test_previous_sle_and_last_valuation_rate_batch_queries_use_bounded_union_shape(self):
		candidates = [
			{
				"item_code": "ITEM-1",
				"warehouse": "W1",
				"posting_datetime": "2026-01-05 10:00:00",
				"voucher_no": "SI-1",
				"voucher_type": "Sales Invoice",
			},
			{
				"item_code": "ITEM-2",
				"warehouse": "W2",
				"posting_datetime": "2026-01-06 11:00:00",
				"voucher_no": "SI-2",
				"voucher_type": "Sales Invoice",
			},
		]
		prev_sql, prev_params = build_previous_sle_batch_query(candidates)
		self.assertEqual(prev_sql.count("UNION ALL"), 1)
		self.assertIn("ORDER BY `posting_datetime` DESC, `creation` DESC", prev_sql)
		self.assertEqual(
			prev_params,
			["ITEM-1", "W1", "2026-01-05 10:00:00", "ITEM-2", "W2", "2026-01-06 11:00:00"],
		)

		vr_sql, vr_params = build_last_valuation_rate_batch_query(candidates)
		self.assertEqual(vr_sql.count("UNION ALL"), 1)
		self.assertIn("NOT (`voucher_no` = %s AND `voucher_type` = %s)", vr_sql)
		self.assertEqual(
			vr_params,
			[
				"ITEM-1", "W1", "SI-1", "Sales Invoice", "2026-01-05 10:00:00",
				"ITEM-2", "W2", "SI-2", "Sales Invoice", "2026-01-06 11:00:00",
			],
		)

	def test_preload_average_buying_rates_handles_fifo_moving_avg_item_fallback_and_skips_serial_batch(self):
		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = AttrDict(company="Company")
		generator.average_buying_rate = {}
		generator.non_stock_items = set()
		generator.delivery_notes = {}
		generator.product_bundles = {}
		generator.bundle_deliveries = {}
		generator._tie_pairs = set()
		generator._matched_sles = {
			normalize_sle_key("ITEM-FIFO", "W1", "Sales Invoice", "SI-1", "R1"): {"stock_value": 10},
			normalize_sle_key("ITEM-MA", "W1", "Sales Invoice", "SI-2", "R2"): {"stock_value": 10},
			normalize_sle_key("ITEM-FALLBACK", "W1", "Sales Invoice", "SI-3", "R3"): {"stock_value": 10},
			normalize_sle_key("ITEM-SERIAL", "W1", "Sales Invoice", "SI-4", "R4"): {"stock_value": 10},
		}
		generator._sle_predecessors = {}
		generator._sle_pair_exists = {
			("ITEM-FIFO", "W1"): True,
			("ITEM-MA", "W1"): True,
			("ITEM-FALLBACK", "W1"): True,
			("ITEM-SERIAL", "W1"): True,
		}
		generator.si_list = [
			AttrDict(parenttype="Sales Invoice", parent="SI-4", item_row="R4", item_code="ITEM-SERIAL", warehouse="W1", update_stock=1, qty=-1, posting_date="2026-01-04", posting_time="10:00:00"),
			AttrDict(parenttype="Sales Invoice", parent="SI-3", item_row="R3", item_code="ITEM-FALLBACK", warehouse="W1", update_stock=1, qty=-1, posting_date="2026-01-03", posting_time="10:00:00"),
			AttrDict(parenttype="Sales Invoice", parent="SI-2", item_row="R2", item_code="ITEM-MA", warehouse="W1", update_stock=1, qty=-1, posting_date="2026-01-02", posting_time="10:00:00"),
			AttrDict(parenttype="Sales Invoice", parent="SI-1", item_row="R1", item_code="ITEM-FIFO", warehouse="W1", update_stock=1, qty=-2, posting_date="2026-01-01", posting_time="10:00:00"),
		]

		class FakeDB:
			def get_single_value(self, doctype, field):
				return "FIFO"

			def sql(self, sql, params=None, as_dict=False):
				if "FROM `tabItem`" in sql:
					return [
						AttrDict(name="ITEM-FIFO", has_serial_no=0, has_batch_no=0, valuation_method="FIFO", valuation_rate=5.0, standard_rate=0),
						AttrDict(name="ITEM-MA", has_serial_no=0, has_batch_no=0, valuation_method="Moving Average", valuation_rate=7.0, standard_rate=0),
						AttrDict(name="ITEM-FALLBACK", has_serial_no=0, has_batch_no=0, valuation_method="FIFO", valuation_rate=19.5, standard_rate=0),
						AttrDict(name="ITEM-SERIAL", has_serial_no=1, has_batch_no=0, valuation_method="FIFO", valuation_rate=30.0, standard_rate=0),
					]
				if "`stock_queue`" in sql:
					return [
						AttrDict(khitmee_idx=0, stock_queue="[[4, 12.5]]", valuation_rate=12.5),
						AttrDict(khitmee_idx=1, stock_queue="[]", valuation_rate=14.25),
						AttrDict(khitmee_idx=2, stock_queue="[]", valuation_rate=99.0),
					]
				if "`valuation_rate` >=" in sql:
					return []
				return []

		stock_utils = types.ModuleType("erpnext.stock.utils")
		stock_utils._get_fifo_lifo_rate = lambda q, qty, method: float(q[0][1])
		stock_utils.get_combine_datetime = lambda d, t: f"{d} {t}"
		stock_pkg = types.ModuleType("erpnext.stock")
		stock_pkg.utils = stock_utils
		erpnext_pkg = types.ModuleType("erpnext")
		erpnext_pkg.stock = stock_pkg

		with patch.object(fast_gross_profit, "standard_gross_profit", object()), patch.object(
			fast_gross_profit,
			"frappe",
			types.SimpleNamespace(db=FakeDB(), _dict=AttrDict, get_cached_value=lambda *a, **k: None),
		), patch.dict(
			sys.modules,
			{"erpnext": erpnext_pkg, "erpnext.stock": stock_pkg, "erpnext.stock.utils": stock_utils},
		):
			generator._preload_average_buying_rates()

		self.assertEqual(generator.average_buying_rate[("ITEM-FIFO", "W1", "2026-01-01", "10:00:00", None)], 12.5)
		self.assertEqual(generator.average_buying_rate[("ITEM-MA", "W1", "2026-01-02", "10:00:00", None)], 14.25)
		self.assertEqual(generator.average_buying_rate[("ITEM-FALLBACK", "W1", "2026-01-03", "10:00:00", None)], 19.5)
		self.assertNotIn(("ITEM-SERIAL", "W1", "2026-01-04", "10:00:00", None), generator.average_buying_rate)


try:
	import frappe
	from erpnext.accounts.report.gross_profit.gross_profit import execute as standard_execute

	from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import execute as fast_execute

	HAS_BENCH_DATABASE = bool(getattr(frappe, "db", None)) and bool(frappe.db)
except Exception:
	frappe = None
	standard_execute = None
	fast_execute = None
	HAS_BENCH_DATABASE = False


class TestInvoicePackedBundleLimits(unittest.TestCase):
	def setUp(self):
		self.connection = sqlite3.connect(":memory:")
		self.addCleanup(self.connection.close)
		self.connection.executescript("""
			CREATE TABLE `tabPacked Item` (
				parenttype, parent, parent_item, item_code, warehouse, qty, rate,
				parent_detail_docname, serial_and_batch_bundle, docstatus
			);
			CREATE TABLE `tabDelivery Note Item` (
				name, parent, item_code, stock_qty, si_detail, against_sales_invoice, docstatus
			);
		""")
		self.packed_rows_read = []
		frappe_stub = types.SimpleNamespace(
			_dict=AttrDict, db=types.SimpleNamespace(sql=self.sql, get_default=lambda field: 3)
		)
		self.enterContext(patch.object(fast_gross_profit, "frappe", frappe_stub))

	def sql(self, query, params=None, as_dict=False):
		cursor = self.connection.execute(query.replace("%s", "?"), params or ())
		rows = cursor.fetchall()
		if "(-1 * qty) AS total_qty" in query:
			self.packed_rows_read.append((params[0], len(rows)))
		if as_dict:
			columns = [field[0] for field in cursor.description]
			return [AttrDict(zip(columns, row, strict=True)) for row in rows]
		return rows

	def make_generator(self, *, direct=False):
		class InvoiceRow(AttrDict):
			__setattr__ = dict.__setitem__

		generator = FastGrossProfitGenerator.__new__(FastGrossProfitGenerator)
		generator.filters = AttrDict(group_by="Invoice")
		generator._invoice_row_limit = invoice_batches.MAX_SINGLE_FAMILY_ROWS
		generator._valuation_source_by_row_id = {}
		generator.si_list = [InvoiceRow(
			parenttype="Sales Invoice", parent="SI-1", item_row="SI-LINE", item_code="BUNDLE",
			qty=2, base_net_amount=100, dn_detail="DN-LINE" if direct else None,
			delivery_note="DN-1" if direct else None,
		)]
		generator.get_invoice_row = lambda row: InvoiceRow(indent=0, invoice_or_item=row.parent)
		generator._preload_stock_ledger_data = lambda: None
		generator.skip_row = lambda row: False
		generator.is_not_invoice_row = lambda row: row.indent != 0
		generator.update_return_invoices = lambda row, item_row: None
		generator.get_buying_amount = lambda row, item_code: row["qty"] * {"PART-A": 4, "PART-B": 7}[item_code]
		return generator

	def test_one_invoice_line_on_delivery_with_5001_unrelated_packed_items_still_renders(self):
		self.connection.executemany(
			"INSERT INTO `tabPacked Item` VALUES (?, ?, ?, ?, 'W1', 1, 0, ?, NULL, 1)",
			(("Delivery Note", "DN-1", "OTHER-BUNDLE", f"UNRELATED-{i:05}", "DN-OTHER") for i in range(5001)),
		)
		self.connection.executescript("""
			INSERT INTO `tabPacked Item` VALUES
				('Delivery Note', 'DN-1', 'BUNDLE', 'PART-A', 'W1', 2, 4, 'DN-LINE', NULL, 1),
				('Delivery Note', 'DN-1', 'BUNDLE', 'PART-B', 'W1', 4, 7, 'DN-LINE', NULL, 1);
			INSERT INTO `tabDelivery Note Item` VALUES
				('DN-LINE', 'DN-1', 'BUNDLE', 2, 'SI-LINE', 'SI-1', 1),
				('DN-OTHER', 'DN-1', 'OTHER-BUNDLE', 1, 'SI-HIDDEN-LINE', 'SI-1', 1);
		""")
		for direct in (True, False):
			with self.subTest(direct=direct):
				generator = self.make_generator(direct=direct)
				self.packed_rows_read.clear()
				generator.load_product_bundle()
				generator.load_bundle_deliveries()
				generator.group_items_by_invoice()
				generator.process()
				self.assertEqual(self.packed_rows_read, [("Sales Invoice", 0), ("Delivery Note", 2)])
				self.assertEqual(
					[(row.invoice_or_item, row.indent, row.base_amount, row.buying_amount) for row in generator.si_list],
					[("SI-1", 0, 100, 36), ("BUNDLE", 1, 100, 36)],
				)

	def test_actual_invoice_bundle_components_exceeding_family_limit_are_rejected(self):
		self.connection.executemany(
			"INSERT INTO `tabPacked Item` VALUES ('Sales Invoice', 'SI-1', 'BUNDLE', ?, 'W1', 1, 0, 'SI-LINE', NULL, 1)",
			((f"PART-{i:05}",) for i in range(5001)),
		)
		with self.assertRaises(fast_gross_profit.InvoiceBatchTooLarge):
			self.make_generator().load_product_bundle()

	def test_repeated_bundle_code_counts_its_full_group_under_each_invoice_line(self):
		self.connection.executescript("""
			INSERT INTO `tabPacked Item` VALUES
				('Sales Invoice', 'SI-1', 'BUNDLE', 'PART-A', 'W1', 1, 4, 'SI-LINE', NULL, 1),
				('Sales Invoice', 'SI-1', 'BUNDLE', 'PART-B', 'W1', 1, 7, 'SI-LINE-2', NULL, 1);
		""")
		generator = self.make_generator()
		generator._invoice_row_limit = 6
		generator.si_list.append(AttrDict(generator.si_list[0], item_row="SI-LINE-2"))
		generator.load_product_bundle()
		with self.assertRaises(fast_gross_profit.InvoiceBatchTooLarge):
			generator.group_items_by_invoice()

		generator._invoice_row_limit = 7
		generator.get_bundle_item_row = lambda row, item: AttrDict(indent=2, invoice_or_item=item.item_code)
		generator.group_items_by_invoice()
		self.assertEqual(
			[(row.indent, row.invoice_or_item) for row in generator.si_list],
			[(0, "SI-1"), (1, "BUNDLE"), (2, "PART-A"), (2, "PART-B"), (1, "BUNDLE"), (2, "PART-A"), (2, "PART-B")],
		)


class TestInvoicePageOracleParity(unittest.TestCase):
	def test_concatenated_pages_match_unpaged_stock_rows_for_each_actor_and_return_toggle(self):
		# Stock emits main invoices first and then linked returns whose original is
		# outside that actor's visible result. A linked return still adjusts a visible
		# original when the standalone-return toggle is off.
		def family(name, amount, buying, qty):
			return [
				{"sales_invoice": name, "indent": 0, "parent_invoice": "", "selling_amount": amount, "buying_amount": buying},
				{"sales_invoice": "ITEM", "indent": 1, "parent_invoice": name, "qty": qty,
				 "selling_amount": amount, "buying_amount": buying},
			]

		families = {
			"SI-A": family("SI-A", 75, 49, 3),
			"SI-HIDDEN": family("SI-HIDDEN", 50, 31, 2),
			"RET-STANDALONE": family("RET-STANDALONE", -10, -6, -1),
			"RET-HIDDEN-ORIGINAL": family("RET-HIDDEN-ORIGINAL", -25, -15, -1),
		}
		fields = ("sales_invoice", "parent_invoice", "qty", "selling_amount", "buying_amount", "gross_profit_%")
		columns = [{"fieldname": field} for field in fields]
		cases = (
			("Administrator", 0, ("SI-A", "SI-HIDDEN"), 125, 80, 36),
			("Administrator", 1, ("SI-A", "SI-HIDDEN", "RET-STANDALONE"), 115, 74, 35.652),
			("restricted@example.test", 0, ("SI-A", "RET-HIDDEN-ORIGINAL"), 50, 34, 32),
			("restricted@example.test", 1, ("SI-A", "RET-STANDALONE", "RET-HIDDEN-ORIGINAL"), 40, 28, 30),
		)
		db = sqlite3.connect(":memory:")
		self.addCleanup(db.close)
		db.executescript(
			"""
			CREATE TABLE invoice (name, posting_time, is_return, return_against, hidden);
			INSERT INTO invoice VALUES
				('SI-A', 10, 0, NULL, 0),
				('SI-HIDDEN', 10, 0, NULL, 1),
				('RET-STANDALONE', 8, 1, NULL, 0),
				('RET-ABSORBED-A', 9, 1, 'SI-A', 0),
				('RET-HIDDEN-ORIGINAL', 11, 1, 'SI-HIDDEN', 0);
			"""
		)
		session = types.SimpleNamespace(user="Administrator")

		def scoped_keys(filters, invoice_ids, *, count_rows):
			self.assertIsNone(invoice_ids)
			for phase, name in db.execute(
				"""
				WITH visible AS (SELECT * FROM invoice WHERE hidden = 0 OR ? = 'Administrator'),
				main AS (SELECT * FROM visible WHERE is_return = 0 OR (? AND return_against IS NULL))
				SELECT phase, name FROM (
					SELECT 1 AS phase, name, posting_time FROM main
					UNION ALL
					SELECT 0, name, posting_time FROM visible WHERE is_return = 1 AND return_against IS NOT NULL
						AND return_against NOT IN (SELECT name FROM main)
				) ORDER BY phase DESC, posting_time DESC, name ASC
				""",
				(session.user, filters.include_returned_invoices),
			):
				yield phase, name, len(families[name]) if count_rows else 0

		def render_batch(filters, *, invoice_ids, invoice_phase, invoice_row_limit):
			self.assertTrue(all((name != "RET-HIDDEN-ORIGINAL") == (invoice_phase == 1) for name in invoice_ids))
			self.assertLessEqual(len(invoice_ids), 2)
			rows = [dict(row) for name in invoice_ids for row in families[name]]
			buying = sum(row["buying_amount"] for row in rows if row["indent"] == 1)
			selling = sum(row["selling_amount"] for row in rows if row["indent"] == 1)
			return columns, [*rows, fast_gross_profit.invoice_total_row(filters, selling, buying)]

		frappe_stub = types.SimpleNamespace(
			_dict=AttrDict, session=session, db=types.SimpleNamespace(get_default=lambda field: 3)
		)
		with TemporaryDirectory() as directory, patch.object(fast_gross_profit, "frappe", frappe_stub), patch.object(
			fast_gross_profit, "validate_date_range"
		), patch.object(fast_gross_profit, "run_detail_gross_profit", side_effect=render_batch), patch.object(
			invoice_batches, "_invoice_keys", side_effect=scoped_keys
		), patch.object(invoice_batches, "INVOICE_KEY_BATCH_SIZE", 2):
			for actor, include_returns, names, selling, buying, margin in cases:
				with self.subTest(actor=actor, include_returns=include_returns):
					session.user = actor
					filters = {"group_by": "Invoice", "include_returned_invoices": include_returns, "currency": "MMK"}
					stock_rows = [row for name in names for row in families[name]] + [{
						"sales_invoice": "Total", "qty": None, "avg._selling_rate": None, "valuation_rate": None,
						"selling_amount": selling, "buying_amount": buying, "gross_profit": selling - buying,
						"gross_profit_%": margin, "currency": "MMK",
					}]
					for page_size in (2, 3):
						with self.subTest(page_size=page_size), patch.object(invoice_batches, "INVOICE_PAGE_SIZE", page_size):
							paged_rows = []
							for page in range(1, (len(stock_rows) - 1) // page_size + 3):
								for page_columns, rows in invoice_batches.iter_invoice_detail_batches(filters, page=page):
									self.assertEqual(page_columns, columns)
									paged_rows.extend(rows)
							self.assertEqual(paged_rows, stock_rows)

					path = Path(directory) / "stock-parity.csv"
					self.assertEqual(export_service.write_profit_csv(filters, path), len(stock_rows))
					with path.open(newline="", encoding="utf-8") as output:
						reader = csv.DictReader(output)
						self.assertEqual(reader.fieldnames, list(fields))
						decoded = [
							{field: Decimal(value) if index >= 2 and value else value for index, (field, value) in enumerate(row.items())}
							for row in reader
						]
					expected = [
						{field: Decimal(str(row[field])) if index >= 2 and row.get(field) is not None else row.get(field) or ""
						 for index, field in enumerate(fields)}
						for row in stock_rows
					]
					self.assertEqual(decoded, expected)


class TestFastGrossProfitERPNextParity(unittest.TestCase):
	"""Full-site parity harness; pure local unit runs do not have a site database."""

	def test_line_by_line_buying_amounts_and_totals_match_standard_gross_profit(self):
		if not HAS_BENCH_DATABASE:
			return
		company = os.environ.get("KHITMEE_PARITY_COMPANY") or frappe.db.get_value(
			"Company", {"is_group": 0}, "name"
		)
		self.assertTrue(company, "No non-group Company found for parity test")
		from_date = os.environ.get("KHITMEE_PARITY_FROM_DATE") or "2000-01-01"
		to_date = os.environ.get("KHITMEE_PARITY_TO_DATE") or frappe.utils.nowdate()

		base_filters = {
			"company": company,
			"from_date": from_date,
			"to_date": to_date,
			"group_by": os.environ.get("KHITMEE_PARITY_GROUP_BY") or "Invoice",
			"include_returned_invoices": 1,
		}
		standard_columns, standard_data = standard_execute(frappe._dict(base_filters.copy()))
		fast_columns, fast_data = fast_execute(frappe._dict(base_filters.copy()))

		self.assertEqual(len(standard_columns), len(fast_columns))
		self.assertEqual(len(standard_data), len(fast_data))
		compared_rows = 0
		identity_fields = ("invoice_or_item", "sales_invoice", "item_code", "warehouse")
		numeric_fields = ("qty", "base_amount", "buying_amount", "gross_profit")
		for standard_row, fast_row in zip(standard_data, fast_data, strict=True):
			for fieldname in identity_fields:
				if fieldname in standard_row or fieldname in fast_row:
					self.assertEqual(
						standard_row.get(fieldname),
						fast_row.get(fieldname),
						f"{fieldname} mismatch in {standard_row} vs {fast_row}",
					)
			for fieldname in numeric_fields:
				if fieldname in standard_row or fieldname in fast_row:
					self.assertAlmostEqual(
						float(standard_row.get(fieldname) or 0),
						float(fast_row.get(fieldname) or 0),
						places=3,
						msg=f"{fieldname} mismatch in {standard_row} vs {fast_row}",
					)
			compared_rows += 1

		self.assertGreater(compared_rows, 0)


if __name__ == "__main__":
	unittest.main()
