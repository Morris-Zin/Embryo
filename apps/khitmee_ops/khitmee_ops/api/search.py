import sys
import threading
import time
from array import array
from bisect import bisect_right

import frappe
from frappe import _
from frappe.utils import cint

try:
	from erpnext.stock.doctype.item.item_search import fold_text, get_item_search_candidates
except ImportError:  # pragma: no cover - offline unit tests exercise the pure-Python fallback
	get_item_search_candidates = None

	def fold_text(text):
		return str(text or "").casefold()

from khitmee_ops.api.payloads import public_item_row

# Strict allowlist of public searchable Item fields. Supplier, valuation, purchase-rate
# and any other sensitive fields configured in Item.search_fields can never influence
# or leak into counter search results.
PUBLIC_SEARCHABLE_ITEM_FIELDS = ("name", "item_code", "item_name", "item_group", "brand")
PREFIX_SEARCHABLE_ITEM_FIELDS = ("name", "item_name", "item_group", "brand")
CUSTOMER_PAGE_LENGTH = 50
ITEM_SEARCH_VERSION_KEY = "khitmee_ops:item_search_version"
_ITEM_CATALOG_CACHE = {}
_ITEM_CATALOG_LOCK = threading.Lock()
_SEPARATOR_CANDIDATES = ("\x1f", "\x1e", "\x1d", "\x1c", "\ufdd0", "\ufdd1")


def _require_counter_user():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to use the counter."), frappe.PermissionError)
	frappe.has_permission("Item", ptype="read", throw=True)


def _validate_warehouse(warehouse):
	if not warehouse:
		frappe.throw(_("Warehouse is required"))
	if not frappe.get_cached_value("Warehouse", warehouse, "name"):
		frappe.throw(_("Warehouse {0} does not exist").format(warehouse))
	frappe.has_permission("Warehouse", ptype="read", doc=warehouse, throw=True)
	return warehouse


def _validate_price_list(price_list):
	if not price_list:
		frappe.throw(_("Price List is required"))
	row = frappe.get_cached_value("Price List", price_list, ["selling", "enabled"], as_dict=True)
	if not row or not row.selling or not row.enabled:
		frappe.throw(_("Price List {0} is not an enabled selling price list").format(price_list))
	frappe.has_permission("Price List", ptype="read", doc=price_list, throw=True)
	return price_list


def _escape_like(value):
	"""Escape a value for a MariaDB LIKE pattern using '!' as the escape character."""
	return str(value or "").replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _cache_client():
	cache = getattr(frappe, "cache", None)
	if callable(cache) and not hasattr(cache, "get_value"):
		return cache()
	return cache


def _cache_get_value(cache, key):
	if hasattr(cache, "get_value"):
		return cache.get_value(key)
	if hasattr(cache, "get"):
		return cache.get(key)
	return None


def _cache_set_value(cache, key, value):
	if hasattr(cache, "set_value"):
		cache.set_value(key, value)
	elif hasattr(cache, "set"):
		cache.set(key, value)


def _raw_cache_key(cache, key):
	try:
		return cache.make_key(key) if cache is not None and hasattr(cache, "make_key") else key
	except Exception:
		return key


def _decode_cache_value(value):
	if isinstance(value, bytes):
		return value.decode()
	return value


def _raw_cache_get(cache, key):
	if cache is None or not hasattr(cache, "get"):
		return None
	try:
		return _decode_cache_value(cache.get(_raw_cache_key(cache, key)))
	except Exception:
		return None


def _seed_raw_item_search_version(cache):
	"""Seed a missing raw Redis version with a non-reusable timestamp value."""
	raw_key = _raw_cache_key(cache, ITEM_SEARCH_VERSION_KEY)
	seed = time.time_ns()
	try:
		cache.set(raw_key, seed, nx=True)
	except TypeError:
		if cache.get(raw_key) is None:
			cache.set(raw_key, seed)
	return raw_key, seed


def _bump_item_search_version():
	cache = _cache_client()

	if cache is not None and all(hasattr(cache, attr) for attr in ("get", "set", "incr")):
		try:
			raw_key, _seed = _seed_raw_item_search_version(cache)
			cache.incr(raw_key)
			return
		except Exception:
			pass

	try:
		current = _cache_get_value(cache, ITEM_SEARCH_VERSION_KEY) or "1"
		next_version = cint(current) + 1
	except Exception:
		next_version = 2
	_cache_set_value(cache, ITEM_SEARCH_VERSION_KEY, str(next_version))


def _bump_item_search_version_safely():
	try:
		_bump_item_search_version()
	except Exception:
		pass


def invalidate_item_search_cache(doc=None, method=None, *args, **kwargs):
	"""Defer the shared catalog version bump until the Item write commits."""
	after_commit = getattr(getattr(frappe, "db", None), "after_commit", None)
	if hasattr(after_commit, "add"):
		after_commit.add(_bump_item_search_version_safely)
	else:
		_bump_item_search_version_safely()


def _item_search_version():
	cache = _cache_client()
	if cache is not None and all(hasattr(cache, attr) for attr in ("get", "set")):
		raw_version = _raw_cache_get(cache, ITEM_SEARCH_VERSION_KEY)
		if raw_version is not None:
			return str(raw_version)
		try:
			raw_key, seed = _seed_raw_item_search_version(cache)
			return str(_raw_cache_get(cache, ITEM_SEARCH_VERSION_KEY) or seed)
		except Exception:
			pass
	else:
		raw_version = _raw_cache_get(cache, ITEM_SEARCH_VERSION_KEY)
		if raw_version is not None:
			return str(raw_version)
	return str(_cache_get_value(cache, ITEM_SEARCH_VERSION_KEY) or "1")


def _current_site():
	local = getattr(frappe, "local", None)
	return getattr(local, "site", None) or getattr(frappe, "site", "") or ""


def _fold_search_text(value):
	text = str(value or "")
	if text.isascii():
		return text.casefold()
	try:
		return str(fold_text(text)).casefold()
	except Exception:
		return text.casefold()


def _load_item_catalog_rows():
	return frappe.db.sql(
		"""
		SELECT
			item.name,
			item.item_code,
			item.item_name,
			item.stock_uom,
			item.item_group,
			item.brand,
			COALESCE(barcode.barcode, '') AS barcode,
			COALESCE(barcode.barcode_text, '') AS barcode_text
		FROM `tabItem` AS item
		LEFT JOIN (
			SELECT
				parent,
				MIN(barcode) AS barcode,
				GROUP_CONCAT(barcode ORDER BY barcode SEPARATOR ' ') AS barcode_text
			FROM `tabItem Barcode`
			WHERE parentfield = 'barcodes'
			GROUP BY parent
		) AS barcode ON barcode.parent = item.name
		WHERE item.disabled = 0 AND item.has_variants = 0
		ORDER BY item.name
		""",
		as_dict=False,
	)


def _catalog_row_values(source_row):
	if hasattr(source_row, "get"):
		return tuple(source_row.get(fieldname) for fieldname in (
			"name", "item_code", "item_name", "stock_uom", "item_group", "brand", "barcode", "barcode_text"
		))
	values = tuple(source_row or ())
	if len(values) < 8:
		values = values + (None,) * (8 - len(values))
	return values[:8]


def _build_item_catalog(rows):
	fold_cache = {}

	def fold_field(value):
		text = str(value or "")
		if text not in fold_cache:
			folded = _fold_search_text(text)
			if folded == text:
				folded = sys.intern(text)
			fold_cache[text] = folded
		return fold_cache[text]

	item_tuples = []
	prepared = []
	exact_barcode_map = {}
	for source_row in rows or []:
		name_value, item_code, item_name, stock_uom, item_group, brand, barcode, barcode_text = _catalog_row_values(source_row)
		name = str(name_value or item_code or "")
		if not name:
			continue

		item_code = str(item_code or name)
		item_name = str(item_name or "")
		stock_uom = str(stock_uom or "")
		item_group = str(item_group or "")
		brand = str(brand or "")
		barcode = str(barcode or "")
		barcode_text = str(barcode_text or "")

		barcodes = barcode_text.split()
		if barcode and barcode not in barcodes:
			barcodes.insert(0, barcode)
		for barcode_value in barcodes:
			folded_barcode = fold_field(barcode_value)
			if folded_barcode and folded_barcode not in exact_barcode_map:
				exact_barcode_map[folded_barcode] = name

		f_name = fold_field(name)
		f_item_code = fold_field(item_code)
		f_item_name = fold_field(item_name)
		f_item_group = fold_field(item_group)
		f_brand = fold_field(brand)
		f_barcode_text = fold_field(barcode_text)
		prefix_folded = (f_name, f_item_name, f_item_group, f_brand)
		search_folded = (f_name, f_item_code, f_item_name, f_item_group, f_brand, f_barcode_text)

		item_tuples.append((name, item_name, stock_uom, barcode))
		prepared.append((name, prefix_folded, search_folded))

	sep = None
	for candidate in _SEPARATOR_CANDIDATES:
		if not any(
			candidate in folded
			for _name, _prefix_folded, search_folded in prepared
			for folded in search_folded
		):
			sep = candidate
			break

	names = [item[0] for item in item_tuples]
	catalog = {
		"names": names,
		"index_by_name": {name: index for index, name in enumerate(names)},
		"item_tuples": item_tuples,
		"exact_barcode_map": exact_barcode_map,
		"sep": sep,
		"search_blob": None,
		"search_starts": array("q"),
		"prefix_blob": None,
		"prefix_starts": array("q"),
		"fallback_entries": [],
	}

	if sep is None:
		catalog["fallback_entries"] = prepared
		return catalog

	search_parts = []
	prefix_parts = []
	search_starts = array("q")
	prefix_starts = array("q")
	search_offset = 0
	prefix_offset = 0
	for _name, prefix_folded, search_folded in prepared:
		search_segment = sep + sep.join(search_folded)
		search_starts.append(search_offset)
		search_parts.append(search_segment)
		search_offset += len(search_segment)

		prefix_segment = sep + sep.join(prefix_folded)
		prefix_starts.append(prefix_offset)
		prefix_parts.append(prefix_segment)
		prefix_offset += len(prefix_segment)

	search_parts.append(sep)
	prefix_parts.append(sep)
	catalog["search_blob"] = "".join(search_parts)
	catalog["search_starts"] = search_starts
	catalog["prefix_blob"] = "".join(prefix_parts)
	catalog["prefix_starts"] = prefix_starts
	return catalog


def _iter_hits(blob, starts, needle):
	if not needle or not starts:
		return
	pos, n, end = 0, len(starts), len(blob)
	while (hit := blob.find(needle, pos)) >= 0:
		i = bisect_right(starts, hit) - 1
		yield i
		pos = starts[i + 1] if i + 1 < n else end


def _get_item_catalog():
	site = _current_site()
	version = _item_search_version()
	key = (site, version)
	catalog = _ITEM_CATALOG_CACHE.get(key)
	if catalog is not None:
		return catalog

	with _ITEM_CATALOG_LOCK:
		catalog = _ITEM_CATALOG_CACHE.get(key)
		if catalog is None:
			catalog = _build_item_catalog(_load_item_catalog_rows())
			_ITEM_CATALOG_CACHE[key] = catalog
			for old_key in list(_ITEM_CATALOG_CACHE):
				if old_key[0] == site and old_key != key:
					_ITEM_CATALOG_CACHE.pop(old_key, None)
	return catalog


def _catalog_item_matches_term(catalog, item_code, folded_term):
	needle = str(folded_term or "")
	if not needle:
		return False
	index = catalog["index_by_name"].get(item_code)
	if index is None:
		return False

	if catalog.get("sep") is not None:
		if catalog["sep"] in needle:
			return False
		starts = catalog["search_starts"]
		blob = catalog["search_blob"]
		end = starts[index + 1] if index + 1 < len(starts) else len(blob)
		return needle in blob[starts[index] : end]

	entry = catalog["fallback_entries"][index]
	return any(needle in value for value in entry[2])


def _catalog_item_metadata(catalog, item_codes):
	rows = []
	row_type = getattr(frappe, "_dict", dict)
	for item_code in item_codes:
		index = catalog["index_by_name"].get(item_code)
		if index is None:
			continue
		name, item_name, stock_uom, barcode = catalog["item_tuples"][index]
		rows.append(
			row_type(
				{
					"item_code": name,
					"item_name": item_name,
					"stock_uom": stock_uom,
					"barcode": barcode,
				}
			)
		)
	return rows


def _user_has_restricted_item_permissions(user):
	if not user or user == "Administrator":
		return False
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM `tabUser Permission`
			WHERE `user` = %s AND `allow` IN ('Item', 'Item Group', 'Brand')
			LIMIT 1
			""",
			(user,),
		)
	)


def _item_permission_restrictions_active():
	user = getattr(frappe.session, "user", None)
	if user == "Administrator":
		return False
	get_hook = getattr(frappe, "get_hook", None) or frappe.get_hooks
	try:
		hooks = get_hook("permission_query_conditions", {}) or {}
	except TypeError:
		hooks = get_hook("permission_query_conditions") or {}
	if hooks.get("Item"):
		return True
	return _user_has_restricted_item_permissions(user)


def _permitted_catalog_item_codes(item_codes):
	if not item_codes:
		return []
	if not _item_permission_restrictions_active():
		return list(item_codes)
	return _permitted_item_codes(item_codes)


def _exact_barcode_item(term):
	rows = frappe.db.sql(
		"""
		SELECT item.name AS item_code
		FROM `tabItem Barcode` AS barcode
		INNER JOIN `tabItem` AS item ON item.name = barcode.parent
		WHERE barcode.barcode = %s
			AND barcode.parentfield = 'barcodes'
			AND item.disabled = 0
			AND item.has_variants = 0
		ORDER BY item.name
		LIMIT 1
		""",
		(term,),
		as_dict=True,
	)
	return rows[0].item_code if rows else None


def _prefix_item_codes(term, limit):
	escaped = _escape_like(term)
	return frappe.db.sql(
		"""
		SELECT name AS item_code
		FROM `tabItem`
		WHERE disabled = 0
			AND has_variants = 0
			AND (
				name LIKE %s ESCAPE '!'
				OR item_name LIKE %s ESCAPE '!'
				OR item_group LIKE %s ESCAPE '!'
				OR brand LIKE %s ESCAPE '!'
			)
		ORDER BY name
		LIMIT %s
		""",
		(f"{escaped}%", f"{escaped}%", f"{escaped}%", f"{escaped}%", cint(limit)),
		as_dict=True,
	)


def _substring_item_codes(term, limit):
	"""Bounded permission-aware substring fallback over the approved public fields.

	Used only when the ERPNext FTS index is unavailable (returns None), never when it
	returns a genuine empty result.
	"""
	like = f"%{term}%"
	return frappe.get_list(
		"Item",
		filters={"disabled": 0, "has_variants": 0},
		or_filters=[
			{"name": ("like", like)},
			{"item_name": ("like", like)},
			{"item_group": ("like", like)},
			{"brand": ("like", like)},
		],
		pluck="name",
		order_by="name",
		limit_page_length=cint(limit),
	)


def _item_matches_term(row, term):
	needle = (term or "").casefold()
	if needle in (row.get("barcode_text") or "").casefold():
		return True

	for fieldname in PUBLIC_SEARCHABLE_ITEM_FIELDS:
		if needle in str(row.get(f"search_{fieldname}") or row.get(fieldname) or "").casefold():
			return True
	return False


def _fetch_item_metadata(item_codes):
	if not item_codes:
		return []

	placeholders = ", ".join(["%s"] * len(item_codes))
	extra_fields = [
		fieldname
		for fieldname in PUBLIC_SEARCHABLE_ITEM_FIELDS
		if fieldname not in {"name", "item_code", "item_name", "stock_uom"}
	]
	extra_selects = "".join(f", item.`{fieldname}` AS `search_{fieldname}`" for fieldname in extra_fields)
	return frappe.db.sql(
		f"""
		SELECT
			item.name AS item_code,
			item.item_name,
			item.stock_uom,
			COALESCE(barcode.barcode, '') AS barcode,
			COALESCE(barcode.barcode_text, '') AS barcode_text
			{extra_selects}
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


def _permitted_item_codes(item_codes):
	"""Intersect candidate item codes with the items the session user may actually read."""
	if not item_codes:
		return []
	permitted = set(
		frappe.get_list(
			"Item",
			filters={"name": ["in", item_codes], "disabled": 0},
			pluck="name",
			limit_page_length=len(item_codes),
		)
	)
	return [item_code for item_code in item_codes if item_code in permitted]


def _pick_price(candidates, requested_uom, stock_uom, customer):
	customer_specific = [row for row in candidates if customer and (row.customer or "") == customer]
	general = [row for row in candidates if not (row.customer or "")]
	pool = customer_specific or general

	for uom in dict.fromkeys(uom for uom in (requested_uom, stock_uom) if uom):
		match = next((row for row in pool if (row.uom or "") == uom), None)
		if match:
			return match
	# Only a blank-UOM price is a valid last resort; never an arbitrary other-UOM price.
	return next((row for row in pool if not (row.uom or "")), None)


def _search_include_rates(value):
	if isinstance(value, str):
		return value.strip().lower() not in {"0", "false", "no", "off"}
	return bool(value)


def _fetch_quantities(rows, warehouse):
	item_codes = [row.item_code for row in rows]
	if not item_codes:
		return {}
	placeholders = ", ".join(["%s"] * len(item_codes))
	bin_rows = frappe.db.sql(
		f"""
		SELECT item_code, actual_qty
		FROM `tabBin`
		WHERE warehouse = %s AND item_code IN ({placeholders})
		""",
		[warehouse, *item_codes],
		as_dict=True,
	)
	return {row.item_code: row.actual_qty for row in bin_rows}


def _fetch_rates_and_quantities(rows, warehouse, price_list, customer=None, uom=None):
	item_codes = [row.item_code for row in rows]
	if not item_codes:
		return rows

	placeholders = ", ".join(["%s"] * len(item_codes))
	price_rows = frappe.db.sql(
		f"""
		SELECT item_code, price_list_rate, uom, customer
		FROM `tabItem Price`
		WHERE price_list = %s
			AND selling = 1
			AND item_code IN ({placeholders})
			AND (valid_from IS NULL OR valid_from <= CURDATE())
			AND (valid_upto IS NULL OR valid_upto >= CURDATE())
			AND (batch_no IS NULL OR batch_no = '')
			AND (customer IS NULL OR customer = '' OR customer = %s)
		ORDER BY modified DESC
		""",
		[price_list, *item_codes, customer or ""],
		as_dict=True,
	)
	price_candidates = {}
	for price in price_rows:
		price_candidates.setdefault(price.item_code, []).append(price)

	quantities = _fetch_quantities(rows, warehouse)
	for row in rows:
		price = _pick_price(price_candidates.get(row.item_code) or [], uom, row.stock_uom, customer)
		row.rate = price.price_list_rate if price else 0
		row.actual_qty = quantities.get(row.item_code) or 0

	return rows


def _fts_item_candidates(term):
	"""Return FTS candidates, or None when the index is unavailable.

	None (unavailable / too-short / too-broad term) is deliberately distinct from a
	genuine empty list so the caller can fall back without masking real zero-hits.
	"""
	try:
		return get_item_search_candidates(term, list(PUBLIC_SEARCHABLE_ITEM_FIELDS))
	except Exception:
		frappe.log_error("Khitmee item search index lookup failed")
		return None


def _ranked_item_codes(term, limit, catalog=None, folded_term=None):
	catalog = catalog if catalog is not None else _get_item_catalog()
	folded_term = _fold_search_text(term) if folded_term is None else folded_term
	cap = max(limit * 4, limit)
	if not folded_term:
		return catalog["names"][:cap]

	ordered = []
	seen = set()

	def add(item_code):
		if item_code and item_code not in seen and len(ordered) < cap:
			seen.add(item_code)
			ordered.append(item_code)

	add(catalog["exact_barcode_map"].get(folded_term))

	if catalog.get("sep") is not None:
		sep = catalog["sep"]
		if sep in folded_term:
			return ordered[:cap]

		prefix_matches = 0
		for index in _iter_hits(catalog["prefix_blob"], catalog["prefix_starts"], sep + folded_term):
			if prefix_matches >= limit:
				break
			item_code = catalog["names"][index]
			if item_code not in seen:
				prefix_matches += 1
			add(item_code)

		for index in _iter_hits(catalog["search_blob"], catalog["search_starts"], folded_term):
			if len(ordered) >= cap:
				break
			add(catalog["names"][index])
		return ordered[:cap]

	prefix_matches = 0
	for name, prefix_folded, _search_folded in catalog["fallback_entries"]:
		if prefix_matches >= limit:
			break
		if any(value.startswith(folded_term) for value in prefix_folded):
			if name not in seen:
				prefix_matches += 1
			add(name)

	for name, _prefix_folded, search_folded in catalog["fallback_entries"]:
		if len(ordered) >= cap:
			break
		if any(folded_term in value for value in search_folded):
			add(name)

	return ordered[:cap]


@frappe.whitelist()
def search_items(q, warehouse, price_list=None, limit=15, customer=None, uom=None, include_rates=True):
	"""Counter-safe item search.

	The response is intentionally limited to public selling fields. Cost, valuation,
	purchase-rate and supplier fields are never selected or returned.
	"""
	_require_counter_user()
	_validate_warehouse(warehouse)

	term = str(q or "").strip()
	include_rates = _search_include_rates(include_rates)
	if include_rates:
		_validate_price_list(price_list)
	folded_term = _fold_search_text(term)
	if not folded_term:
		return {"items": []}

	limit = min(max(cint(limit) or 15, 1), 25)
	catalog = _get_item_catalog()
	candidate_codes = _permitted_catalog_item_codes(
		_ranked_item_codes(term, limit, catalog=catalog, folded_term=folded_term)
	)
	metadata = _catalog_item_metadata(catalog, candidate_codes)
	metadata = [
		row
		for row in metadata
		if _catalog_item_matches_term(catalog, row.item_code, folded_term)
	]

	order = {item_code: index for index, item_code in enumerate(candidate_codes)}
	metadata.sort(key=lambda row: order.get(row.item_code, len(order)))
	metadata = metadata[:limit]
	if include_rates:
		_fetch_rates_and_quantities(metadata, warehouse, price_list, customer=customer, uom=uom)
	else:
		quantities = _fetch_quantities(metadata, warehouse)
		for row in metadata:
			row.rate = 0
			row.actual_qty = quantities.get(row.item_code) or 0

	return {"items": [public_item_row(row) for row in metadata]}


def _customer_page(query=None, start=0, page_length=CUSTOMER_PAGE_LENGTH):
	start = max(cint(start) or 0, 0)
	page_length = min(max(cint(page_length) or CUSTOMER_PAGE_LENGTH, 1), 100)
	customer_filters = {"disabled": 0}
	if query:
		customer_filters["customer_name"] = ("like", f"%{query}%")

	rows = frappe.get_list(
		"Customer",
		filters=customer_filters,
		fields=["name", "customer_name", "customer_group", "territory"],
		order_by="customer_name",
		start=start,
		limit_page_length=page_length + 1,
	)
	has_more = len(rows) > page_length
	return rows[:page_length], start + len(rows[:page_length]), has_more


@frappe.whitelist()
def search_customers(query=None, start=0, page_length=CUSTOMER_PAGE_LENGTH):
	"""Live permission-aware customer search/pagination for the Sales Counter."""
	_require_counter_user()
	frappe.has_permission("Customer", ptype="read", throw=True)
	customers, next_start, has_more = _customer_page(query, start, page_length)
	return {"customers": customers, "next_start": next_start, "has_more": has_more}


@frappe.whitelist()
def get_counter_context(mode="sales", customer_query=None, customer_start=0):
	_require_counter_user()

	warehouses = frappe.get_list(
		"Warehouse",
		filters={"disabled": 0, "is_group": 0},
		fields=["name", "warehouse_name", "company"],
		order_by="warehouse_name",
		limit_page_length=500,
	)

	context = {
		"mode": mode,
		"warehouses": warehouses,
		"default_warehouse": frappe.defaults.get_user_default("Warehouse") or "",
		"default_company": frappe.defaults.get_user_default("Company") or "",
		"print_endpoint": frappe.conf.get("khitmee_print_endpoint") or "http://127.0.0.1:18421/print",
		"print_token": frappe.conf.get("khitmee_print_token") or "",
		"shop_name": frappe.defaults.get_user_default("Company") or frappe.local.site,
	}

	if mode == "transfer":
		# Stock User cannot read Customer or Mode of Payment; never query them here.
		return context

	frappe.has_permission("Customer", ptype="read", throw=True)
	customers, next_start, has_more = _customer_page(customer_query, customer_start)
	modes_of_payment = frappe.get_list(
		"Mode of Payment",
		filters={"enabled": 1},
		fields=["name", "type"],
		order_by="name",
		limit_page_length=100,
	)
	default_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	if not default_price_list:
		default_price_list = frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")

	context.update(
		{
			"default_price_list": default_price_list or "",
			"customers": customers,
			"customers_next_start": next_start,
			"customers_have_more": has_more,
			"modes_of_payment": modes_of_payment,
		}
	)
	return context
