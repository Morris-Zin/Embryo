"""Ordered Item-before-Bin locking for counter vouchers and Desk submit hooks.

Legacy retains its batched exclusive locks. ADR 0003 qualifies shared Item
locks for ordinary sales and uses ordered, indexed Bin point locks. Missing
coordinates need an exclusive Item. A surprise miss requires a full rollback.
Transaction callbacks clear ownership when commit or rollback ends the plan.
"""

from dataclasses import dataclass, replace
from itertools import islice
import threading

import frappe
from frappe.utils import cint

LOCK_BATCH_SIZE = 500

_TXN_LOCK_STATE = threading.local()
_ORDINARY_VALUATION = {"FIFO", "Moving Average"}
_ITEM_TRAITS = "item.is_stock_item, item.has_serial_no, item.has_batch_no, item.allow_negative_stock, item.valuation_method"


class StockLockRestart(RuntimeError):
	"""The whole voucher transaction must be rolled back before retrying."""

	def __init__(self, items):
		self.items = frozenset(items)
		super().__init__("Stock items or warehouses changed during submission. Please retry the voucher.")


@dataclass(frozen=True)
class StockLockPlan:
	items: tuple
	pairs: tuple
	aliases: dict
	item_aliases: dict
	shared: dict
	company: str | None
	company_method: str | None


def stock_lock_protocol():
	conf = getattr(frappe, "conf", None)
	value = conf.get("khitmee_stock_lock_protocol", "legacy") if conf else "legacy"
	if value not in {"legacy", "adr0003"}:
		raise ValueError("khitmee_stock_lock_protocol must be legacy or adr0003")
	return value


def stock_retry_items():
	return getattr(_TXN_LOCK_STATE, "retry_items", frozenset())


def set_stock_retry_items(items):
	_TXN_LOCK_STATE.retry_items = frozenset(items)


def stock_attempt_pending():
	return bool(getattr(_TXN_LOCK_STATE, "held", {}).get("plan"))


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
		del _TXN_LOCK_STATE.held


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


def _field(row, key):
	return row.get(key) if hasattr(row, "get") else getattr(row, key, None)


def _doc_rows(doc, fieldname):
	return (doc.get(fieldname) if hasattr(doc, "get") else getattr(doc, fieldname, None)) or ()


def document_item_codes(doc):
	"""Return sorted, deduplicated Item codes referenced by items and packed_items."""
	codes = []
	for fieldname in ("items", "packed_items", "supplied_items"):
		for row in _doc_rows(doc, fieldname):
			codes.append(_field(row, "item_code") or _field(row, "rm_item_code"))
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
	for fieldname in ("items", "packed_items", "supplied_items"):
		for row in _doc_rows(doc, fieldname):
			if fieldname == "supplied_items":
				item = _field(row, "rm_item_code")
				pairs.extend((item, warehouse) for warehouse in (
					_field(row, "reserve_warehouse"), _field(doc, "supplier_warehouse")
				))
				continue
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

			if s_warehouse or t_warehouse or _field(doc, "doctype") == "Stock Entry":
				if s_warehouse or default_from:
					pairs.append((item_code, s_warehouse or default_from))
				if t_warehouse or default_to:
					pairs.append((item_code, t_warehouse or default_to))
			elif warehouse or default_wh:
				pairs.append((item_code, warehouse or default_wh))

			if target_warehouse or default_target_wh:
				pairs.append((item_code, target_warehouse or default_target_wh))
			if rejected := _field(row, "rejected_warehouse") or _field(doc, "rejected_warehouse"):
				if _field(row, "rejected_qty"):
					pairs.append((item_code, rejected))
	return ordered_bin_pairs(pairs)


def ordinary_sale_document(doc):
	"""Only unlinked, unpacked sales can request shared Item locks."""
	return (
		_field(doc, "doctype") == "Sales Invoice"
		and cint(_field(doc, "update_stock"))
		and not any(_field(doc, key) for key in (
			"is_return", "is_debit_note", "return_against", "is_internal_customer", "packed_items",
		))
		and _field(doc, "is_opening") != "Yes"
		and all(
			not any(_field(row, key) for key in (
				"sales_order", "so_detail", "delivery_note", "dn_detail", "against_pick_list",
				"target_warehouse", "serial_no", "batch_no", "serial_and_batch_bundle", "use_serial_batch_fields",
				"delivered_by_supplier",
			))
			for row in _doc_rows(doc, "items")
		)
	)


def plan_stock_locks(pairs, item_codes=(), *, ordinary_sale=False, company=None):
	"""Resolve the complete voucher before any stock lock. Bin existence is advisory."""
	pairs = ordered_bin_pairs(pairs)
	represented = {code for code, _ in pairs}
	requests = pairs + [(code, "") for code in ordered_item_codes(item_codes) if code not in represented]
	if not requests:
		return StockLockPlan((), (), {}, {}, {}, None, None)
	method = frappe.db.get_value("Company", company, "valuation_method") if ordinary_sale and company else None
	ordinary_sale = ordinary_sale and company and not cint(
		frappe.db.get_single_value("Stock Settings", "allow_negative_stock", cache=False)
	)
	item_aliases, aliases, eligible = {}, {}, {}
	for chunk in batched(requests, LOCK_BATCH_SIZE):
		select = " UNION ALL ".join(["SELECT %s AS pos, %s AS item_code, %s AS warehouse"] * len(chunk))
		args = tuple(value for pos, pair in enumerate(chunk) for value in (pos, *pair))
		rows = frappe.db.sql(
			f"""
			SELECT requested.pos, item.name, wh.name, bin.name, {_ITEM_TRAITS}, wh.company
			FROM ({select}) AS requested
			LEFT JOIN `tabItem` AS item ON item.name = requested.item_code
			LEFT JOIN `tabWarehouse` AS wh ON wh.name = requested.warehouse
			LEFT JOIN `tabBin` AS bin ON bin.item_code = item.name AND bin.warehouse = wh.name
			""",
			args,
		)
		if len(rows) != len(chunk):
			raise RuntimeError("Stock planning requires unique Item, Warehouse and Bin identities.")
		for pos, item, warehouse, bin_name, stock, serial, batch, negative, valuation, warehouse_company in rows:
			raw = chunk[pos]
			if not item or (raw[1] and not warehouse):
				frappe.throw(frappe._("A stock item or warehouse no longer exists."), frappe.ValidationError)
			item_aliases[raw[0]] = item
			if warehouse:
				aliases[raw] = (item, warehouse)
			eligible.setdefault(item, valuation)
			if not (
				ordinary_sale and bin_name and stock and not (serial or batch or negative)
				and (valuation or method) in _ORDINARY_VALUATION and warehouse_company == company
				and item not in stock_retry_items()
			):
				eligible[item] = False
	shared = {item: method for item, method in eligible.items() if method is not False}
	return StockLockPlan(
		tuple(sorted(set(item_aliases.values()))), tuple(sorted(set(aliases.values()))),
		aliases, item_aliases, shared, company, method,
	)


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


def acquire_stock_plan(plan, *, fresh_snapshot=True):
	if not fresh_snapshot and plan.shared:
		plan = replace(plan, shared={})
	state = _get_txn_lock_state()
	if state.get("plan") is not None or state["items"] or state["pairs"]:
		raise StockLockRestart(plan.items)
	_register_txn_cleanup(state)
	if not state["registered"]:
		raise RuntimeError("Stock locks require transaction cleanup callbacks.")
	# Record the attempt before issuing SQL so a partially acquired set can
	# never be extended by a later hook in the same transaction.
	state["plan"] = plan
	item_link_rows = []
	for item in plan.items:
		shared = item in plan.shared
		clause = "LOCK IN SHARE MODE" if shared else "FOR UPDATE"
		rows = frappe.db.sql(
			f"SELECT item.name, {_ITEM_TRAITS} FROM `tabItem` AS item WHERE item.name = %s {clause}",
			(item,),
		)
		if not rows:
			raise StockLockRestart([item])
		name, stock, serial, batch, negative, valuation = rows[0]
		item_link_rows.append((name, batch, serial))
		if shared and (not stock or serial or batch or negative or valuation != plan.shared[item]):
			raise StockLockRestart([item])
	bins = []
	protected_missing = []
	for item, warehouse in plan.pairs:
		rows = frappe.db.sql(
			"""SELECT name, item_code, warehouse FROM `tabBin` FORCE INDEX (`unique_item_warehouse`)
			WHERE item_code = %s AND warehouse = %s FOR UPDATE""",
			(item, warehouse),
		)
		if rows:
			name, code, wh = rows[0]
			bins.append({"name": name, "item_code": code, "warehouse": wh})
		elif item in plan.shared:
			raise StockLockRestart([item])
		else:
			protected_missing.append((item, warehouse))
	if plan.shared:
		# Current reads on settings/master data also work in Desk's older
		# snapshot. Keep the decision protected through voucher posting.
		negative = frappe.db.sql(
			"""SELECT value FROM `tabSingles` WHERE doctype = 'Stock Settings'
			AND field = 'allow_negative_stock' LOCK IN SHARE MODE"""
		)
		if any(cint(row[0]) for row in negative):
			raise StockLockRestart(plan.shared)
		if any(not method for method in plan.shared.values()):
			company = frappe.db.sql(
				"SELECT valuation_method FROM `tabCompany` WHERE name = %s LOCK IN SHARE MODE", (plan.company,)
			)
			if not company or company[0][0] != plan.company_method:
				raise StockLockRestart(plan.shared)
	state["complete"] = True
	return {
		"items": list(plan.items), "pairs": list(plan.pairs), "bins": bins,
		"protected_missing": protected_missing, "item_link_rows": item_link_rows,
	}


def _lock_stock_adr0003(pairs, item_codes, *, require_prelocked, ordinary_sale, company):
	state = _get_txn_lock_state()
	pairs = ordered_bin_pairs(pairs)
	items = ordered_item_codes([*(item_codes or ()), *(item for item, _ in pairs)])
	if not items:
		return {"items": [], "bins": [], "pairs": []}
	if plan := state.get("plan"):
		canonical_items = {plan.item_aliases.get(item, item) for item in items}
		canonical_pairs = {plan.aliases.get(pair, pair) for pair in pairs}
		expansion = canonical_items - set(plan.items)
		expansion.update(item for item, _ in canonical_pairs - set(plan.pairs))
		if not ordinary_sale:
			expansion.update(canonical_items.intersection(plan.shared))
		if expansion or not state.get("complete"):
			raise StockLockRestart(expansion or canonical_items)
		return {"items": sorted(canonical_items), "bins": [], "pairs": sorted(canonical_pairs)}
	if require_prelocked:
		raise StockLockRestart(items)
	return acquire_stock_plan(plan_stock_locks(pairs, items, ordinary_sale=ordinary_sale, company=company))


def lock_stock_coordinates(pairs, item_codes=None, *, require_prelocked=False, ordinary_sale=False, company=None):
	"""Acquire sorted tabItem locks followed by sorted existing tabBin locks.

	Re-entrant within a single transaction: if the current transaction already
	holds locks for the superset of requested items and pairs, returns immediately
	with zero SQL.
	"""
	if stock_lock_protocol() == "adr0003":
		return _lock_stock_adr0003(
			pairs, item_codes, require_prelocked=require_prelocked, ordinary_sale=ordinary_sale, company=company
		)
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
	if stock_lock_protocol() == "adr0003":
		return _lock_doc_adr0003(doc, pairs, item_codes)
	if getattr(getattr(doc, "flags", None), "khitmee_stock_prelocked", False):
		return lock_stock_coordinates(pairs, item_codes=item_codes, require_prelocked=True)
	return lock_stock_coordinates(pairs, item_codes=item_codes)


def lock_purchase_receipt_stock(doc, method=None):
	"""before_submit hook for Purchase Receipt: lock sorted Items then existing Bins."""
	item_codes = document_item_codes(doc)
	pairs = document_bin_pairs(doc)
	if not item_codes and not pairs:
		return {"items": [], "bins": [], "pairs": []}
	if stock_lock_protocol() == "adr0003":
		return _lock_doc_adr0003(doc, pairs, item_codes)
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
	if stock_lock_protocol() == "adr0003":
		return _lock_doc_adr0003(doc, pairs, item_codes)
	return lock_stock_coordinates(pairs, item_codes=item_codes)


def _lock_doc_adr0003(doc, pairs, items):
	prelocked = bool(getattr(getattr(doc, "flags", None), "khitmee_stock_prelocked", False))
	try:
		return lock_stock_coordinates(
			pairs, items, require_prelocked=prelocked,
			ordinary_sale=ordinary_sale_document(doc), company=_field(doc, "company"),
		)
	except StockLockRestart as exc:
		if prelocked:
			raise
		frappe.throw(frappe._(str(exc)), frappe.ValidationError)
