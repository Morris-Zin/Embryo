#!/usr/bin/env python3
"""Offline in-memory FIFO replay of the seeder pipeline (no database, no Frappe).

Re-runs the exact planning and movement logic used by run_seed for a scale preset
and proves:
  * zero negative stock movements (qty_after_transaction / stock_value never < 0)
  * strictly monotonic (posting_datetime, creation) across every movement
  * healthy closing buffer stock per warehouse for k6 load tests
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_dataset import (
	HOT_BUFFER_TARGET_QTY,
	MIN_IN_STOCK_QTY,
	SCALE_CONFIGS,
	STOCK_UOM,
	WAREHOUSE_LOGICAL_NAMES,
	MonotonicClock,
	SalesPlanGenerator,
	aggregate_sales_demand,
	build_buffer_receipt_events,
	build_bundles,
	build_inventory_events,
	load_catalog,
	load_line_distribution,
	process_receipt_event,
	process_sale_invoice,
	process_transfer_event,
)

COMPANY = "Khit Me Distribution"


class ReplayInserter:
	def __init__(self) -> None:
		self.counts: dict[str, int] = defaultdict(int)
		self.sle_rows: list[dict] = []

	def add(self, table, columns, row) -> None:
		self.counts[table] += 1
		if table == "tabStock Ledger Entry":
			self.sle_rows.append(dict(zip(columns, row)))

	def flush_key(self, key) -> None:
		pass

	def flush_all(self) -> None:
		pass


class ReplaySeries:
	def __init__(self) -> None:
		self.counters: dict[str, int] = defaultdict(int)

	def _next(self, prefix: str) -> str:
		self.counters[prefix] += 1
		return f"{prefix}{self.counters[prefix]:06d}"

	def sale(self, posting_dt, is_return) -> str:
		return self._next("ACC-SINV-RET-" if is_return else "ACC-SINV-")

	def stock_entry(self, posting_dt) -> str:
		return self._next("MAT-STE-")

	def sle(self, posting_dt) -> str:
		return self._next("MAT-SLE-")

	def gl(self, posting_dt) -> str:
		return self._next("ACC-GLE-")

	def ple(self, posting_dt) -> str:
		return self._next("ACC-PLE-")

	def closing(self) -> str:
		return self._next("CBAL-")


class ReplayFiscalMap:
	def lookup(self, posting_date) -> str:
		return str(posting_date.year)


def replay(scale: str) -> dict:
	config = SCALE_CONFIGS[scale]
	end = date(2026, 1, 31)
	start = end - timedelta(days=config.default_days - 1)
	stock_items = load_catalog(None, config.item_count, config.seed)
	line_distribution = load_line_distribution(None)
	bundles = build_bundles(stock_items, config.product_bundles, config.seed, None)
	demand = aggregate_sales_demand(
		SalesPlanGenerator(config, stock_items, bundles, line_distribution, start, end).iter_invoices(),
		bundles, start, end, config.inventory_periods,
	)
	events = build_inventory_events(demand, config, start, end, float(line_distribution["cold_item_annual_qty_threshold"]))
	warehouses = [f"{logical} - KMD" for logical in WAREHOUSE_LOGICAL_NAMES]
	accounts = {
		"receivable": "Debtors - KMD",
		"income": "Sales - KMD",
		"cogs": "Cost of Goods Sold - KMD",
		"stock_adjustment": "Stock Adjustment - KMD",
		"default_inventory": "Stock In Hand - KMD",
		"cost_center": "Main - KMD",
		"warehouse_accounts": {logical: f"Stock - {logical} - KMD" for logical in WAREHOUSE_LOGICAL_NAMES},
		"warehouses": dict(zip(WAREHOUSE_LOGICAL_NAMES, warehouses)),
	}
	customers = [f"KM-CUST-{idx + 1:04d}" for idx in range(config.customers)]
	party_defaults = {"customer_group": "Khit Me Retail", "territory": "Cambodia", "supplier_group": "Khit Me Suppliers"}
	inserter = ReplayInserter()
	series = ReplaySeries()
	fiscal_map = ReplayFiscalMap()
	clock = MonotonicClock()
	states: dict = {}
	stats: dict[str, int] = defaultdict(int)
	plans = SalesPlanGenerator(config, stock_items, bundles, line_distribution, start, end).iter_invoices()
	next_sale = next(plans, None)
	for event in events:
		while next_sale is not None and next_sale.posting_dt < event.posting_dt:
			process_sale_invoice(next_sale, inserter, series, fiscal_map, states, stock_items, bundles, customers, warehouses, accounts, COMPANY, "USD", "Standard Selling", party_defaults, stats, clock)
			next_sale = next(plans, None)
		if event.kind == "receipt":
			process_receipt_event(event, inserter, series, fiscal_map, states, stock_items, warehouses, accounts, COMPANY, "USD", stats, clock)
		else:
			process_transfer_event(event, inserter, series, fiscal_map, states, stock_items, warehouses, accounts, COMPANY, "USD", stats, clock)
	while next_sale is not None:
		process_sale_invoice(next_sale, inserter, series, fiscal_map, states, stock_items, bundles, customers, warehouses, accounts, COMPANY, "USD", "Standard Selling", party_defaults, stats, clock)
		next_sale = next(plans, None)
	buffer_events = build_buffer_receipt_events(states, stock_items, end, config.inventory_periods)
	for event in buffer_events:
		process_receipt_event(event, inserter, series, fiscal_map, states, stock_items, warehouses, accounts, COMPANY, "USD", stats, clock)

	negatives = [
		row for row in inserter.sle_rows
		if row["qty_after_transaction"] < -0.000001 or row["stock_value"] < -0.01
	]
	inversions = 0
	prev_posting = None
	prev_creation = None
	for row in inserter.sle_rows:
		posting = row["posting_datetime"]
		creation = row["creation"]
		if prev_posting is not None and (posting <= prev_posting or creation <= prev_creation):
			inversions += 1
		prev_posting = posting
		prev_creation = creation

	# no voucher may mix periods or routes
	mixed_period_vouchers = 0
	for event in events:
		periods = {operation.period for operation in event.lines}
		if len(periods) > 1:
			mixed_period_vouchers += 1
		if event.kind == "transfer":
			routes = {(int(operation.source_hub_idx), operation.warehouse_idx) for operation in event.lines}
			if len(routes) > 1:
				mixed_period_vouchers += 1
		elif event.kind == "receipt":
			dests = {operation.warehouse_idx for operation in event.lines}
			if len(dests) > 1:
				mixed_period_vouchers += 1

	buffer_report = {}
	buffer_failures = []
	for warehouse_idx, warehouse_name in enumerate(warehouses):
		qtys = []
		for item_idx in range(len(stock_items)):
			state = states.get((item_idx, warehouse_idx))
			qty = state.total_stock_and_value()[0] if state else 0.0
			qtys.append(qty)
		in_stock_20 = sum(1 for qty in qtys if qty >= MIN_IN_STOCK_QTY)
		hot_at_50 = sum(1 for qty in qtys[: min(250, len(qtys))] if qty >= HOT_BUFFER_TARGET_QTY)
		buffer_report[warehouse_name] = {"skus_qty>=20": in_stock_20, "top250_qty>=50": hot_at_50}
		if in_stock_20 < 100 or hot_at_50 < min(250, len(qtys)):
			buffer_failures.append(warehouse_name)

	return {
		"scale": scale,
		"sle_movements": len(inserter.sle_rows),
		"sales_invoices": stats["sales_invoices"],
		"receipt_vouchers": stats["receipt_vouchers"],
		"transfer_vouchers": stats["transfer_vouchers"],
		"payment_ledger_entries": stats["payment_ledger_entries"],
		"inventory_events": len(events),
		"buffer_events": len(buffer_events),
		"negative_movements": len(negatives),
		"timestamp_inversions": inversions,
		"mixed_period_or_route_vouchers": mixed_period_vouchers,
		"buffer_report": buffer_report,
		"buffer_failures": buffer_failures,
		"negative_sample": negatives[0] if negatives else None,
	}


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--scales", nargs="+", default=["smoke", "month"], choices=sorted(SCALE_CONFIGS))
	args = parser.parse_args(argv)
	failures = 0
	for scale in args.scales:
		print(f"=== FIFO replay: {scale} ===", flush=True)
		result = replay(scale)
		for key in (
			"sle_movements", "sales_invoices", "receipt_vouchers", "transfer_vouchers",
			"payment_ledger_entries", "inventory_events", "buffer_events",
		):
			print(f"  {key}: {result[key]}")
		for warehouse, report in result["buffer_report"].items():
			print(f"  buffer {warehouse}: skus_qty>=20={report['skus_qty>=20']} top250_qty>=50={report['top250_qty>=50']}")
		ok = True
		for key in ("negative_movements", "timestamp_inversions", "mixed_period_or_route_vouchers"):
			value = result[key]
			status = "PASS" if value == 0 else "FAIL"
			print(f"  {status} {key} = {value}")
			if value != 0:
				ok = False
		if result["negative_sample"]:
			print(f"  first negative: {result['negative_sample']}")
		if result["buffer_failures"]:
			print(f"  FAIL buffer stock inadequate in: {result['buffer_failures']}")
			ok = False
		else:
			print("  PASS buffer stock adequate in all 6 warehouses")
		if not ok:
			failures += 1
	if failures:
		print("FIFO REPLAY FAIL")
		return 1
	print("FIFO REPLAY PASS: 0 negative stock movements, 0 timestamp inversions, healthy buffers")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
