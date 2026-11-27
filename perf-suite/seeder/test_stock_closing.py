#!/usr/bin/env python3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_closing import (
    dimension_fieldnames,
    inspect_stock_closing_rows,
    stock_closing_balance_columns,
    stock_closing_lock_name,
)


def row(name, to_date, status, **overrides):
    data = {
        "name": name,
        "from_date": "2026-01-01",
        "to_date": to_date,
        "status": status,
        "modified": datetime(2026, 1, 1, 0, 0, 0),
        "docstatus": 1,
    }
    data.update(overrides)
    return data


class StockClosingPlanningTests(unittest.TestCase):
    def test_lock_name_is_deterministic_and_mysql_sized(self):
        first = stock_closing_lock_name("Khitmee Co", "2026-01-31")
        second = stock_closing_lock_name("Khitmee Co", "2026-01-31")
        other = stock_closing_lock_name("Khitmee Co", "2026-02-01")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(first), 64)

    def test_dimension_fieldnames_accept_dicts_and_objects(self):
        class Dimension:
            fieldname = "inventory_zone"

        fields = dimension_fieldnames([
            {"fieldname": "custom_aisle"},
            {"target_fieldname": "custom_bin"},
            Dimension(),
        ])
        self.assertEqual(fields, ["custom_aisle", "custom_bin", "inventory_zone"])

    def test_balance_columns_include_only_existing_dynamic_dimensions(self):
        columns = stock_closing_balance_columns(
            [{"fieldname": "custom_aisle"}, {"fieldname": "missing_dimension"}],
            lambda field: field == "custom_aisle",
        )
        self.assertIn("custom_aisle", columns)
        self.assertNotIn("missing_dimension", columns)

    def test_no_rows_plans_create(self):
        action, selected, reason = inspect_stock_closing_rows([], "2026-01-31")
        self.assertEqual(action, "create")
        self.assertIsNone(selected)
        self.assertIn("no conflicting", reason)

    def test_completed_exact_row_is_reused(self):
        completed = row("SCE-1", "2026-01-31", "Completed")
        action, selected, _ = inspect_stock_closing_rows([completed], "2026-01-31")
        self.assertEqual((action, selected), ("reuse", completed))

    def test_failed_exact_row_is_recovered(self):
        failed = row("SCE-2", "2026-01-31", "Failed")
        action, selected, _ = inspect_stock_closing_rows([failed], "2026-01-31")
        self.assertEqual((action, selected), ("recover", failed))

    def test_submitted_draft_row_is_an_abandoned_creation_and_recoverable(self):
        draft = row("SCE-DRAFT", "2026-01-31", "Draft", docstatus=1)
        action, selected, reason = inspect_stock_closing_rows([draft], "2026-01-31")
        self.assertEqual((action, selected), ("recover", draft))
        self.assertIn("never started", reason)

    def test_fresh_active_row_is_not_recovered(self):
        now = datetime(2026, 1, 1, 1, 0, 0)
        active = row("SCE-3", "2026-01-31", "In Progress", modified=now - timedelta(seconds=30))
        action, selected, reason = inspect_stock_closing_rows([active], "2026-01-31", now=now)
        self.assertEqual((action, selected), ("active", active))
        self.assertIn("live owner", reason)

    def test_stale_active_row_is_recoverable(self):
        now = datetime(2026, 1, 1, 2, 0, 0)
        active = row("SCE-4", "2026-01-31", "Queued", modified=now - timedelta(seconds=2000))
        action, selected, reason = inspect_stock_closing_rows(
            [active], "2026-01-31", abandoned_after_seconds=1800, now=now
        )
        self.assertEqual((action, selected), ("recover", active))
        self.assertIn("stale", reason)

    def test_scoped_exact_row_conflicts(self):
        scoped = row("SCE-5", "2026-01-31", "Completed", warehouse="Shop 1")
        action, _, reason = inspect_stock_closing_rows([scoped], "2026-01-31")
        self.assertEqual(action, "error")
        self.assertIn("scoped", reason)

    def test_later_closing_conflicts(self):
        later = row("SCE-6", "2026-02-15", "Completed")
        action, _, reason = inspect_stock_closing_rows([later], "2026-01-31")
        self.assertEqual(action, "error")
        self.assertIn("later closing", reason)


if __name__ == "__main__":
    unittest.main()
