"""Thread-safe, lock-guarded fast paths for counter vouchers.

1. `proven_empty_future_sle_fastpath`:
   ERPNext's `update_qty_in_future_sle` checks `tabStock Ledger Entry` once per item
   row (200x per counter sale, 400x per 200-item transfer). When a counter sale or
   direct/dispatch transfer already holds deterministic `tabItem` + `tabBin` locks
   for every `(item_code, warehouse)` coordinate, has distinct non-tracked items, and
   chunked `UNION ALL` index-backed queries prove ZERO non-cancelled SLE rows exist
   at or after `posting_datetime` for those locked coordinates, those coordinates
   provably have no future SLEs to repost.

2. `voucher_transaction_read_cache`:
   Strictly thread-local, single-transaction read-only metadata cache around
   `doc.insert()` eliminating repeated identical lookups inside one voucher:
   - `get_accounting_dimensions` (155x-600x -> 1x)
   - `is_product_bundle` (50x-200x -> 1 bulk query)
   - `get_sre_reserved_qty_for_item_and_warehouse` (100x-400x -> 1 bulk query)
   - `get_pos_profile` (50x-200x -> 1x)

Architecture guarantee (F02):
   Every wrapped callable binds its captured `original` directly inside a closure
   marked with `_khitmee_dispatcher = True` and is installed idempotently ONCE
   under a lock. We NEVER clear global original references or uninstall wrappers
   while other threads or lazily-imported caller modules may hold a reference.
   When a thread has no active `threading.local()` context, the closure simply
   delegates directly to `original(*args, **kwargs)`, making `NoneType` crashes,
   `RecursionError`, and lazy-import worker poisoning architecturally impossible.
"""

from contextlib import contextmanager
import copy
from dataclasses import dataclass, field
import datetime
import importlib
from itertools import islice
import sys
import threading

import frappe
from frappe.utils import cint

FUTURE_SLE_UNION_BATCH_SIZE = 50
READ_CACHE_BATCH_SIZE = 500

_DISPATCH_LOCK = threading.Lock()
_THREAD_STATE = threading.local()

_READ_CACHE_LOCK = threading.Lock()
_READ_CACHE_STATE = threading.local()

_ACCOUNTING_DIM_MODULE = "erpnext.accounts.doctype.accounting_dimension.accounting_dimension"
_ACCOUNTING_DIM_CALLERS = (
	_ACCOUNTING_DIM_MODULE,
	"erpnext.accounts.general_ledger",
	"erpnext.controllers.accounts_controller",
	"erpnext.accounts.doctype.sales_invoice.sales_invoice",
	"erpnext.stock.doctype.stock_entry.stock_entry",
	"erpnext.controllers.stock_controller",
)

_PRODUCT_BUNDLE_MODULE = "erpnext.stock.doctype.packed_item.packed_item"
_PRODUCT_BUNDLE_CALLERS = (
	_PRODUCT_BUNDLE_MODULE,
	"erpnext.stock.get_item_details",
	"erpnext.controllers.selling_controller",
	"erpnext.accounts.doctype.sales_invoice.sales_invoice",
)

_SRE_MODULE = "erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry"
_SRE_CALLERS = (
	_SRE_MODULE,
	"erpnext.stock.utils",
	"erpnext.stock.stock_ledger",
	"erpnext.stock.doctype.stock_entry.stock_entry",
)

_POS_PROFILE_MODULE = "erpnext.stock.get_item_details"
_POS_PROFILE_CALLERS = (
	_POS_PROFILE_MODULE,
	"erpnext.accounts.doctype.sales_invoice.sales_invoice",
)


def batched(iterable, size):
	iterator = iter(iterable)
	while chunk := tuple(islice(iterator, size)):
		yield chunk


def _normalize_posting_date(value):
	if value is None:
		return ""
	if isinstance(value, datetime.datetime):
		return value.date().strftime("%Y-%m-%d")
	if isinstance(value, datetime.date):
		return value.strftime("%Y-%m-%d")
	text = str(value).strip()
	if not text:
		return ""
	return text[:10] if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-" else text


def _normalize_posting_time(value):
	if value is None:
		return ""
	if isinstance(value, datetime.timedelta):
		total_us = int(round(value.total_seconds() * 1_000_000))
		if total_us < 0:
			total_us = 0
		total_s, us = divmod(total_us, 1_000_000)
		hours, rem = divmod(total_s, 3600)
		minutes, seconds = divmod(rem, 60)
		base = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
		return f"{base}.{us:06d}" if us else base
	if isinstance(value, datetime.time):
		base = value.strftime("%H:%M:%S")
		return f"{base}.{value.microsecond:06d}" if value.microsecond else base
	text = str(value).strip()
	if not text:
		return ""
	if "." in text:
		hms, frac = text.split(".", 1)
	else:
		hms, frac = text, ""
	parts = hms.split(":")
	if len(parts) == 2:
		parts.append("00")
	if len(parts) == 3 and all(p.isdigit() for p in parts):
		normalized = f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
		if frac and frac.isdigit():
			frac_trimmed = frac.rstrip("0")
			if frac_trimmed:
				return f"{normalized}.{frac_trimmed.ljust(6, '0')[:6]}"
		return normalized
	return text


def _posting_datetime_str(posting_date, posting_time):
	norm_date = _normalize_posting_date(posting_date)
	norm_time = _normalize_posting_time(posting_time)
	if not norm_date or not norm_time:
		return ""
	return f"{norm_date} {norm_time}"


@dataclass
class FutureSleFastpathContext:
	active: bool
	voucher_type: str = ""
	posting_date: str = ""
	posting_time: str = ""
	doc: object = None
	proven_empty_pairs: frozenset = field(default_factory=frozenset)
	skipped_pairs: set = field(default_factory=set)
	skipped_count: int = 0
	fallback_count: int = 0


@dataclass
class VoucherReadCacheContext:
	active: bool
	accounting_dimensions: dict = field(default_factory=dict)
	prefetched_items: frozenset = field(default_factory=frozenset)
	product_bundles: frozenset = field(default_factory=frozenset)
	extra_product_bundles: dict = field(default_factory=dict)
	prefetched_sre_pairs: frozenset = field(default_factory=frozenset)
	sre_reserved_qty: dict = field(default_factory=dict)
	pos_profiles: dict = field(default_factory=dict)
	hits: int = 0
	misses: int = 0


def _field(obj, name, default=None):
	if hasattr(obj, "get"):
		return obj.get(name, default)
	return getattr(obj, name, default)


def _is_tracked_item_row(row):
	return bool(
		_field(row, "serial_no")
		or _field(row, "batch_no")
		or _field(row, "serial_and_batch_bundle")
		or cint(_field(row, "use_serial_batch_fields", 0))
		or cint(_field(row, "has_serial_no", 0))
		or cint(_field(row, "has_batch_no", 0))
	)


def _extract_eligible_pairs(doc, locked_pairs):
	"""Validate all preconditions and return required `(item_code, warehouse)` pairs, or None."""
	if doc is None or not locked_pairs:
		return None

	doctype = str(_field(doc, "doctype", "") or "")
	if doctype == "Sales Invoice":
		if not cint(_field(doc, "update_stock", 0)):
			return None
	elif doctype == "Stock Entry":
		if str(_field(doc, "purpose", "") or "") != "Material Transfer":
			return None
	else:
		return None

	if (
		cint(_field(doc, "is_return", 0))
		or cint(_field(doc, "is_debit_note", 0))
		or cint(_field(doc, "docstatus", 0)) == 2
		or str(_field(doc, "is_opening", "") or "") == "Yes"
		or bool(_field(doc, "packed_items"))
	):
		return None

	posting_date = _normalize_posting_date(_field(doc, "posting_date", ""))
	posting_time = _normalize_posting_time(_field(doc, "posting_time", ""))
	if not posting_date or not posting_time:
		return None

	items = _field(doc, "items") or []
	if not items:
		return None

	locked_set = {
		(str(item_code or "").strip(), str(warehouse or "").strip())
		for item_code, warehouse in locked_pairs
	}

	seen_item_codes = set()
	required_pairs = set()
	default_wh = str(_field(doc, "set_warehouse", "") or "").strip()
	default_from = str(_field(doc, "from_warehouse", "") or "").strip()
	default_to = str(_field(doc, "to_warehouse", "") or "").strip()

	for row in items:
		if _is_tracked_item_row(row):
			return None
		item_code = str(_field(row, "item_code", "") or "").strip()
		if not item_code or item_code in seen_item_codes:
			return None
		seen_item_codes.add(item_code)

		if doctype == "Sales Invoice":
			warehouse = str(_field(row, "warehouse", "") or default_wh).strip()
			if not warehouse:
				return None
			pair = (item_code, warehouse)
			if pair not in locked_set:
				return None
			required_pairs.add(pair)
		else:
			s_wh = str(_field(row, "s_warehouse", "") or default_from).strip()
			t_wh = str(_field(row, "t_warehouse", "") or default_to).strip()
			if not s_wh or not t_wh or s_wh == t_wh:
				return None
			for wh in (s_wh, t_wh):
				pair = (item_code, wh)
				if pair not in locked_set:
					return None
				required_pairs.add(pair)

	return required_pairs


def find_pairs_with_future_sle(pairs, posting_date, posting_time):
	"""Return the subset of `(item_code, warehouse)` pairs with any SLE at or after `posting_datetime`.

	Uses chunked parenthesized `UNION ALL` branches with `FORCE INDEX` on
	`item_code_warehouse_posting_datetime_creation_index` + `LIMIT 1` per pair so
	MariaDB performs O(1) index probes instead of a full table scan.
	"""
	ordered = sorted(set(pairs or ()))
	if not ordered:
		return set()

	posting_dt = _posting_datetime_str(posting_date, posting_time)
	if not posting_dt:
		return set(ordered)

	branch_sql = (
		"(SELECT %s AS item_code, %s AS warehouse "
		"FROM `tabStock Ledger Entry` "
		"FORCE INDEX (`item_code_warehouse_posting_datetime_creation_index`) "
		"WHERE item_code = %s AND warehouse = %s AND posting_datetime >= %s AND is_cancelled = 0 "
		"LIMIT 1)"
	)

	dirty = set()
	for chunk in batched(ordered, FUTURE_SLE_UNION_BATCH_SIZE):
		sql = "\nUNION ALL\n".join([branch_sql] * len(chunk))
		params = []
		for item_code, warehouse in chunk:
			params.extend([item_code, warehouse, item_code, warehouse, posting_dt])
		rows = frappe.db.sql(sql, tuple(params))
		for row in rows or ():
			if hasattr(row, "get"):
				dirty.add((str(row.get("item_code") or ""), str(row.get("warehouse") or "")))
			else:
				dirty.add((str(row[0] or ""), str(row[1] or "")))
	return dirty


def _should_skip_future_sle_update(
	ctx, args, allow_zero_rate=False, allow_negative_stock=False, via_landed_cost_voucher=False
):
	if ctx is None or not ctx.active or via_landed_cost_voucher or allow_negative_stock or not args:
		return False
	if cint(_field(args, "is_cancelled", 0)):
		return False
	if _field(args, "reserved_stock") or _field(args, "has_reserved_stock"):
		return False
	read_ctx = getattr(_THREAD_STATE, "read_cache", None)
	if read_ctx is not None and read_ctx.active and read_ctx.sre_reserved_map is not None:
		key = (
			str(_field(args, "item_code", "") or "").strip(),
			str(_field(args, "warehouse", "") or "").strip(),
		)
		if read_ctx.sre_reserved_map.get(key, 0.0) > 0:
			return False
	if _is_tracked_item_row(args):
		return False
	if str(_field(args, "voucher_type", "") or "") != ctx.voucher_type:
		return False

	doc_name = getattr(ctx.doc, "name", None) if ctx.doc is not None else None
	voucher_no = _field(args, "voucher_no")
	if doc_name and voucher_no and str(voucher_no) != str(doc_name):
		return False

	if (
		_normalize_posting_date(_field(args, "posting_date", "")) != ctx.posting_date
		or _normalize_posting_time(_field(args, "posting_time", "")) != ctx.posting_time
	):
		return False

	pair = (
		str(_field(args, "item_code", "") or "").strip(),
		str(_field(args, "warehouse", "") or "").strip(),
	)
	if pair not in ctx.proven_empty_pairs or pair in ctx.skipped_pairs:
		return False

	ctx.skipped_pairs.add(pair)
	ctx.skipped_count += 1
	return True


def _make_future_sle_dispatcher(original):
	def _dispatch_update_qty_in_future_sle(*call_args, **call_kwargs):
		ctx = getattr(_THREAD_STATE, "context", None)
		sle_args = call_args[0] if call_args else call_kwargs.get("args")
		allow_neg = bool(call_kwargs.get("allow_negative_stock", False))
		if len(call_args) >= 2 and bool(call_args[1]):
			allow_neg = True
		via_lcv = bool(call_kwargs.get("via_landed_cost_voucher", False))
		if len(call_args) >= 3 and bool(call_args[2]):
			via_lcv = True
		if _should_skip_future_sle_update(
			ctx,
			sle_args,
			allow_negative_stock=allow_neg,
			via_landed_cost_voucher=via_lcv,
		):
			return None

		if ctx is not None and ctx.active:
			ctx.fallback_count += 1

		return original(*call_args, **call_kwargs)

	_dispatch_update_qty_in_future_sle._khitmee_dispatcher = True
	_dispatch_update_qty_in_future_sle._khitmee_original = original
	return _dispatch_update_qty_in_future_sle


def _try_import(module_name):
	if module_name in sys.modules:
		return sys.modules[module_name]
	try:
		return importlib.import_module(module_name)
	except ImportError:
		return None


def _ensure_future_sle_dispatcher_installed(stock_ledger_module):
	with _DISPATCH_LOCK:
		current = getattr(stock_ledger_module, "update_qty_in_future_sle", None)
		if current is None or getattr(current, "_khitmee_dispatcher", False):
			return
		stock_ledger_module.update_qty_in_future_sle = _make_future_sle_dispatcher(current)


@contextmanager
def proven_empty_future_sle_fastpath(doc, locked_pairs, *, enabled=True):
	"""Thread-local context manager enabling proven-empty future-SLE skip for one locked voucher."""
	if not enabled:
		yield FutureSleFastpathContext(active=False)
		return

	required_pairs = _extract_eligible_pairs(doc, locked_pairs)
	if not required_pairs:
		yield FutureSleFastpathContext(active=False)
		return

	stock_ledger_module = _try_import("erpnext.stock.stock_ledger")
	if stock_ledger_module is None or not hasattr(stock_ledger_module, "update_qty_in_future_sle"):
		yield FutureSleFastpathContext(active=False)
		return

	posting_date = _normalize_posting_date(_field(doc, "posting_date", ""))
	posting_time = _normalize_posting_time(_field(doc, "posting_time", ""))
	dirty_pairs = find_pairs_with_future_sle(required_pairs, posting_date, posting_time)
	proven_empty = frozenset(required_pairs - dirty_pairs)
	if not proven_empty:
		yield FutureSleFastpathContext(active=False)
		return

	ctx = FutureSleFastpathContext(
		active=True,
		voucher_type=str(_field(doc, "doctype", "") or ""),
		posting_date=posting_date,
		posting_time=posting_time,
		doc=doc,
		proven_empty_pairs=proven_empty,
	)

	_ensure_future_sle_dispatcher_installed(stock_ledger_module)
	prev_ctx = getattr(_THREAD_STATE, "context", None)
	_THREAD_STATE.context = ctx
	try:
		yield ctx
	finally:
		_THREAD_STATE.context = prev_ctx


# ---------------------------------------------------------------------------
# Thread-local single-transaction read-metadata cache
# ---------------------------------------------------------------------------


def _freeze_key_part(value):
	if isinstance(value, dict):
		return tuple((k, _freeze_key_part(v)) for k, v in sorted(value.items()))
	if isinstance(value, list | tuple):
		return tuple(_freeze_key_part(v) for v in value)
	if isinstance(value, set | frozenset):
		return tuple(sorted(_freeze_key_part(v) for v in value))
	return value


def _safe_cache_key(args, kwargs):
	try:
		key = (_freeze_key_part(args), _freeze_key_part(kwargs))
		hash(key)
		return key
	except TypeError:
		return None


def _prefetch_product_bundles(item_codes):
	ordered = sorted({str(code or "").strip() for code in (item_codes or ()) if str(code or "").strip()})
	if not ordered:
		return frozenset(), frozenset()
	bundles = set()
	try:
		for chunk in batched(ordered, READ_CACHE_BATCH_SIZE):
			placeholders = ", ".join(["%s"] * len(chunk))
			rows = frappe.db.sql(
				f"""
				SELECT new_item_code
				FROM `tabProduct Bundle`
				WHERE disabled = 0 AND new_item_code IN ({placeholders})
				""",
				tuple(chunk),
			)
			for row in rows or ():
				code = row.get("new_item_code") if hasattr(row, "get") else row[0]
				if code:
					bundles.add(str(code))
	except Exception:
		return frozenset(), frozenset()
	return frozenset(ordered), frozenset(bundles)


def _prefetch_sre_reserved_qty(locked_pairs):
	ordered = sorted(
		{
			(str(item_code or "").strip(), str(warehouse or "").strip())
			for item_code, warehouse in (locked_pairs or ())
			if str(item_code or "").strip() and str(warehouse or "").strip()
		}
	)
	if not ordered:
		return frozenset(), {}
	reserved = {}
	try:
		for chunk in batched(ordered, READ_CACHE_BATCH_SIZE):
			placeholders = ", ".join(["(%s, %s)"] * len(chunk))
			params = []
			for item_code, warehouse in chunk:
				params.append(item_code)
				params.append(warehouse)
			rows = frappe.db.sql(
				f"""
				SELECT item_code, warehouse, SUM(reserved_qty - delivered_qty) AS reserved_qty
				FROM `tabStock Reservation Entry`
				WHERE docstatus = 1
					AND status NOT IN ('Delivered', 'Cancelled')
					AND (item_code, warehouse) IN ({placeholders})
				GROUP BY item_code, warehouse
				""",
				tuple(params),
			)
			for row in rows or ():
				if hasattr(row, "get"):
					pair = (str(row.get("item_code") or ""), str(row.get("warehouse") or ""))
					qty = float(row.get("reserved_qty") or 0.0)
				else:
					pair = (str(row[0] or ""), str(row[1] or ""))
					qty = float(row[2] or 0.0)
				reserved[pair] = qty
	except Exception:
		return frozenset(), {}
	return frozenset(ordered), reserved


def _make_accounting_dims_dispatcher(original):
	def _dispatch_get_accounting_dimensions(*args, **kwargs):
		ctx = getattr(_READ_CACHE_STATE, "context", None)
		if ctx is None or not ctx.active:
			return original(*args, **kwargs)

		key = _safe_cache_key(args, kwargs)
		if key is None:
			ctx.misses += 1
			return original(*args, **kwargs)
		if key in ctx.accounting_dimensions:
			ctx.hits += 1
			return copy.deepcopy(ctx.accounting_dimensions[key])

		ctx.misses += 1
		result = original(*args, **kwargs)
		ctx.accounting_dimensions[key] = copy.deepcopy(result)
		return result

	_dispatch_get_accounting_dimensions._khitmee_dispatcher = True
	_dispatch_get_accounting_dimensions._khitmee_original = original
	return _dispatch_get_accounting_dimensions


def _make_product_bundle_dispatcher(original):
	def _dispatch_is_product_bundle(*args, **kwargs):
		ctx = getattr(_READ_CACHE_STATE, "context", None)
		if ctx is None or not ctx.active:
			return original(*args, **kwargs)

		if len(args) == 1 and not kwargs:
			item_code = args[0]
		elif not args and set(kwargs) == {"item_code"}:
			item_code = kwargs["item_code"]
		else:
			ctx.misses += 1
			return original(*args, **kwargs)

		if not item_code:
			return original(*args, **kwargs)

		code = str(item_code).strip()
		if code in ctx.prefetched_items:
			ctx.hits += 1
			return code in ctx.product_bundles
		if code in ctx.extra_product_bundles:
			ctx.hits += 1
			return ctx.extra_product_bundles[code]

		ctx.misses += 1
		result = bool(original(*args, **kwargs))
		ctx.extra_product_bundles[code] = result
		return result

	_dispatch_is_product_bundle._khitmee_dispatcher = True
	_dispatch_is_product_bundle._khitmee_original = original
	return _dispatch_is_product_bundle


def _make_sre_reserved_qty_dispatcher(original):
	def _dispatch_get_sre_reserved_qty(*args, **kwargs):
		ctx = getattr(_READ_CACHE_STATE, "context", None)
		if ctx is None or not ctx.active:
			return original(*args, **kwargs)

		if len(args) == 2 and not kwargs:
			item_code, warehouse = args[0], args[1]
		elif len(args) == 1 and set(kwargs) == {"warehouse"}:
			item_code, warehouse = args[0], kwargs["warehouse"]
		elif not args and set(kwargs) == {"item_code", "warehouse"}:
			item_code, warehouse = kwargs["item_code"], kwargs["warehouse"]
		else:
			ctx.misses += 1
			return original(*args, **kwargs)

		if not item_code or not warehouse:
			return original(*args, **kwargs)

		pair = (str(item_code).strip(), str(warehouse).strip())
		if pair in ctx.prefetched_sre_pairs:
			ctx.hits += 1
			return float(ctx.sre_reserved_qty.get(pair, 0.0))

		ctx.misses += 1
		return original(*args, **kwargs)

	_dispatch_get_sre_reserved_qty._khitmee_dispatcher = True
	_dispatch_get_sre_reserved_qty._khitmee_original = original
	return _dispatch_get_sre_reserved_qty


def _make_pos_profile_dispatcher(original):
	def _dispatch_get_pos_profile(*args, **kwargs):
		ctx = getattr(_READ_CACHE_STATE, "context", None)
		if ctx is None or not ctx.active:
			return original(*args, **kwargs)

		key = _safe_cache_key(args, kwargs)
		if key is None:
			ctx.misses += 1
			return original(*args, **kwargs)
		if key in ctx.pos_profiles:
			ctx.hits += 1
			return copy.deepcopy(ctx.pos_profiles[key])

		ctx.misses += 1
		result = original(*args, **kwargs)
		ctx.pos_profiles[key] = copy.deepcopy(result)
		return result

	_dispatch_get_pos_profile._khitmee_dispatcher = True
	_dispatch_get_pos_profile._khitmee_original = original
	return _dispatch_get_pos_profile


_READ_CACHE_TARGETS = (
	(
		"get_accounting_dimensions",
		_ACCOUNTING_DIM_MODULE,
		_ACCOUNTING_DIM_CALLERS,
		_make_accounting_dims_dispatcher,
	),
	(
		"is_product_bundle",
		_PRODUCT_BUNDLE_MODULE,
		_PRODUCT_BUNDLE_CALLERS,
		_make_product_bundle_dispatcher,
	),
	(
		"get_sre_reserved_qty_for_item_and_warehouse",
		_SRE_MODULE,
		_SRE_CALLERS,
		_make_sre_reserved_qty_dispatcher,
	),
	(
		"get_pos_profile",
		_POS_PROFILE_MODULE,
		_POS_PROFILE_CALLERS,
		_make_pos_profile_dispatcher,
	),
)


def _ensure_read_cache_dispatchers_installed():
	with _READ_CACHE_LOCK:
		for attr_name, canonical_mod_name, caller_mod_names, factory in _READ_CACHE_TARGETS:
			canonical_mod = _try_import(canonical_mod_name)
			if canonical_mod is None or not hasattr(canonical_mod, attr_name):
				continue
			current = getattr(canonical_mod, attr_name)
			if getattr(current, "_khitmee_dispatcher", False):
				dispatcher = current
				original = getattr(current, "_khitmee_original", None)
			else:
				original = current
				dispatcher = factory(original)
				setattr(canonical_mod, attr_name, dispatcher)

			for mod_name in caller_mod_names:
				if mod_name == canonical_mod_name:
					continue
				mod = sys.modules.get(mod_name)
				if mod is not None and getattr(mod, attr_name, None) is original:
					setattr(mod, attr_name, dispatcher)


@contextmanager
def voucher_transaction_read_cache(doc=None, *, item_codes=None, locked_pairs=None, enabled=True):
	"""Thread-local single-transaction read-metadata cache around voucher `doc.insert()`."""
	if not enabled:
		yield VoucherReadCacheContext(active=False)
		return

	doctype = str(_field(doc, "doctype", "") or "") if doc is not None else ""
	prefetched_items, product_bundles = (
		_prefetch_product_bundles(item_codes)
		if (item_codes and doctype != "Stock Entry")
		else (frozenset(), frozenset())
	)
	prefetched_sre_pairs, sre_reserved_qty = (
		_prefetch_sre_reserved_qty(locked_pairs) if locked_pairs else (frozenset(), {})
	)

	ctx = VoucherReadCacheContext(
		active=True,
		prefetched_items=prefetched_items,
		product_bundles=product_bundles,
		prefetched_sre_pairs=prefetched_sre_pairs,
		sre_reserved_qty=sre_reserved_qty,
	)

	_ensure_read_cache_dispatchers_installed()
	prev_ctx = getattr(_READ_CACHE_STATE, "context", None)
	_READ_CACHE_STATE.context = ctx
	try:
		yield ctx
	finally:
		_READ_CACHE_STATE.context = prev_ctx
