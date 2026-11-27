#!/usr/bin/env python3
import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replenish_stock
from replenish_stock import (
    ReplenishmentSafetyError,
    assert_safe_replenishment_site,
    compute_replenishment_plan,
)


def sample_manifest():
    return {
        "in_stock_items": {
            "Dist Hub 1 - KMD": [
                {"item_code": "HUB-HOT-1", "qty": 100.0, "uom": "Nos"},
                {"item_code": "HUB-HOT-2", "qty": 500.0, "uom": "Nos"},
                {"item_code": "HUB-COLD", "qty": 250.0, "uom": "Nos"},
            ],
            "Shop 1 - KMD": [
                {"item_code": "SHOP-HOT-1", "qty": 100.0, "uom": "Nos"},
                {"item_code": "SHOP-HOT-2", "qty": 450.0, "uom": "Nos"},
                {"item_code": "SHOP-COLD", "qty": 125.0, "uom": "Nos"},
            ],
        }
    }


def plan_kwargs():
    return {
        "min_hub_hot_qty": 1500.0,
        "hub_hot_rank": 2,
        "min_shop_hot_qty": 400.0,
        "shop_hot_rank": 2,
    }


class ReplenishmentPlanTests(unittest.TestCase):
    def test_hub_and_shop_hot_floor_targets_are_role_specific(self):
        manifest = sample_manifest()
        live_bins = {
            ("Dist Hub 1 - KMD", "HUB-HOT-1"): 100.0,
            ("Dist Hub 1 - KMD", "HUB-HOT-2"): 1400.0,
            ("Dist Hub 1 - KMD", "HUB-COLD"): 250.0,
            ("Shop 1 - KMD", "SHOP-HOT-1"): 399.0,
            ("Shop 1 - KMD", "SHOP-HOT-2"): 450.0,
            ("Shop 1 - KMD", "SHOP-COLD"): 125.0,
        }
        plan = compute_replenishment_plan(manifest, live_bins, **plan_kwargs())

        hub_rows = plan["by_warehouse"]["Dist Hub 1 - KMD"]
        shop_rows = plan["by_warehouse"]["Shop 1 - KMD"]
        self.assertEqual(
            [(row["item_code"], row["qty"], row["target_qty"], row["live_qty"]) for row in hub_rows],
            [("HUB-HOT-1", 1400.0, 1500.0, 100.0), ("HUB-HOT-2", 100.0, 1500.0, 1400.0)],
        )
        self.assertEqual(
            [(row["item_code"], row["qty"], row["target_qty"], row["live_qty"]) for row in shop_rows],
            [("SHOP-HOT-1", 1.0, 400.0, 399.0)],
        )
        self.assertEqual(plan["total_deficits"], 3)
        self.assertEqual(plan["hot_summary"]["distribution"]["hot_items"], 2)
        self.assertEqual(plan["hot_summary"]["distribution"]["hot_deficits"], 2)
        self.assertEqual(plan["hot_summary"]["retail"]["hot_items"], 2)
        self.assertEqual(plan["hot_summary"]["retail"]["hot_deficits"], 1)

    def test_seed_qty_above_hot_floor_is_preserved_as_target(self):
        manifest = {
            "in_stock_items": {
                "Shop 2 - KMD": [{"item_code": "BIG-SEED", "qty": 900.0, "uom": "Nos"}],
            }
        }
        plan = compute_replenishment_plan(
            manifest,
            {("Shop 2 - KMD", "BIG-SEED"): 850.0},
            **plan_kwargs(),
        )
        row = plan["by_warehouse"]["Shop 2 - KMD"][0]
        self.assertEqual(row["target_qty"], 900.0)
        self.assertEqual(row["qty"], 50.0)

    def test_zero_deficit_plan_is_a_noop_shape(self):
        manifest = sample_manifest()
        live_bins = {}
        for warehouse, items in manifest["in_stock_items"].items():
            for index, item in enumerate(items):
                minimum = 1500.0 if "dist" in warehouse.lower() or "hub" in warehouse.lower() else 400.0
                target = max(float(item["qty"]), minimum) if index < 2 else float(item["qty"])
                live_bins[(warehouse, item["item_code"])] = target
        plan = compute_replenishment_plan(manifest, live_bins, **plan_kwargs())
        self.assertEqual(plan["total_deficits"], 0)
        self.assertEqual(plan["by_warehouse"], {})

    def test_missing_live_bin_counts_as_zero_stock(self):
        manifest = {
            "in_stock_items": {
                "Shop 1 - KMD": [{"item_code": "MISSING", "qty": 125.0, "uom": "Nos"}],
            }
        }
        plan = compute_replenishment_plan(
            manifest,
            {},
            min_hub_hot_qty=1500.0,
            hub_hot_rank=0,
            min_shop_hot_qty=400.0,
            shop_hot_rank=0,
        )
        row = plan["by_warehouse"]["Shop 1 - KMD"][0]
        self.assertEqual(row["live_qty"], 0.0)
        self.assertEqual(row["target_qty"], 125.0)
        self.assertEqual(row["qty"], 125.0)

    def test_deficit_is_rounded_to_six_places(self):
        manifest = {
            "in_stock_items": {
                "Shop 1 - KMD": [{"item_code": "ROUNDME", "qty": 1.23456789, "uom": "Nos"}],
            }
        }
        plan = compute_replenishment_plan(
            manifest,
            {("Shop 1 - KMD", "ROUNDME"): 0.00000123},
            min_hub_hot_qty=1500.0,
            hub_hot_rank=0,
            min_shop_hot_qty=400.0,
            shop_hot_rank=0,
        )
        self.assertEqual(plan["by_warehouse"]["Shop 1 - KMD"][0]["qty"], 1.234567)

    def test_compute_plan_does_not_mutate_manifest(self):
        manifest = sample_manifest()
        before = copy.deepcopy(manifest)
        compute_replenishment_plan(manifest, {}, **plan_kwargs())
        self.assertEqual(manifest, before)


class ReplenishmentCliTests(unittest.TestCase):
    def test_apply_zero_deficit_is_read_only_noop(self):
        manifest = {
            "company": "Khit Me Distribution",
            "in_stock_items": {
                "Shop 1 - KMD": [{"item_code": "HEALTHY", "qty": 400.0, "uom": "Nos"}],
            },
        }
        live_bins = {("Shop 1 - KMD", "HEALTHY"): 400.0}
        args = SimpleNamespace(site="bench-loadtest.localhost", allow_non_bench_site=False, manifest=None)
        output = io.StringIO()
        with (
            mock.patch.object(replenish_stock, "connect_and_assert_site", return_value="bench-loadtest.localhost"),
            mock.patch.object(replenish_stock, "load_manifest", return_value=(manifest, "fixture-manifest.json")),
            mock.patch.object(replenish_stock, "fetch_live_bins", return_value=live_bins),
            mock.patch.object(replenish_stock, "post_replenishment_stock_entries") as post_entries,
            redirect_stdout(output),
        ):
            rc = replenish_stock.run_apply(args)
        self.assertEqual(rc, 0)
        post_entries.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["total_deficits"], 0)


class ReplenishmentSafetyTests(unittest.TestCase):
    def test_khitmee_localhost_is_refused(self):
        with self.assertRaises(ReplenishmentSafetyError):
            assert_safe_replenishment_site("khitmee.localhost")

    def test_khitmee_subdomain_is_refused(self):
        with self.assertRaises(ReplenishmentSafetyError):
            assert_safe_replenishment_site("dev.khitmee.localhost")

    def test_non_bench_site_requires_explicit_override(self):
        with self.assertRaises(ReplenishmentSafetyError):
            assert_safe_replenishment_site("other-loadtest.localhost")
        assert_safe_replenishment_site("other-loadtest.localhost", allow_non_bench_site=True)

    def test_connected_site_must_match_requested_site(self):
        with self.assertRaises(ReplenishmentSafetyError):
            assert_safe_replenishment_site(
                "bench-loadtest.localhost",
                actual_site="khitmee.localhost",
            )
        assert_safe_replenishment_site(
            "bench-loadtest.localhost",
            actual_site="bench-loadtest.localhost",
        )


if __name__ == "__main__":
    unittest.main()
