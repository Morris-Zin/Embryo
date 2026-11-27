"""Small pure-Python payload helpers shared by the counter APIs.

The functions in this module deliberately do not import Frappe so their security-sensitive
shapes can be tested outside a bench environment.
"""

import hashlib
import json

PUBLIC_ITEM_FIELDS = ("item_code", "item_name", "stock_uom", "barcode", "rate", "actual_qty")
RECEIPT_PRINTER_TYPES = {"voucher", "a4"}
MAX_CLIENT_VOUCHER_ID_LENGTH = 140


def public_item_row(row):
	"""Return only the counter-safe Item fields, dropping every cost, valuation and supplier field."""
	source = row or {}
	result = {fieldname: source.get(fieldname) for fieldname in PUBLIC_ITEM_FIELDS}
	result["barcode"] = result["barcode"] or ""
	result["rate"] = result["rate"] or 0
	result["actual_qty"] = result["actual_qty"] or 0
	return result


def normalize_client_voucher_id(client_voucher_id):
	value = str(client_voucher_id or "").strip()
	if not value:
		raise ValueError("client_voucher_id is required")
	if len(value) > MAX_CLIENT_VOUCHER_ID_LENGTH:
		raise ValueError("client_voucher_id is too long")
	return value


def idempotency_key(voucher_kind, client_voucher_id, user=None):
	"""Scope idempotency keys to the submitting user so different users never collide."""
	parts = [voucher_kind, normalize_client_voucher_id(client_voucher_id)]
	if user:
		parts.insert(0, str(user))
	return ":".join(parts)


def _canonical_item(item):
	return {
		"item_code": str(item.get("item_code") or "").strip(),
		"qty": float(item.get("qty") or 0),
		"uom": str(item.get("uom") or "").strip(),
	}


def request_fingerprint(**parts):
	"""SHA-256 fingerprint of the canonical normalized voucher request payload.

	Callers pass the normalized request pieces (customer, warehouses, transfer_mode,
	outgoing_stock_entry, payment_mode, items) as keyword arguments. Rates, printer
	settings and other non-material fields are deliberately excluded by the callers.
	"""
	canonical = {}
	for key, value in sorted(parts.items()):
		if key == "items":
			canonical[key] = sorted(
				(_canonical_item(item) for item in (value or [])),
				key=lambda row: (row["item_code"], row["uom"], row["qty"]),
			)
		elif value is None:
			canonical[key] = None
		elif isinstance(value, str):
			canonical[key] = value.strip()
		else:
			canonical[key] = value
	serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _qty(value):
	"""Quantities keep their full submitted precision (0.005 stays 0.005)."""
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0


def _money(value, precision=2):
	"""Monetary fields round to the document currency precision (None: no rounding)."""
	try:
		number = float(value or 0)
	except (TypeError, ValueError):
		return 0
	return number if precision is None else round(number, precision)


def make_receipt_payload(
	*,
	printer_type,
	voucher_no,
	title,
	shop_name,
	date,
	party,
	lines,
	total,
	footer,
	currency_precision=2,
):
	if printer_type not in RECEIPT_PRINTER_TYPES:
		raise ValueError("printer_type must be 'voucher' or 'a4'")

	receipt_lines = []
	for line in lines or []:
		receipt_lines.append(
			{
				"name": line.get("name") or line.get("item_name") or line.get("item_code") or "",
				"qty": _qty(line.get("qty")),
				"rate": _money(line.get("rate"), currency_precision),
				"amount": _money(line.get("amount"), currency_precision),
			}
		)

	return {
		"printer_type": printer_type,
		"voucher_no": voucher_no,
		"title": title,
		"shop_name": shop_name,
		"date": date,
		"party": party or "",
		"lines": receipt_lines,
		"total": _money(total, currency_precision),
		"footer": footer or "",
	}
