"""Deadlock-safe deterministic Item + Bin row locking for stock-posting vouchers.

All stock writers (counter sales, direct/dispatch/receive transfers, and native
Desk Sales Invoice / Stock Entry / Delivery Note / Stock Reconciliation /
Purchase Receipt / Purchase Invoice submissions) acquire InnoDB row locks in the
exact same global order:

1. Sorted `tabItem` rows (batched `ORDER BY name FOR UPDATE`), which serializes
   concurrent creation of brand-new `tabBin` rows as well as Item master updates.
2. Existing `tabBin` rows in sorted `(item_code, warehouse)` order (batched
   `ORDER BY item_code, warehouse, name FOR UPDATE`). Missing Bin rows are never
   rejected because the Item lock already serializes concurrent Bin creation.

A transaction-scoped `threading.local()` guard (cleared via `after_commit` and
`after_rollback`) makes `before_submit` hooks re-entrant: when `submit_sale` or
`submit_transfer` already locked the superset of `(items, pairs)`, the hook is a
zero-SQL no-op.
"""

from itertools import islice
import threading

import frappe
from frappe.utils import cint

LOCK_BATCH_SIZE = 500

_TXN_LOCK_STATE = threading.local()


def batched(iterable, size):
	iterator = iter(iterable)
	while chunk := tuple(islice(iterator, size)):
		yield chunk


def _get_txn_lock_state():
	state = getattr(_TXN_LOCK_STATE, "held", None)
	if state is None:
		state = {"items": set(), "pairs": set(), "registered": False}
		_TXN_LOCK_STATE.held = state
	return state


def clear_transaction_stock_locks():
	"""Clear the transaction-scoped re-entrancy guard for the current thread."""
	state = getattr(_TXN_LOCK_STATE, "held", None)
	if state is not None:
		state["items"].clear()
		state["pairs"].clear()
		state["registered"] = False


def _register_txn_cleanup(state):
	if state["registered"]:
		return
	db = getattr(frappe, "db", None)
	after_commit = getattr(db, "after_commit", None)
	after_rollback = getattr(db, "after_rollback", None)
	if hasattr(after_commit, "add") and hasattr(after_rollback, "add"):
		after_commit.add(clear_transaction_stock_locks)
		after_rollback.add(clear_transaction_stock_locks)
		state["registered"] = True


def ordered_item_codes(item_codes):
	"""Return sorted, deduplicated non-empty Item codes."""
	cleaned = set()
	for code in item_codes or ():
		value = str(code or "").strip()
		if value:
			cleaned.add(value)
	return sorted(cleaned)


def ordered_bin_pairs(pairs):
	"""Return sorted, deduplicated (item_code, warehouse) tuples with non-empty values."""
	cleaned = set()
	for item_code, warehouse in pairs or ():
		item_value = str(item_code or "").strip()
		wh_value = str(warehouse or "").strip()
		if item_value and wh_value:
			cleaned.add((item_value, wh_value))
	return sorted(cleaned)


def sale_bin_pairs(warehouse, cleaned_items, *, company, is_pos):
	"""Resolve counter rows and their packed components before acquiring stock locks."""
	item_codes = ordered_item_codes(row.get("item_code") for row in cleaned_items)
	pairs = [(code, warehouse) for code in item_codes]
	for chunk in batched(item_codes, LOCK_BATCH_SIZE):
		placeholders = ", ".join(["%s"] * len(chunk))
		# Counter rows have no linked orders, pick lists or existing packed rows.
		# Match ERPNext's packed warehouse rule for a new, unlinked invoice.
		pairs.extend(
			frappe.db.sql(
				f"""
				SELECT component.item_code,
					CASE WHEN %s OR item.is_stock_item THEN %s
						ELSE COALESCE(NULLIF(defaults.default_warehouse, ''), %s)
					END
				FROM `tabProduct Bundle` AS bundle
				JOIN `tabProduct Bundle Item` AS component ON component.parent = bundle.name
				LEFT JOIN `tabItem` AS item ON item.name = component.item_code
				LEFT JOIN `tabItem Default` AS defaults
					ON defaults.parent = item.name AND defaults.company = %s
				WHERE bundle.disabled = 0 AND bundle.new_item_code IN ({placeholders})
				""",
				(int(is_pos), warehouse, warehouse, company, *chunk),
			)
		)
	return ordered_bin_pairs(pairs)


def transfer_bin_pairs(source_warehouse, target_warehouse, cleaned_items):
	"""Return deterministic source and target (item_code, warehouse) coordinates for a transfer."""
	pairs = []
	for row in cleaned_items or ():
		item_code = row.get("item_code")
		pairs.append((item_code, source_warehouse))
		pairs.append((item_code, target_warehouse))
	return ordered_bin_pairs(pairs)


def _doc_rows(doc, fieldname):
	return (doc.get(fieldname) if hasattr(doc, "get") else getattr(doc, fieldname, None)) or ()


def document_item_codes(doc):
	"""Return sorted, deduplicated Item codes referenced by items and packed_items."""
	codes = []
	for fieldname in ("items", "packed_items"):
		for row in _doc_rows(doc, fieldname):
			codes.append(row.get("item_code") if hasattr(row, "get") else getattr(row, "item_code", None))
	return ordered_item_codes(codes)


def document_bin_pairs(doc):
	"""Return sorted, deduplicated (item_code, warehouse) pairs for items, packed_items, and internal transfers."""
	default_wh = doc.get("set_warehouse") if hasattr(doc, "get") else getattr(doc, "set_warehouse", None)
	default_target_wh = (
		doc.get("set_target_warehouse") if hasattr(doc, "get") else getattr(doc, "set_target_warehouse", None)
	)
	default_from = (
		doc.get("from_warehouse") if hasattr(doc, "get") else getattr(doc, "from_warehouse", None)
	)
	default_to = doc.get("to_warehouse") if hasattr(doc, "get") else getattr(doc, "to_warehouse", None)

	pairs = []
	for fieldname in ("items", "packed_items"):
		for row in _doc_rows(doc, fieldname):
			if hasattr(row, "get"):
				item_code = row.get("item_code")
				warehouse = row.get("warehouse")
				s_warehouse = row.get("s_warehouse")
				t_warehouse = row.get("t_warehouse")
				target_warehouse = row.get("target_warehouse")
			else:
				item_code = getattr(row, "item_code", None)
				warehouse = getattr(row, "warehouse", None)
				s_warehouse = getattr(row, "s_warehouse", None)
				t_warehouse = getattr(row, "t_warehouse", None)
				target_warehouse = getattr(row, "target_warehouse", None)

			if s_warehouse or t_warehouse:
				if s_warehouse or default_from:
					pairs.append((item_code, s_warehouse or default_from))
				if t_warehouse or default_to:
					pairs.append((item_code, t_warehouse or default_to))
			elif warehouse or default_wh:
				pairs.append((item_code, warehouse or default_wh))

			if target_warehouse or default_target_wh:
				pairs.append((item_code, target_warehouse or default_target_wh))
	return ordered_bin_pairs(pairs)


def _row_value(row, key, index):
	if hasattr(row, "get"):
		return row.get(key)
	return row[index]


def lock_items(item_codes):
	"""Acquire deterministic sorted FOR UPDATE locks on existing tabItem rows."""
	ordered = ordered_item_codes(item_codes)
	if not ordered:
		return []

	locked = []
	for chunk in batched(ordered, LOCK_BATCH_SIZE):
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = frappe.db.sql(
			f"""
			SELECT name
			FROM `tabItem`
			WHERE name IN ({placeholders})
			ORDER BY name
			FOR UPDATE
			""",
			tuple(chunk),
		)
		for row in rows or ():
			locked.append(_row_value(row, "name", 0))
	return locked


def lock_existing_bins(pairs):
	"""Acquire deterministic sorted FOR UPDATE locks on existing tabBin rows.

	Missing Bin rows are intentionally allowed: callers hold sorted tabItem locks
	first, which serializes concurrent creation of new Bin rows without rejecting
	valid first-time stock receipts or transfers.
	"""
	ordered = ordered_bin_pairs(pairs)
	if not ordered:
		return []

	locked = []
	for chunk in batched(ordered, LOCK_BATCH_SIZE):
		placeholders = ", ".join(["(%s, %s)"] * len(chunk))
		params = []
		for item_code, warehouse in chunk:
			params.append(item_code)
			params.append(warehouse)
		rows = frappe.db.sql(
			f"""
			SELECT name, item_code, warehouse
			FROM `tabBin`
			WHERE (item_code, warehouse) IN ({placeholders})
			ORDER BY item_code, warehouse, name
			FOR UPDATE
			""",
			tuple(params),
		)
		for row in rows or ():
			locked.append(
				{
					"name": _row_value(row, "name", 0),
					"item_code": _row_value(row, "item_code", 1),
					"warehouse": _row_value(row, "warehouse", 2),
				}
			)
	return locked


def lock_stock_coordinates(pairs, item_codes=None, *, require_prelocked=False):
	"""Acquire sorted tabItem locks followed by sorted existing tabBin locks.

	Re-entrant within a single transaction: if the current transaction already
	holds locks for the superset of requested items and pairs, returns immediately
	with zero SQL.
	"""
	ordered_pairs = ordered_bin_pairs(pairs)
	combined_items = list(item_codes or ())
	combined_items.extend(item_code for item_code, _warehouse in ordered_pairs)
	ordered_items = ordered_item_codes(combined_items)

	if not ordered_items and not ordered_pairs:
		return {"items": [], "bins": [], "pairs": []}

	state = _get_txn_lock_state()
	if state["registered"] and state["items"].issuperset(ordered_items) and state["pairs"].issuperset(ordered_pairs):
		return {
			"items": ordered_items,
			"bins": [],
			"pairs": ordered_pairs,
		}

	if require_prelocked:
		frappe.throw(
			frappe._("The sale's stock items or warehouses changed during submission. Please retry."),
			frappe.ValidationError,
		)

	locked_items = lock_items(ordered_items)
	locked_bins = lock_existing_bins(ordered_pairs)

	_register_txn_cleanup(state)
	if state["registered"]:
		state["items"].update(ordered_items)
		state["pairs"].update(ordered_pairs)

	return {
		"items": locked_items,
		"bins": locked_bins,
		"pairs": ordered_pairs,
	}


def lock_stock_voucher_before_submit(doc, method=None):
	"""Re-entrant before_submit lock hook covering all stock-posting doctypes."""
	doctype = str(
		(doc.get("doctype") if hasattr(doc, "get") else getattr(doc, "doctype", "")) or ""
	)
	item_codes = document_item_codes(doc)
	if doctype == "Sales Invoice":
		update_stock = cint(
			doc.get("update_stock") if hasattr(doc, "get") else getattr(doc, "update_stock", 0)
		)
		if not update_stock:
			return {"items": [], "bins": [], "pairs": []}
		pairs = document_bin_pairs(doc)
	elif doctype == "Purchase Invoice":
		update_stock = cint(
			doc.get("update_stock") if hasattr(doc, "get") else getattr(doc, "update_stock", 0)
		)
		pairs = document_bin_pairs(doc) if update_stock else []
	else:
		pairs = document_bin_pairs(doc)

	if not item_codes and not pairs:
		return {"items": [], "bins": [], "pairs": []}
	if getattr(getattr(doc, "flags", None), "khitmee_stock_prelocked", False):
		return lock_stock_coordinates(pairs, item_codes=item_codes, require_prelocked=True)
	return lock_stock_coordinates(pairs, item_codes=item_codes)


def lock_purchase_receipt_stock(doc, method=None):
	"""before_submit hook for Purchase Receipt: lock sorted Items then existing Bins."""
	item_codes = document_item_codes(doc)
	pairs = document_bin_pairs(doc)
	if not item_codes and not pairs:
		return {"items": [], "bins": [], "pairs": []}
	return lock_stock_coordinates(pairs, item_codes=item_codes)


def lock_purchase_invoice_stock(doc, method=None):
	"""before_submit hook for Purchase Invoice: lock sorted Items and (if update_stock) existing Bins."""
	item_codes = document_item_codes(doc)
	update_stock = cint(
		doc.get("update_stock") if hasattr(doc, "get") else getattr(doc, "update_stock", 0)
	)
	pairs = document_bin_pairs(doc) if update_stock else []
	if not item_codes and not pairs:
		return {"items": [], "bins": [], "pairs": []}
	return lock_stock_coordinates(pairs, item_codes=item_codes)
