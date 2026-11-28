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

	def test_hooks_wire_purchase_before_submit_without_overrides(self):
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


if __name__ == "__main__":
	unittest.main()
