#!/usr/bin/env python3
"""Pure-Python unit tests for the Khitmee benchmark seeder (no database required)."""

from __future__ import annotations

import random
import sys
import unittest
from unittest import mock
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fifo_replay
import seed_dataset
from seed_dataset import (
	BENCH_SITE,
	PROTECTED_SITE,
	PROTECTED_SITES,
	SCALE_CONFIGS,
	FIFOQueue,
	MonotonicClock,
	ScaleConfig,
	allocate_counts_by_day,
	allocate_line_counts,
	assert_safe_site,
	build_bundles,
	build_inventory_events,
	calculate_stock_closing_cutoffs,
	decide_stock_closing_action,
	generate_synthetic_catalog,
	aggregate_sales_demand,
	load_line_distribution,
	SalesPlanGenerator,
	StockClosingConflictError,
	StockClosingStateError,
	receipt_rate,
	validate_manifest_schema,
	zipf_weights,
)


class ZipfDistributionTests(unittest.TestCase):
	def test_zipf_weights_are_normalized_monotonic_and_hot(self):
		weights = zipf_weights(1000, 1.15)
		self.assertAlmostEqual(sum(weights), 1.0, places=12)
		self.assertTrue(all(weights[idx] > weights[idx + 1] for idx in range(200)))
		self.assertGreater(sum(weights[:100]), sum(weights[900:]))
		self.assertGreater(weights[0], 100 * weights[-1])

	def test_invalid_zipf_arguments_raise(self):
		with self.assertRaises(ValueError):
			zipf_weights(0)
		with self.assertRaises(ValueError):
			zipf_weights(10, 0)


class AllocationTests(unittest.TestCase):
	def test_line_allocation_is_exact_and_bounded(self):
		rng = random.Random(42)
		counts = allocate_line_counts(1000, 2638, rng)
		self.assertEqual(len(counts), 1000)
		self.assertEqual(sum(counts), 2638)
		self.assertTrue(all(1 <= count <= 5 for count in counts))

	def test_day_allocation_is_exact(self):
		counts = allocate_counts_by_day(date(2026, 1, 1), date(2026, 12, 31), 360000)
		self.assertEqual(len(counts), 365)
		self.assertEqual(sum(counts), 360000)
		self.assertTrue(all(count >= 0 for count in counts))


class FIFOQueueTests(unittest.TestCase):
	def test_multiple_layers_are_consumed_fifo_and_returns_add_layer(self):
		stock = FIFOQueue()
		stock.add_stock(10, 100, "2025-01-01")
		stock.add_stock(5, 120, "2025-02-01")
		self.assertEqual(stock.queue, [[10.0, 100.0, "2025-01-01"], [5.0, 120.0, "2025-02-01"]])

		consumed, cost = stock.remove_stock(12)
		self.assertEqual(consumed, [[10.0, 100.0], [2.0, 120.0]])
		self.assertAlmostEqual(cost, 1240.0)
		self.assertEqual(stock.queue, [[3.0, 120.0, "2025-02-01"]])

		stock.add_stock(2, 110, "2025-03-01")
		self.assertEqual(stock.queue, [[3.0, 120.0, "2025-02-01"], [2.0, 110.0, "2025-03-01"]])
		consumed, cost = stock.remove_stock(4)
		self.assertEqual(consumed, [[3.0, 120.0], [1.0, 110.0]])
		self.assertAlmostEqual(cost, 470.0)
		self.assertEqual(stock.queue, [[1.0, 110.0, "2025-03-01"]])
		qty, value = stock.total_stock_and_value()
		self.assertAlmostEqual(qty, 1.0)
		self.assertAlmostEqual(value, 110.0)

	def test_fifo_merges_same_rate_and_date_layers(self):
		stock = FIFOQueue()
		stock.add_stock(2, 50, "2025-01-05")
		stock.add_stock(3, 50, "2025-01-05")
		self.assertEqual(stock.queue, [[5.0, 50.0, "2025-01-05"]])

	def test_fifo_keeps_same_rate_layers_with_distinct_dates(self):
		stock = FIFOQueue()
		stock.add_stock(2, 50, "2025-01-05")
		stock.add_stock(3, 50, "2025-02-05")
		self.assertEqual(stock.queue, [[2.0, 50.0, "2025-01-05"], [3.0, 50.0, "2025-02-05"]])

	def test_compact_json_contains_qty_rate_pairs(self):
		stock = FIFOQueue()
		stock.add_stock(2, 50.1234567, "2025-01-05")
		self.assertEqual(stock.compact_json(), "[[2.0,50.123457]]")

	def test_dated_json_contains_qty_date_pairs_for_ageing(self):
		stock = FIFOQueue()
		stock.add_stock(2, 50, "2025-01-05")
		stock.add_stock(3, 60, "2025-02-05")
		self.assertEqual(stock.dated_json("2025-12-31"), '[[2.0,"2025-01-05"],[3.0,"2025-02-05"]]')

	def test_receipt_rate_is_deterministic_and_varies_by_period(self):
		item = generate_synthetic_catalog(1, 7)[0]
		rates = {period: receipt_rate(item, period) for period in range(12)}
		self.assertEqual(receipt_rate(item, 3), rates[3])
		self.assertGreater(len(set(rates.values())), 1)
		self.assertTrue(all(rate > 0 for rate in rates.values()))


class MonotonicClockTests(unittest.TestCase):
	def test_ticks_are_strictly_increasing_even_with_collisions_and_regressions(self):
		clock = MonotonicClock()
		base = datetime(2026, 1, 1, 8, 0, 0)
		ticks = [clock.tick(base) for _ in range(5)]
		self.assertTrue(all(ticks[idx] < ticks[idx + 1] for idx in range(4)))
		earlier = base - timedelta(days=10)
		regressed = clock.tick(earlier)
		self.assertGreater(regressed, ticks[-1])
		later = base + timedelta(days=10)
		self.assertEqual(clock.tick(later), later)


class InventoryGroupingTests(unittest.TestCase):
	def test_vouchers_never_mix_periods_or_routes(self):
		config = SCALE_CONFIGS["smoke"]
		start, end = date(2026, 1, 1), date(2026, 1, 30)
		stock_items = generate_synthetic_catalog(config.item_count, config.seed)
		line_distribution = load_line_distribution(None)
		bundles = build_bundles(stock_items, config.product_bundles, config.seed)
		demand = aggregate_sales_demand(
			SalesPlanGenerator(config, stock_items, bundles, line_distribution, start, end).iter_invoices(),
			bundles, start, end, config.inventory_periods,
		)
		events = build_inventory_events(demand, config, start, end, float(line_distribution["cold_item_annual_qty_threshold"]))
		self.assertTrue(events)
		for event in events:
			periods = {operation.period for operation in event.lines}
			self.assertEqual(len(periods), 1, f"voucher mixes periods: {periods}")
			if event.kind == "transfer":
				routes = {(int(operation.source_hub_idx), operation.warehouse_idx) for operation in event.lines}
				self.assertEqual(len(routes), 1, f"transfer voucher mixes routes: {routes}")
				for operation in event.lines:
					self.assertIsNotNone(operation.source_hub_idx)
			else:
				dests = {operation.warehouse_idx for operation in event.lines}
				self.assertEqual(len(dests), 1, f"receipt voucher mixes destinations: {dests}")
		# receipts of a period post before transfers of that period, which post before any sale hour
		by_kind_period = {}
		for event in events:
			by_kind_period.setdefault((event.period, event.kind), []).append(event.posting_dt)
		for period in {event.period for event in events}:
			receipts = by_kind_period.get((period, "receipt"), [])
			transfers = by_kind_period.get((period, "transfer"), [])
			if receipts and transfers:
				self.assertLess(max(receipts).time().hour, 8)
				self.assertLess(max(transfers).time().hour, 8)
				self.assertLessEqual(max(receipts), max(transfers) + timedelta(hours=23))


class StockClosingTests(unittest.TestCase):
	def test_year_window_uses_cutoff_before_30_days_and_final_cutoff(self):
		cutoffs = calculate_stock_closing_cutoffs(date(2025, 11, 26), date(2026, 11, 25), date(2026, 11, 26))
		self.assertEqual(cutoffs, [date(2026, 10, 26), date(2026, 11, 25)])

	def test_window_over_30_days_uses_two_cutoffs_but_30_days_uses_only_final(self):
		self.assertEqual(
			calculate_stock_closing_cutoffs(date(2026, 10, 26), date(2026, 11, 25), date(2026, 11, 26)),
			[date(2026, 10, 26), date(2026, 11, 25)],
		)
		self.assertEqual(
			calculate_stock_closing_cutoffs(date(2026, 10, 27), date(2026, 11, 25), date(2026, 11, 26)),
			[date(2026, 11, 25)],
		)

	def test_completed_exact_cutoff_is_reused_even_when_a_valid_later_closing_exists(self):
		rows = [
			{"name": "CBAL-00001", "to_date": "2026-10-26", "status": "Completed"},
			{"name": "CBAL-00002", "to_date": "2026-11-25", "status": "Completed"},
		]
		action, row = decide_stock_closing_action(rows, date(2026, 10, 26))
		self.assertEqual(action, "reuse")
		self.assertEqual(row["name"], "CBAL-00001")

	def test_missing_cutoff_creates_when_no_conflicts_exist(self):
		action, row = decide_stock_closing_action([], date(2026, 10, 26))
		self.assertEqual(action, "create")
		self.assertIsNone(row)

	def test_queued_or_in_progress_exact_cutoff_is_waited_on(self):
		for status in ("Queued", "In Progress"):
			with self.subTest(status=status):
				rows = [{"name": "CBAL-00001", "to_date": date(2026, 10, 26), "status": status}]
				action, row = decide_stock_closing_action(rows, date(2026, 10, 26))
				self.assertEqual(action, "wait")
				self.assertEqual(row["name"], "CBAL-00001")

	def test_failed_or_outdated_exact_cutoff_is_surfaced(self):
		for status in ("Failed", "Outdated"):
			with self.subTest(status=status):
				rows = [{"name": "CBAL-00001", "to_date": "2026-10-26", "status": status, "error_message": "stale ledger"}]
				with self.assertRaisesRegex(StockClosingStateError, status):
					decide_stock_closing_action(rows, date(2026, 10, 26))

	def test_missing_exact_cutoff_conflicts_with_later_closing(self):
		rows = [{"name": "CBAL-00002", "to_date": "2026-11-25", "status": "Completed"}]
		with self.assertRaisesRegex(StockClosingConflictError, "later closing entries"):
			decide_stock_closing_action(rows, date(2026, 10, 26))

	def test_scoped_exact_cutoff_is_not_reused_for_unscoped_closing(self):
		rows = [{"name": "CBAL-00002", "to_date": "2026-10-26", "status": "Completed", "warehouse": "Shop 1 - KMD"}]
		with self.assertRaisesRegex(StockClosingConflictError, "scoped entries"):
			decide_stock_closing_action(rows, date(2026, 10, 26))


class ManifestSchemaTests(unittest.TestCase):
	def valid_manifest(self):
		warehouses = {
			"Dist Hub 1": "Dist Hub 1 - KMD",
			"Dist Hub 2": "Dist Hub 2 - KMD",
			"Shop 1": "Shop 1 - KMD",
			"Shop 2": "Shop 2 - KMD",
			"Shop 3": "Shop 3 - KMD",
			"Shop 4": "Shop 4 - KMD",
		}
		in_stock = {
			name: [{"item_code": "KM-00001", "barcode": "8850000000017", "uom": "Nos", "qty": 50.0}]
			for name in warehouses.values()
		}
		return {
			"schema_version": 2,
			"seed_id": "9b1e0d3c-0000-4000-8000-abcdefabcdef",
			"seeded_at": "2026-01-01T00:00:00Z",
			"site_name": "bench-loadtest.localhost",
			"scale": "smoke",
			"company": "Khit Me Distribution",
			"currency": "USD",
			"price_list": "Standard Selling",
			"payment_mode": "Cash",
			"report_window": {"from_date": "2025-01-01", "to_date": "2025-12-31"},
			"warehouses": warehouses,
			"warehouse_names": list(warehouses.values()),
			"items": [
				{"item_code": "KM-00001", "item_name": "Sample", "barcode": "8850000000017", "uom": "Nos", "rate": 1.5, "rank": 1}
			],
			"in_stock_items": in_stock,
			"customers": ["KM-CUST-0001"],
			"suppliers": ["KM-SUP-001"],
			"counts": {"tabSales Invoice": 1},
			"final_vouchers": {"sales": [], "returns": [], "transfers": []},
		}

	def test_valid_manifest_has_no_errors(self):
		self.assertEqual(validate_manifest_schema(self.valid_manifest()), [])

	def test_invalid_manifest_reports_missing_key(self):
		manifest = self.valid_manifest()
		del manifest["warehouses"]
		errors = validate_manifest_schema(manifest)
		self.assertTrue(any("warehouses" in error for error in errors))

	def test_manifest_requires_seed_id_site_name_payment_mode_and_report_window(self):
		for key in ("seed_id", "site_name", "payment_mode", "report_window", "in_stock_items"):
			manifest = self.valid_manifest()
			del manifest[key]
			errors = validate_manifest_schema(manifest)
			self.assertTrue(any(key in error for error in errors), key)

	def test_manifest_rejects_wrong_payment_mode(self):
		manifest = self.valid_manifest()
		manifest["payment_mode"] = "Bank Draft"
		errors = validate_manifest_schema(manifest)
		self.assertTrue(any("payment_mode" in error for error in errors))

	def test_manifest_rejects_warehouse_without_stocked_skus(self):
		manifest = self.valid_manifest()
		first = manifest["warehouse_names"][0]
		manifest["in_stock_items"][first] = [{"item_code": "KM-00001", "barcode": "8850000000017", "uom": "Nos", "qty": 3.0}]
		errors = validate_manifest_schema(manifest)
		self.assertTrue(any("qty >= 20" in error for error in errors))


class SafetyGuardTests(unittest.TestCase):
	def test_protected_live_site_is_refused(self):
		with self.assertRaises(SystemExit):
			assert_safe_site(PROTECTED_SITE)

	def test_protected_site_refusal_is_unconditional_even_with_override(self):
		with self.assertRaises(SystemExit):
			assert_safe_site(PROTECTED_SITE, allow_non_bench_site=True)

	def test_all_protected_names_are_refused_with_and_without_override(self):
		for site in PROTECTED_SITES:
			with self.assertRaises(SystemExit):
				assert_safe_site(site)
			with self.assertRaises(SystemExit):
				assert_safe_site(site, allow_non_bench_site=True)

	def test_bench_site_is_allowed(self):
		assert_safe_site(BENCH_SITE)

	def test_other_site_is_refused_without_override(self):
		with self.assertRaises(SystemExit):
			assert_safe_site("example.localhost")

	def test_other_disposable_site_is_allowed_with_override(self):
		assert_safe_site("example.localhost", allow_non_bench_site=True)


class CatalogTests(unittest.TestCase):
	def test_synthetic_catalog_and_bundles_are_consistent(self):
		items = generate_synthetic_catalog(30, 1)
		bundles = build_bundles(items, 5, 1)
		self.assertEqual(len(items), 30)
		self.assertEqual(len({item.item_code for item in items}), 30)
		self.assertEqual(len({item.barcode for item in items}), 30)
		self.assertEqual(len(bundles), 5)
		for bundle in bundles:
			self.assertGreaterEqual(len(bundle.components), 2)
			self.assertTrue(all(0 <= item_idx < len(items) for item_idx, _qty in bundle.components))
			self.assertGreater(bundle.selling_rate, 0)


class StreamingStockClosingWiringTests(unittest.TestCase):
	def test_sequential_closing_creation_delegates_to_streaming_generator(self):
		cutoffs = [date(2026, 1, 31)]
		expected = {"2026-01-31": "CBAL-00001"}
		with (
			mock.patch.object(seed_dataset, "frappe", object()),
			mock.patch.object(seed_dataset, "create_streaming_sequential_stock_closings", return_value=expected) as streaming,
		):
			result = seed_dataset.create_sequential_stock_closings("Khitmee Co", cutoffs)
		self.assertEqual(result, expected)
		streaming.assert_called_once_with("Khitmee Co", cutoffs)


class OfflineReplayTests(unittest.TestCase):
	"""End-to-end in-memory replay proving 0 negative movements and 0 timestamp inversions."""

	@classmethod
	def setUpClass(cls):
		cls.tiny_config = ScaleConfig("tiny", 120, 400, 900, 20, 8, 3, 3, 0.05, 2, 99, 12)
		SCALE_CONFIGS["tiny"] = cls.tiny_config

	@classmethod
	def tearDownClass(cls):
		SCALE_CONFIGS.pop("tiny", None)

	def test_tiny_replay_has_no_negatives_no_inversions_and_pure_vouchers(self):
		result = fifo_replay.replay("tiny")
		self.assertEqual(result["negative_movements"], 0, result.get("negative_sample"))
		self.assertEqual(result["timestamp_inversions"], 0)
		self.assertEqual(result["mixed_period_or_route_vouchers"], 0)
		self.assertEqual(result["buffer_failures"], [])
		self.assertGreater(result["sle_movements"], 0)
		self.assertEqual(result["payment_ledger_entries"], result["sales_invoices"])


if __name__ == "__main__":
	unittest.main()
