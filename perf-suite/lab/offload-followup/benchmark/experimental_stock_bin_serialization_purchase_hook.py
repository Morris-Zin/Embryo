"""Benchmark-only Bin-row serialization for app-originated stock writes.

This module is copied only into disposable measurement arms. It is not imported by the
reviewed application hooks in the repository. The wrapped functions remain the app's own
sale and direct/dispatch transfer constructors; the only behavioral change is that every
stock item/warehouse Bin row and, for Purchase Receipt admission, every Item master row needed by the voucher is locked in deterministic order before
the constructor assigns posting_date/posting_time and enters ERPNext document insertion.

Scope/limitations, deliberately narrow:
- app-originated counter sales, direct/dispatch transfers, and Purchase Receipt admission only;
- receive-mode transfers, native purchase invoices, desk/API documents other than the
  hooked Purchase Receipt before-submit admission lock, serial/batch-specific allocation rules,
  and repost/cancellation paths are not covered;
- a missing or ambiguous Bin row is rejected rather than bypassed;
- independent native transactions do not participate in this admission order, so no
  general ERPNext concurrency or valuation policy is claimed.
"""
from functools import wraps
import json
import os
import time
import uuid

import frappe
from frappe import _

from khitmee_ops.api import vouchers


ATTEMPT_LOG_PATH = "/tmp/khitmee-bin-serialization-attempts.jsonl"


def _append_attempt_record(record):
	# Single small O_APPEND write per request; benchmark-only and contains no credentials or payload.
	line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
	fd = os.open(ATTEMPT_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
	try:
		os.write(fd, line)
	finally:
		os.close(fd)


def _transfer_logical_voucher_id():
	form = getattr(frappe, "form_dict", None)
	if hasattr(form, "get"):
		return form.get("client_voucher_id")
	return None


def _begin_attempt(voucher_type, logical_voucher_id, coordinate_count):
	attempt = {
		"attempt_id": uuid.uuid4().hex,
		"voucher_type": voucher_type,
		"logical_voucher_id": logical_voucher_id,
		"coordinate_count": coordinate_count,
		"lock_count": 0,
		"successful_lock_count": 0,
		"failed_lock_count": 0,
		"error_type": None,
		"reference_doctype": None,
		"reference_name": None,
		"recorded": False,
		"started_unix": time.time(),
		"process_id": os.getpid(),
	}

	def record(outcome):
		if attempt["recorded"]:
			return
		attempt["recorded"] = True
		out = {key: attempt[key] for key in (
			"attempt_id", "voucher_type", "logical_voucher_id", "coordinate_count", "lock_count",
			"successful_lock_count", "failed_lock_count", "error_type", "reference_doctype",
			"reference_name", "started_unix", "process_id"
		)}
		out["outcome"] = outcome
		out["recorded_unix"] = time.time()
		try:
			_append_attempt_record(out)
		except Exception:
			# Observability must never change voucher semantics in this disposable experiment.
			pass

	frappe.db.after_commit.add(lambda: record("committed"))
	frappe.db.after_rollback.add(lambda: record("rolled_back"))
	return attempt


def _finish_attempt(attempt, function, *args):
	try:
		doc = function(*args)
	except Exception as exc:
		attempt["error_type"] = type(exc).__name__
		raise
	attempt["reference_doctype"] = getattr(doc, "doctype", None)
	attempt["reference_name"] = getattr(doc, "name", None)
	return doc


def _clean_pair(item_code, warehouse):
	item_code = str(item_code or "").strip()
	warehouse = str(warehouse or "").strip()
	if not item_code or not warehouse:
		frappe.throw(
			_("Stock serialization requires a non-empty item code and warehouse"),
			frappe.ValidationError,
		)
	return item_code, warehouse


def _ordered_pairs(pairs):
	return sorted({_clean_pair(item_code, warehouse) for item_code, warehouse in pairs})


def sale_bin_pairs(warehouse, cleaned_items):
	"""Return deduplicated item/source Bin coordinates for a counter sale."""
	return _ordered_pairs((row.get("item_code"), warehouse) for row in cleaned_items)


def transfer_bin_pairs(source_warehouse, target_warehouse, cleaned_items):
	"""Return deduplicated source and target Bin coordinates for a direct/dispatch transfer."""
	pairs = []
	for row in cleaned_items:
		item_code = row.get("item_code")
		pairs.append((item_code, source_warehouse))
		pairs.append((item_code, target_warehouse))
	return _ordered_pairs(pairs)


def lock_existing_bins(pairs, attempt=None):
	"""Lock existing Bin rows in caller-visible deterministic item/warehouse order.

	The locks are ordinary InnoDB row locks in the current transaction and are therefore
	released by the request's commit or rollback, not by this function. Rows are acquired
	one at a time in sorted order so overlapping app writers cannot acquire the same set
	in reverse order.
	"""
	locked = []
	for item_code, warehouse in _ordered_pairs(pairs):
		try:
			rows = frappe.db.sql(
				"SELECT name, item_code, warehouse FROM `tabBin` "
				"WHERE item_code = %s AND warehouse = %s FOR UPDATE",
				(item_code, warehouse),
			)
		except Exception as exc:
			if attempt is not None:
				attempt["failed_lock_count"] += 1
				attempt["lock_count"] = attempt["successful_lock_count"] + attempt["failed_lock_count"]
				attempt["error_type"] = attempt["error_type"] or type(exc).__name__
			raise
		if attempt is not None:
			attempt["successful_lock_count"] += 1
			attempt["lock_count"] = attempt["successful_lock_count"] + attempt["failed_lock_count"]
		if len(rows) != 1:
			frappe.throw(
				_(
					"Stock serialization requires exactly one existing Bin row for item {0} "
					"in warehouse {1}; found {2}. The voucher is rejected before posting."
				).format(item_code, warehouse, len(rows)),
				frappe.ValidationError,
			)
		row = rows[0]
		locked.append({"name": row[0], "item_code": row[1], "warehouse": row[2]})
	return locked


def purchase_receipt_bin_pairs(doc):
	"""Return deduplicated item/warehouse Bin coordinates needed by one Purchase Receipt."""
	default_warehouse = doc.get("set_warehouse") if hasattr(doc, "get") else None
	return _ordered_pairs(
		(row.get("item_code"), row.get("warehouse") or default_warehouse)
		for row in (doc.get("items") or [])
	)


def purchase_document_item_codes(doc):
	"""Return deduplicated Item master rows needed by one purchase document."""
	return sorted({str(row.get("item_code") or "").strip() for row in (doc.get("items") or [])} - {""})


def lock_existing_items(item_codes):
	"""Lock existing Item rows in deterministic order before native purchase posting."""
	locked = []
	for item_code in sorted(set(item_codes)):
		rows = frappe.db.sql(
			"SELECT name FROM `tabItem` WHERE name = %s FOR UPDATE",
			(item_code,),
		)
		if len(rows) != 1:
			frappe.throw(
				_(
					"Stock serialization requires exactly one existing Item row for item {0}; "
					"found {1}. The voucher is rejected before posting."
				).format(item_code, len(rows)),
				frappe.ValidationError,
			)
		locked.append(rows[0][0])
	return locked


def _purchase_logical_voucher_id(doc):
	remarks = doc.get("remarks") if hasattr(doc, "get") else None
	marker = "Khitmee seasonal client_voucher_id="
	if remarks and marker in remarks:
		return remarks.split(marker, 1)[1].strip().split()[0]
	return None


def lock_purchase_receipt_bins(doc, method=None):
	"""Acquire the same deterministic Bin admission locks before native receipt stock posting.

	This document-event hook runs before Purchase Receipt.on_submit inserts the first Stock
	Ledger Entry. Native receipts therefore acquire Bin rows in the same Bin-before-SLE
	order as the serialized app-originated writers instead of the native SLE-before-Bin order.
	"""
	pairs = purchase_receipt_bin_pairs(doc)
	if not pairs:
		return []
	attempt = _begin_attempt("purchase_receipt", _purchase_logical_voucher_id(doc), len(pairs))
	attempt["reference_doctype"] = doc.get("doctype") if hasattr(doc, "get") else "Purchase Receipt"
	attempt["reference_name"] = doc.get("name") if hasattr(doc, "get") else None
	try:
		lock_existing_items(purchase_document_item_codes(doc))
		return lock_existing_bins(pairs, attempt=attempt)
	except Exception as exc:
		attempt["error_type"] = type(exc).__name__
		raise



def lock_purchase_invoice_items(doc, method=None):
	"""Acquire deterministic Item admission locks before native Purchase Invoice posting."""
	return lock_existing_items(purchase_document_item_codes(doc))


_ORIGINAL_CREATE_SALES_INVOICE = vouchers._create_sales_invoice
_ORIGINAL_CREATE_DIRECT_OR_DISPATCH_TRANSFER = vouchers._create_direct_or_dispatch_transfer


@wraps(_ORIGINAL_CREATE_SALES_INVOICE)
def create_sales_invoice_serialized(
	client_voucher_id, warehouse, customer, price_list, cleaned_items, payment_mode, override_rate
):
	# Repeat the constructor's initial validation before locking so invalid-input error
	# precedence remains unchanged; the original constructor validates again unchanged.
	vouchers._validate_warehouse(warehouse, _("Warehouse"))
	vouchers._validate_sale_inputs(customer, price_list, payment_mode)
	pairs = sale_bin_pairs(warehouse, cleaned_items)
	attempt = _begin_attempt("sale", client_voucher_id, len(pairs))
	try:
		lock_existing_bins(pairs, attempt=attempt)
	except Exception as exc:
		attempt["error_type"] = type(exc).__name__
		raise
	return _finish_attempt(
		attempt,
		_ORIGINAL_CREATE_SALES_INVOICE,
		client_voucher_id,
		warehouse,
		customer,
		price_list,
		cleaned_items,
		payment_mode,
		override_rate,
	)


@wraps(_ORIGINAL_CREATE_DIRECT_OR_DISPATCH_TRANSFER)
def create_direct_or_dispatch_transfer_serialized(
	source_warehouse, target_warehouse, transfer_mode, cleaned_items
):
	# Repeat the constructor's initial validation before locking so invalid-input error
	# precedence remains unchanged; the original constructor validates again unchanged.
	vouchers._validate_warehouse(source_warehouse, _("Source Warehouse"))
	vouchers._validate_warehouse(target_warehouse, _("Target Warehouse"))
	pairs = transfer_bin_pairs(source_warehouse, target_warehouse, cleaned_items)
	attempt = _begin_attempt("transfer", _transfer_logical_voucher_id(), len(pairs))
	try:
		lock_existing_bins(pairs, attempt=attempt)
	except Exception as exc:
		attempt["error_type"] = type(exc).__name__
		raise
	return _finish_attempt(
		attempt,
		_ORIGINAL_CREATE_DIRECT_OR_DISPATCH_TRANSFER,
		source_warehouse,
		target_warehouse,
		transfer_mode,
		cleaned_items,
	)


def install():
	if getattr(vouchers._create_sales_invoice, "_khitmee_bin_serialized", False):
		return
	create_sales_invoice_serialized._khitmee_bin_serialized = True
	create_direct_or_dispatch_transfer_serialized._khitmee_bin_serialized = True
	vouchers._create_sales_invoice = create_sales_invoice_serialized
	vouchers._create_direct_or_dispatch_transfer = create_direct_or_dispatch_transfer_serialized


install()
