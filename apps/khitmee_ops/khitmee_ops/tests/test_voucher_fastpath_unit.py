import datetime
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))


def _ensure_stubs():
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

	for mod_name in (
		"erpnext",
		"erpnext.accounts",
		"erpnext.accounts.doctype",
		"erpnext.accounts.doctype.accounting_dimension",
		"erpnext.stock",
		"erpnext.stock.doctype",
		"erpnext.stock.doctype.packed_item",
		"erpnext.stock.doctype.stock_reservation_entry",
		"erpnext.stock.doctype.stock_entry",
	):
		sys.modules.setdefault(mod_name, types.ModuleType(mod_name))

	stock_ledger = sys.modules.setdefault("erpnext.stock.stock_ledger", types.ModuleType("erpnext.stock.stock_ledger"))
	if not hasattr(stock_ledger, "update_qty_in_future_sle"):
		stock_ledger.update_qty_in_future_sle = lambda args, **kwargs: ("upstream", args.get("item_code"))

	stock_entry = sys.modules.setdefault(
		"erpnext.stock.doctype.stock_entry.stock_entry",
		types.ModuleType("erpnext.stock.doctype.stock_entry.stock_entry"),
	)
	if not hasattr(stock_entry, "make_stock_in_entry"):
		stock_entry.make_stock_in_entry = lambda name: {}

	get_item_details = sys.modules.setdefault(
		"erpnext.stock.get_item_details",
		types.ModuleType("erpnext.stock.get_item_details"),
	)
	if not hasattr(get_item_details, "get_conversion_factor"):
		get_item_details.get_conversion_factor = lambda item_code, uom, throw_if_missing=True: {
			"conversion_factor": 1
		}
	if not hasattr(get_item_details, "get_pos_profile"):
		get_item_details.get_pos_profile = lambda company, pos_profile=None, user=None: None

	acc_dim = sys.modules.setdefault(
		"erpnext.accounts.doctype.accounting_dimension.accounting_dimension",
		types.ModuleType("erpnext.accounts.doctype.accounting_dimension.accounting_dimension"),
	)
	if not hasattr(acc_dim, "get_accounting_dimensions"):
		acc_dim.get_accounting_dimensions = lambda as_list=True: []

	packed_item = sys.modules.setdefault(
		"erpnext.stock.doctype.packed_item.packed_item",
		types.ModuleType("erpnext.stock.doctype.packed_item.packed_item"),
	)
	if not hasattr(packed_item, "is_product_bundle"):
		packed_item.is_product_bundle = lambda item_code: False

	sre = sys.modules.setdefault(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry",
		types.ModuleType("erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry"),
	)
	if not hasattr(sre, "get_sre_reserved_qty_for_item_and_warehouse"):
		sre.get_sre_reserved_qty_for_item_and_warehouse = lambda item_code, warehouse=None: 0.0

	return stock_ledger, acc_dim, packed_item, sre, get_item_details


STOCK_LEDGER_MOD, ACC_DIM_MOD, PACKED_ITEM_MOD, SRE_MOD, GET_ITEM_DETAILS_MOD = _ensure_stubs()

from khitmee_ops.api import voucher_fastpath


def _sale_doc(items, *, name="SI-0001", is_return=0, docstatus=1, packed_items=None, posting_date="2026-11-28", posting_time="09:05:00"):
	return SimpleNamespace(
		doctype="Sales Invoice",
		name=name,
		update_stock=1,
		is_return=is_return,
		is_debit_note=0,
		is_opening="No",
		docstatus=docstatus,
		posting_date=posting_date,
		posting_time=posting_time,
		set_warehouse="WH-1",
		packed_items=packed_items or [],
		items=[SimpleNamespace(**row) for row in items],
	)


class TestVoucherFastpath(unittest.TestCase):
	def setUp(self):
		self.upstream_calls = []

		def upstream(args, allow_zero_rate=False, allow_negative_stock=False, via_landed_cost_voucher=False, **kwargs):
			self.upstream_calls.append(
				(args.get("item_code"), args.get("warehouse"), via_landed_cost_voucher)
			)
			return "upstream"

		self.upstream = upstream
		STOCK_LEDGER_MOD.update_qty_in_future_sle = upstream

	def tearDown(self):
		STOCK_LEDGER_MOD.update_qty_in_future_sle = self.upstream
		self.assertIsNone(getattr(voucher_fastpath._THREAD_STATE, "context", None))

	def test_find_pairs_uses_union_all_force_index_and_normalized_posting_datetime(self):
		queries = []

		def fake_sql(sql, params=None, **kwargs):
			queries.append((sql, params))
			return [("ITEM-2", "WH-1")]

		with (
			patch.object(voucher_fastpath, "FUTURE_SLE_UNION_BATCH_SIZE", 2),
			patch.object(voucher_fastpath.frappe.db, "sql", side_effect=fake_sql),
		):
			dirty = voucher_fastpath.find_pairs_with_future_sle(
				[("ITEM-1", "WH-1"), ("ITEM-2", "WH-1"), ("ITEM-3", "WH-1")],
				datetime.date(2026, 11, 28),
				"9:05:00",
			)

		self.assertEqual(dirty, {("ITEM-2", "WH-1")})
		self.assertEqual(len(queries), 2)
		self.assertIn("UNION ALL", queries[0][0])
		self.assertIn("FORCE INDEX (`item_code_warehouse_posting_datetime_creation_index`)", queries[0][0])
		self.assertIn("posting_datetime >= %s", queries[0][0])
		self.assertIn("LIMIT 1", queries[0][0])
		self.assertEqual(queries[0][1][4], "2026-11-28 09:05:00")

	def test_proven_empty_pairs_skip_upstream_with_mixed_date_time_types_and_dirty_fallback(self):
		doc = _sale_doc(
			[
				{"item_code": "ITEM-CLEAN", "warehouse": "WH-1"},
				{"item_code": "ITEM-DIRTY", "warehouse": "WH-1"},
			],
			posting_date=datetime.date(2026, 11, 28),
			posting_time="9:05:00",
		)
		locked = [("ITEM-CLEAN", "WH-1"), ("ITEM-DIRTY", "WH-1")]

		with patch.object(
			voucher_fastpath.frappe.db,
			"sql",
			return_value=[("ITEM-DIRTY", "WH-1")],
		) as sql:
			with voucher_fastpath.proven_empty_future_sle_fastpath(doc, locked) as ctx:
				self.assertTrue(ctx.active)
				self.assertEqual(ctx.proven_empty_pairs, frozenset({("ITEM-CLEAN", "WH-1")}))

				# Different Python types (date + timedelta) representing the exact same timestamp must match!
				STOCK_LEDGER_MOD.update_qty_in_future_sle(
					{
						"voucher_type": "Sales Invoice",
						"voucher_no": "SI-0001",
						"item_code": "ITEM-CLEAN",
						"warehouse": "WH-1",
						"posting_date": "2026-11-28",
						"posting_time": datetime.timedelta(hours=9, minutes=5),
					}
				)
				STOCK_LEDGER_MOD.update_qty_in_future_sle(
					{
						"voucher_type": "Sales Invoice",
						"voucher_no": "SI-0001",
						"item_code": "ITEM-DIRTY",
						"warehouse": "WH-1",
						"posting_date": "2026-11-28",
						"posting_time": "09:05:00",
					}
				)

		self.assertEqual(sql.call_count, 1)
		self.assertEqual(ctx.skipped_count, 1)
		self.assertEqual(ctx.fallback_count, 1)
		self.assertEqual(self.upstream_calls, [("ITEM-DIRTY", "WH-1", False)])
		self.assertTrue(getattr(STOCK_LEDGER_MOD.update_qty_in_future_sle, "_khitmee_dispatcher", False))
		self.assertIs(STOCK_LEDGER_MOD.update_qty_in_future_sle._khitmee_original, self.upstream)
		# Calling after exit delegates directly to upstream without skipping
		STOCK_LEDGER_MOD.update_qty_in_future_sle(
			{
				"voucher_type": "Sales Invoice",
				"voucher_no": "SI-0001",
				"item_code": "ITEM-CLEAN",
				"warehouse": "WH-1",
				"posting_date": "2026-11-28",
				"posting_time": "09:05:00",
			}
		)
		self.assertEqual(len(self.upstream_calls), 2)

	def test_ineligible_vouchers_never_activate_fastpath(self):
		cases = [
			_sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1"}, {"item_code": "ITEM-1", "warehouse": "WH-1"}]),
			_sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1"}], is_return=1),
			_sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1"}], docstatus=2),
			_sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1", "batch_no": "BATCH-1"}]),
			_sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1"}], packed_items=[{"item_code": "SUB-1"}]),
		]
		for doc in cases:
			with patch.object(voucher_fastpath.frappe.db, "sql", side_effect=AssertionError("must not query")):
				with voucher_fastpath.proven_empty_future_sle_fastpath(doc, [("ITEM-1", "WH-1")]) as ctx:
					self.assertFalse(ctx.active)
					self.assertIs(STOCK_LEDGER_MOD.update_qty_in_future_sle, self.upstream)

	def test_unlocked_coordinate_disables_fastpath(self):
		doc = _sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1"}])
		with patch.object(voucher_fastpath.frappe.db, "sql", side_effect=AssertionError("must not query")):
			with voucher_fastpath.proven_empty_future_sle_fastpath(doc, [("ITEM-OTHER", "WH-1")]) as ctx:
				self.assertFalse(ctx.active)

	def test_exception_and_rollback_always_restore_upstream_and_thread_state(self):
		doc = _sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1"}])
		with patch.object(voucher_fastpath.frappe.db, "sql", return_value=[]):
			with self.assertRaisesRegex(RuntimeError, "simulated rollback"):
				with voucher_fastpath.proven_empty_future_sle_fastpath(doc, [("ITEM-1", "WH-1")]) as ctx:
					self.assertTrue(ctx.active)
					raise RuntimeError("simulated rollback")

		self.assertIsNone(getattr(voucher_fastpath._THREAD_STATE, "context", None))
		# After rollback/exception, dispatcher delegates straight to upstream
		STOCK_LEDGER_MOD.update_qty_in_future_sle({"item_code": "ITEM-1", "warehouse": "WH-1"})
		self.assertEqual(self.upstream_calls, [("ITEM-1", "WH-1", False)])

	def test_concurrent_threads_are_isolated_and_never_stomp_context(self):
		doc_a = _sale_doc([{"item_code": "ITEM-A", "warehouse": "WH-1"}], name="SI-A")
		doc_b = _sale_doc([{"item_code": "ITEM-B", "warehouse": "WH-2"}], name="SI-B")
		barrier = threading.Barrier(3)
		errors = []
		ctx_results = {}

		def worker_a():
			try:
				with voucher_fastpath.proven_empty_future_sle_fastpath(doc_a, [("ITEM-A", "WH-1")]) as ctx:
					barrier.wait(timeout=3)
					STOCK_LEDGER_MOD.update_qty_in_future_sle(
						{
							"voucher_type": "Sales Invoice",
							"voucher_no": "SI-A",
							"item_code": "ITEM-A",
							"warehouse": "WH-1",
							"posting_date": "2026-11-28",
							"posting_time": "09:05:00",
						}
					)
					ctx_results["a"] = (ctx.skipped_count, ctx.fallback_count)
					barrier.wait(timeout=3)
			except Exception as exc:
				errors.append(exc)

		def worker_b():
			try:
				with voucher_fastpath.proven_empty_future_sle_fastpath(doc_b, [("ITEM-B", "WH-2")]) as ctx:
					barrier.wait(timeout=3)
					STOCK_LEDGER_MOD.update_qty_in_future_sle(
						{
							"voucher_type": "Sales Invoice",
							"voucher_no": "SI-B",
							"item_code": "ITEM-B",
							"warehouse": "WH-2",
							"posting_date": "2026-11-28",
							"posting_time": "09:05:00",
						}
					)
					ctx_results["b"] = (ctx.skipped_count, ctx.fallback_count)
					barrier.wait(timeout=3)
			except Exception as exc:
				errors.append(exc)

		def worker_unwrapped():
			try:
				barrier.wait(timeout=3)
				STOCK_LEDGER_MOD.update_qty_in_future_sle(
					{
						"voucher_type": "Sales Invoice",
						"voucher_no": "SI-A",
						"item_code": "ITEM-A",
						"warehouse": "WH-1",
						"posting_date": "2026-11-28",
						"posting_time": "09:05:00",
					}
				)
				barrier.wait(timeout=3)
			except Exception as exc:
				errors.append(exc)

		with patch.object(voucher_fastpath.frappe.db, "sql", return_value=[]):
			threads = [
				threading.Thread(target=worker_a),
				threading.Thread(target=worker_b),
				threading.Thread(target=worker_unwrapped),
			]
			for t in threads:
				t.start()
			for t in threads:
				t.join(timeout=5)

		self.assertEqual(errors, [])
		self.assertEqual(ctx_results["a"], (1, 0))
		self.assertEqual(ctx_results["b"], (1, 0))
		self.assertEqual(self.upstream_calls, [("ITEM-A", "WH-1", False)])
		self.assertIsNone(getattr(voucher_fastpath._THREAD_STATE, "context", None))
		# Never double-wraps on re-entry
		first_dispatcher = STOCK_LEDGER_MOD.update_qty_in_future_sle
		with patch.object(voucher_fastpath.frappe.db, "sql", return_value=[]):
			with voucher_fastpath.proven_empty_future_sle_fastpath(doc_a, [("ITEM-A", "WH-1")]):
				self.assertIs(STOCK_LEDGER_MOD.update_qty_in_future_sle, first_dispatcher)


class TestVoucherTransactionReadCache(unittest.TestCase):
	def setUp(self):
		self.dim_calls = 0
		self.bundle_calls = []
		self.sre_calls = []
		self.pos_calls = 0

		def strict_get_accounting_dimensions(as_list=True):
			self.dim_calls += 1
			return ["shop"] if as_list else [{"fieldname": "shop"}]

		def orig_bundle(item_code):
			self.bundle_calls.append(item_code)
			return item_code == "BUNDLE-EXTRA"

		def orig_sre(item_code, warehouse=None):
			self.sre_calls.append((item_code, warehouse))
			return 5.0

		def orig_pos(company, pos_profile=None, user=None):
			self.pos_calls += 1
			return {"name": "POS-1", "company": company}

		self.orig_dims = strict_get_accounting_dimensions
		self.orig_bundle = orig_bundle
		self.orig_sre = orig_sre
		self.orig_pos = orig_pos

		ACC_DIM_MOD.get_accounting_dimensions = self.orig_dims
		PACKED_ITEM_MOD.is_product_bundle = self.orig_bundle
		SRE_MOD.get_sre_reserved_qty_for_item_and_warehouse = self.orig_sre
		GET_ITEM_DETAILS_MOD.get_pos_profile = self.orig_pos

	def tearDown(self):
		ACC_DIM_MOD.get_accounting_dimensions = self.orig_dims
		PACKED_ITEM_MOD.is_product_bundle = self.orig_bundle
		SRE_MOD.get_sre_reserved_qty_for_item_and_warehouse = self.orig_sre
		GET_ITEM_DETAILS_MOD.get_pos_profile = self.orig_pos
		self.assertIsNone(getattr(voucher_fastpath._READ_CACHE_STATE, "context", None))

	def test_read_cache_deduplicates_queries_and_returns_defensive_copies(self):
		def fake_sql(sql, params=None, **kwargs):
			if "`tabProduct Bundle`" in sql:
				return [("ITEM-BUNDLE",)]
			if "`tabStock Reservation Entry`" in sql:
				return [("ITEM-1", "WH-1", 3.5)]
			return []

		doc = _sale_doc([{"item_code": "ITEM-1", "warehouse": "WH-1"}])
		with patch.object(voucher_fastpath.frappe.db, "sql", side_effect=fake_sql):
			with voucher_fastpath.voucher_transaction_read_cache(
				doc,
				item_codes=["ITEM-1", "ITEM-BUNDLE"],
				locked_pairs=[("ITEM-1", "WH-1"), ("ITEM-BUNDLE", "WH-1")],
			) as ctx:
				# Accounting dimensions: strict ERPNext v16 signature (no `filters` kwarg!)
				dims1 = ACC_DIM_MOD.get_accounting_dimensions()
				dims1.append("mutated")
				dims2 = ACC_DIM_MOD.get_accounting_dimensions()
				dims3 = ACC_DIM_MOD.get_accounting_dimensions(as_list=False)
				self.assertEqual(dims2, ["shop"])
				self.assertEqual(dims3, [{"fieldname": "shop"}])
				self.assertEqual(self.dim_calls, 2)

				# Product bundle: bulk prefetched (0 upstream calls) + fallback for unprefetched item
				self.assertFalse(PACKED_ITEM_MOD.is_product_bundle("ITEM-1"))
				self.assertTrue(PACKED_ITEM_MOD.is_product_bundle("ITEM-BUNDLE"))
				self.assertEqual(self.bundle_calls, [])
				self.assertTrue(PACKED_ITEM_MOD.is_product_bundle("BUNDLE-EXTRA"))
				self.assertEqual(self.bundle_calls, ["BUNDLE-EXTRA"])

				# SRE reserved qty: bulk prefetched (0 upstream calls) + fallback for unlocked pair
				self.assertEqual(SRE_MOD.get_sre_reserved_qty_for_item_and_warehouse("ITEM-1", "WH-1"), 3.5)
				self.assertEqual(SRE_MOD.get_sre_reserved_qty_for_item_and_warehouse("ITEM-BUNDLE", "WH-1"), 0.0)
				self.assertEqual(self.sre_calls, [])
				self.assertEqual(SRE_MOD.get_sre_reserved_qty_for_item_and_warehouse("OTHER", "WH-9"), 5.0)
				self.assertEqual(self.sre_calls, [("OTHER", "WH-9")])

				# POS profile: cached with defensive copy
				p1 = GET_ITEM_DETAILS_MOD.get_pos_profile("Company")
				p1["name"] = "MUTATED"
				p2 = GET_ITEM_DETAILS_MOD.get_pos_profile("Company")
				self.assertEqual(p2["name"], "POS-1")
				self.assertEqual(self.pos_calls, 1)
				self.assertGreater(ctx.hits, 0)

		# After exit, dispatchers delegate directly to original callables (never NoneType crash)
		before_dims = self.dim_calls
		self.assertEqual(ACC_DIM_MOD.get_accounting_dimensions(), ["shop"])
		self.assertEqual(self.dim_calls, before_dims + 1)

	def test_lazy_import_during_context_never_poisons_worker_after_exit(self):
		# Simulate a caller module lazily binding get_accounting_dimensions INSIDE the context
		# and then invoking that bound reference AFTER the context exits (F02 regression).
		with patch.object(voucher_fastpath.frappe.db, "sql", return_value=[]):
			with voucher_fastpath.voucher_transaction_read_cache(item_codes=["ITEM-1"]):
				lazily_bound_fn = ACC_DIM_MOD.get_accounting_dimensions
				self.assertEqual(lazily_bound_fn(), ["shop"])
				self.assertEqual(lazily_bound_fn(), ["shop"])
				self.assertEqual(self.dim_calls, 1)

		# After context exits, calling the lazily bound reference must call original cleanly!
		self.assertEqual(lazily_bound_fn(), ["shop"])
		self.assertEqual(lazily_bound_fn(), ["shop"])
		self.assertEqual(self.dim_calls, 3)

	def test_read_cache_thread_isolation_and_exception_cleanup(self):
		barrier = threading.Barrier(2)
		errors = []
		unwrapped_dims_count = []

		def cached_worker():
			try:
				with self.assertRaisesRegex(RuntimeError, "boom"):
					with voucher_fastpath.voucher_transaction_read_cache(
						item_codes=["ITEM-1"], locked_pairs=[("ITEM-1", "WH-1")]
					):
						ACC_DIM_MOD.get_accounting_dimensions()
						ACC_DIM_MOD.get_accounting_dimensions()
						barrier.wait(timeout=3)
						barrier.wait(timeout=3)
						raise RuntimeError("boom")
			except Exception as exc:
				errors.append(exc)

		def unwrapped_worker():
			try:
				barrier.wait(timeout=3)
				before = self.dim_calls
				ACC_DIM_MOD.get_accounting_dimensions()
				unwrapped_dims_count.append(self.dim_calls - before)
				barrier.wait(timeout=3)
				# Also call concurrently while cached_worker exits/raises
				self.assertEqual(ACC_DIM_MOD.get_accounting_dimensions(), ["shop"])
			except Exception as exc:
				errors.append(exc)

		with patch.object(voucher_fastpath.frappe.db, "sql", return_value=[]):
			t1 = threading.Thread(target=cached_worker)
			t2 = threading.Thread(target=unwrapped_worker)
			t1.start()
			t2.start()
			t1.join(timeout=5)
			t2.join(timeout=5)

		self.assertEqual(errors, [])
		self.assertEqual(unwrapped_dims_count, [1])
		self.assertIsNone(getattr(voucher_fastpath._READ_CACHE_STATE, "context", None))


if __name__ == "__main__":
	unittest.main()
