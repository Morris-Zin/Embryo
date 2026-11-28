import datetime
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))


def _ensure_frappe_stubs():
	frappe = sys.modules.get("frappe")
	if frappe is None:
		frappe = types.ModuleType("frappe")
		sys.modules["frappe"] = frappe

	defaults = {
		"ValidationError": type("ValidationError", (Exception,), {}),
		"QueryDeadlockError": type("QueryDeadlockError", (Exception,), {}),
		"PermissionError": PermissionError,
		"_": lambda message, *args: message,
		"session": SimpleNamespace(user="counter@example.com"),
		"has_permission": Mock(return_value=True),
		"get_doc": Mock(),
		"get_cached_value": Mock(return_value=None),
		"get_hooks": lambda *args, **kwargs: {},
		"get_list": lambda *args, **kwargs: [],
		"parse_json": json.loads,
		"throw": lambda message, exc=None: (_ for _ in ()).throw((exc or Exception)(message)),
		"log_error": lambda *args, **kwargs: None,
		"cache": None,
		"flags": SimpleNamespace(),
		"form_dict": {},
		"local": SimpleNamespace(site="test-site", response={}),
		"db": SimpleNamespace(
			exists=lambda *args, **kwargs: True,
			get_value=lambda *args, **kwargs: None,
			get_single_value=lambda *args, **kwargs: None,
			is_duplicate_entry=lambda exc: False,
			sql=lambda *args, **kwargs: [],
			rollback=Mock(),
			set_single_value=lambda *args, **kwargs: None,
			after_commit=SimpleNamespace(add=lambda fn: None),
			after_rollback=SimpleNamespace(add=lambda fn: None),
		),
	}
	for attr, val in defaults.items():
		if not hasattr(frappe, attr):
			setattr(frappe, attr, val)

	if not hasattr(frappe, "whitelist"):
		def whitelist(*args, **kwargs):
			if args and callable(args[0]):
				return args[0]
			return lambda fn: fn

		frappe.whitelist = whitelist

	utils = sys.modules.get("frappe.utils")
	if utils is None:
		utils = types.ModuleType("frappe.utils")
		sys.modules["frappe.utils"] = utils

	def _flt(value, precision=None):
		value = float(value or 0)
		return round(value, precision) if precision is not None else value

	def _add_days(date_str, days):
		base = datetime.date.fromisoformat(str(date_str))
		return (base + datetime.timedelta(days=int(days))).isoformat()

	util_defaults = {
		"cint": lambda value: int(value or 0),
		"cstr": lambda value: "" if value is None else str(value),
		"flt": _flt,
		"nowdate": lambda: "2026-11-26",
		"nowtime": lambda: "12:00:00",
		"random_string": lambda length: "x" * length,
		"add_days": _add_days,
	}
	for attr, val in util_defaults.items():
		if not hasattr(utils, attr):
			setattr(utils, attr, val)


_ensure_frappe_stubs()

from khitmee_ops import hooks
from khitmee_ops.api import stock_locks


class TestStockLocks(unittest.TestCase):
	def setUp(self):
		stock_locks.clear_transaction_stock_locks()

	def tearDown(self):
		stock_locks.clear_transaction_stock_locks()

	def test_plan_canonicalizes_aliases_and_upgrades_all_pairs_for_a_predicted_miss(self):
		rows = [
			(0, "ITEM-1", "WH-B", None, 1, 0, 0, 0, "FIFO", "Co"),
			(1, "ITEM-2", "WH-A", "BIN-C", 1, 0, 0, 0, "FIFO", "Co"),
			(2, "ITEM-1", "WH-A", "BIN-A", 1, 0, 0, 0, "FIFO", "Co"),
		]
		db = SimpleNamespace(sql=lambda *args: rows, get_value=lambda *a: "FIFO", get_single_value=lambda *a, **kw: 0)
		with patch.object(stock_locks.frappe, "db", db):
			plan = stock_locks.plan_stock_locks(
				[("ITEM-2", "WH-A"), ("item-1", "wh-a"), ("ITEM-1", "WH-B"), ("item-1", "wh-a")],
				ordinary_sale=True, company="Co",
			)
		self.assertEqual(plan.items, ("ITEM-1", "ITEM-2"))
		self.assertEqual(plan.pairs, (("ITEM-1", "WH-A"), ("ITEM-1", "WH-B"), ("ITEM-2", "WH-A")))
		self.assertEqual(plan.shared, {"ITEM-2": "FIFO"})
		self.assertEqual(plan.aliases[("item-1", "wh-a")], ("ITEM-1", "WH-A"))

	def test_surprise_miss_aborts_after_all_items_without_promoting_or_extending(self):
		plan = stock_locks.StockLockPlan(
			("A", "B"), (("A", "W"), ("B", "W")), {}, {}, {"A": "FIFO"}, "Co", "FIFO"
		)
		queries = []

		def sql(statement, params=()):
			queries.append((statement, params))
			return [(params[0], 1, 0, 0, 0, "FIFO")] if "`tabItem`" in statement else []

		db = SimpleNamespace(
			sql=sql, after_commit=SimpleNamespace(add=lambda fn: None),
			after_rollback=SimpleNamespace(add=lambda fn: None),
		)
		with patch.object(stock_locks.frappe, "db", db):
			with self.assertRaises(stock_locks.StockLockRestart) as restart:
				stock_locks.acquire_stock_plan(plan)
			with self.assertRaises(stock_locks.StockLockRestart):
				stock_locks.acquire_stock_plan(plan)
		self.assertEqual(restart.exception.items, {"A"})
		self.assertEqual([params for _, params in queries], [("A",), ("B",), ("A", "W")])
		self.assertIn("LOCK IN SHARE MODE", queries[0][0])
		self.assertIn("FOR UPDATE", queries[1][0])
		self.assertIn("FORCE INDEX (`unique_item_warehouse`)", queries[2][0])

	def test_prelocked_submit_cannot_expand_coordinates_or_strengthen_mode(self):
		plan = stock_locks.StockLockPlan(("A",), (("A", "W"),), {}, {}, {"A": "FIFO"}, "Co", "FIFO")
		sql = Mock(side_effect=[[("A", 1, 0, 0, 0, "FIFO")], [("BIN", "A", "W")], []])
		callbacks = []
		db = SimpleNamespace(
			sql=sql, after_commit=SimpleNamespace(add=callbacks.append), after_rollback=SimpleNamespace(add=lambda _: None)
		)
		with (
			patch.object(stock_locks.frappe, "db", db),
			patch.object(stock_locks, "stock_lock_protocol", return_value="adr0003"),
		):
			stock_locks.acquire_stock_plan(plan)
			stock_locks.lock_stock_coordinates([("A", "W")], require_prelocked=True, ordinary_sale=True)
			with self.assertRaises(stock_locks.StockLockRestart):
				stock_locks.lock_stock_coordinates([("A", "W")], require_prelocked=True)
			with self.assertRaises(stock_locks.StockLockRestart):
				stock_locks.lock_stock_coordinates([("A", "OTHER")], require_prelocked=True, ordinary_sale=True)
			self.assertEqual(sql.call_count, 3)
			callbacks[0]()
			self.assertFalse(stock_locks.stock_attempt_pending())

	def test_document_coordinates_include_rejected_and_supplier_stock(self):
		doc = {
			"doctype": "Purchase Receipt",
			"set_warehouse": "ACCEPTED",
			"rejected_warehouse": "REJECTED",
			"supplier_warehouse": "SUPPLIER",
			"items": [{"item_code": "FINISHED", "rejected_qty": 1}],
			"supplied_items": [{"rm_item_code": "COMPONENT"}],
		}
		self.assertEqual(stock_locks.document_item_codes(doc), ["COMPONENT", "FINISHED"])
		self.assertEqual(
			stock_locks.document_bin_pairs(doc),
			[("COMPONENT", "SUPPLIER"), ("FINISHED", "ACCEPTED"), ("FINISHED", "REJECTED")],
		)

	def test_lock_stock_coordinates_locks_sorted_items_before_bins_in_batched_queries(self):
		queries = []

		def fake_sql(sql, params=None, **kwargs):
			queries.append((sql, params))
			if "`tabItem`" in sql:
				return [("ITEM-A",), ("ITEM-B",)]
			if "`tabBin`" in sql:
				return [("BIN-1", "ITEM-A", "WH-1"), ("BIN-2", "ITEM-B", "WH-2")]
			return []

		with patch.object(stock_locks.frappe.db, "sql", side_effect=fake_sql):
			result = stock_locks.lock_stock_coordinates(
				[("ITEM-B", "WH-2"), ("ITEM-A", "WH-1"), ("ITEM-B", "WH-2")]
			)

		self.assertEqual(len(queries), 2)
		item_sql, item_params = queries[0]
		bin_sql, bin_params = queries[1]
		self.assertIn("FROM `tabItem`", item_sql)
		self.assertIn("ORDER BY name", item_sql)
		self.assertIn("FOR UPDATE", item_sql)
		self.assertEqual(item_params, ("ITEM-A", "ITEM-B"))

		self.assertIn("FROM `tabBin`", bin_sql)
		self.assertIn("ORDER BY item_code, warehouse, name", bin_sql)
		self.assertIn("FOR UPDATE", bin_sql)
		self.assertEqual(bin_params, ("ITEM-A", "WH-1", "ITEM-B", "WH-2"))

		self.assertEqual(result["items"], ["ITEM-A", "ITEM-B"])
		self.assertEqual(result["pairs"], [("ITEM-A", "WH-1"), ("ITEM-B", "WH-2")])
		self.assertEqual(len(result["bins"]), 2)

	def test_missing_bin_rows_are_never_rejected(self):
		def fake_sql(sql, params=None, **kwargs):
			if "`tabItem`" in sql:
				return [("ITEM-NEW",)]
			return []

		with patch.object(stock_locks.frappe.db, "sql", side_effect=fake_sql):
			result = stock_locks.lock_stock_coordinates([("ITEM-NEW", "WH-NEW")])

		self.assertEqual(result["items"], ["ITEM-NEW"])
		self.assertEqual(result["bins"], [])
		self.assertEqual(result["pairs"], [("ITEM-NEW", "WH-NEW")])

	def test_large_lock_sets_are_chunked_by_batch_size(self):
		queries = []

		def fake_sql(sql, params=None, **kwargs):
			queries.append((sql, params))
			return []

		pairs = [(f"ITEM-{i:04d}", "WH-1") for i in range(5)]
		with (
			patch.object(stock_locks, "LOCK_BATCH_SIZE", 2),
			patch.object(stock_locks.frappe.db, "sql", side_effect=fake_sql),
		):
			stock_locks.lock_stock_coordinates(pairs)

		self.assertEqual(len(queries), 6)
		self.assertEqual(queries[0][1], ("ITEM-0000", "ITEM-0001"))
		self.assertEqual(queries[1][1], ("ITEM-0002", "ITEM-0003"))
		self.assertEqual(queries[2][1], ("ITEM-0004",))

	def test_purchase_receipt_hook_locks_items_and_bins(self):
		doc = {
			"doctype": "Purchase Receipt",
			"set_warehouse": "WH-MAIN",
			"items": [
				{"item_code": "ITEM-2", "warehouse": "WH-B"},
				{"item_code": "ITEM-1", "warehouse": None},
			],
		}
		with patch.object(
			stock_locks, "lock_stock_coordinates", return_value={"items": [], "bins": [], "pairs": []}
		) as lock_fn:
			stock_locks.lock_purchase_receipt_stock(doc)

		lock_fn.assert_called_once_with(
			[("ITEM-1", "WH-MAIN"), ("ITEM-2", "WH-B")],
			item_codes=["ITEM-1", "ITEM-2"],
		)

	def test_purchase_invoice_hook_locks_bins_only_when_update_stock_enabled(self):
		doc_no_stock = {
			"doctype": "Purchase Invoice",
			"update_stock": 0,
			"set_warehouse": "WH-MAIN",
			"items": [{"item_code": "ITEM-1", "warehouse": "WH-MAIN"}],
		}
		doc_with_stock = {
			"doctype": "Purchase Invoice",
			"update_stock": 1,
			"set_warehouse": "WH-MAIN",
			"items": [{"item_code": "ITEM-1", "warehouse": "WH-MAIN"}],
		}
		with patch.object(
			stock_locks, "lock_stock_coordinates", return_value={"items": [], "bins": [], "pairs": []}
		) as lock_fn:
			stock_locks.lock_purchase_invoice_stock(doc_no_stock)
			stock_locks.lock_purchase_invoice_stock(doc_with_stock)

		self.assertEqual(lock_fn.call_args_list[0].args, ([],))
		self.assertEqual(lock_fn.call_args_list[0].kwargs, {"item_codes": ["ITEM-1"]})
		self.assertEqual(lock_fn.call_args_list[1].args, ([("ITEM-1", "WH-MAIN")],))
		self.assertEqual(lock_fn.call_args_list[1].kwargs, {"item_codes": ["ITEM-1"]})

	def test_hooks_wire_all_six_stock_doctypes_before_submit_without_overrides(self):
		self.assertEqual(
			hooks.doc_events["Sales Invoice"]["before_submit"],
			"khitmee_ops.api.stock_locks.lock_stock_voucher_before_submit",
		)
		self.assertEqual(
			hooks.doc_events["Stock Entry"]["before_submit"],
			"khitmee_ops.api.stock_locks.lock_stock_voucher_before_submit",
		)
		self.assertEqual(
			hooks.doc_events["Delivery Note"]["before_submit"],
			"khitmee_ops.api.stock_locks.lock_stock_voucher_before_submit",
		)
		self.assertEqual(
			hooks.doc_events["Stock Reconciliation"]["before_submit"],
			"khitmee_ops.api.stock_locks.lock_stock_voucher_before_submit",
		)
		self.assertEqual(
			hooks.doc_events["Purchase Receipt"]["before_submit"],
			"khitmee_ops.api.stock_locks.lock_purchase_receipt_stock",
		)
		self.assertEqual(
			hooks.doc_events["Purchase Invoice"]["before_submit"],
			"khitmee_ops.api.stock_locks.lock_purchase_invoice_stock",
		)
		self.assertFalse(hasattr(hooks, "override_whitelisted_methods"))
		self.assertFalse(hasattr(hooks, "override_doctype_class"))

	def test_reentrant_before_submit_is_zero_sql_noop_and_clears_on_commit(self):
		commit_callbacks = []
		rollback_callbacks = []
		sql_calls = []

		def fake_sql(sql, params=None, **kwargs):
			sql_calls.append(sql)
			return []

		fake_db = SimpleNamespace(
			sql=fake_sql,
			after_commit=SimpleNamespace(add=commit_callbacks.append),
			after_rollback=SimpleNamespace(add=rollback_callbacks.append),
		)
		doc = {
			"doctype": "Sales Invoice",
			"update_stock": 1,
			"set_warehouse": "WH-1",
			"items": [{"item_code": "ITEM-1", "warehouse": "WH-1"}],
		}
		with patch.object(stock_locks.frappe, "db", fake_db):
			# First lock (e.g. from _create_sales_invoice) executes 2 SQL queries (tabItem + tabBin)
			stock_locks.lock_stock_coordinates([("ITEM-1", "WH-1")])
			self.assertEqual(len(sql_calls), 2)
			# Re-entrant before_submit hook on subset/equal coords executes 0 SQL!
			stock_locks.lock_stock_voucher_before_submit(doc)
			self.assertEqual(len(sql_calls), 2)
			# Commit callback clears held locks so next transaction locks again
			self.assertEqual(len(commit_callbacks), 1)
			commit_callbacks[0]()
			stock_locks.lock_stock_voucher_before_submit(doc)
			self.assertEqual(len(sql_calls), 4)

	def test_sales_invoice_without_update_stock_skips_before_submit_lock(self):
		doc = {
			"doctype": "Sales Invoice",
			"update_stock": 0,
			"set_warehouse": "WH-1",
			"items": [{"item_code": "ITEM-1", "warehouse": "WH-1"}],
		}
		with patch.object(stock_locks, "lock_stock_coordinates", side_effect=AssertionError("must not lock")):
			result = stock_locks.lock_stock_voucher_before_submit(doc)
		self.assertEqual(result, {"items": [], "bins": [], "pairs": []})


	def test_document_coordinates_include_packed_items_and_target_warehouse(self):
		doc = SimpleNamespace(
			set_warehouse="Shop 1 - KMD",
			set_target_warehouse="Shop 2 - KMD",
			items=[SimpleNamespace(item_code="BUNDLE-1", warehouse="Shop 1 - KMD", target_warehouse=None)],
			packed_items=[SimpleNamespace(item_code="COMP-1", warehouse="Shop 1 - KMD")],
		)
		self.assertEqual(stock_locks.document_item_codes(doc), ["BUNDLE-1", "COMP-1"])
		self.assertEqual(
			stock_locks.document_bin_pairs(doc),
			[("BUNDLE-1", "Shop 1 - KMD"), ("BUNDLE-1", "Shop 2 - KMD"), ("COMP-1", "Shop 1 - KMD"), ("COMP-1", "Shop 2 - KMD")],
		)


if __name__ == "__main__":
	unittest.main()
