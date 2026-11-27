import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))


def install_frappe_stub():
	frappe = sys.modules.get("frappe") or types.ModuleType("frappe")
	if not hasattr(frappe, "db"):
		frappe.db = SimpleNamespace(sql=lambda *args, **kwargs: [])
	if not hasattr(frappe, "clear_document_cache"):
		frappe.clear_document_cache = lambda *args, **kwargs: None
	if not hasattr(frappe, "cache"):
		frappe.cache = lambda: SimpleNamespace(delete_key=lambda key: None, hdel=lambda *args: None)

	utils = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")
	if not hasattr(utils, "cint"):
		utils.cint = lambda value: int(value or 0)
	if not hasattr(utils, "add_days"):
		utils.add_days = lambda value, days: value
	if not hasattr(utils, "nowdate"):
		utils.nowdate = lambda: "2026-01-01"
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils


install_frappe_stub()

from khitmee_ops.setup import indexes


class RecordingDB:
	def __init__(self, index_rows=None, report_row=None):
		self.index_rows = index_rows or {}
		self.report_row = report_row if report_row is not None else {"prepared_report": 0, "disable_prepared_report_automation": 1}
		self.calls = []

	def sql(self, sql, params=None, as_dict=False, **kwargs):
		self.calls.append((sql, params, as_dict))
		if "information_schema.STATISTICS" in sql:
			return self.index_rows.get((params[0], params[1]), [])
		if "information_schema.COLUMNS" in sql:
			return [(1,)]
		if sql.startswith("SELECT 1 FROM `tabReport`"):
			return [(1,)]
		if "SELECT `prepared_report`, `disable_prepared_report_automation`" in sql:
			return [self.report_row] if as_dict else [(self.report_row["prepared_report"], self.report_row["disable_prepared_report_automation"])]
		return []


class TestIndexDefinitions(unittest.TestCase):
	def test_composite_indexes_have_exact_required_column_order(self):
		definition = {name: (table, columns) for table, name, columns in indexes.COMPOSITE_INDEXES}

		self.assertEqual(
			definition["khitmee_si_co_ds_ret_date"],
			(
				"tabSales Invoice",
				("company", "docstatus", "is_return", "posting_date", "posting_time", "return_against", "is_opening"),
			),
		)
		self.assertEqual(
			definition["khitmee_sii_parent_warehouse"],
			("tabSales Invoice Item", ("parent", "warehouse")),
		)
		self.assertEqual(
			definition["khitmee_sle_company_cancelled_date_wh"],
			("tabStock Ledger Entry", ("company", "is_cancelled", "warehouse", "posting_date")),
		)
		self.assertEqual(
			definition["khitmee_scb_entry_warehouse"],
			("tabStock Closing Balance", ("stock_closing_entry", "warehouse")),
		)
		self.assertEqual(
			definition["khitmee_pi_pt_ds_item"],
			("tabPacked Item", ("parenttype", "docstatus", "parent_item")),
		)

	def test_superseded_indexes_are_dropped_as_obsolete(self):
		self.assertIn(("tabSales Invoice", "khitmee_si_co_ds_date_time_ret"), indexes.OBSOLETE_INDEXES)
		self.assertIn(("tabSales Invoice Item", "khitmee_sii_parent_name_warehouse"), indexes.OBSOLETE_INDEXES)

	def test_index_columns_match_requires_exact_ordered_columns(self):
		rows = [(1, "parent"), (2, "warehouse")]
		self.assertTrue(indexes._index_columns_match(rows, ("parent", "warehouse")))
		self.assertFalse(indexes._index_columns_match([(1, "warehouse"), (2, "parent")], ("parent", "warehouse")))
		self.assertFalse(indexes._index_columns_match(rows + [(3, "name")], ("parent", "warehouse")))
		self.assertFalse(indexes._index_columns_match([], ("parent", "warehouse")))

	def test_mismatched_existing_index_is_dropped_and_recreated(self):
		table = "tabSales Invoice Item"
		name = "khitmee_sii_parent_warehouse"
		db = RecordingDB({(table, name): [(1, "warehouse"), (2, "parent")]})
		indexes.frappe.db = db

		created = indexes.create_composite_indexes()

		self.assertIn(name, created)
		sql_statements = [call[0] for call in db.calls]
		self.assertTrue(any(f"DROP INDEX `{name}` ON `{table}`" in sql for sql in sql_statements))
		self.assertTrue(
			any(
				f"CREATE INDEX `{name}` ON `{table}` (`parent`, `warehouse`)" in sql
				for sql in sql_statements
			)
		)


class TestFastReportSettingsSync(unittest.TestCase):
	def test_fail_closed_sync_verifies_readback_and_invalidates_cache(self):
		db = RecordingDB()
		indexes.frappe.db = db
		with patch.object(indexes, "_invalidate_fast_report_cache") as invalidate:
			self.assertTrue(indexes.sync_fast_report_settings())

		invalidate.assert_called_once_with()
		self.assertTrue(any(sql.startswith("UPDATE `tabReport`") for sql, _params, _as_dict in db.calls))
		self.assertTrue(
			any("SELECT `prepared_report`, `disable_prepared_report_automation`" in sql for sql, _params, _as_dict in db.calls)
		)

	def test_fail_closed_sync_raises_when_readback_is_wrong(self):
		indexes.frappe.db = RecordingDB(report_row={"prepared_report": 1, "disable_prepared_report_automation": 1})
		with patch.object(indexes, "_invalidate_fast_report_cache"):
			with self.assertRaisesRegex(RuntimeError, "failed closed verification"):
				indexes.sync_fast_report_settings()

	def test_cache_invalidation_clears_document_boot_and_report_timing_keys(self):
		calls = []

		class Cache:
			def delete_key(self, key):
				calls.append(("delete_key", key))

			def hdel(self, key, field):
				calls.append(("hdel", key, field))

		indexes.frappe.cache = lambda: Cache()
		with patch.object(indexes.frappe, "clear_document_cache") as clear_document_cache:
			indexes._invalidate_fast_report_cache()

		clear_document_cache.assert_called_once_with("Report", "Fast Gross Profit")
		self.assertIn(("delete_key", "bootinfo"), calls)
		self.assertIn(("hdel", "report_execution_time", "Fast Gross Profit"), calls)


if __name__ == "__main__":
	unittest.main()
