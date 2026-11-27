"""Permission-checked incremental catalog snapshots for counter clients.

The payload intentionally contains only public counter fields. Rates and quantities are
advisory UI snapshots; voucher submission remains authoritative in ERPNext.
"""

import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint

from khitmee_ops.api.payloads import public_item_row
from khitmee_ops.api.search import (
	_fetch_rates_and_quantities,
	_item_permission_restrictions_active,
	_permitted_item_codes,
	_require_counter_user,
	_validate_price_list,
	_validate_warehouse,
)

SYNC_SCHEMA_VERSION = 1
SYNC_OVERLAP_SECONDS = 10
MAX_DELTA_AGE = timedelta(hours=24)
MAX_CATALOG_ITEMS = 20000
PERMISSION_BATCH_SIZE = 500


def _format_datetime(value):
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d %H:%M:%S.%f")
	return str(value or "")


def _parse_datetime(value):
	text = str(value or "").strip()
	if not text:
		return None
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			pass
	return None


def _server_now():
	row = frappe.db.sql("SELECT NOW(6)", as_dict=False)
	return _format_datetime(row[0][0] if row and row[0] else datetime.now())


def _json_state(value):
	if isinstance(value, dict):
		return dict(value)
	text = str(value or "").strip()
	if not text:
		return {}
	try:
		parsed = json.loads(text)
	except (TypeError, ValueError):
		return {}
	return dict(parsed) if isinstance(parsed, dict) else {}


def _state_token(state):
	return json.dumps(state, sort_keys=True, separators=(",", ":"))


def _permission_version(user):
	"""Return a compact revision for user-permission rows relevant to catalog visibility."""
	rows = frappe.db.sql(
		"""
		SELECT COUNT(*), COALESCE(MAX(modified), '')
		FROM `tabUser Permission`
		WHERE `user` = %s AND `allow` IN ('Item', 'Item Group', 'Brand')
		""",
		(user,),
		as_dict=False,
	)
	count, modified = rows[0] if rows else (0, "")
	hooks = "hooks" if _item_permission_restrictions_active() else "plain"
	return f"{hooks}:{cint(count)}:{_format_datetime(modified)}"


def _scope(warehouse, price_list, customer):
	return {
		"warehouse": warehouse or "",
		"price_list": price_list or "",
		"customer": customer or "",
	}


def _permitted_codes(item_codes):
	if not item_codes:
		return []
	if not _item_permission_restrictions_active():
		return list(item_codes)
	permitted = []
	for start in range(0, len(item_codes), PERMISSION_BATCH_SIZE):
		permitted.extend(_permitted_item_codes(item_codes[start : start + PERMISSION_BATCH_SIZE]))
	return set(permitted)


def _active_item_count():
	rows = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabItem` WHERE disabled = 0 AND has_variants = 0",
		as_dict=False,
	)
	return cint(rows[0][0]) if rows else 0


def _all_active_item_codes():
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabItem`
		WHERE disabled = 0 AND has_variants = 0
		ORDER BY name
		LIMIT %s
		""",
		(MAX_CATALOG_ITEMS,),
		as_dict=False,
	)
	return [row[0] for row in rows]


def _changed_item_codes(since, warehouse):
	"""Return possibly affected item codes using a deliberate overlap window.

	Overlap makes the delta idempotent across timestamp precision and transaction visibility
	races. Duplicate rows are harmless because clients upsert by item code.
	"""
	cutoff = _format_datetime(since - timedelta(seconds=SYNC_OVERLAP_SECONDS))
	rows = frappe.db.sql(
		"""
		SELECT changed.item_code FROM (
			SELECT item.name AS item_code
			FROM `tabItem` AS item
			WHERE item.modified >= %s
			UNION
			SELECT barcode.parent AS item_code
			FROM `tabItem Barcode` AS barcode
			WHERE barcode.parentfield = 'barcodes' AND barcode.modified >= %s
			UNION
			SELECT price.item_code AS item_code
			FROM `tabItem Price` AS price
			WHERE price.modified >= %s
			UNION
			SELECT bin.item_code AS item_code
			FROM `tabBin` AS bin
			WHERE bin.warehouse = %s AND bin.modified >= %s
		) AS changed
		ORDER BY changed.item_code
		""",
		(cutoff, cutoff, cutoff, warehouse, cutoff),
		as_dict=False,
	)
	return [row[0] for row in rows]


def _fetch_catalog_rows(item_codes):
	if not item_codes:
		return {}
	placeholders = ", ".join(["%s"] * len(item_codes))
	rows = frappe.db.sql(
		f"""
		SELECT
			item.name AS item_code,
			item.item_name,
			item.stock_uom,
			item.item_group,
			item.brand,
			COALESCE(barcode.barcode, '') AS barcode,
			COALESCE(barcode.barcode_text, '') AS barcode_text,
			item.modified
		FROM `tabItem` AS item
		LEFT JOIN (
			SELECT
				parent,
				MIN(barcode) AS barcode,
				GROUP_CONCAT(barcode ORDER BY barcode SEPARATOR ' ') AS barcode_text
			FROM `tabItem Barcode`
			WHERE parent IN ({placeholders}) AND parentfield = 'barcodes'
			GROUP BY parent
		) AS barcode ON barcode.parent = item.name
		WHERE item.name IN ({placeholders}) AND item.disabled = 0 AND item.has_variants = 0
		""",
		[*item_codes, *item_codes],
		as_dict=True,
	)
	return {row.item_code: row for row in rows}


def _catalog_public_rows(rows, warehouse, price_list, customer):
	if not rows:
		return []
	working_rows = []
	row_type = getattr(frappe, "_dict", dict)
	for row in rows:
		working_rows.append(row_type(dict(row)))
	_fetch_rates_and_quantities(working_rows, warehouse, price_list, customer=customer)
	result = []
	for row in working_rows:
		public = public_item_row(row)
		public["item_group"] = row.get("item_group") or ""
		public["brand"] = row.get("brand") or ""
		public["barcode_text"] = row.get("barcode_text") or ""
		public["modified"] = _format_datetime(row.get("modified"))
		result.append(public)
	return result


def _delta_is_stale(since, now):
	if since is None or now is None:
		return True
	return now - since > MAX_DELTA_AGE


@frappe.whitelist()
def sync_catalog(warehouse, price_list, state=None, customer=None):
	"""Return a reset or overlapping incremental catalog delta for one counter scope.

	The client-side catalog is a read-only cache. It is deliberately disposable: any schema,
	scope, permission or staleness mismatch causes a reset response, and ERPNext validates
	items, prices, permissions and inventory again during voucher submission.
	"""
	_require_counter_user()
	warehouse = _validate_warehouse(warehouse)
	price_list = _validate_price_list(price_list)
	customer = str(customer or "").strip()
	if customer:
		frappe.has_permission("Customer", ptype="read", doc=customer, throw=True)

	now_text = _server_now()
	now = _parse_datetime(now_text)
	incoming = _json_state(state)
	permission_version = _permission_version(frappe.session.user)
	scope = _scope(warehouse, price_list, customer)
	previous_since = _parse_datetime(incoming.get("since"))
	permissioned_reset = _item_permission_restrictions_active()
	reset = (
		permissioned_reset
		or cint(incoming.get("schema")) != SYNC_SCHEMA_VERSION
		or incoming.get("permission") != permission_version
		or incoming.get("scope") != scope
		or previous_since is None
		or _delta_is_stale(previous_since, now)
		# Item Price validity is date-based. A daily replacement prevents scheduled
		# prices that start or expire at midnight from remaining locally stale forever.
		or previous_since.date() != now.date()
	)

	complete = True
	if reset:
		complete = _active_item_count() <= MAX_CATALOG_ITEMS
		changed_codes = _all_active_item_codes()
	else:
		changed_codes = _changed_item_codes(previous_since, warehouse)

	metadata = _fetch_catalog_rows(changed_codes)
	permitted = _permitted_codes(list(metadata))
	active_rows = []
	for item_code in changed_codes:
		row = metadata.get(item_code)
		if row is None or item_code not in permitted:
			continue
		active_rows.append(row)

	# Permission-aware deployments use a full replacement rather than tombstones. A
	# tombstone for an item the cashier could never see would disclose its item code.
	active_payload = _catalog_public_rows(active_rows, warehouse, price_list, customer)
	active_by_code = {row["item_code"]: row for row in active_payload}
	items = []
	for item_code in changed_codes:
		if item_code in active_by_code:
			items.append(active_by_code[item_code])
		elif not reset:
			items.append({"item_code": item_code, "deleted": True})

	new_state = {
		"schema": SYNC_SCHEMA_VERSION,
		"since": now_text,
		"permission": permission_version,
		"scope": scope,
	}
	return {
		"schema_version": SYNC_SCHEMA_VERSION,
		"reset": bool(reset),
		"sync_token": _state_token(new_state),
		"server_time": now_text,
		"stale_after_seconds": 120,
		"complete": bool(complete),
		"items": items,
	}
