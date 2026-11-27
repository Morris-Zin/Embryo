from datetime import date
from decimal import Decimal
import unittest

from duckdb_benchmark import compare_result_rows, values_equal
from offload_spec import QUERIES_BY_KEY, TABLES_BY_KEY, format_timedelta, query_parameters


class TableSpecTest(unittest.TestCase):
    def test_select_sql_is_bounded_to_declared_columns(self):
        spec = TABLES_BY_KEY["sales_invoice"]
        sql = spec.select_sql()
        self.assertIn("FROM `tabSales Invoice`", sql)
        self.assertIn("`posting_date`", sql)
        self.assertNotIn("SELECT *", sql)

    def test_duckdb_columns_expression_contains_types(self):
        expression = TABLES_BY_KEY["stock_ledger_entry"].duckdb_columns_expression()
        self.assertIn("'posting_date': 'DATE'", expression)
        self.assertIn("'actual_qty': 'DECIMAL(21,9)'", expression)

    def test_inventory_uses_only_to_date(self):
        self.assertEqual(query_parameters("inventory_balance", "Company", "2026-01-01", "2026-11-26"), ("Company", "2026-11-26"))
        self.assertEqual(query_parameters("invoice_fact_scan", "Company", "2026-01-01", "2026-11-26"), ("Company", "2026-01-01", "2026-11-26"))

    def test_every_query_has_both_dialects(self):
        for key, query in QUERIES_BY_KEY.items():
            self.assertIn("%s", query.mariadb_sql, key)
            self.assertIn("?", query.duckdb_sql, key)
            self.assertNotIn("`tab", query.duckdb_sql, key)


class ParityNormalizationTest(unittest.TestCase):
    def test_decimal_strings_compare_by_numeric_value(self):
        self.assertTrue(values_equal(Decimal("12.3400"), "12.34"))
        self.assertTrue(values_equal(12, "12.0"))
        self.assertFalse(values_equal(Decimal("12.34"), "12.35"))

    def test_dates_compare_to_iso_database_json(self):
        self.assertTrue(values_equal(date(2026, 11, 26), "2026-11-26"))

    def test_rows_compare_exactly_after_numeric_normalization(self):
        result = compare_result_rows(
            [["2026-11-26", "Shop 1", "10.000000", "25.500000"]],
            [(date(2026, 11, 26), "Shop 1", Decimal("10"), Decimal("25.5"))],
        )
        self.assertTrue(result["passed"], result)

    def test_mismatch_is_reported_with_bounded_evidence(self):
        result = compare_result_rows([["a", "1"]], [("a", "2")])
        self.assertFalse(result["passed"])
        self.assertEqual(result["mismatches"][0]["kind"], "value")


class DuckDBDeltaMergeTest(unittest.TestCase):
    def setUp(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is not installed in the offline fast-check environment")

    def test_delta_replaces_changed_primary_keys(self):
        import csv
        import gzip
        import json
        import tempfile
        from pathlib import Path

        import duckdb

        from duckdb_benchmark import apply_delta, load_full

        columns = [column.name for column in TABLES_BY_KEY["item"].columns]

        def write_extract(root, mode, rows):
            root.mkdir(parents=True, exist_ok=True)
            with gzip.open(root / "item.csv.gz", "wt", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(columns)
                writer.writerows(rows)
            (root / "manifest.json").write_text(json.dumps({"mode": mode, "table_order": ["item"]}), encoding="utf-8")

        row1 = ["I-1", "2026-01-01 00:00:00", "I-1", "Item 1", "Group", "Brand", 1, 0]
        row2 = ["I-2", "2026-01-01 00:00:00", "I-2", "Item 2", "Group", "Brand", 1, 0]
        row2_changed = ["I-2", "2026-01-02 00:00:00", "I-2", "Item 2 changed", "Group", "Brand", 1, 0]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            delta = Path(tmp) / "delta"
            write_extract(base, "full", [row1, row2])
            write_extract(delta, "delta", [row2_changed])
            conn = duckdb.connect(":memory:")
            load_full(conn, base, ["item"])
            apply_delta(conn, delta, ["item"])
            rows = conn.execute("SELECT name, item_name FROM item ORDER BY name").fetchall()
            conn.close()
        self.assertEqual(rows, [("I-1", "Item 1"), ("I-2", "Item 2 changed")])


class FormatTest(unittest.TestCase):
    def test_timedelta_format_is_sql_time_compatible(self):
        from datetime import timedelta

        self.assertEqual(format_timedelta(timedelta(hours=8, minutes=1, seconds=2)), "08:01:02")


if __name__ == "__main__":
    unittest.main()
