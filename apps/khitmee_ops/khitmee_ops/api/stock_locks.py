"""Deadlock-safe deterministic Item + Bin row locking for stock-posting vouchers.

All stock writers (counter sales, direct/dispatch/receive transfers, and native
Purchase Receipt / Purchase Invoice submissions) acquire InnoDB row locks in the
exact same global order:

1. Sorted `tabItem` rows (batched `ORDER BY name FOR UPDATE`), which serializes
   concurrent creation of brand-new `tabBin` rows as well as Item master updates.
2. Existing `tabBin` rows in sorted `(item_code, warehouse)` order (batched
   `ORDER BY item_code, warehouse, name FOR UPDATE`). Missing Bin rows are never
   rejected because the Item lock already serializes concurrent Bin creation.
"""

from itertools import islice

import frappe
from frappe.utils import cint

LOCK_BATCH_SIZE = 500


def batched(iterable, size):
	iterator = iter(iterable)
	while chunk := tuple(islice(iterator, size)):
		yield chunk


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


def sale_bin_pairs(warehouse, cleaned_items):
	"""Return deterministic (item_code, warehouse) coordinates for a counter sale."""
	return ordered_bin_pairs((row.get("item_code"), warehouse) for row in (cleaned_items or ()))


def transfer_bin_pairs(source_warehouse, target_warehouse, cleaned_items):
	"""Return deterministic source and target (item_code, warehouse) coordinates for a transfer."""
	pairs = []
	for row in cleaned_items or ():
		item_code = row.get("item_code")
		pairs.append((item_code, source_warehouse))
		pairs.append((item_code, target_warehouse))
	return ordered_bin_pairs(pairs)


def document_item_codes(doc):
	"""Return sorted, deduplicated Item codes referenced by a voucher document."""
	items = doc.get("items") if hasattr(doc, "get") else getattr(doc, "items", None)
	return ordered_item_codes(
		row.get("item_code") if hasattr(row, "get") else getattr(row, "item_code", None)
		for row in (items or ())
	)


def document_bin_pairs(doc):
	"""Return sorted, deduplicated (item_code, warehouse) pairs referenced by a voucher document."""
	items = doc.get("items") if hasattr(doc, "get") else getattr(doc, "items", None)
	default_wh = doc.get("set_warehouse") if hasattr(doc, "get") else getattr(doc, "set_warehouse", None)
	default_from = (
		doc.get("from_warehouse") if hasattr(doc, "get") else getattr(doc, "from_warehouse", None)
	)
	default_to = doc.get("to_warehouse") if hasattr(doc, "get") else getattr(doc, "to_warehouse", None)

	pairs = []
	for row in items or ():
		if hasattr(row, "get"):
			item_code = row.get("item_code")
			warehouse = row.get("warehouse")
			s_warehouse = row.get("s_warehouse")
			t_warehouse = row.get("t_warehouse")
		else:
			item_code = getattr(row, "item_code", None)
			warehouse = getattr(row, "warehouse", None)
			s_warehouse = getattr(row, "s_warehouse", None)
			t_warehouse = getattr(row, "t_warehouse", None)

		if s_warehouse or t_warehouse:
			if s_warehouse or default_from:
				pairs.append((item_code, s_warehouse or default_from))
			if t_warehouse or default_to:
				pairs.append((item_code, t_warehouse or default_to))
		elif warehouse or default_wh:
			pairs.append((item_code, warehouse or default_wh))
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


def lock_stock_coordinates(pairs, item_codes=None):
	"""Acquire sorted tabItem locks followed by sorted existing tabBin locks.

	Returns a dict with:
	- `items`: locked tabItem names
	- `bins`: locked existing tabBin rows
	- `pairs`: normalized sorted `(item_code, warehouse)` tuples covered by the lock
	"""
	ordered_pairs = ordered_bin_pairs(pairs)
	combined_items = list(item_codes or ())
	combined_items.extend(item_code for item_code, _warehouse in ordered_pairs)
	ordered_items = ordered_item_codes(combined_items)

	locked_items = lock_items(ordered_items)
	locked_bins = lock_existing_bins(ordered_pairs)
	return {
		"items": locked_items,
		"bins": locked_bins,
		"pairs": ordered_pairs,
	}


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
