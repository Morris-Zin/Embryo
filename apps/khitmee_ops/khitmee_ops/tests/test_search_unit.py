import random
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))


class AttrDict(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


def install_frappe_stub():
	frappe = sys.modules.get("frappe") or types.ModuleType("frappe")
	frappe.PermissionError = PermissionError
	frappe._ = lambda message, *args: message
	frappe.session = SimpleNamespace(user="counter@example.com")
	frappe.has_permission = lambda *args, **kwargs: True
	frappe.throw = lambda message, exc=None: (_ for _ in ()).throw((exc or Exception)(message))
	frappe.get_cached_value = lambda *args, **kwargs: None
	frappe.get_hooks = lambda *args, **kwargs: {}
	frappe.get_list = lambda *args, **kwargs: []
	frappe.log_error = lambda *args, **kwargs: None
	frappe._dict = AttrDict
	frappe.db = SimpleNamespace(sql=lambda *args, **kwargs: [], get_single_value=lambda *args: None, get_value=lambda *args: None)
	frappe.local = SimpleNamespace(site="test-site")

	def whitelist(*args, **kwargs):
		if args and callable(args[0]):
			return args[0]
		return lambda fn: fn

	frappe.whitelist = whitelist
	frappe.cache = lambda: None

	utils = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)

	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils


install_frappe_stub()

from khitmee_ops.api import search


class FakeCache:
	def __init__(self):
		self.values = {search.ITEM_SEARCH_VERSION_KEY: "1"}
		self.incr_calls = 0

	def get_value(self, key):
		return self.values.get(key)

	def set_value(self, key, value):
		self.values[key] = str(value)

	def incr(self, key):
		self.incr_calls += 1
		self.values[key] = str(int(self.values.get(key) or "0") + 1)


class RawRedisCache(FakeCache):
	def __init__(self):
		super().__init__()
		self.raw_values = {}

	def make_key(self, key):
		return f"db|{key}".encode()

	def get(self, key):
		return self.raw_values.get(key)

	def set(self, key, value, nx=False):
		if nx and key in self.raw_values:
			return False
		self.raw_values[key] = str(value).encode()
		return True

	def incr(self, key):
		next_value = int((self.raw_values.get(key) or b"0").decode()) + 1
		self.raw_values[key] = str(next_value).encode()
		return next_value


def catalog_rows():
	return [
		{
			"name": "A-PREFIX",
			"item_code": "A-PREFIX",
			"item_name": "Red Hammer",
			"stock_uom": "Nos",
			"item_group": "Tools",
			"brand": "Brand",
			"barcode": "111",
			"barcode_text": "111",
		},
		{
			"name": "B-SUBSTRING",
			"item_code": "B-SUBSTRING",
			"item_name": "Large Red Nail",
			"stock_uom": "Nos",
			"item_group": "Tools",
			"brand": "Brand",
			"barcode": "222",
			"barcode_text": "222",
		},
		{
			"name": "C-EXACT",
			"item_code": "C-EXACT",
			"item_name": "Other",
			"stock_uom": "Nos",
			"item_group": "Tools",
			"brand": "Brand",
			"barcode": "red",
			"barcode_text": "red",
		},
	]


class TestItemCatalogCache(unittest.TestCase):
	def setUp(self):
		search._ITEM_CATALOG_CACHE.clear()
		self.cache = FakeCache()
		search.frappe.cache = lambda: self.cache
		search.frappe.local.site = "test-site"

	def test_catalog_query_loads_enabled_nonvariants_in_name_order_with_aggregated_barcodes(self):
		queries = []
		search.frappe.db.sql = lambda sql, params=None, **kwargs: queries.append(sql) or []

		search._load_item_catalog_rows()

		self.assertIn("ORDER BY item.name", queries[0])
		self.assertIn("parentfield = 'barcodes'", queries[0])
		self.assertIn("GROUP_CONCAT(barcode ORDER BY barcode SEPARATOR ' ')", queries[0])
		self.assertIn("item.disabled = 0 AND item.has_variants = 0", queries[0])

	def test_ranking_is_exact_barcode_then_prefix_then_substring(self):
		catalog = search._build_item_catalog(catalog_rows())
		with patch.object(search, "_get_item_catalog", return_value=catalog):
			self.assertEqual(search._ranked_item_codes("red", 10), ["C-EXACT", "A-PREFIX", "B-SUBSTRING"])

	def test_like_wildcards_are_literal_in_memory_matching(self):
		rows = [
			{"name": "A", "item_code": "A", "item_name": "10% discount", "stock_uom": "Nos", "item_group": "G", "brand": "B", "barcode": "", "barcode_text": ""},
			{"name": "B", "item_code": "B", "item_name": "plain", "stock_uom": "Nos", "item_group": "G", "brand": "B", "barcode": "", "barcode_text": ""},
			{"name": "C", "item_code": "C", "item_name": "under_score", "stock_uom": "Nos", "item_group": "G", "brand": "B", "barcode": "", "barcode_text": ""},
		]
		catalog = search._build_item_catalog(rows)
		with patch.object(search, "_get_item_catalog", return_value=catalog):
			self.assertEqual(search._ranked_item_codes("%", 10), ["A"])
			self.assertEqual(search._ranked_item_codes("_", 10), ["C"])

	def test_version_invalidation_evicts_old_site_catalog_and_reloads(self):
		loads = []
		versions = []

		def load_rows():
			loads.append(1)
			return catalog_rows()

		def version():
			value = self.cache.get_value(search.ITEM_SEARCH_VERSION_KEY)
			versions.append(value)
			return value

		with (
			patch.object(search, "_load_item_catalog_rows", side_effect=load_rows),
			patch.object(search, "_item_search_version", side_effect=version),
		):
			first = search._get_item_catalog()
			search.invalidate_item_search_cache()
			second = search._get_item_catalog()

		self.assertIsNot(first, second)
		self.assertEqual(len(loads), 2)
		self.assertEqual(self.cache.values[search.ITEM_SEARCH_VERSION_KEY], "2")
		self.assertEqual(list(search._ITEM_CATALOG_CACHE), [("test-site", "2")])

	def test_atomic_raw_redis_incr_uses_prefixed_unpickled_version_key(self):
		raw_cache = RawRedisCache()
		search.frappe.cache = lambda: raw_cache

		with patch.object(search.time, "time_ns", return_value=1_000):
			search.invalidate_item_search_cache()

		raw_key = raw_cache.make_key(search.ITEM_SEARCH_VERSION_KEY)
		self.assertEqual(raw_cache.raw_values[raw_key], b"1001")
		self.assertEqual(search._item_search_version(), "1001")
		self.assertEqual(raw_cache.values[search.ITEM_SEARCH_VERSION_KEY], "1")

	def test_invalidate_accepts_after_rename_positional_arguments(self):
		raw_cache = RawRedisCache()
		search.frappe.cache = lambda: raw_cache

		with patch.object(search.time, "time_ns", return_value=2_000):
			search.invalidate_item_search_cache(AttrDict(name="ITEM-2"), "after_rename", "ITEM-1", "ITEM-2", False)

		raw_key = raw_cache.make_key(search.ITEM_SEARCH_VERSION_KEY)
		self.assertEqual(raw_cache.raw_values[raw_key], b"2001")

	def test_restricted_user_falls_back_to_permission_filtered_item_list(self):
		with (
			patch.object(search, "_item_permission_restrictions_active", return_value=True),
			patch.object(search, "_permitted_item_codes", return_value=["A"]) as permitted,
		):
			self.assertEqual(search._permitted_catalog_item_codes(["A", "B"]), ["A"])
		permitted.assert_called_once_with(["A", "B"])

	def test_unrestricted_user_skips_extra_permission_query(self):
		with (
			patch.object(search, "_item_permission_restrictions_active", return_value=False),
			patch.object(search, "_permitted_item_codes", side_effect=AssertionError("must not query")) ,
		):
			self.assertEqual(search._permitted_catalog_item_codes(["A", "B"]), ["A", "B"])

	def test_permission_hooks_and_user_permissions_activate_restricted_fallback(self):
		search.frappe.session.user = "restricted@example.com"
		search.frappe.get_hooks = lambda *args, **kwargs: {"Item": ["hook"]}
		self.assertTrue(search._item_permission_restrictions_active())

		search.frappe.get_hooks = lambda *args, **kwargs: {}
		search.frappe.db.sql = lambda *args, **kwargs: [(1,)]
		self.assertTrue(search._item_permission_restrictions_active())

		search.frappe.db.sql = lambda *args, **kwargs: []
		self.assertFalse(search._item_permission_restrictions_active())

	def test_search_items_uses_ranked_catalog_order_limit_and_public_row_shape(self):
		row = catalog_rows()[0] | {"valuation_rate": 100, "last_purchase_rate": 90, "supplier": "Secret"}
		catalog = search._build_item_catalog([row, *catalog_rows()[1:]])
		rate_calls = []

		def fetch_rates(rows, warehouse, price_list, customer=None, uom=None):
			rate_calls.append((list(rows), warehouse, price_list, customer, uom))
			for item in rows:
				item.rate = 12.5
				item.actual_qty = 3
			return rows

		with (
			patch.object(search, "_get_item_catalog", return_value=catalog),
			patch.object(search, "_require_counter_user", return_value=None),
			patch.object(search, "_validate_warehouse", return_value="W"),
			patch.object(search, "_validate_price_list", return_value="PL"),
			patch.object(search, "_item_permission_restrictions_active", return_value=False),
			patch.object(search, "_fetch_rates_and_quantities", side_effect=fetch_rates),
		):
			result = search.search_items("red", "W", "PL", limit=2)

		self.assertEqual([row["item_code"] for row in result["items"]], ["C-EXACT", "A-PREFIX"])
		self.assertEqual(
			set(result["items"][0]),
			{"item_code", "item_name", "stock_uom", "barcode", "rate", "actual_qty"},
		)
		self.assertNotIn("valuation_rate", result["items"][0])
		self.assertEqual(rate_calls[0][1:], ("W", "PL", None, None))
		self.assertEqual(result["items"][0]["rate"], 12.5)

	def test_missing_price_list_is_rejected_for_default_sales_search(self):
		with (
			patch.object(search, "_require_counter_user", return_value=None),
			patch.object(search, "_validate_warehouse", return_value="W"),
			patch.object(search, "_validate_price_list", side_effect=Exception("invalid price list")) as validate_price_list,
		):
			with self.assertRaisesRegex(Exception, "invalid price list"):
				search.search_items("red", "W", None)
		validate_price_list.assert_called_once_with(None)

	def test_explicit_no_rate_transfer_search_skips_price_list_and_rate_query(self):
		catalog = search._build_item_catalog(catalog_rows())
		with (
			patch.object(search, "_get_item_catalog", return_value=catalog),
			patch.object(search, "_require_counter_user", return_value=None),
			patch.object(search, "_validate_warehouse", return_value="W"),
			patch.object(search, "_validate_price_list", side_effect=AssertionError("must not validate price list")),
			patch.object(search, "_item_permission_restrictions_active", return_value=False),
			patch.object(search, "_fetch_rates_and_quantities", side_effect=AssertionError("must not query item prices")),
			patch.object(search, "_fetch_quantities", return_value={"C-EXACT": 7, "A-PREFIX": 2}),
		):
			result = search.search_items("red", "W", None, limit=2, include_rates="false")

		self.assertEqual([row["item_code"] for row in result["items"]], ["C-EXACT", "A-PREFIX"])
		self.assertEqual(result["items"][0]["rate"], 0)
		self.assertEqual(result["items"][0]["actual_qty"], 7)


def legacy_ranked_item_codes(rows, term, limit):
	"""Independent Pass-2 ranking oracle used by the flat-index differential tests."""
	entries = []
	exact_barcode_map = {}
	for source_row in rows or []:
		if hasattr(source_row, "get"):
			row = dict(source_row)
		else:
			row = dict(zip(
				("name", "item_code", "item_name", "stock_uom", "item_group", "brand", "barcode", "barcode_text"),
				source_row,
			))
		name = str(row.get("name") or row.get("item_code") or "")
		if not name:
			continue
		row["name"] = name
		barcode_text = str(row.get("barcode_text") or "")
		barcodes = barcode_text.split()
		if row.get("barcode") and row.get("barcode") not in barcodes:
			barcodes.insert(0, row.get("barcode"))
		for barcode in barcodes:
			folded_barcode = search._fold_search_text(barcode)
			if folded_barcode and folded_barcode not in exact_barcode_map:
				exact_barcode_map[folded_barcode] = name
		entries.append(
			{
				"name": name,
				"prefix_folded": tuple(search._fold_search_text(row.get(fieldname)) for fieldname in search.PREFIX_SEARCHABLE_ITEM_FIELDS),
				"search_folded": tuple(search._fold_search_text(row.get(fieldname)) for fieldname in search.PUBLIC_SEARCHABLE_ITEM_FIELDS),
				"barcode_text_folded": search._fold_search_text(barcode_text),
			}
		)

	folded_term = search._fold_search_text(term)
	cap = max(limit * 4, limit)
	if not folded_term:
		return [entry["name"] for entry in entries][:cap]
	ordered = []
	seen = set()

	def add(item_code):
		if item_code and item_code not in seen and len(ordered) < cap:
			seen.add(item_code)
			ordered.append(item_code)

	add(exact_barcode_map.get(folded_term))
	prefix_matches = 0
	for entry in entries:
		if prefix_matches >= limit:
			break
		if any(value.startswith(folded_term) for value in entry["prefix_folded"]):
			if entry["name"] not in seen:
				prefix_matches += 1
			add(entry["name"])
	for entry in entries:
		if len(ordered) >= cap:
			break
		if folded_term in entry["barcode_text_folded"] or any(
			folded_term in value for value in entry["search_folded"]
		):
			add(entry["name"])
	return ordered[:cap]


class TestPass3SearchOptimizations(unittest.TestCase):
	def setUp(self):
		search._ITEM_CATALOG_CACHE.clear()
		self.cache = FakeCache()
		search.frappe.cache = lambda: self.cache
		search.frappe.local.site = "test-site"

	def test_catalog_query_requests_tuple_rows(self):
		kwargs_seen = []
		search.frappe.db.sql = lambda sql, params=None, **kwargs: kwargs_seen.append(kwargs) or []

		search._load_item_catalog_rows()

		self.assertIn("as_dict", kwargs_seen[0])
		self.assertFalse(kwargs_seen[0]["as_dict"])

	def test_build_accepts_tuple_and_dict_rows_and_keeps_compact_public_shape(self):
		tuple_row = ("T-1", "T-CODE", "Tuple Item", "Nos", "Tools", "Brand", "123", "123 456")
		catalog = search._build_item_catalog([tuple_row, catalog_rows()[0]])

		self.assertNotIn("entries", catalog)
		self.assertNotIn("rows_by_name", catalog)
		self.assertEqual(catalog["item_tuples"][0], ("T-1", "Tuple Item", "Nos", "123"))
		self.assertEqual(catalog["index_by_name"]["T-1"], 0)
		self.assertEqual(catalog["exact_barcode_map"]["456"], "T-1")
		self.assertEqual(catalog["names"], ["T-1", "A-PREFIX"])

	def test_flat_blob_ranking_matches_legacy_loop_on_differential_fuzz_corpus(self):
		rng = random.Random(20261127)
		alphabet = ["a", "b", "red", " hammer", "%", "_", "é", "ß", "𝄞", "\x1f", "\x1e", ""]
		for case in range(18):
			rows = []
			for index in range(rng.randrange(1, 12)):
				def value():
					return rng.choice(alphabet) + (str(index) if rng.random() < 0.35 else "")

				rows.append(
					{
						"name": f"I{index}" + value(),
						"item_code": f"C{index}" + value(),
						"item_name": value(),
						"stock_uom": "Nos",
						"item_group": value(),
						"brand": value(),
						"barcode": value(),
						"barcode_text": value(),
					}
				)
			catalog = search._build_item_catalog(rows)
			for term in ("red", "é", "%", "\x1f", "a", rng.choice(alphabet)):
				for limit in (1, 2, 7, 25):
					self.assertEqual(
						search._ranked_item_codes(term, limit, catalog=catalog),
						legacy_ranked_item_codes(rows, term, limit),
						f"case={case} term={term!r} limit={limit}",
					)

	def test_all_separator_chars_use_fallback_loop_with_legacy_parity(self):
		all_separators = "".join(("\x1f", "\x1e", "\x1d", "\x1c", "\ufdd0", "\ufdd1"))
		rows = [
			{
				"name": "SEP-ITEM",
				"item_code": "SEP-ITEM",
				"item_name": f"contains {all_separators} every separator",
				"stock_uom": "Nos",
				"item_group": "G",
				"brand": "B",
				"barcode": "",
				"barcode_text": "",
			}
		]
		catalog = search._build_item_catalog(rows)

		self.assertIsNone(catalog["sep"])
		for term in ("\x1f", "separator", "every"):
			self.assertEqual(
				search._ranked_item_codes(term, 10, catalog=catalog),
				legacy_ranked_item_codes(rows, term, 10),
			)

	def test_separator_in_term_has_no_blob_matches_when_separator_is_unused(self):
		catalog = search._build_item_catalog(catalog_rows())

		self.assertIsNotNone(catalog["sep"])
		self.assertEqual(search._ranked_item_codes("red" + catalog["sep"], 10, catalog=catalog), [])

	def test_empty_term_direct_ranker_returns_ordered_names_up_to_cap(self):
		catalog = search._build_item_catalog(catalog_rows())

		self.assertEqual(search._ranked_item_codes("", 1, catalog=catalog), catalog["names"][:4])

	def test_ascii_fold_fast_path_never_calls_erpnext_fold_text(self):
		with patch.object(search, "fold_text", side_effect=AssertionError("ASCII must not fold")):
			self.assertEqual(search._fold_search_text("ASCII Mixer"), "ascii mixer")

	def test_unicode_fold_idempotence_over_deterministic_non_ascii_corpus(self):
		codepoints = list(range(0x80, 0x180)) + list(range(0x300, 0x380)) + list(range(0x1F300, 0x1F380, 7))
		corpus = [chr(codepoint) for codepoint in codepoints] + ["ß", "İstanbul", "é𝄞\ufdd0"]
		for text in corpus:
			folded = search._fold_search_text(text)
			self.assertEqual(search._fold_search_text(folded), folded)

	def test_catalog_build_folds_each_distinct_raw_string_once(self):
		calls = []
		real_fold = search._fold_search_text

		def counting_fold(value):
			calls.append(str(value or ""))
			return real_fold(value)

		rows = [dict(row) for row in catalog_rows()[:2]]
		with patch.object(search, "_fold_search_text", side_effect=counting_fold):
			search._build_item_catalog(rows)

		self.assertEqual(calls.count("Tools"), 1)
		self.assertEqual(calls.count("Brand"), 1)
		self.assertEqual(calls.count("Nos"), 0)  # stock_uom is not a searchable folded field

	def test_thread_safe_catalog_build_race_loads_once(self):
		load_calls = []

		def load_rows():
			with load_calls_lock:
				load_calls.append(1)
			time.sleep(0.02)
			return catalog_rows()

		load_calls_lock = threading.Lock()
		errors = []
		results = []

		def worker():
			try:
				results.append(search._get_item_catalog())
			except Exception as exc:
				errors.append(exc)

		with (
			patch.object(search, "_load_item_catalog_rows", side_effect=load_rows),
			patch.object(search, "_item_search_version", return_value="1"),
		):
			threads = [threading.Thread(target=worker) for _ in range(6)]
			for thread in threads:
				thread.start()
			for thread in threads:
				thread.join(timeout=3)

		self.assertFalse(errors)
		self.assertEqual(len(load_calls), 1)
		self.assertTrue(all(result is results[0] for result in results))

	def test_after_commit_defers_version_bump_until_commit_callback(self):
		raw_cache = RawRedisCache()
		search.frappe.cache = lambda: raw_cache
		callbacks = []
		after_commit = SimpleNamespace(add=callbacks.append)
		raw_key = raw_cache.make_key(search.ITEM_SEARCH_VERSION_KEY)

		with (
			patch.object(search.frappe.db, "after_commit", after_commit, create=True),
			patch.object(search.time, "time_ns", return_value=5_000),
		):
			search.invalidate_item_search_cache()
			self.assertNotIn(raw_key, raw_cache.raw_values)
			self.assertEqual(len(callbacks), 1)
			callbacks[0]()

		self.assertEqual(raw_cache.raw_values[raw_key], b"5001")

	def test_after_commit_version_bump_exception_is_swallowed(self):
		with patch.object(search, "_bump_item_search_version", side_effect=RuntimeError("cache down")):
			search._bump_item_search_version_safely()

	def test_missing_raw_redis_version_is_seeded_with_time_ns_not_reusable_one(self):
		raw_cache = RawRedisCache()
		search.frappe.cache = lambda: raw_cache
		search._ITEM_CATALOG_CACHE[("test-site", "1")] = object()
		raw_key = raw_cache.make_key(search.ITEM_SEARCH_VERSION_KEY)

		with patch.object(search.time, "time_ns", return_value=9_000):
			version = search._item_search_version()

		self.assertEqual(version, "9000")
		self.assertEqual(raw_cache.raw_values[raw_key], b"9000")
		self.assertNotEqual(version, "1")

	def test_search_items_folds_non_ascii_term_exactly_once(self):
		rows = catalog_rows()
		rows[0]["item_name"] = "Éclair Hammer"
		catalog = search._build_item_catalog(rows)
		fold_calls = []
		real_fold_text = search.fold_text

		def counting_fold_text(text):
			fold_calls.append(text)
			return real_fold_text(text)

		def fetch_rates(rows, warehouse, price_list, customer=None, uom=None):
			for item in rows:
				item.rate = 1
				item.actual_qty = 1
			return rows

		with (
			patch.object(search, "fold_text", side_effect=counting_fold_text),
			patch.object(search, "_get_item_catalog", return_value=catalog),
			patch.object(search, "_require_counter_user", return_value=None),
			patch.object(search, "_validate_warehouse", return_value="W"),
			patch.object(search, "_validate_price_list", return_value="PL"),
			patch.object(search, "_item_permission_restrictions_active", return_value=False),
			patch.object(search, "_fetch_rates_and_quantities", side_effect=fetch_rates),
		):
			result = search.search_items("éclair", "W", "PL", limit=5)

		self.assertEqual(len(result["items"]), 1)
		self.assertEqual(len(fold_calls), 1)

	def test_empty_search_term_still_validates_price_list_before_empty_result(self):
		with (
			patch.object(search, "_require_counter_user", return_value=None),
			patch.object(search, "_validate_warehouse", return_value="W"),
			patch.object(search, "_validate_price_list", side_effect=Exception("invalid price list")),
		):
			with self.assertRaisesRegex(Exception, "invalid price list"):
				search.search_items("", "W", "PL")


if __name__ == "__main__":
	unittest.main()
