import random
import time

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, nowdate, nowtime, random_string

from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry
from erpnext.stock.get_item_details import get_conversion_factor

from khitmee_ops.api.payloads import (
	RECEIPT_PRINTER_TYPES,
	idempotency_key,
	make_receipt_payload,
	normalize_client_voucher_id,
	request_fingerprint,
)
from khitmee_ops.api.stock_locks import (
	clear_transaction_stock_locks,
	lock_stock_coordinates,
	sale_bin_pairs,
	transfer_bin_pairs,
)
from khitmee_ops.api.voucher_fastpath import (
	proven_empty_future_sle_fastpath,
	voucher_transaction_read_cache,
)

IDEMPOTENCY_TABLE = "tabKhitmee Idempotency Log"


class _NullIdempotencyLock:
	def release(self):
		return None


def _require_logged_in():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to use the counter."), frappe.PermissionError)


def _validate_printer_type(printer_type):
	if printer_type not in RECEIPT_PRINTER_TYPES:
		raise ValueError("printer_type must be 'voucher' or 'a4'")


def _item_permission_restrictions_active():
	user = getattr(getattr(frappe, "session", None), "user", None)
	if user == "Administrator":
		return False
	if not user:
		return True

	get_hook = getattr(frappe, "get_hook", None) or getattr(frappe, "get_hooks", None)
	if not callable(get_hook):
		return True
	for hook_name in ("permission_query_conditions", "has_permission"):
		try:
			hooks = get_hook(hook_name, {}) or {}
		except TypeError:
			hooks = get_hook(hook_name) or {}
		if isinstance(hooks, dict) and hooks.get("Item"):
			return True

	# Fail closed if the user's Item read role permission relies on if_owner=1
	# or if role permissions cannot be inspected.
	permissions_mod = getattr(frappe, "permissions", None)
	get_role_permissions = getattr(permissions_mod, "get_role_permissions", None)
	if not callable(get_role_permissions):
		return True
	try:
		role_perms = get_role_permissions("Item", user=user)
	except Exception:
		return True
	if not isinstance(role_perms, dict):
		return True
	if_owner = role_perms.get("if_owner")
	if isinstance(if_owner, dict) and if_owner.get("read"):
		return True

	get_user_permissions = getattr(permissions_mod, "get_user_permissions", None)
	if not callable(get_user_permissions):
		defaults_mod = getattr(frappe, "defaults", None)
		get_user_permissions = getattr(defaults_mod, "get_user_permissions", None)
	if callable(get_user_permissions):
		try:
			perms = get_user_permissions(user)
			if isinstance(perms, dict):
				if any(perms.get(doctype) for doctype in ("Item", "Item Group", "Brand")):
					return True
				for entries in perms.values():
					for entry in entries or ():
						if isinstance(entry, dict) and (entry.get("applicable_for") or "") == "Item":
							return True
				return False
		except Exception:
			return True
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM `tabUser Permission`
			WHERE `user` = %s
				AND (`allow` IN ('Item', 'Item Group', 'Brand') OR `applicable_for` = 'Item')
			LIMIT 1
			""",
			(user,),
		)
	)


def _validate_items(items):
	if not isinstance(items, list | tuple) or not items:
		frappe.throw(_("At least one item is required"))

	frappe.has_permission("Item", ptype="read", throw=True)
	check_per_item = _item_permission_restrictions_active()

	cleaned = []
	permitted_item_codes = set()
	for row in items:
		if not isinstance(row, dict):
			frappe.throw(_("Every item row must be an object"))
		item_code = cstr(row.get("item_code")).strip()
		qty = flt(row.get("qty"))
		if not item_code:
			frappe.throw(_("Item Code is required in every row"))
		if qty <= 0:
			frappe.throw(_("Qty must be positive for item {0}").format(item_code))
		if check_per_item and item_code not in permitted_item_codes:
			frappe.has_permission("Item", ptype="read", doc=item_code, throw=True)
			permitted_item_codes.add(item_code)
		cleaned.append(
			{
				"item_code": item_code,
				"qty": qty,
				"uom": cstr(row.get("uom")).strip() or None,
				"rate": flt(row.get("rate")) if row.get("rate") is not None else None,
			}
		)
	return cleaned


def _validate_warehouse(warehouse, label=None):
	warehouse = cstr(warehouse).strip()
	if not warehouse:
		frappe.throw(_("{0} is required").format(label or _("Warehouse")))
	row = frappe.get_cached_value(
		"Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True
	)
	if not row:
		frappe.throw(_("Warehouse {0} does not exist").format(warehouse))
	if row.disabled:
		frappe.throw(_("Warehouse {0} is disabled").format(warehouse))
	if row.is_group:
		frappe.throw(_("Warehouse {0} is a group warehouse").format(warehouse))
	frappe.has_permission("Warehouse", ptype="read", doc=warehouse, throw=True)
	return row


def _truthy(value):
	return value in (True, 1, "1", "true", "True")


def _lookup_idempotency(key, for_update=False):
	"""Read the reservation row; locking reads bypass REPEATABLE READ snapshots."""
	sql = (
		f"SELECT idempotency_key, reference_doctype, reference_name, request_fingerprint "
		f"FROM `{IDEMPOTENCY_TABLE}` WHERE idempotency_key = %s"
	)
	if for_update:
		sql += " FOR UPDATE"
	rows = frappe.db.sql(sql, (key,), as_dict=True)
	return rows[0] if rows else None


def _verify_replay(existing, fingerprint, key):
	"""A replayed key must carry the identical canonical request fingerprint."""
	stored = (existing.get("request_fingerprint") or "") if existing else ""
	if stored and fingerprint and stored != fingerprint:
		frappe.throw(
			_(
				"Idempotency key {0} was already used with a different request payload. "
				"Use a new client_voucher_id for a different voucher."
			).format(key),
			frappe.ValidationError,
		)


def _load_idempotent_document(existing, for_update=False):
	if not existing or not existing.reference_name:
		return None
	if existing.reference_doctype not in {"Sales Invoice", "Stock Entry"}:
		frappe.throw(
			_("Unexpected idempotent document type {0}").format(existing.reference_doctype),
			frappe.ValidationError,
		)
	if for_update:
		rows = frappe.db.sql(
			f"SELECT name FROM `tab{existing.reference_doctype}` WHERE name = %s FOR UPDATE",
			(existing.reference_name,),
		)
		if not rows:
			return None
		doc = frappe.get_doc(existing.reference_doctype, existing.reference_name, for_update=True)
	else:
		if not frappe.db.exists(existing.reference_doctype, existing.reference_name):
			return None
		doc = frappe.get_doc(existing.reference_doctype, existing.reference_name)
	frappe.has_permission(existing.reference_doctype, ptype="read", doc=doc, throw=True)
	return doc


def _wait_for_idempotent_document(key, fingerprint, attempts=10):
	for _attempt in range(attempts):
		existing = _lookup_idempotency(key, for_update=True)
		if existing and existing.reference_name:
			_verify_replay(existing, fingerprint, key)
			if doc := _load_idempotent_document(existing, for_update=True):
				return doc
		frappe.db.rollback()
		time.sleep(0.2)
	return None


def _execute_with_deadlock_retry(callable_fn, retries=3):
	deadlock_exc = getattr(frappe, "QueryDeadlockError", None)
	for attempt in range(retries + 1):
		try:
			return callable_fn()
		except Exception as exc:
			if deadlock_exc is not None and isinstance(exc, deadlock_exc) and attempt < retries:
				frappe.db.rollback()
				base_delay = min(0.02 * (2**attempt), 0.16)
				time.sleep(random.uniform(base_delay, min(base_delay * 2, 0.25)))
				continue
			raise


def _cache_client():
	cache = getattr(frappe, "cache", None)
	if cache is None:
		return None
	if hasattr(cache, "lock"):
		return cache
	if callable(cache):
		return cache()
	return cache


def _acquire_idempotency_lock(key):
	try:
		cache = _cache_client()
		if cache is None:
			raise TypeError("cache unavailable")
		lock = cache.lock(cache.make_key(f"khitmee_ops:idempotency-lock:{key}"), timeout=60)
		if lock.acquire(blocking=False):
			return lock
		return None
	except Exception:
		frappe.log_error("Khitmee idempotency lock unavailable; continuing with database uniqueness")
		return _NullIdempotencyLock()


def _release_lock(lock):
	try:
		lock.release()
	except Exception:
		pass


def _release_lock_after_transaction(lock):
	def release():
		_release_lock(lock)

	frappe.db.after_commit.add(release)
	frappe.db.after_rollback.add(release)


def _reserve_idempotency_key(key, fingerprint):
	"""Atomically insert the reservation row before the voucher exists.

	The unique key serializes concurrent retries at the database level: a conflicting
	insert blocks until the winning transaction commits or rolls back, so committed
	vouchers are never missed even under MariaDB REPEATABLE READ.
	"""
	try:
		frappe.db.sql(
			f"""
			INSERT INTO `{IDEMPOTENCY_TABLE}` (
				`name`, `creation`, `modified`, `modified_by`, `owner`,
				`docstatus`, `idx`, `idempotency_key`, `request_fingerprint`
			) VALUES (%s, NOW(6), NOW(6), %s, %s, 0, 0, %s, %s)
			""",
			(random_string(10), frappe.session.user, frappe.session.user, key, fingerprint),
		)
		return True
	except Exception as exc:
		if frappe.db.is_duplicate_entry(exc):
			return False
		raise


def _bind_reservation(key, reference_doctype, reference_name):
	frappe.db.sql(
		f"""
		UPDATE `{IDEMPOTENCY_TABLE}`
		SET `reference_doctype` = %s, `reference_name` = %s, `modified` = NOW(6)
		WHERE `idempotency_key` = %s
		""",
		(reference_doctype, reference_name, key),
	)


def _run_idempotent(
	client_voucher_id, voucher_kind, reference_doctype, fingerprint, create_document, prepare=None
):
	key = idempotency_key(voucher_kind, client_voucher_id, user=frappe.session.user)

	existing = _lookup_idempotency(key)
	if existing and existing.reference_name:
		_verify_replay(existing, fingerprint, key)
		if doc := _load_idempotent_document(existing):
			return doc, False

	prepared = prepare() if callable(prepare) else None

	# Clear any REPEATABLE READ MVCC snapshot opened by pre-lock reads (such as
	# _lookup_idempotency or master-data cache misses in prepare()) before
	# reserving the idempotency key and acquiring stock locks.
	frappe.db.rollback()
	clear_transaction_stock_locks()

	lock = _acquire_idempotency_lock(key)
	if lock is None:
		if doc := _wait_for_idempotent_document(key, fingerprint):
			return doc, False
		frappe.throw(
			_("Voucher {0} is already being submitted. Retry in a moment.").format(
				normalize_client_voucher_id(client_voucher_id)
			)
		)

	lock_handled = False
	try:
		# Reserve the idempotency row directly; unique index handles duplicates without gap locks.
		reserved = _reserve_idempotency_key(key, fingerprint)
		if not reserved:
			# Another transaction committed or reserved this key first; inspect existing row.
			_release_lock(lock)
			lock_handled = True
			if doc := _wait_for_idempotent_document(key, fingerprint):
				return doc, False
			frappe.throw(
				_("Voucher {0} is already being submitted. Retry in a moment.").format(
					normalize_client_voucher_id(client_voucher_id)
				)
			)

		doc = create_document(prepared) if callable(prepare) else create_document()
		_bind_reservation(key, doc.doctype, doc.name)
		_release_lock_after_transaction(lock)
		lock_handled = True
		return doc, True
	finally:
		if not lock_handled:
			_release_lock(lock)


def _check_voucher_read_permission(doc_or_doctype, name=None):
	if hasattr(doc_or_doctype, "doctype"):
		doc = doc_or_doctype
		frappe.has_permission(doc.doctype, ptype="read", doc=doc, throw=True)
		return

	frappe.has_permission(doc_or_doctype, ptype="read", doc=name, throw=True)


def _receipt_lines_from_sales_invoice(doc):
	return [
		{
			"name": item.item_name or item.item_code,
			"qty": item.qty,
			"rate": item.rate,
			"amount": item.amount,
		}
		for item in doc.items
	]


def _sale_response(doc, printer_type):
	lines = _receipt_lines_from_sales_invoice(doc)
	grand_total = flt(doc.rounded_total) or flt(doc.grand_total)
	currency_precision = frappe.get_precision("Sales Invoice Item", "rate", currency=doc.currency)
	receipt_payload = make_receipt_payload(
		printer_type=printer_type,
		voucher_no=doc.name,
		title="Sales Invoice",
		shop_name=doc.company,
		date=cstr(doc.posting_date),
		party=doc.customer,
		lines=lines,
		total=grand_total,
		footer="Thank you",
		currency_precision=currency_precision,
	)
	return {
		"voucher_no": doc.name,
		"doctype": "Sales Invoice",
		"posting_date": cstr(doc.posting_date),
		"warehouse": doc.set_warehouse,
		"party": doc.customer,
		"items": [
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"qty": item.qty,
				"uom": item.uom,
				"rate": item.rate,
				"amount": item.amount,
			}
			for item in doc.items
		],
		"grand_total": grand_total,
		"receipt_payload": receipt_payload,
	}


def _validate_sale_inputs(customer, price_list, payment_mode):
	if not customer or not frappe.get_cached_value("Customer", customer, "name"):
		frappe.throw(_("A valid Customer is required"))
	frappe.has_permission("Customer", ptype="read", doc=customer, throw=True)
	price_list_row = frappe.get_cached_value(
		"Price List", price_list, ["selling", "enabled"], as_dict=True
	)
	if not price_list_row or not price_list_row.selling or not price_list_row.enabled:
		frappe.throw(_("A valid enabled selling Price List is required"))
	frappe.has_permission("Price List", ptype="read", doc=price_list, throw=True)
	if payment_mode:
		if not frappe.get_cached_value("Mode of Payment", payment_mode, "name"):
			frappe.throw(_("Mode of Payment {0} does not exist").format(payment_mode))
		frappe.has_permission("Mode of Payment", ptype="read", doc=payment_mode, throw=True)


def _sales_invoice_payable_total(doc):
	return flt(getattr(doc, "rounded_total", 0)) or flt(getattr(doc, "grand_total", 0))


def _sales_invoice_payments_match_total(doc, total):
	total = flt(total)
	payment_total = sum(flt(getattr(payment, "amount", 0)) for payment in getattr(doc, "payments", []) or [])
	return (
		abs(flt(getattr(doc, "paid_amount", 0)) - total) <= 0.005
		and abs(payment_total - total) <= 0.005
	)


def _prepare_sale_metadata(warehouse, customer, price_list, payment_mode):
	warehouse_row = _validate_warehouse(warehouse, _("Warehouse"))
	_validate_sale_inputs(customer, price_list, payment_mode)
	return {
		"company": warehouse_row.company,
		"company_currency": frappe.get_cached_value("Company", warehouse_row.company, "default_currency"),
		"customer_currency": frappe.get_cached_value("Customer", customer, "default_currency"),
		"price_list_currency": frappe.get_cached_value("Price List", price_list, "currency"),
	}


def _create_sales_invoice(
	client_voucher_id,
	warehouse,
	customer,
	price_list,
	cleaned_items,
	payment_mode,
	override_rate,
	prepared=None,
):
	meta = prepared if prepared is not None else _prepare_sale_metadata(
		warehouse, customer, price_list, payment_mode
	)
	company = meta["company"]
	company_currency = meta["company_currency"]
	customer_currency = meta["customer_currency"]
	price_list_currency = meta["price_list_currency"]

	invoice_items = []
	for row in cleaned_items:
		item = {
			"item_code": row["item_code"],
			"qty": row["qty"],
			"uom": row["uom"],
			"warehouse": warehouse,
		}
		if override_rate and row["rate"] is not None:
			item["rate"] = row["rate"]
		invoice_items.append(item)

	lock_result = lock_stock_coordinates(sale_bin_pairs(warehouse, cleaned_items))
	posting_date = nowdate()
	posting_time = nowtime()

	invoice_payload = {
		"doctype": "Sales Invoice",
		"customer": customer,
		"company": company,
		"posting_date": posting_date,
		"posting_time": posting_time,
		"set_posting_time": 1,
		"update_stock": 1,
		"set_warehouse": warehouse,
		"selling_price_list": price_list,
		"is_pos": 1 if payment_mode else 0,
		"items": invoice_items,
		"payments": [{"mode_of_payment": payment_mode, "amount": 0}] if payment_mode else [],
	}
	if customer_currency or company_currency:
		invoice_payload["currency"] = customer_currency or company_currency
	if price_list_currency or company_currency:
		invoice_payload["price_list_currency"] = price_list_currency or company_currency
	doc = frappe.get_doc(invoice_payload)
	doc.flags.ignore_pos_profile = True
	if payment_mode:
		doc.flags.khitmee_counter_autopay = True
	if override_rate:
		doc.flags.ignore_pricing_rule = True

	doc.docstatus = 1
	item_codes = [row["item_code"] for row in cleaned_items]
	with (
		voucher_transaction_read_cache(doc, item_codes=item_codes, locked_pairs=lock_result["pairs"]),
		proven_empty_future_sle_fastpath(doc, lock_result["pairs"]),
	):
		doc.insert()
	return doc


def _can_fast_sync_autopay_totals(doc, payable_total):
	payments = getattr(doc, "payments", None) or ()
	if len(payments) != 1:
		return False
	conversion_rate = flt(getattr(doc, "conversion_rate", 1) or 1)
	if abs(conversion_rate - 1.0) > 1e-9:
		return False
	if (
		flt(getattr(doc, "total_advance", 0)) != 0.0
		or flt(getattr(doc, "write_off_amount", 0)) != 0.0
		or flt(getattr(doc, "loyalty_amount", 0)) != 0.0
	):
		return False
	if cint(getattr(doc, "is_return", 0)):
		return False

	total = flt(payable_total)
	payment_total = sum(flt(getattr(payment, "amount", 0)) for payment in payments)
	paid_amount = flt(getattr(doc, "paid_amount", 0))
	if abs(paid_amount - total) > 1e-9 or abs(payment_total - total) > 1e-9:
		return False

	base_payable_total = (
		flt(getattr(doc, "base_rounded_total", 0))
		or flt(getattr(doc, "base_grand_total", 0))
		or total
	)
	if abs(base_payable_total - total) > 1e-9:
		return False
	raw_base_paid = getattr(doc, "base_paid_amount", None)
	if raw_base_paid is not None and abs(flt(raw_base_paid) - base_payable_total) > 1e-9:
		return False
	return True


def sync_counter_autopay_payment(doc, method=None):
	if not cint(getattr(doc, "is_pos", 0)):
		return
	flags = getattr(doc, "flags", {})
	if hasattr(flags, "get"):
		autopay = flags.get("khitmee_counter_autopay")
	else:
		autopay = getattr(flags, "khitmee_counter_autopay", False)
	if not autopay or not getattr(doc, "payments", None):
		return

	payable_total = _sales_invoice_payable_total(doc)
	doc.payments[0].amount = payable_total
	doc.set_paid_amount()
	doc.set_account_for_mode_of_payment()
	if _can_fast_sync_autopay_totals(doc, payable_total):
		if flt(getattr(doc, "base_paid_amount", 0)) == 0 and payable_total != 0:
			doc.base_paid_amount = (
				flt(getattr(doc, "base_rounded_total", 0))
				or flt(getattr(doc, "base_grand_total", 0))
				or flt(doc.paid_amount)
			)
		doc.outstanding_amount = 0.0
		doc.change_amount = 0.0
		doc.base_change_amount = 0.0
	else:
		doc.calculate_taxes_and_totals()
	doc.validate_pos()
	if hasattr(doc, "validate_pos_paid_amount"):
		doc.validate_pos_paid_amount()


@frappe.whitelist(methods=["POST"])
def submit_sale(
	client_voucher_id,
	warehouse,
	customer,
	price_list,
	items,
	payment_mode=None,
	printer_type="voucher",
	override_rate=False,
):
	"""Submit a counter sale.

	By default ERPNext resolves authoritative server-side selling rates for the actual
	customer/UOM/qty/price list; client-supplied rates are ignored unless
	override_rate is explicitly flagged AND the user may edit selling prices.
	"""
	_require_logged_in()
	frappe.has_permission("Sales Invoice", ptype="create", throw=True)
	frappe.has_permission("Sales Invoice", ptype="submit", throw=True)

	override_rate = _truthy(override_rate)
	if override_rate:
		frappe.has_permission("Sales Invoice", ptype="write", throw=True)

	try:
		normalize_client_voucher_id(client_voucher_id)
		items = frappe.parse_json(items)
		_validate_printer_type(printer_type)
	except ValueError as exc:
		frappe.throw(str(exc), frappe.ValidationError)

	cleaned_items = _validate_items(items)
	fingerprint = request_fingerprint(
		customer=customer,
		warehouse=warehouse,
		price_list=price_list,
		payment_mode=payment_mode,
		items=cleaned_items,
	)

	doc, _created = _execute_with_deadlock_retry(
		lambda: _run_idempotent(
			client_voucher_id,
			"sale",
			"Sales Invoice",
			fingerprint,
			lambda prepared: _create_sales_invoice(
				client_voucher_id,
				warehouse,
				customer,
				price_list,
				cleaned_items,
				payment_mode,
				override_rate,
				prepared=prepared,
			),
			prepare=lambda: _prepare_sale_metadata(warehouse, customer, price_list, payment_mode),
		)
	)
	_check_voucher_read_permission(doc)
	return _sale_response(doc, printer_type)


def _transfer_receipt_lines(doc):
	return [
		{"name": item.item_name or item.item_code, "qty": item.qty, "rate": 0, "amount": 0}
		for item in doc.items
	]


def _transfer_response(doc, printer_type):
	total_qty = sum(flt(item.qty) for item in doc.items)
	receipt_payload = make_receipt_payload(
		printer_type=printer_type,
		voucher_no=doc.name,
		title="Stock Transfer",
		shop_name=doc.company,
		date=cstr(doc.posting_date),
		party=doc.to_warehouse or "",
		lines=_transfer_receipt_lines(doc),
		total=total_qty,
		footer="",
		currency_precision=None,
	)
	return {
		"voucher_no": doc.name,
		"doctype": "Stock Entry",
		"posting_date": cstr(doc.posting_date),
		"source_warehouse": doc.from_warehouse,
		"target_warehouse": doc.to_warehouse,
		"items": [
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"qty": item.qty,
				"uom": item.uom,
			}
			for item in doc.items
		],
		"total_qty": total_qty,
		"receipt_payload": receipt_payload,
	}


def _prepare_direct_transfer_metadata(source_warehouse, target_warehouse):
	source_row = _validate_warehouse(source_warehouse, _("Source Warehouse"))
	_validate_warehouse(target_warehouse, _("Target Warehouse"))
	return {"company": source_row.company}


def _create_direct_or_dispatch_transfer(
	source_warehouse, target_warehouse, transfer_mode, cleaned_items, prepared=None
):
	meta = prepared if prepared is not None else _prepare_direct_transfer_metadata(
		source_warehouse, target_warehouse
	)

	lock_result = lock_stock_coordinates(
		transfer_bin_pairs(source_warehouse, target_warehouse, cleaned_items)
	)
	posting_date = nowdate()
	posting_time = nowtime()

	doc = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Transfer",
			"purpose": "Material Transfer",
			"company": meta["company"],
			"posting_date": posting_date,
			"posting_time": posting_time,
			"set_posting_time": 1,
			"from_warehouse": source_warehouse,
			"to_warehouse": target_warehouse,
			"add_to_transit": 1 if transfer_mode == "dispatch" else 0,
			"items": [
				{
					"item_code": row["item_code"],
					"qty": row["qty"],
					"uom": row["uom"],
					"s_warehouse": source_warehouse,
					"t_warehouse": target_warehouse,
				}
				for row in cleaned_items
			],
		}
	)
	doc.docstatus = 1
	item_codes = [row["item_code"] for row in cleaned_items]
	with (
		voucher_transaction_read_cache(doc, item_codes=item_codes, locked_pairs=lock_result["pairs"]),
		proven_empty_future_sle_fastpath(doc, lock_result["pairs"]),
	):
		doc.insert()
	return doc


def _requested_stock_quantities(cleaned_items):
	requested = {}
	for row in cleaned_items:
		uom = row["uom"] or frappe.get_cached_value("Item", row["item_code"], "stock_uom")
		conversion_factor = get_conversion_factor(row["item_code"], uom, throw_if_missing=True)[
			"conversion_factor"
		]
		requested[row["item_code"]] = requested.get(row["item_code"], 0) + (
			flt(row["qty"]) * flt(conversion_factor)
		)
	return requested


def _is_tracked_row(row_data):
	return bool(
		row_data.get("serial_and_batch_bundle") or row_data.get("serial_no") or row_data.get("batch_no")
	)


def _create_receive_transfer(
	source_warehouse, target_warehouse, outgoing_stock_entry, cleaned_items
):
	if not outgoing_stock_entry or not frappe.db.exists("Stock Entry", outgoing_stock_entry):
		frappe.throw(_("A valid outgoing Stock Entry is required for receive mode"))
	frappe.has_permission("Stock Entry", ptype="read", doc=outgoing_stock_entry, throw=True)
	_validate_warehouse(source_warehouse, _("Source Warehouse"))
	_validate_warehouse(target_warehouse, _("Target Warehouse"))

	outgoing = frappe.get_doc("Stock Entry", outgoing_stock_entry)
	if outgoing.docstatus != 1 or outgoing.purpose != "Material Transfer" or not outgoing.add_to_transit:
		frappe.throw(_("Outgoing Stock Entry must be a submitted dispatch transfer"))
	outgoing_targets = {item.t_warehouse for item in outgoing.items}
	if source_warehouse not in outgoing_targets:
		frappe.throw(_("Source Warehouse {0} is not the transit warehouse of {1}").format(source_warehouse, outgoing_stock_entry))

	mapped = make_stock_in_entry(outgoing_stock_entry)
	if isinstance(mapped, dict):
		mapped = frappe.get_doc(mapped)
	if not mapped.items:
		frappe.throw(_("No remaining quantity exists on {0}").format(outgoing_stock_entry))

	requested = _requested_stock_quantities(cleaned_items)
	# Only rows that genuinely originate from the requested transit warehouse may
	# satisfy this receipt; each row keeps its own ste_detail and s_warehouse.
	available_rows = {}
	for row in mapped.items:
		if cstr(row.s_warehouse) != cstr(source_warehouse):
			continue
		available_rows.setdefault(row.item_code, []).append(row)

	missing = sorted(set(requested) - set(available_rows))
	if missing:
		frappe.throw(_("Items not available on {0}: {1}").format(outgoing_stock_entry, ", ".join(missing)))

	new_rows = []
	remaining = dict(requested)
	for item_code, rows in available_rows.items():
		needed = remaining.get(item_code, 0)
		for source_row in rows:
			if needed <= 0:
				break
			row_stock_capacity = flt(source_row.qty) * flt(source_row.conversion_factor or 1)
			allocated_stock_qty = min(row_stock_capacity, needed)
			if allocated_stock_qty <= 0:
				continue
			row_data = source_row.as_dict().copy()
			for transient_field in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"idx",
				"docstatus",
				"creation",
				"modified",
				"modified_by",
				"owner",
			):
				row_data.pop(transient_field, None)
			if _is_tracked_row(row_data) and allocated_stock_qty < row_stock_capacity:
				frappe.throw(
					_(
						"Item {0} is serial/batch tracked; partial receipt requires exact "
						"serial/batch allocations, which the counter does not capture."
					).format(item_code)
				)
			row_data["qty"] = allocated_stock_qty / flt(source_row.conversion_factor or 1)
			row_data["transfer_qty"] = allocated_stock_qty
			row_data["t_warehouse"] = target_warehouse
			new_rows.append(row_data)
			needed -= allocated_stock_qty
		if needed > 0:
			frappe.throw(_("Requested qty for {0} exceeds the remaining transfer qty").format(item_code))

	mapped.items = []
	for row_data in new_rows:
		mapped.append("items", row_data)
	lock_stock_coordinates(transfer_bin_pairs(source_warehouse, target_warehouse, new_rows))
	mapped.posting_date = nowdate()
	mapped.posting_time = nowtime()
	mapped.set_posting_time = 1
	mapped.to_warehouse = target_warehouse
	mapped.from_warehouse = source_warehouse
	mapped.docstatus = 1
	mapped.insert()
	return mapped


def _create_transfer(
	source_warehouse, target_warehouse, transfer_mode, outgoing_stock_entry, cleaned_items, prepared=None
):
	if transfer_mode in {"direct", "dispatch"}:
		return _create_direct_or_dispatch_transfer(
			source_warehouse, target_warehouse, transfer_mode, cleaned_items, prepared=prepared
		)
	if transfer_mode == "receive":
		return _create_receive_transfer(
			source_warehouse, target_warehouse, outgoing_stock_entry, cleaned_items
		)
	frappe.throw(_("transfer_mode must be direct, dispatch, or receive"))


@frappe.whitelist(methods=["POST"])
def submit_transfer(
	client_voucher_id,
	source_warehouse,
	target_warehouse,
	transfer_mode,
	outgoing_stock_entry=None,
	items=None,
	printer_type="voucher",
):
	_require_logged_in()
	frappe.has_permission("Stock Entry", ptype="create", throw=True)
	frappe.has_permission("Stock Entry", ptype="submit", throw=True)

	try:
		normalize_client_voucher_id(client_voucher_id)
		items = frappe.parse_json(items)
		_validate_printer_type(printer_type)
	except ValueError as exc:
		frappe.throw(str(exc), frappe.ValidationError)

	cleaned_items = _validate_items(items)
	fingerprint = request_fingerprint(
		source_warehouse=source_warehouse,
		target_warehouse=target_warehouse,
		transfer_mode=transfer_mode,
		outgoing_stock_entry=outgoing_stock_entry,
		items=cleaned_items,
	)

	prepare_fn = (
		(lambda: _prepare_direct_transfer_metadata(source_warehouse, target_warehouse))
		if transfer_mode in {"direct", "dispatch"}
		else None
	)
	doc, _created = _execute_with_deadlock_retry(
		lambda: _run_idempotent(
			client_voucher_id,
			"transfer",
			"Stock Entry",
			fingerprint,
			(
				(lambda prepared: _create_transfer(
					source_warehouse, target_warehouse, transfer_mode, outgoing_stock_entry, cleaned_items, prepared=prepared
				))
				if prepare_fn
				else (lambda: _create_transfer(
					source_warehouse, target_warehouse, transfer_mode, outgoing_stock_entry, cleaned_items
				))
			),
			prepare=prepare_fn,
		)
	)
	_check_voucher_read_permission(doc)
	return _transfer_response(doc, printer_type)
