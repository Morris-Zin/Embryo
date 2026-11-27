import json
import sys
import types
import unittest
from datetime import datetime, timedelta
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
	def get_cached_value(_doctype, name, fieldname=None, **_kwargs):
		if fieldname == "name":
			return name
		return AttrDict({"selling": 1, "enabled": 1, "name": name})

	frappe.get_cached_value = get_cached_value
	frappe.get_value = lambda *args, **kwargs: True
	frappe.throw = lambda message, exc=None: (_ for _ in ()).throw((exc or Exception)(message))
	frappe._dict = AttrDict
	frappe.db = SimpleNamespace(sql=lambda *args, **kwargs: [])
	frappe.local = SimpleNamespace(site="test-site")

	def whitelist(*args, **kwargs):
		if args and callable(args[0]):
			return args[0]
		return lambda fn: fn

	frappe.whitelist = whitelist
	utils = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils


install_frappe_stub()

from khitmee_ops.api import catalog


class TestCatalogSyncState(unittest.TestCase):
	def test_state_round_trip_and_invalid_json(self):
		state = {"schema": 1, "since": "2026-01-01 10:00:00.000000", "scope": {"warehouse": "W"}}
		self.assertEqual(catalog._json_state(catalog._state_token(state)), state)
		self.assertEqual(catalog._json_state("not-json"), {})
		self.assertEqual(catalog._json_state(None), {})

	def test_datetime_formats_and_overlap(self):
		parsed = catalog._parse_datetime("2026-01-01 10:00:00.123456")
		self.assertEqual(catalog._format_datetime(parsed), "2026-01-01 10:00:00.123456")
		self.assertIsNone(catalog._parse_datetime("bad-value"))

	def test_delta_query_uses_overlap_and_warehouse_scope(self):
		queries = []
		params = []

		def sql(query, values=None, **kwargs):
			queries.append(query)
			params.append(values)
			return [("ITEM-1",)]

		catalog.frappe.db.sql = sql
		since = datetime(2026, 1, 2, 12, 0, 0)
		self.assertEqual(catalog._changed_item_codes(since, "Shop 1"), ["ITEM-1"])
		self.assertIn("UNION", queries[0])
		self.assertIn("`tabItem Price`", queries[0])
		self.assertIn("`tabBin`", queries[0])
		self.assertNotIn("`tabKhitmee Catalog Tombstone`", queries[0])
		self.assertEqual(catalog._tombstoned_item_codes(since), ["ITEM-1"])
		self.assertIn("`tabKhitmee Catalog Tombstone`", queries[1])
		self.assertEqual(params[0][3], "Shop 1")
		self.assertEqual(params[0][0], "2026-01-02 11:59:50.000000")

	def test_item_price_deletion_is_refreshed_from_deleted_document_data(self):
		queries = []

		def sql(query, values=None, **kwargs):
			queries.append(query)
			if "`tabDeleted Document`" in query:
				return [(json.dumps({"item_code": "ITEM-1"}),)]
			return []

		catalog.frappe.db.sql = sql
		since = datetime(2026, 1, 2, 12, 0, 0)
		self.assertEqual(catalog._price_deletion_item_codes(since), ["ITEM-1"])
		self.assertEqual(catalog._price_deletion_item_codes(since), ["ITEM-1"])
		self.assertIn("deleted_doctype = 'Item Price'", queries[0])

	def test_stale_delta_older_than_one_day_requires_reset(self):
		now = datetime(2026, 1, 2, 12, 0, 0)
		self.assertTrue(catalog._delta_is_stale(now - timedelta(hours=24, seconds=1), now))
		self.assertFalse(catalog._delta_is_stale(now - timedelta(hours=1), now))


class TestCatalogTombstones(unittest.TestCase):
	def test_trash_and_rename_record_old_item_code(self):
		queries = []
		catalog.frappe.db.sql = lambda query, values=None, **kwargs: queries.append((query, values)) or []
		catalog.record_item_catalog_tombstone(SimpleNamespace(name="OLD-ITEM"), "on_trash")
		self.assertEqual(queries[-1][1], ("OLD-ITEM",))
		catalog.record_item_catalog_tombstone(SimpleNamespace(name="NEW-ITEM"), "after_rename", "OLD-ITEM", "NEW-ITEM", False)
		self.assertEqual(queries[-1][1], ("OLD-ITEM",))
		catalog.record_item_catalog_tombstone(SimpleNamespace(name="NEW-ITEM"), "after_rename", "NEW-ITEM", "NEW-ITEM", False)
		self.assertEqual(len(queries), 2)


class TestCatalogEndpoint(unittest.TestCase):
	def setUp(self):
		catalog.frappe.session.user = "counter@example.com"
		for target, replacement in {
			"_item_permission_restrictions_active": lambda: False,
			"_validate_warehouse": lambda value: value,
			"_validate_price_list": lambda value: value,
		}.items():
			patcher = patch.object(catalog, target, side_effect=replacement)
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_reset_payload_is_public_and_has_monotonic_state(self):
		now = datetime(2026, 1, 2, 12, 0, 0, 123456)
		item_row = {
			"item_code": "ITEM-1",
			"item_name": "Tea",
			"stock_uom": "Nos",
			"item_group": "Food",
			"brand": "House",
			"barcode": "111",
			"barcode_text": "111 222",
			"modified": now,
			"valuation_rate": 999,
			"supplier": "secret",
		}

		with (
			patch.object(catalog, "_require_counter_user", return_value=None),
			patch.object(catalog, "_validate_warehouse", return_value="Shop 1"),
			patch.object(catalog, "_validate_price_list", return_value="Retail"),
			patch.object(catalog, "_server_now", return_value=catalog._format_datetime(now)),
			patch.object(catalog, "_permission_version", return_value="plain:0:"),
			patch.object(catalog, "_all_active_item_codes", return_value=["ITEM-1"]),
			patch.object(catalog, "_fetch_catalog_rows", return_value={"ITEM-1": item_row}),
			patch.object(catalog, "_permitted_codes", return_value={"ITEM-1"}),
			patch.object(catalog, "_fetch_rates_and_quantities", side_effect=lambda rows, *args, **kwargs: [setattr(row, "rate", 12.5) or setattr(row, "actual_qty", 7) for row in rows]),
		):
			result = catalog.sync_catalog("Shop 1", "Retail")

		self.assertTrue(result["reset"])
		self.assertEqual(result["schema_version"], 1)
		self.assertEqual(len(result["items"]), 1)
		row = result["items"][0]
		self.assertEqual(row["item_code"], "ITEM-1")
		self.assertEqual(row["barcode_text"], "111 222")
		self.assertEqual(row["rate"], 12.5)
		self.assertEqual(row["actual_qty"], 7)
		self.assertNotIn("valuation_rate", row)
		self.assertNotIn("supplier", row)
		state = json.loads(result["sync_token"])
		self.assertEqual(state["since"], "2026-01-02 12:00:00.123456")
		self.assertEqual(state["scope"], {"warehouse": "Shop 1", "price_list": "Retail", "customer": ""})

	def test_delta_marks_missing_or_unpermitted_items_deleted(self):
		now = datetime(2026, 1, 2, 12, 0, 0)
		previous = {
			"schema": 1,
			"since": catalog._format_datetime(now - timedelta(minutes=2)),
			"permission": "plain:0:",
			"scope": {"warehouse": "Shop 1", "price_list": "Retail", "customer": ""},
		}
		with (
			patch.object(catalog, "_require_counter_user", return_value=None),
			patch.object(catalog, "_validate_warehouse", return_value="Shop 1"),
			patch.object(catalog, "_validate_price_list", return_value="Retail"),
			patch.object(catalog, "_server_now", return_value=catalog._format_datetime(now)),
			patch.object(catalog, "_permission_version", return_value="plain:0:"),
			patch.object(catalog, "_changed_item_codes", return_value=["DELETED", "ACTIVE"]),
			patch.object(catalog, "_fetch_catalog_rows", return_value={"ACTIVE": {"item_code": "ACTIVE", "item_name": "Tea", "stock_uom": "Nos", "modified": now}}),
			patch.object(catalog, "_permitted_codes", return_value=set()),
			patch.object(catalog, "_fetch_rates_and_quantities", return_value=[]),
		):
			result = catalog.sync_catalog("Shop 1", "Retail", state=json.dumps(previous))

		self.assertFalse(result["reset"])
		self.assertEqual(result["items"], [
			{"item_code": "DELETED", "deleted": True},
			{"item_code": "ACTIVE", "deleted": True},
		])

	def test_item_permission_restrictions_force_reset(self):
		now = datetime(2026, 1, 2, 12, 0, 0)
		previous = {
			"schema": 1,
			"since": catalog._format_datetime(now - timedelta(minutes=2)),
			"permission": "hooks:v1",
			"scope": {"warehouse": "Shop 1", "price_list": "Retail", "customer": ""},
		}
		with (
			patch.object(catalog, "_server_now", return_value=catalog._format_datetime(now)),
			patch.object(catalog, "_permission_version", return_value="hooks:v1"),
			patch.object(catalog, "_item_permission_restrictions_active", return_value=True),
			patch.object(catalog, "_all_active_item_codes", return_value=[]),
			patch.object(catalog, "_active_item_count", return_value=0),
			patch.object(catalog, "_changed_item_codes") as changed,
		):
			result = catalog.sync_catalog("Shop 1", "Retail", state=json.dumps(previous))
		self.assertTrue(result["reset"])
		changed.assert_not_called()

	def test_case_only_rename_adds_new_code_and_deletes_old_code(self):
		now = datetime(2026, 1, 2, 12, 0, 0)
		previous = {
			"schema": 1,
			"since": catalog._format_datetime(now - timedelta(minutes=2)),
			"permission": "plain:v1",
			"scope": {"warehouse": "Shop 1", "price_list": "Retail", "customer": ""},
		}
		new_row = {"item_code": "item", "item_name": "Tea", "stock_uom": "Nos", "item_group": "", "brand": "", "barcode": "", "barcode_text": "", "modified": now}
		with (
			patch.object(catalog, "_server_now", return_value=catalog._format_datetime(now)),
			patch.object(catalog, "_permission_version", return_value="plain:v1"),
			patch.object(catalog, "_changed_item_codes", return_value=["item"]),
			patch.object(catalog, "_tombstoned_item_codes", return_value=["ITEM"]),
			patch.object(catalog, "_fetch_catalog_rows", return_value={"item": new_row}),
			patch.object(catalog, "_permitted_codes", return_value={"item"}),
			patch.object(catalog, "_catalog_public_rows", return_value=[{"item_code": "item"}]),
		):
			result = catalog.sync_catalog("Shop 1", "Retail", state=json.dumps(previous))
		self.assertEqual(result["items"], [{"item_code": "item"}, {"item_code": "ITEM", "deleted": True}])

	def test_crossing_midnight_forces_price_validity_reset(self):
		now = datetime(2026, 1, 2, 0, 0, 30)
		previous = {
			"schema": 1,
			"since": catalog._format_datetime(datetime(2026, 1, 1, 23, 59, 0)),
			"permission": "plain:v1",
			"scope": {"warehouse": "Shop 1", "price_list": "Retail", "customer": ""},
		}
		with (
			patch.object(catalog, "_server_now", return_value=catalog._format_datetime(now)),
			patch.object(catalog, "_permission_version", return_value="plain:v1"),
			patch.object(catalog, "_all_active_item_codes", return_value=[]),
			patch.object(catalog, "_active_item_count", return_value=0),
			patch.object(catalog, "_changed_item_codes") as changed,
		):
			result = catalog.sync_catalog("Shop 1", "Retail", state=json.dumps(previous))
		self.assertTrue(result["reset"])
		changed.assert_not_called()

	def test_scope_or_permission_mismatch_forces_reset(self):
		now = datetime(2026, 1, 2, 12, 0, 0)
		previous = {
			"schema": 1,
			"since": catalog._format_datetime(now - timedelta(minutes=2)),
			"permission": "old",
			"scope": {"warehouse": "Other", "price_list": "Retail", "customer": ""},
		}
		with (
			patch.object(catalog, "_require_counter_user", return_value=None),
			patch.object(catalog, "_validate_warehouse", return_value="Shop 1"),
			patch.object(catalog, "_validate_price_list", return_value="Retail"),
			patch.object(catalog, "_server_now", return_value=catalog._format_datetime(now)),
			patch.object(catalog, "_permission_version", return_value="plain:0:"),
			patch.object(catalog, "_all_active_item_codes", return_value=[]),
			patch.object(catalog, "_fetch_catalog_rows", return_value={}),
			patch.object(catalog, "_permitted_codes", return_value=set()),
		):
			result = catalog.sync_catalog("Shop 1", "Retail", state=json.dumps(previous))
		self.assertTrue(result["reset"])


if __name__ == "__main__":
	unittest.main()
