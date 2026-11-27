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


def _install_frappe_and_erpnext_stubs():
	try:
		import frappe  # noqa: F401
		import erpnext  # noqa: F401
		return
	except ImportError:
		pass

	frappe = types.ModuleType("frappe")
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
	frappe.PermissionError = PermissionError
	frappe._ = lambda message, *args: message
	frappe.session = SimpleNamespace(user="counter@example.com")
	frappe.has_permission = unittest.mock.Mock(return_value=True)
	frappe.get_doc = unittest.mock.Mock()
	frappe.get_cached_value = unittest.mock.Mock(return_value=None)
	frappe.parse_json = json.loads
	frappe.throw = lambda message, exc=None: (_ for _ in ()).throw((exc or Exception)(message))
	frappe.log_error = lambda *args, **kwargs: None
	frappe.cache = None
	frappe.db = SimpleNamespace(
		exists=lambda *args, **kwargs: True,
		get_value=lambda *args, **kwargs: None,
		is_duplicate_entry=lambda exc: False,
		sql=lambda *args, **kwargs: [],
		rollback=unittest.mock.Mock(),
		set_single_value=lambda *args, **kwargs: None,
		after_commit=SimpleNamespace(add=lambda fn: None),
		after_rollback=SimpleNamespace(add=lambda fn: None),
	)

	def whitelist(*args, **kwargs):
		if args and callable(args[0]):
			return args[0]
		return lambda fn: fn

	frappe.whitelist = whitelist

	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	utils.cstr = lambda value: "" if value is None else str(value)

	def flt(value, precision=None):
		value = float(value or 0)
		return round(value, precision) if precision is not None else value

	utils.flt = flt
	utils.nowdate = lambda: "2026-11-26"
	utils.nowtime = lambda: "12:00:00"
	utils.random_string = lambda length: "x" * length

	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils

	for module_name in (
		"erpnext",
		"erpnext.stock",
		"erpnext.stock.doctype",
		"erpnext.stock.doctype.stock_entry",
	):
		sys.modules.setdefault(module_name, types.ModuleType(module_name))

	stock_entry_module = types.ModuleType("erpnext.stock.doctype.stock_entry.stock_entry")
	stock_entry_module.make_stock_in_entry = lambda name: {}
	sys.modules["erpnext.stock.doctype.stock_entry.stock_entry"] = stock_entry_module

	get_item_details_module = types.ModuleType("erpnext.stock.get_item_details")
	get_item_details_module.get_conversion_factor = lambda item_code, uom, throw_if_missing=True: {
		"conversion_factor": 1
	}
	sys.modules["erpnext.stock.get_item_details"] = get_item_details_module


_install_frappe_and_erpnext_stubs()

from khitmee_ops.api import vouchers
from khitmee_ops.api.payloads import (
	idempotency_key,
	make_receipt_payload,
	normalize_client_voucher_id,
	public_item_row,
)


class AttrDict(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


class FakePayment:
	def __init__(self, mode_of_payment=None, amount=0):
		self.mode_of_payment = mode_of_payment
		self.amount = amount
		self.account = None


class FakeSalesInvoice:
	def __init__(self, payable_total=100):
		self.doctype = "Sales Invoice"
		self.name = "SI-TEST-0001"
		self.docstatus = 0
		self.payments = []
		self.flags = AttrDict()
		self.is_pos = 0
		self.rounded_total = payable_total
		self.grand_total = payable_total
		self.paid_amount = 0
		self.events = []
		self.insert_calls = 0
		self.save_calls = 0
		self.submit_calls = 0

	def append(self, fieldname, row):
		if fieldname != "payments":
			raise AssertionError(f"unexpected child table {fieldname}")
		self.payments.append(FakePayment(row.get("mode_of_payment"), row.get("amount", 0)))

	def set_missing_values(self):
		self.events.append("set_missing_values")

	def calculate_taxes_and_totals(self):
		self.events.append("calculate_taxes_and_totals")

	def set_paid_amount(self):
		self.events.append("set_paid_amount")
		self.paid_amount = sum(float(payment.amount or 0) for payment in self.payments)

	def set_account_for_mode_of_payment(self):
		self.events.append("set_account_for_mode_of_payment")
		for payment in self.payments:
			payment.account = f"Cash - {payment.mode_of_payment}"

	def validate_pos(self):
		self.events.append("validate_pos")

	def validate_pos_paid_amount(self):
		self.events.append("validate_pos_paid_amount")

	def insert(self):
		self.events.append("insert")
		self.insert_calls += 1

	def save(self):
		self.events.append("save")
		self.save_calls += 1

	def submit(self):
		self.events.append("submit")
		self.submit_calls += 1
		self.docstatus = 1


class DriftingSalesInvoice(FakeSalesInvoice):
	def set_paid_amount(self):
		self.events.append("set_paid_amount")
		if self.insert_calls:
			self.paid_amount = sum(float(payment.amount or 0) for payment in self.payments)
		else:
			self.paid_amount = sum(float(payment.amount or 0) for payment in self.payments) - 0.01


class FakeStockEntry:
	def __init__(self):
		self.doctype = "Stock Entry"
		self.name = "SE-TEST-0001"
		self.docstatus = 0
		self.insert_calls = 0
		self.submit_calls = 0

	def insert(self):
		self.insert_calls += 1

	def submit(self):
		self.submit_calls += 1
		self.docstatus = 1


def create_invoice(doc, payment_mode="Cash"):
	with (
		patch.object(vouchers, "_validate_warehouse", return_value=SimpleNamespace(company="Company")),
		patch.object(vouchers, "_validate_sale_inputs", return_value=None),
		patch.object(vouchers.frappe, "get_doc", return_value=doc) as get_doc,
	):
		result = vouchers._create_sales_invoice(
			"client-1",
			"Main Warehouse",
			"Customer",
			"Standard Selling",
			[{"item_code": "ITEM-1", "qty": 1, "uom": "Nos", "rate": 100}],
			payment_mode,
			False,
		)
	return result, get_doc.call_args.args[0]


class TestSinglePassVoucherSubmit(unittest.TestCase):
	def test_sales_invoice_currency_defaults_come_from_cached_metadata(self):
		doc = FakeSalesInvoice(payable_total=100)
		cached_values = {
			("Company", "Company", "default_currency"): "USD",
			("Customer", "Customer", "default_currency"): "EUR",
			("Price List", "Standard Selling", "currency"): "JPY",
		}

		def get_cached_value(doctype, name, fieldname):
			return cached_values.get((doctype, name, fieldname))

		with patch.object(vouchers.frappe, "get_cached_value", side_effect=get_cached_value):
			_result, payload = create_invoice(doc)

		self.assertEqual(payload["currency"], "EUR")
		self.assertEqual(payload["price_list_currency"], "JPY")

	def test_paid_sales_invoice_uses_one_insert_and_defers_autopay_to_validate_hook(self):
		doc = FakeSalesInvoice(payable_total=100)

		result, payload = create_invoice(doc)

		self.assertIs(result, doc)
		self.assertEqual(payload["is_pos"], 1)
		self.assertEqual(payload["payments"], [{"mode_of_payment": "Cash", "amount": 0}])
		self.assertTrue(doc.flags.khitmee_counter_autopay)
		self.assertTrue(doc.flags.ignore_pos_profile)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.insert_calls, 1)
		self.assertEqual(doc.save_calls, 0)
		self.assertEqual(doc.submit_calls, 0)
		for redundant_event in (
			"set_missing_values",
			"calculate_taxes_and_totals",
			"set_paid_amount",
			"set_account_for_mode_of_payment",
		):
			self.assertNotIn(redundant_event, doc.events)

	def test_non_pos_sales_invoice_uses_single_pass_insert_without_autopay(self):
		doc = FakeSalesInvoice(payable_total=100)

		_create_result, payload = create_invoice(doc, payment_mode=None)

		self.assertEqual(payload["is_pos"], 0)
		self.assertEqual(payload["payments"], [])
		self.assertFalse(doc.flags.get("khitmee_counter_autopay"))
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.insert_calls, 1)
		self.assertEqual(doc.save_calls, 0)
		self.assertEqual(doc.submit_calls, 0)

	def test_counter_autopay_validate_hook_syncs_payment_before_pos_validation(self):
		doc = FakeSalesInvoice(payable_total=100)
		doc.is_pos = 1
		doc.flags.khitmee_counter_autopay = True
		doc.payments = [FakePayment("Cash", 0)]

		vouchers.sync_counter_autopay_payment(doc)

		self.assertEqual(doc.payments[0].amount, 100)
		self.assertEqual(doc.paid_amount, 100)
		self.assertEqual(doc.payments[0].account, "Cash - Cash")
		self.assertEqual(
			doc.events,
			[
				"set_paid_amount",
				"set_account_for_mode_of_payment",
				"calculate_taxes_and_totals",
				"validate_pos",
				"validate_pos_paid_amount",
			],
		)

	def test_counter_autopay_validate_hook_leaves_credit_and_unflagged_invoices_untouched(self):
		for is_pos, flagged in ((0, True), (1, False)):
			doc = FakeSalesInvoice(payable_total=100)
			doc.is_pos = is_pos
			doc.flags.khitmee_counter_autopay = flagged
			doc.payments = [FakePayment("Cash", 0)]

			vouchers.sync_counter_autopay_payment(doc)

			self.assertEqual(doc.payments[0].amount, 0)
			self.assertEqual(doc.events, [])

	def test_direct_stock_entry_uses_single_pass_insert(self):
		doc = FakeStockEntry()
		with (
			patch.object(vouchers, "_validate_warehouse", return_value=SimpleNamespace(company="Company")),
			patch.object(vouchers.frappe, "get_doc", return_value=doc),
		):
			result = vouchers._create_direct_or_dispatch_transfer(
				"Source", "Target", "direct", [{"item_code": "ITEM-1", "qty": 1, "uom": "Nos"}]
			)

		self.assertIs(result, doc)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.insert_calls, 1)
		self.assertEqual(doc.submit_calls, 0)


class TestIdempotencyLockCleanup(unittest.TestCase):
	class Lock:
		def __init__(self):
			self.release_calls = 0

		def acquire(self, blocking=False):
			return True

		def release(self):
			self.release_calls += 1

	class ModernCache:
		def __init__(self):
			self.lock_instance = TestIdempotencyLockCleanup.Lock()

		def __call__(self):
			raise AssertionError("modern cache objects must not be called")

		def make_key(self, key):
			return key

		def lock(self, key, timeout=60):
			return self.lock_instance

	def test_acquire_lock_uses_modern_cache_object_without_calling_it(self):
		cache = self.ModernCache()
		with patch.object(vouchers.frappe, "cache", cache):
			lock = vouchers._acquire_idempotency_lock("client-1")

		self.assertIs(lock, cache.lock_instance)

	def test_acquire_lock_supports_legacy_callable_cache_accessor(self):
		cache = self.ModernCache()
		with patch.object(vouchers.frappe, "cache", lambda: cache):
			lock = vouchers._acquire_idempotency_lock("client-1")

		self.assertIs(lock, cache.lock_instance)

	def test_non_duplicate_database_exception_still_releases_redis_lock(self):
		lock = self.Lock()
		with (
			patch.object(vouchers, "_lookup_idempotency", return_value=None),
			patch.object(vouchers, "_acquire_idempotency_lock", return_value=lock),
			patch.object(vouchers, "_reserve_idempotency_key", side_effect=RuntimeError("database down")),
		):
			with self.assertRaisesRegex(RuntimeError, "database down"):
				vouchers._run_idempotent(
					"client-1", "sale", "Sales Invoice", "fingerprint", lambda: FakeSalesInvoice()
				)

		self.assertEqual(lock.release_calls, 1)

	def test_duplicate_reservation_releases_lock_before_waiting_for_winner(self):
		lock = self.Lock()
		winning_doc = FakeSalesInvoice()
		with (
			patch.object(vouchers, "_lookup_idempotency", return_value=None),
			patch.object(vouchers, "_acquire_idempotency_lock", return_value=lock),
			patch.object(vouchers, "_reserve_idempotency_key", return_value=False),
			patch.object(vouchers, "_wait_for_idempotent_document", return_value=winning_doc) as wait,
		):
			doc, created = vouchers._run_idempotent(
				"client-1", "sale", "Sales Invoice", "fingerprint", lambda: FakeSalesInvoice()
			)

		self.assertIs(doc, winning_doc)
		self.assertFalse(created)
		self.assertEqual(lock.release_calls, 1)
		wait.assert_called_once()


class TestPass3IdempotencyAndDeadlockCorrectness(unittest.TestCase):
	def test_load_idempotent_document_for_update_uses_locking_read_and_locked_get_doc(self):
		existing = AttrDict(reference_doctype="Sales Invoice", reference_name="SI-LOCK-1")
		doc = SimpleNamespace(doctype="Sales Invoice", name="SI-LOCK-1")

		with (
			patch.object(vouchers.frappe.db, "sql", return_value=[("SI-LOCK-1",)]) as sql,
			patch.object(vouchers.frappe.db, "exists", side_effect=AssertionError("exists bypasses locking read")),
			patch.object(vouchers.frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(vouchers.frappe, "has_permission", return_value=True),
		):
			result = vouchers._load_idempotent_document(existing, for_update=True)

		self.assertIs(result, doc)
		self.assertIn("SELECT name FROM `tabSales Invoice` WHERE name = %s FOR UPDATE", sql.call_args.args[0])
		self.assertEqual(sql.call_args.args[1], ("SI-LOCK-1",))
		get_doc.assert_called_once_with("Sales Invoice", "SI-LOCK-1", for_update=True)

	def test_load_idempotent_document_rejects_unexpected_reference_doctype(self):
		existing = AttrDict(reference_doctype="Customer", reference_name="CUST-1")

		with self.assertRaises(vouchers.frappe.ValidationError):
			vouchers._load_idempotent_document(existing, for_update=True)

	def test_waiter_rolls_back_before_sleeping_when_winner_is_not_visible(self):
		events = []
		with (
			patch.object(vouchers, "_lookup_idempotency", return_value=None),
			patch.object(vouchers.frappe.db, "rollback", side_effect=lambda: events.append("rollback")),
			patch.object(vouchers.time, "sleep", side_effect=lambda amount: events.append("sleep")),
		):
			self.assertIsNone(vouchers._wait_for_idempotent_document("key", "fingerprint", attempts=1))

		self.assertEqual(events, ["rollback", "sleep"])

	def test_waiter_loads_bound_document_with_for_update(self):
		existing = AttrDict(reference_doctype="Stock Entry", reference_name="SE-1")
		doc = SimpleNamespace(doctype="Stock Entry", name="SE-1")
		with (
			patch.object(vouchers, "_lookup_idempotency", return_value=existing),
			patch.object(vouchers, "_verify_replay", return_value=None),
			patch.object(vouchers, "_load_idempotent_document", return_value=doc) as load,
		):
			self.assertIs(vouchers._wait_for_idempotent_document("key", "fingerprint", attempts=1), doc)

		load.assert_called_once_with(existing, for_update=True)

	def test_deadlock_retry_rolls_back_sleeps_and_retries_exactly_once(self):
		calls = []

		def flaky():
			calls.append(1)
			if len(calls) == 1:
				raise vouchers.frappe.QueryDeadlockError("deadlock")
			return "created"

		with (
			patch.object(vouchers.frappe.db, "rollback") as rollback,
			patch.object(vouchers.time, "sleep") as sleep,
			patch.object(vouchers.random, "uniform", return_value=0.03),
		):
			self.assertEqual(vouchers._execute_with_deadlock_retry(flaky), "created")

		self.assertEqual(len(calls), 2)
		rollback.assert_called_once_with()
		sleep.assert_called_once_with(0.03)

	def test_deadlock_retry_is_bounded_to_one_retry(self):
		calls = []

		def always_deadlocked():
			calls.append(1)
			raise vouchers.frappe.QueryDeadlockError("deadlock")

		with (
			patch.object(vouchers.frappe.db, "rollback"),
			patch.object(vouchers.time, "sleep"),
			patch.object(vouchers.random, "uniform", return_value=0.03),
		):
			with self.assertRaises(vouchers.frappe.QueryDeadlockError):
				vouchers._execute_with_deadlock_retry(always_deadlocked)

		self.assertEqual(len(calls), 2)

	def test_validation_errors_are_never_retried_as_deadlocks(self):
		calls = []

		def invalid():
			calls.append(1)
			raise vouchers.frappe.ValidationError("invalid")

		with patch.object(vouchers.frappe.db, "rollback") as rollback:
			with self.assertRaises(vouchers.frappe.ValidationError):
				vouchers._execute_with_deadlock_retry(invalid)

		self.assertEqual(len(calls), 1)
		rollback.assert_not_called()

	def test_submit_sale_runs_voucher_creation_through_deadlock_retry_helper(self):
		with (
			patch.object(vouchers, "_require_logged_in", return_value=None),
			patch.object(vouchers.frappe, "has_permission", return_value=True),
			patch.object(vouchers, "_validate_items", return_value=[{"item_code": "ITEM-1", "qty": 1, "uom": "Nos", "rate": None}]),
			patch.object(vouchers, "request_fingerprint", return_value="fingerprint"),
			patch.object(vouchers, "_execute_with_deadlock_retry", return_value=(object(), True)) as retry,
			patch.object(vouchers, "_check_voucher_read_permission", return_value=None),
			patch.object(vouchers, "_sale_response", return_value={"ok": True}),
		):
			result = vouchers.submit_sale(
				"client-1", "W", "Customer", "PL", json.dumps([{"item_code": "ITEM-1", "qty": 1}])
			)

		self.assertEqual(result, {"ok": True})
		retry.assert_called_once()

	def test_submit_transfer_runs_voucher_creation_through_deadlock_retry_helper(self):
		with (
			patch.object(vouchers, "_require_logged_in", return_value=None),
			patch.object(vouchers.frappe, "has_permission", return_value=True),
			patch.object(vouchers, "_validate_items", return_value=[{"item_code": "ITEM-1", "qty": 1, "uom": "Nos", "rate": None}]),
			patch.object(vouchers, "request_fingerprint", return_value="fingerprint"),
			patch.object(vouchers, "_execute_with_deadlock_retry", return_value=(object(), True)) as retry,
			patch.object(vouchers, "_check_voucher_read_permission", return_value=None),
			patch.object(vouchers, "_transfer_response", return_value={"ok": True}),
		):
			result = vouchers.submit_transfer(
				"client-1", "Source", "Target", "direct", items=json.dumps([{"item_code": "ITEM-1", "qty": 1}])
			)

		self.assertEqual(result, {"ok": True})
		retry.assert_called_once()


class TestPermissionDeduplication(unittest.TestCase):
	def test_validate_items_checks_permissions_once_per_unique_item_code(self):
		with patch.object(vouchers.frappe, "has_permission", return_value=True) as has_permission:
			cleaned = vouchers._validate_items(
				[
					{"item_code": "ITEM-1", "qty": 1},
					{"item_code": "ITEM-1", "qty": 2},
					{"item_code": "ITEM-2", "qty": 1},
					{"item_code": "ITEM-1", "qty": 3},
				]
			)

		self.assertEqual(len(cleaned), 4)
		item_calls = [call.kwargs.get("doc") for call in has_permission.call_args_list if call.args[0] == "Item"]
		self.assertEqual(item_calls, ["ITEM-1", "ITEM-2"])

	def test_voucher_read_permission_reuses_the_loaded_document_object(self):
		doc = SimpleNamespace(doctype="Sales Invoice", name="SI-0001")
		with patch.object(vouchers.frappe, "has_permission", return_value=True) as has_permission:
			vouchers._check_voucher_read_permission(doc)

		self.assertIs(has_permission.call_args.kwargs["doc"], doc)
		self.assertEqual(has_permission.call_args.kwargs["ptype"], "read")


class TestSearchPayloadAllowlist(unittest.TestCase):
	def test_public_item_row_strips_cost_valuation_purchase_and_supplier_fields(self):
		row = {
			"item_code": "ITEM-001",
			"item_name": "Counter Item",
			"stock_uom": "Nos",
			"barcode": "123456",
			"rate": 12.5,
			"actual_qty": 7,
			"valuation_rate": 999,
			"last_purchase_rate": 888,
			"supplier": "Secret Supplier",
			"supplier_part_no": "SECRET-1",
			"buying_price_list": "Secret Buying",
		}

		result = public_item_row(row)

		self.assertEqual(
			set(result),
			{"item_code", "item_name", "stock_uom", "barcode", "rate", "actual_qty"},
		)
		self.assertNotIn("valuation_rate", result)
		self.assertNotIn("last_purchase_rate", result)
		self.assertNotIn("supplier", result)
		self.assertEqual(result["rate"], 12.5)
		self.assertEqual(result["actual_qty"], 7)


class TestIdempotencyKeys(unittest.TestCase):
	def test_normalize_client_voucher_id_trims_and_preserves_value(self):
		self.assertEqual(normalize_client_voucher_id("  sale-0001  "), "sale-0001")

	def test_normalize_client_voucher_id_rejects_empty_and_overlong_values(self):
		with self.assertRaises(ValueError):
			normalize_client_voucher_id("   ")
		with self.assertRaises(ValueError):
			normalize_client_voucher_id("x" * 141)

	def test_idempotency_key_scopes_sale_and_transfer_namespaces(self):
		self.assertEqual(idempotency_key("sale", "abc"), "sale:abc")
		self.assertEqual(idempotency_key("transfer", "abc"), "transfer:abc")
		self.assertNotEqual(idempotency_key("sale", "abc"), idempotency_key("transfer", "abc"))


class TestReceiptPayloadShape(unittest.TestCase):
	def test_receipt_payload_matches_print_companion_contract(self):
		payload = make_receipt_payload(
			printer_type="voucher",
			voucher_no="SINV-0001",
			title="Sales Invoice",
			shop_name="Khitmee",
			date="2026-11-26",
			party="Walk-in Customer",
			lines=[{"name": "Counter Item", "qty": "2", "rate": "12.5", "amount": "25"}],
			total="25",
			footer="Thank you",
		)

		self.assertEqual(
			list(payload),
			[
				"printer_type",
				"voucher_no",
				"title",
				"shop_name",
				"date",
				"party",
				"lines",
				"total",
				"footer",
			],
		)
		self.assertEqual(payload["lines"], [{"name": "Counter Item", "qty": 2.0, "rate": 12.5, "amount": 25.0}])
		self.assertEqual(payload["total"], 25.0)

	def test_receipt_payload_rejects_unknown_printer_type(self):
		with self.assertRaises(ValueError):
			make_receipt_payload(
				printer_type="label",
				voucher_no="V",
				title="T",
				shop_name="S",
				date="",
				party="",
				lines=[],
				total=0,
				footer="",
			)


if __name__ == "__main__":
	unittest.main()
