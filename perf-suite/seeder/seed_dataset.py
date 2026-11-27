#!/usr/bin/env python3
"""High-volume ERPNext benchmark dataset seeder for disposable bench-loadtest sites."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import sys
import time as time_module
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

try:
	import frappe
except ImportError:  # unit tests exercise the pure-Python parts without Frappe installed
	frappe = None

from stock_closing import create_sequential_stock_closings as create_streaming_sequential_stock_closings


PROTECTED_SITE = "khitmee.localhost"
PROTECTED_SITES = frozenset(
	{
		"khitmee.localhost",
		"khitmee.com",
		"www.khitmee.com",
		"erp.khitmee.com",
		"prod.khitmee.localhost",
		"production.khitmee.localhost",
		"dev.khitmee.localhost",
		"stage.khitmee.localhost",
		"staging.khitmee.localhost",
	}
)
BENCH_SITE = "bench-loadtest.localhost"
DEFAULT_COMPANY = "Khit Me Distribution"
DEFAULT_COMPANY_ABBR = "KMD"
DEFAULT_CURRENCY = "USD"
DEFAULT_COUNTRY = "Cambodia"
DEFAULT_PRICE_LIST = "Standard Selling"
DEFAULT_PAYMENT_MODE = "Cash"
STOCK_UOM = "Nos"
WAREHOUSE_LOGICAL_NAMES = ["Dist Hub 1", "Dist Hub 2", "Shop 1", "Shop 2", "Shop 3", "Shop 4"]
HUB_INDEXES = (0, 1)
SHOP_INDEXES = (2, 3, 4, 5)
HOT_BUFFER_TARGET_QTY = 500.0
HOT_BUFFER_ITEM_COUNT = 250
MIN_IN_STOCK_QTY = 20.0
STOCK_CLOSING_ACTIVE_STATUSES = frozenset({"Queued", "In Progress"})
STOCK_CLOSING_FAILURE_STATUSES = frozenset({"Failed", "Outdated", "Cancelled"})
STOCK_CLOSING_SCOPE_FIELDS = ("warehouse", "item_code", "item_group", "warehouse_type")
STOCK_CLOSING_TIMEOUT_SECONDS = 1800
STOCK_CLOSING_POLL_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class ScaleConfig:
	name: str
	item_count: int
	sales_invoices: int
	sales_lines: int
	material_transfers: int
	customers: int
	suppliers: int
	product_bundles: int
	return_fraction: float
	inventory_periods: int
	seed: int
	default_days: int


SCALE_CONFIGS: dict[str, ScaleConfig] = {
	"smoke": ScaleConfig("smoke", 500, 5_000, 11_000, 140, 60, 10, 12, 0.02, 2, 20240601, 30),
	"month": ScaleConfig("month", 14_728, 31_563, 80_000, 1_000, 200, 25, 50, 0.02, 4, 20240602, 31),
	"year": ScaleConfig("year", 14_728, 360_000, 950_000, 11_000, 500, 50, 100, 0.02, 12, 20240603, 365),
}


@dataclass(frozen=True)
class CatalogItem:
	item_code: str
	item_name: str
	item_group: str
	brand: str
	barcode: str
	cost_rate: float
	selling_rate: float
	popularity_weight: float = 0.0


@dataclass(frozen=True)
class BundleDefinition:
	item_code: str
	item_name: str
	barcode: str
	selling_rate: float
	cost_rate: float
	components: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class InvoiceLinePlan:
	kind: str
	index: int
	qty: float


@dataclass(frozen=True)
class InvoicePlan:
	index: int
	posting_dt: datetime
	warehouse_idx: int
	customer_idx: int
	is_return: bool
	lines: tuple[InvoiceLinePlan, ...]


@dataclass(frozen=True)
class InventoryOperation:
	period: int
	item_idx: int
	warehouse_idx: int
	qty: float
	source_hub_idx: int | None = None


@dataclass(frozen=True)
class InventoryEvent:
	kind: str
	period: int
	posting_dt: datetime
	lines: tuple[InventoryOperation, ...]


DEFAULT_LINE_DISTRIBUTION: dict[str, Any] = {
	"zipf_alpha": 1.15,
	"lines_per_invoice_weights": {"0": 0.15, "1": 0.34, "2": 0.26, "3": 0.16, "4": 0.09},
	"qty_weights": {"1": 0.55, "2": 0.25, "3": 0.12, "4": 0.05, "5": 0.03},
	"warehouse_weights": {
		"Dist Hub 1": 0.08,
		"Dist Hub 2": 0.07,
		"Shop 1": 0.22,
		"Shop 2": 0.21,
		"Shop 3": 0.21,
		"Shop 4": 0.21,
	},
	"bundle_probability": 0.025,
	"cold_item_annual_qty_threshold": 24,
}


SLE_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx",
	"item_code", "warehouse", "posting_date", "posting_time", "posting_datetime",
	"voucher_type", "voucher_no", "voucher_detail_no", "actual_qty", "qty_after_transaction",
	"incoming_rate", "outgoing_rate", "valuation_rate", "stock_value", "stock_value_difference",
	"stock_queue", "company", "stock_uom", "fiscal_year", "has_batch_no", "has_serial_no",
	"is_cancelled", "to_rename",
]

GL_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx",
	"posting_date", "transaction_date", "fiscal_year", "due_date", "account", "account_currency",
	"against", "party_type", "party", "voucher_type", "voucher_no", "voucher_detail_no",
	"transaction_currency", "against_voucher_type", "against_voucher", "transaction_exchange_rate",
	"reporting_currency_exchange_rate", "debit_in_account_currency", "debit",
	"debit_in_transaction_currency", "debit_in_reporting_currency", "credit_in_account_currency",
	"credit", "credit_in_transaction_currency", "credit_in_reporting_currency", "cost_center",
	"company", "is_opening", "is_advance", "to_rename", "is_cancelled", "remarks",
]

SALES_INVOICE_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "company",
	"naming_series", "customer", "customer_name", "posting_date", "posting_time", "set_posting_time",
	"due_date", "is_pos", "is_return", "return_against", "update_stock", "currency", "conversion_rate",
	"selling_price_list", "price_list_currency", "plc_conversion_rate", "ignore_pricing_rule",
	"set_warehouse", "total_qty", "base_total", "base_net_total", "total", "net_total",
	"base_total_taxes_and_charges", "total_taxes_and_charges", "grand_total", "disable_rounded_total",
	"rounding_adjustment", "rounded_total", "base_grand_total", "base_rounding_adjustment",
	"base_rounded_total", "outstanding_amount", "debit_to", "party_account_currency", "is_opening",
	"against_income_account", "status", "remarks", "customer_group", "territory", "cost_center",
]

SALES_INVOICE_ITEM_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "barcode",
	"item_code", "item_name", "description", "item_group", "brand", "qty", "stock_uom", "uom",
	"conversion_factor", "stock_qty", "price_list_rate", "base_price_list_rate", "rate", "amount",
	"base_rate", "base_amount", "stock_uom_rate", "net_rate", "net_amount", "base_net_rate",
	"base_net_amount", "income_account", "expense_account", "warehouse", "incoming_rate", "actual_qty",
	"cost_center", "parent", "parentfield", "parenttype",
]

PACKED_ITEM_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "parent_item",
	"item_code", "item_name", "description", "warehouse", "conversion_factor", "qty", "rate", "uom",
	"incoming_rate", "parent_detail_docname", "parent", "parentfield", "parenttype",
]

STOCK_ENTRY_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "company",
	"naming_series", "stock_entry_type", "purpose", "set_posting_time", "posting_date", "posting_time",
	"from_warehouse", "to_warehouse", "total_outgoing_value", "total_incoming_value", "value_difference",
	"total_additional_costs", "remarks", "per_transferred", "total_amount", "is_opening",
]

STOCK_ENTRY_DETAIL_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "barcode",
	"s_warehouse", "t_warehouse", "item_code", "item_name", "description", "item_group", "qty",
	"transfer_qty", "uom", "stock_uom", "conversion_factor", "basic_rate", "valuation_rate",
	"basic_amount", "amount", "expense_account", "cost_center", "actual_qty", "transferred_qty",
	"parent", "parentfield", "parenttype",
]

ITEM_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "naming_series",
	"item_code", "item_name", "item_group", "stock_uom", "disabled", "is_stock_item", "is_sales_item",
	"is_purchase_item", "standard_rate", "valuation_method", "valuation_rate", "brand", "description",
]

ITEM_BARCODE_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "barcode",
	"barcode_type", "uom", "parent", "parentfield", "parenttype",
]

UOM_CONVERSION_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "uom",
	"conversion_factor", "parent", "parentfield", "parenttype",
]

ITEM_PRICE_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "item_code", "uom",
	"item_name", "brand", "price_list", "buying", "selling", "currency", "price_list_rate", "valid_from",
]

BIN_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "item_code", "warehouse",
	"actual_qty", "planned_qty", "indented_qty", "ordered_qty", "reserved_qty", "reserved_qty_for_production",
	"reserved_qty_for_sub_contract", "reserved_qty_for_production_plan", "projected_qty", "reserved_stock",
	"stock_uom", "company", "valuation_rate", "stock_value",
]

PRODUCT_BUNDLE_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "new_item_code",
	"description", "disabled",
]

PRODUCT_BUNDLE_ITEM_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "item_code", "qty",
	"description", "rate", "uom", "parent", "parentfield", "parenttype",
]

PLE_COLUMNS = [
	"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx",
	"posting_date", "company", "account_type", "account", "party_type", "party", "due_date",
	"voucher_detail_no", "cost_center", "finance_book", "voucher_type", "voucher_no",
	"against_voucher_type", "against_voucher_no", "amount", "account_currency",
	"amount_in_account_currency", "delinked", "remarks", "project",
]


class MonotonicClock:
	"""Hands out strictly increasing datetimes so (posting_datetime, creation) ordering matches FIFO calculation order exactly."""

	def __init__(self) -> None:
		self.last = datetime(2000, 1, 1)

	def tick(self, desired: datetime) -> datetime:
		if desired <= self.last:
			desired = self.last + timedelta(microseconds=1)
		self.last = desired
		return desired


def assert_safe_site(site: str, allow_non_bench_site: bool = False) -> None:
	if site in PROTECTED_SITES:
		raise SystemExit(
			f"Refusing to touch protected live/dev site {site!r}. This refusal is unconditional: "
			"--allow-non-bench-site can never override it. Use bench-loadtest.localhost."
		)
	if site != BENCH_SITE and not allow_non_bench_site:
		raise SystemExit(
			f"Refusing to seed non-benchmark site {site!r}. Pass --allow-non-bench-site to override."
		)


def zipf_weights(count: int, alpha: float = 1.15) -> list[float]:
	if count <= 0:
		raise ValueError("count must be positive")
	if alpha <= 0:
		raise ValueError("alpha must be positive")
	raw = [1.0 / ((rank + 1) ** alpha) for rank in range(count)]
	total = sum(raw)
	return [value / total for value in raw]


def cumulative_weights(weights: Sequence[float]) -> list[float]:
	out: list[float] = []
	total = 0.0
	for weight in weights:
		total += float(weight)
		out.append(total)
	if not out or total <= 0:
		raise ValueError("weights must contain a positive total")
	out[-1] = 1.0
	return out


def weighted_index(rng: random.Random, cumulative: Sequence[float]) -> int:
	return min(len(cumulative) - 1, bisect.bisect_left(cumulative, rng.random()))


def normalized_numeric_weights(mapping: dict[str, Any], label: str) -> tuple[list[int], list[float]]:
	if not mapping:
		raise ValueError(f"{label} must not be empty")
	keys = sorted(int(key) for key in mapping)
	weights = [float(mapping[str(key)] if str(key) in mapping else mapping[key]) for key in keys]
	if any(weight < 0 for weight in weights) or sum(weights) <= 0:
		raise ValueError(f"{label} weights must be non-negative with a positive total")
	return keys, weights


def allocate_line_counts(
	total_invoices: int,
	total_lines: int,
	rng: random.Random,
	max_lines: int = 5,
	extra_weights: dict[str, Any] | None = None,
) -> list[int]:
	if total_invoices <= 0:
		raise ValueError("total_invoices must be positive")
	if total_lines < total_invoices or total_lines > total_invoices * max_lines:
		raise ValueError("total_lines is impossible for total_invoices and max_lines")
	extra_weights = extra_weights or DEFAULT_LINE_DISTRIBUTION["lines_per_invoice_weights"]
	keys, weights = normalized_numeric_weights(extra_weights, "lines_per_invoice_weights")
	if max(keys) > max_lines - 1:
		raise ValueError("line count weights exceed max_lines")
	cumulative = cumulative_weights(weights)
	counts = [1 + keys[weighted_index(rng, cumulative)] for _ in range(total_invoices)]
	diff = total_lines - sum(counts)
	while diff > 0:
		idx = rng.randrange(total_invoices)
		if counts[idx] < max_lines:
			counts[idx] += 1
			diff -= 1
	while diff < 0:
		idx = rng.randrange(total_invoices)
		if counts[idx] > 1:
			counts[idx] -= 1
			diff += 1
	return counts


def allocate_counts_by_day(start: date, end: date, total: int) -> list[int]:
	if end < start:
		raise ValueError("end date must not precede start date")
	days = (end - start).days + 1
	month_factor = {1: 0.97, 2: 0.95, 3: 1.00, 4: 1.01, 5: 1.03, 6: 1.10, 7: 1.04, 8: 1.00, 9: 0.98, 10: 1.01, 11: 0.99, 12: 1.02}
	weights = [month_factor[(start + timedelta(days=offset)).month] for offset in range(days)]
	weight_total = sum(weights)
	exact = [total * weight / weight_total for weight in weights]
	base = [math.floor(value) for value in exact]
	remainder = total - sum(base)
	order = sorted(range(days), key=lambda idx: (exact[idx] - base[idx], -idx), reverse=True)
	for idx in order[:remainder]:
		base[idx] += 1
	return base


def period_index_for(day: date, start: date, total_days: int, periods: int) -> int:
	offset = (day - start).days
	return min(periods - 1, max(0, (offset * periods) // max(total_days, 1)))


def period_start_date(start: date, total_days: int, periods: int, period: int) -> date:
	return start + timedelta(days=(period * total_days) // periods)


def round_near_zero(value: float, precision: int = 7) -> float:
	return 0.0 if abs(value) < 1.0 / (10**precision) else float(value)


def r6(value: float) -> float:
	return round(float(value) + 0.0, 6)


def money(value: float) -> float:
	return round(float(value) + 0.0, 2)


class FIFOQueue:
	"""FIFO valuation layers. Each layer is [qty, rate, receipt_date_iso]; the date enables accurate stock ageing in Stock Closing Balance rows."""

	def __init__(self) -> None:
		self.queue: list[list[Any]] = []

	def add_stock(self, qty: float, rate: float, receipt_date: str | None = None) -> None:
		qty = float(qty)
		rate = float(rate)
		if qty <= 0:
			raise ValueError("incoming qty must be positive")
		if not self.queue:
			self.queue.append([0.0, 0.0, receipt_date])
		if self.queue[-1][1] == rate and self.queue[-1][2] == receipt_date:
			self.queue[-1][0] += qty
		elif self.queue[-1][0] > 0:
			self.queue.append([qty, rate, receipt_date])
		else:
			new_qty = self.queue[-1][0] + qty
			if new_qty > 0:
				self.queue[-1] = [new_qty, rate, receipt_date]
			else:
				self.queue[-1][0] = new_qty

	def remove_stock(self, qty: float) -> tuple[list[list[float]], float]:
		qty = float(qty)
		if qty <= 0:
			raise ValueError("outgoing qty must be positive")
		remaining = qty
		consumed: list[list[float]] = []
		while remaining:
			if not self.queue:
				self.queue.append([0.0, 0.0, None])
			fifo_bin = self.queue[0]
			if remaining >= fifo_bin[0]:
				remaining = round_near_zero(remaining - fifo_bin[0])
				bin_qty, bin_rate, _bin_date = self.queue.pop(0)
				consumed.append([bin_qty, bin_rate])
				if not self.queue and remaining:
					self.queue.append([-remaining, bin_rate, None])
					consumed.append([remaining, bin_rate])
					break
			else:
				fifo_bin[0] = round_near_zero(fifo_bin[0] - remaining)
				consumed.append([remaining, fifo_bin[1]])
				remaining = 0.0
		return consumed, sum(part_qty * part_rate for part_qty, part_rate in consumed)

	def total_stock_and_value(self) -> tuple[float, float]:
		qty = sum(row[0] for row in self.queue)
		value = sum(row[0] * row[1] for row in self.queue)
		return round_near_zero(qty), round_near_zero(value)

	def compact_json(self) -> str:
		rows = [[r6(row[0]), r6(row[1])] for row in self.queue if round_near_zero(row[0]) > 0]
		return json.dumps(rows, separators=(",", ":"))

	def dated_json(self, fallback_date: str) -> str:
		rows = [
			[r6(row[0]), row[2] or fallback_date]
			for row in self.queue
			if round_near_zero(row[0]) > 0
		]
		return json.dumps(rows, separators=(",", ":"))


def ean13_for_index(index: int) -> str:
	base = f"885{index + 1:09d}"[:12]
	check = (10 - sum((3 if pos % 2 else 1) * int(digit) for pos, digit in enumerate(base)) % 10) % 10
	return base + str(check)


def stable_hash(*parts: Any, length: int = 16) -> str:
	text = "|".join(str(part) for part in parts)
	return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def receipt_rate(item: CatalogItem, period: int) -> float:
	"""Deterministically vary purchase cost by period so FIFO queues contain real layers."""
	variant = int(stable_hash("receipt-rate", item.item_code, period), 16) % 17
	multiplier = 0.94 + variant / 100.0
	return max(0.01, round(item.cost_rate * multiplier, 6))


def generate_synthetic_catalog(count: int, seed: int = 20240601) -> list[CatalogItem]:
	rng = random.Random(seed + 101)
	groups = ["Beverages", "Snacks", "Household", "Personal Care", "Staples", "Cleaning", "Baby Care", "Frozen"]
	brands = ["Khit Me", "Mekong Fresh", "Angkor Best", "Lotus House", "Sahakum", "Phsar Select"]
	items: list[CatalogItem] = []
	for idx in range(count):
		group = groups[idx % len(groups)]
		brand = brands[rng.randrange(len(brands))]
		cost = round(0.35 + (rng.random() ** 2) * 42.0 + (idx % 19) * 0.025, 2)
		margin = 1.18 + rng.random() * 0.35
		selling = round(max(cost * margin, cost + 0.05), 2)
		items.append(
			CatalogItem(
				item_code=f"KM-{idx + 1:05d}",
				item_name=f"{brand} {group[:-1] if group.endswith('s') else group} {idx + 1:05d}",
				item_group=group,
				brand=brand,
				barcode=ean13_for_index(idx),
				cost_rate=cost,
				selling_rate=selling,
			)
		)
	return items


def load_catalog(path: str | None, item_count: int, seed: int) -> list[CatalogItem]:
	if not path:
		return generate_synthetic_catalog(item_count, seed)
	with open(path, encoding="utf-8") as handle:
		payload = json.load(handle)
	rows = payload.get("items", payload) if isinstance(payload, dict) else payload
	if not isinstance(rows, list) or not rows:
		raise ValueError("catalog JSON must be a non-empty list or an object with an items list")
	items: list[CatalogItem] = []
	for idx, row in enumerate(rows):
		if not isinstance(row, dict) or not row.get("item_code"):
			raise ValueError(f"catalog row {idx + 1} misses item_code")
		cost = float(row.get("cost_rate", row.get("standard_rate", 1.0)))
		selling = float(row.get("selling_rate", row.get("price", cost * 1.25)))
		items.append(
			CatalogItem(
				item_code=str(row["item_code"]),
				item_name=str(row.get("item_name") or row["item_code"]),
				item_group=str(row.get("item_group") or "General Merchandise"),
				brand=str(row.get("brand") or "Khit Me"),
				barcode=str(row.get("barcode") or ean13_for_index(idx)),
				cost_rate=round(cost, 6),
				selling_rate=round(selling, 6),
				popularity_weight=float(row.get("popularity_weight", 0.0) or 0.0),
			)
		)
	if len({item.item_code for item in items}) != len(items):
		raise ValueError("catalog item_code values must be unique")
	if len({item.barcode for item in items}) != len(items):
		raise ValueError("catalog barcode values must be unique")
	return items


def build_bundles(
	stock_items: Sequence[CatalogItem],
	count: int,
	seed: int,
	catalog_payload_path: str | None = None,
) -> list[BundleDefinition]:
	if count <= 0:
		return []
	if catalog_payload_path:
		with open(catalog_payload_path, encoding="utf-8") as handle:
			payload = json.load(handle)
		if isinstance(payload, dict) and payload.get("product_bundles"):
			item_index = {item.item_code: idx for idx, item in enumerate(stock_items)}
			bundles: list[BundleDefinition] = []
			for bundle_idx, row in enumerate(payload["product_bundles"]):
				components = []
				for component in row.get("items", []):
					components.append((item_index[component["item_code"]], float(component.get("qty", 1))))
				if not components:
					raise ValueError(f"product bundle {row.get('item_code')} has no components")
				cost = sum(stock_items[item_idx].cost_rate * qty for item_idx, qty in components)
				selling = float(row.get("selling_rate") or sum(stock_items[item_idx].selling_rate * qty for item_idx, qty in components) * 0.92)
				bundles.append(BundleDefinition(str(row["item_code"]), str(row.get("item_name") or row["item_code"]), str(row.get("barcode") or ean13_for_index(900000 + bundle_idx)), round(selling, 2), round(cost, 6), tuple(components)))
			return bundles
	rng = random.Random(seed + 303)
	bundles = []
	hot_pool = list(range(min(len(stock_items), max(100, count * 4))))
	for idx in range(count):
		component_count = rng.choice((2, 2, 3, 4))
		component_indexes = rng.sample(hot_pool, component_count)
		components = tuple((item_idx, float(rng.choice((1, 1, 1, 2)))) for item_idx in component_indexes)
		cost = sum(stock_items[item_idx].cost_rate * qty for item_idx, qty in components)
		selling = round(sum(stock_items[item_idx].selling_rate * qty for item_idx, qty in components) * 0.92, 2)
		bundles.append(
			BundleDefinition(
				item_code=f"KM-BUNDLE-{idx + 1:04d}",
				item_name=f"Khit Me Value Pack {idx + 1:04d}",
				barcode=ean13_for_index(900000 + idx),
				selling_rate=selling,
				cost_rate=cost,
				components=components,
			)
		)
	return bundles


def load_line_distribution(path: str | None) -> dict[str, Any]:
	merged = dict(DEFAULT_LINE_DISTRIBUTION)
	merged["lines_per_invoice_weights"] = dict(DEFAULT_LINE_DISTRIBUTION["lines_per_invoice_weights"])
	merged["qty_weights"] = dict(DEFAULT_LINE_DISTRIBUTION["qty_weights"])
	merged["warehouse_weights"] = dict(DEFAULT_LINE_DISTRIBUTION["warehouse_weights"])
	if path:
		with open(path, encoding="utf-8") as handle:
			overrides = json.load(handle)
		if not isinstance(overrides, dict):
			raise ValueError("line distribution JSON must be an object")
		for key, value in overrides.items():
			if isinstance(value, dict) and isinstance(merged.get(key), dict):
				merged[key].update(value)
			else:
				merged[key] = value
	zipf_alpha = float(merged["zipf_alpha"])
	if zipf_alpha <= 0:
		raise ValueError("zipf_alpha must be positive")
	if not 0 <= float(merged["bundle_probability"]) < 1:
		raise ValueError("bundle_probability must be in [0, 1)")
	return merged


class SalesPlanGenerator:
	def __init__(
		self,
		config: ScaleConfig,
		stock_items: Sequence[CatalogItem],
		bundles: Sequence[BundleDefinition],
		line_distribution: dict[str, Any],
		start: date,
		end: date,
	) -> None:
		self.config = config
		self.stock_items = stock_items
		self.bundles = bundles
		self.line_distribution = line_distribution
		self.start = start
		self.end = end
		self.rng = random.Random(config.seed)
		self.line_counts = allocate_line_counts(
			config.sales_invoices,
			config.sales_lines,
			self.rng,
			5,
			line_distribution["lines_per_invoice_weights"],
		)
		self.day_counts = allocate_counts_by_day(start, end, config.sales_invoices)
		item_weights = [item.popularity_weight for item in stock_items]
		if sum(item_weights) <= 0:
			item_weights = zipf_weights(len(stock_items), float(line_distribution["zipf_alpha"]))
		else:
			total = sum(item_weights)
			item_weights = [weight / total for weight in item_weights]
		self.item_cumulative = cumulative_weights(item_weights)
		warehouse_weights = [float(line_distribution["warehouse_weights"].get(name, 0.0)) for name in WAREHOUSE_LOGICAL_NAMES]
		self.warehouse_cumulative = cumulative_weights(warehouse_weights)
		qty_keys, qty_weights = normalized_numeric_weights(line_distribution["qty_weights"], "qty_weights")
		self.qty_keys = qty_keys
		self.qty_cumulative = cumulative_weights(qty_weights)
		self.bundle_probability = float(line_distribution["bundle_probability"])

	def choose_stock_item(self) -> int:
		return weighted_index(self.rng, self.item_cumulative)

	def iter_invoices(self) -> Iterator[InvoicePlan]:
		invoice_index = 0
		for day_offset, invoice_count in enumerate(self.day_counts):
			day = self.start + timedelta(days=day_offset)
			seconds = sorted(self.rng.randrange(8 * 3600, 21 * 3600) for _ in range(invoice_count))
			for second in seconds:
				warehouse_idx = weighted_index(self.rng, self.warehouse_cumulative)
				customer_idx = self.rng.randrange(self.config.customers)
				is_return = self.rng.random() < self.config.return_fraction
				line_count = self.line_counts[invoice_index]
				lines: list[InvoiceLinePlan] = []
				seen: set[tuple[str, int]] = set()
				while len(lines) < line_count:
					if self.bundles and self.rng.random() < self.bundle_probability:
						kind = "bundle"
						item_idx = self.rng.randrange(len(self.bundles))
					else:
						kind = "item"
						item_idx = self.choose_stock_item()
					key = (kind, item_idx)
					if key in seen:
						continue
					seen.add(key)
					qty = float(self.qty_keys[weighted_index(self.rng, self.qty_cumulative)])
					lines.append(InvoiceLinePlan(kind, item_idx, qty))
				posting_dt = datetime.combine(day, time()) + timedelta(seconds=second)
				yield InvoicePlan(invoice_index, posting_dt, warehouse_idx, customer_idx, is_return, tuple(lines))
				invoice_index += 1


def aggregate_sales_demand(
	plans: Iterable[InvoicePlan],
	bundles: Sequence[BundleDefinition],
	start: date,
	end: date,
	periods: int,
) -> dict[tuple[int, int, int], float]:
	total_days = (end - start).days + 1
	demand: dict[tuple[int, int, int], float] = defaultdict(float)
	for plan in plans:
		if plan.is_return:
			continue
		period = period_index_for(plan.posting_dt.date(), start, total_days, periods)
		for line in plan.lines:
			if line.kind == "item":
				demand[(line.index, plan.warehouse_idx, period)] += line.qty
			else:
				for component_idx, component_qty in bundles[line.index].components:
					demand[(component_idx, plan.warehouse_idx, period)] += line.qty * component_qty
	return dict(demand)


def balanced_groups(operations: Sequence[InventoryOperation], target_vouchers: int) -> list[tuple[InventoryOperation, ...]]:
	if not operations:
		return []
	target_vouchers = max(1, min(target_vouchers, len(operations)))
	base, remainder = divmod(len(operations), target_vouchers)
	groups = []
	position = 0
	for group_idx in range(target_vouchers):
		size = base + (1 if group_idx < remainder else 0)
		groups.append(tuple(operations[position : position + size]))
		position += size
	return groups


def routed_voucher_groups(
	operations: Sequence[InventoryOperation],
	target_vouchers: int,
	keyfn,
) -> list[tuple[Any, tuple[InventoryOperation, ...]]]:
	"""Split operations into vouchers that NEVER mix periods or routes.

	Operations are bucketed by keyfn (e.g. (period, warehouse) for receipts,
	(period, source, destination) for transfers) and each bucket is chunked
	independently, so every resulting voucher is single-period, single-route.
	"""
	buckets: dict[Any, list[InventoryOperation]] = defaultdict(list)
	for operation in operations:
		buckets[keyfn(operation)].append(operation)
	if not buckets:
		return []
	total = sum(len(bucket) for bucket in buckets.values())
	vouchers: list[tuple[Any, tuple[InventoryOperation, ...]]] = []
	for key in sorted(buckets):
		bucket = sorted(buckets[key], key=lambda row: row.item_idx)
		share = max(1, round(target_vouchers * len(bucket) / total))
		for group in balanced_groups(bucket, share):
			vouchers.append((key, group))
	return vouchers


def build_inventory_events(
	demand: dict[tuple[int, int, int], float],
	config: ScaleConfig,
	start: date,
	end: date,
	cold_item_annual_qty_threshold: float | None = None,
) -> list[InventoryEvent]:
	total_days = (end - start).days + 1
	periods = config.inventory_periods
	cold_threshold = float(cold_item_annual_qty_threshold or DEFAULT_LINE_DISTRIBUTION["cold_item_annual_qty_threshold"])
	transfer_ops: list[InventoryOperation] = []
	shop_demand: dict[tuple[int, int], dict[int, float]] = defaultdict(lambda: defaultdict(float))
	hub_direct: dict[tuple[int, int, int], float] = defaultdict(float)
	for (item_idx, warehouse_idx, period), qty in demand.items():
		if warehouse_idx in SHOP_INDEXES:
			shop_demand[(item_idx, warehouse_idx)][period] += qty
		elif warehouse_idx in HUB_INDEXES:
			hub_direct[(item_idx, warehouse_idx, period)] += qty
	for (item_idx, shop_idx), period_quantities in shop_demand.items():
		source_hub = HUB_INDEXES[item_idx % len(HUB_INDEXES)]
		total_qty = sum(period_quantities.values())
		if total_qty <= cold_threshold:
			qty = math.ceil(total_qty * 1.25 + 5)
			transfer_ops.append(InventoryOperation(0, item_idx, shop_idx, qty, source_hub))
		else:
			for period, demand_qty in sorted(period_quantities.items()):
				qty = math.ceil(demand_qty * 1.2 + 3)
				transfer_ops.append(InventoryOperation(period, item_idx, shop_idx, qty, source_hub))
	hub_receipt_requirement: dict[tuple[int, int, int], float] = defaultdict(float)
	for operation in transfer_ops:
		hub_receipt_requirement[(operation.item_idx, int(operation.source_hub_idx), operation.period)] += operation.qty
	for (item_idx, hub_idx, period), qty in hub_direct.items():
		hub_receipt_requirement[(item_idx, hub_idx, period)] += math.ceil(qty * 1.2 + 6)
	receipt_ops = [
		InventoryOperation(period, item_idx, hub_idx, math.ceil(qty * 1.05 + 2), None)
		for (item_idx, hub_idx, period), qty in sorted(hub_receipt_requirement.items())
		if qty > 0
	]
	receipt_target = max(20, min(5_500, max(1, config.material_transfers // 2)))
	events: list[InventoryEvent] = []
	receipt_vouchers = routed_voucher_groups(
		receipt_ops, receipt_target, lambda op: (op.period, op.warehouse_idx)
	)
	transfer_vouchers = routed_voucher_groups(
		transfer_ops, config.material_transfers, lambda op: (op.period, int(op.source_hub_idx), op.warehouse_idx)
	)
	sequence = 0
	for (period, _warehouse_idx), group in receipt_vouchers:
		day = period_start_date(start, total_days, periods, period)
		posting_dt = datetime.combine(day, time(1, 0)) + timedelta(seconds=sequence % 3000)
		sequence += 1
		events.append(InventoryEvent("receipt", period, posting_dt, group))
	sequence = 0
	for (period, _source_idx, _dest_idx), group in transfer_vouchers:
		day = period_start_date(start, total_days, periods, period)
		posting_dt = datetime.combine(day, time(2, 0)) + timedelta(seconds=sequence % 3000)
		sequence += 1
		events.append(InventoryEvent("transfer", period, posting_dt, group))
	events.sort(key=lambda event: event.posting_dt)
	return events


def build_buffer_receipt_events(
	states: dict[tuple[int, int], "FIFOQueue"],
	stock_items: Sequence[CatalogItem],
	end: date,
	periods: int,
	target_qty: float = HOT_BUFFER_TARGET_QTY,
	item_count: int = HOT_BUFFER_ITEM_COUNT,
) -> list[InventoryEvent]:
	"""Top-up Material Receipts posted after the last sale so every warehouse keeps a healthy saleable buffer for live k6 load tests."""
	hot_indexes = sorted(
		range(len(stock_items)),
		key=lambda idx: stock_items[idx].popularity_weight or (1 / ((idx + 1) ** 1.15)),
		reverse=True,
	)[: min(item_count, len(stock_items))]
	ops_by_warehouse: dict[int, list[InventoryOperation]] = defaultdict(list)
	last_period = max(0, periods - 1)
	for warehouse_idx in range(len(WAREHOUSE_LOGICAL_NAMES)):
		for item_idx in hot_indexes:
			state = states.get((item_idx, warehouse_idx))
			qty = state.total_stock_and_value()[0] if state else 0.0
			deficit = target_qty - qty
			if deficit > 0:
				ops_by_warehouse[warehouse_idx].append(
					InventoryOperation(last_period, item_idx, warehouse_idx, math.ceil(deficit), None)
				)
	events: list[InventoryEvent] = []
	for sequence, warehouse_idx in enumerate(sorted(ops_by_warehouse)):
		ops = sorted(ops_by_warehouse[warehouse_idx], key=lambda row: row.item_idx)
		posting_dt = datetime.combine(end, time(22, 0)) + timedelta(seconds=sequence * 30)
		events.append(InventoryEvent("receipt", last_period, posting_dt, tuple(ops)))
	return events


def validate_manifest_schema(manifest: dict[str, Any]) -> list[str]:
	errors: list[str] = []
	for key in (
		"schema_version", "seed_id", "seeded_at", "site_name", "scale", "company", "currency",
		"price_list", "payment_mode", "report_window", "warehouses", "warehouse_names",
		"items", "in_stock_items", "customers", "suppliers", "counts", "final_vouchers",
	):
		if key not in manifest:
			errors.append(f"missing key {key}")
	if errors:
		return errors
	if not isinstance(manifest.get("seed_id"), str) or not manifest["seed_id"]:
		errors.append("seed_id must be a non-empty string")
	if not isinstance(manifest.get("site_name"), str) or not manifest["site_name"]:
		errors.append("site_name must be a non-empty string")
	if manifest.get("payment_mode") != DEFAULT_PAYMENT_MODE:
		errors.append(f"payment_mode must be {DEFAULT_PAYMENT_MODE!r}")
	report_window = manifest.get("report_window")
	if not isinstance(report_window, dict) or not report_window.get("from_date") or not report_window.get("to_date"):
		errors.append("report_window must contain from_date and to_date")
	warehouses = manifest.get("warehouses")
	if not isinstance(warehouses, dict) or sorted(warehouses) != sorted(WAREHOUSE_LOGICAL_NAMES):
		errors.append("warehouses must map the six logical warehouse names to actual warehouse names")
	warehouse_names = manifest.get("warehouse_names")
	if not isinstance(warehouse_names, list) or len(warehouse_names) != len(WAREHOUSE_LOGICAL_NAMES):
		errors.append("warehouse_names must list the six actual warehouse names")
	elif isinstance(warehouses, dict) and sorted(warehouse_names) != sorted(str(v) for v in warehouses.values()):
		errors.append("warehouse_names must match the warehouses mapping values")
	items = manifest.get("items")
	if not isinstance(items, list) or not items:
		errors.append("items must be a non-empty list of {item_code, item_name, barcode, uom, rate, rank}")
	else:
		sample = items[0]
		if not isinstance(sample, dict) or any(key not in sample for key in ("item_code", "item_name", "barcode", "uom", "rate", "rank")):
			errors.append("items entries must contain item_code, item_name, barcode, uom, rate, rank")
	in_stock = manifest.get("in_stock_items")
	if not isinstance(in_stock, dict):
		errors.append("in_stock_items must map warehouse names to stock lists")
	elif isinstance(warehouse_names, list):
		for warehouse_name in warehouse_names:
			rows = in_stock.get(warehouse_name)
			if not isinstance(rows, list):
				errors.append(f"in_stock_items missing warehouse {warehouse_name!r}")
				continue
			if not any(isinstance(row, dict) and float(row.get("qty", 0)) >= MIN_IN_STOCK_QTY for row in rows):
				errors.append(f"in_stock_items[{warehouse_name!r}] has no SKU with qty >= {MIN_IN_STOCK_QTY:g}")
			for row in rows[:5]:
				if not isinstance(row, dict) or any(key not in row for key in ("item_code", "barcode", "uom", "qty")):
					errors.append(f"in_stock_items[{warehouse_name!r}] entries must contain item_code, barcode, uom, qty")
					break
	counts = manifest.get("counts")
	if not isinstance(counts, dict):
		errors.append("counts must be an object")
	final_vouchers = manifest.get("final_vouchers")
	if not isinstance(final_vouchers, dict):
		errors.append("final_vouchers must be an object")
	else:
		for key in ("sales", "returns", "transfers"):
			if not isinstance(final_vouchers.get(key), list):
				errors.append(f"final_vouchers.{key} must be a list")
	return errors


def require_frappe() -> None:
	if frappe is None:
		raise RuntimeError("frappe is not importable; run inside the bench Python environment")


def resolve_sites_path(site: str) -> str:
	candidates = [Path.cwd(), Path.cwd() / "sites", Path("/home/frappe/frappe-bench/sites"), Path("/workspace/frappe-bench/sites")]
	for candidate in candidates:
		if (candidate / site).exists():
			return str(candidate)
	return str(Path("/home/frappe/frappe-bench/sites"))


def connect_site(site: str) -> None:
	require_frappe()
	if getattr(frappe.local, "initialised", False) and getattr(frappe.local, "site", None) == site and getattr(frappe.local, "db", None):
		return
	frappe.init(site=site, sites_path=resolve_sites_path(site), force=True)
	frappe.connect()


def sql_scalar(query: str, values: Sequence[Any] = ()) -> Any:
	rows = frappe.db.sql(query, values)
	return rows[0][0] if rows else None


class SeriesAllocator:
	def __init__(self) -> None:
		self.counters: dict[str, int] = {}

	def next(self, prefix: str, digits: int = 5) -> str:
		if prefix not in self.counters:
			current = sql_scalar("SELECT `current` FROM `tabSeries` WHERE `name`=%s", (prefix,))
			self.counters[prefix] = int(current or 0)
		self.counters[prefix] += 1
		return f"{prefix}{self.counters[prefix]:0{digits}d}"

	def sale(self, posting_dt: datetime, is_return: bool) -> str:
		series = "ACC-SINV-RET" if is_return else "ACC-SINV"
		return self.next(f"{series}-{posting_dt.year}-")

	def stock_entry(self, posting_dt: datetime) -> str:
		return self.next(f"MAT-STE-{posting_dt.year}-")

	def sle(self, posting_dt: datetime) -> str:
		return self.next(f"MAT-SLE-{posting_dt.year}-")

	def gl(self, posting_dt: datetime) -> str:
		return self.next(f"ACC-GLE-{posting_dt.year}-")

	def ple(self, posting_dt: datetime) -> str:
		return self.next(f"ACC-PLE-{posting_dt.year}-")

	def flush(self) -> None:
		for prefix, current in sorted(self.counters.items()):
			exists = sql_scalar("SELECT `current` FROM `tabSeries` WHERE `name`=%s", (prefix,))
			if exists is None:
				frappe.db.sql("INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, %s)", (prefix, current))
			elif current > int(exists):
				frappe.db.sql("UPDATE `tabSeries` SET `current`=%s WHERE `name`=%s", (current, prefix))


class BulkInserter:
	def __init__(self, chunk_size: int = 10_000) -> None:
		self.chunk_size = chunk_size
		self.buffers: dict[tuple[str, tuple[str, ...]], list[tuple[Any, ...]]] = defaultdict(list)
		self.counts: dict[str, int] = defaultdict(int)

	def add(self, table: str, columns: Sequence[str], row: Sequence[Any]) -> None:
		key = (table, tuple(columns))
		if len(row) != len(columns):
			raise ValueError(f"{table} row has {len(row)} values for {len(columns)} columns")
		self.buffers[key].append(tuple(row))
		if len(self.buffers[key]) >= self.chunk_size:
			self.flush_key(key)

	def flush_key(self, key: tuple[str, tuple[str, ...]]) -> None:
		rows = self.buffers.get(key)
		if not rows:
			return
		table, columns = key
		column_sql = ",".join(f"`{column}`" for column in columns)
		one_row = "(" + ",".join(["%s"] * len(columns)) + ")"
		sql = f"INSERT INTO `{table}` ({column_sql}) VALUES " + ",".join([one_row] * len(rows))
		values = [value for row in rows for value in row]
		frappe.db.sql(sql, values)
		self.counts[table] += len(rows)
		rows.clear()
		frappe.db.commit()

	def flush_all(self) -> None:
		for key in list(self.buffers):
			self.flush_key(key)


class FiscalYearMap:
	def __init__(self) -> None:
		rows = frappe.db.sql("SELECT year, year_start_date, year_end_date FROM `tabFiscal Year` ORDER BY year_start_date")
		self.rows = [(str(row[0]), row[1], row[2]) for row in rows]

	def lookup(self, posting_date: date) -> str:
		for year, start, end in self.rows:
			if start <= posting_date <= end:
				return year
		return str(posting_date.year)


def insert_doc_if_missing(doctype: str, filters: dict[str, Any], doc_data: dict[str, Any]) -> str:
	existing = frappe.db.exists(doctype, filters)
	if existing:
		return str(existing)
	doc = frappe.get_doc(doc_data)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def ensure_fiscal_year_for(posting_date: date) -> str:
	existing = sql_scalar(
		"SELECT year FROM `tabFiscal Year` WHERE year_start_date <= %s AND year_end_date >= %s LIMIT 1",
		(posting_date, posting_date),
	)
	if existing:
		return str(existing)
	year = str(posting_date.year)
	doc = frappe.get_doc({"doctype": "Fiscal Year", "year": year, "year_start_date": date(posting_date.year, 1, 1), "year_end_date": date(posting_date.year, 12, 31)})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def ensure_base_masters(warehouse_groups: set[str], brands: set[str], currency: str, price_list: str) -> dict[str, str]:
	if not frappe.db.exists("Party Type", "Customer") or not frappe.db.exists("Warehouse Type", "Transit"):
		from erpnext.setup.setup_wizard.operations.install_fixtures import install as install_erpnext_fixtures

		frappe.set_user("Administrator")
		install_erpnext_fixtures(DEFAULT_COUNTRY)
	if not frappe.db.exists("UOM", STOCK_UOM):
		insert_doc_if_missing("UOM", {"name": STOCK_UOM}, {"doctype": "UOM", "uom_name": STOCK_UOM, "enabled": 1})
	if not frappe.db.exists("Item Group", "All Item Groups"):
		insert_doc_if_missing("Item Group", {"name": "All Item Groups"}, {"doctype": "Item Group", "item_group_name": "All Item Groups", "is_group": 1})
	for group in sorted(warehouse_groups):
		insert_doc_if_missing("Item Group", {"item_group_name": group}, {"doctype": "Item Group", "item_group_name": group, "parent_item_group": "All Item Groups", "is_group": 0})
	for brand in sorted(brands):
		insert_doc_if_missing("Brand", {"name": brand}, {"doctype": "Brand", "brand": brand})
	customer_group = insert_doc_if_missing("Customer Group", {"customer_group_name": "Khit Me Retail"}, {"doctype": "Customer Group", "customer_group_name": "Khit Me Retail", "parent_customer_group": "All Customer Groups" if frappe.db.exists("Customer Group", "All Customer Groups") else None, "is_group": 0})
	territory = insert_doc_if_missing("Territory", {"territory_name": "Cambodia"}, {"doctype": "Territory", "territory_name": "Cambodia", "parent_territory": "All Territories" if frappe.db.exists("Territory", "All Territories") else None, "is_group": 0})
	supplier_group = insert_doc_if_missing("Supplier Group", {"supplier_group_name": "Khit Me Suppliers"}, {"doctype": "Supplier Group", "supplier_group_name": "Khit Me Suppliers", "parent_supplier_group": "All Supplier Groups" if frappe.db.exists("Supplier Group", "All Supplier Groups") else None, "is_group": 0})
	for name, buying, selling in ((price_list, 0, 1), ("Standard Buying", 1, 0)):
		insert_doc_if_missing("Price List", {"price_list_name": name}, {"doctype": "Price List", "price_list_name": name, "enabled": 1, "buying": buying, "selling": selling, "currency": currency})
	for entry_type in ("Material Receipt", "Material Transfer", "Material Issue"):
		insert_doc_if_missing("Stock Entry Type", {"name": entry_type}, {"doctype": "Stock Entry Type", "name": entry_type, "purpose": entry_type})
	if not frappe.db.exists("Warehouse Type", "Transit"):
		doc = frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"})
		doc.flags.ignore_permissions = True
		doc.insert()
	frappe.db.set_value("Currency", currency, "enabled", 1)
	frappe.db.commit()
	return {"customer_group": customer_group, "territory": territory, "supplier_group": supplier_group}


def ensure_company(company_name: str, abbr: str, currency: str, country: str, start: date, end: date) -> None:
	for year in range(start.year, end.year + 1):
		ensure_fiscal_year_for(date(year, 6, 1))
	if frappe.db.exists("Company", company_name):
		company = frappe.get_doc("Company", company_name)
		changed = False
		if not company.enable_perpetual_inventory:
			company.enable_perpetual_inventory = 1
			changed = True
		if company.valuation_method != "FIFO":
			company.valuation_method = "FIFO"
			changed = True
		if changed:
			company.flags.ignore_permissions = True
			company.save()
		return
	doc = frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": company_name,
			"abbr": abbr,
			"default_currency": currency,
			"country": country,
			"enable_perpetual_inventory": 1,
			"valuation_method": "FIFO",
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard",
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.set_single_value("Global Defaults", "default_company", company_name)
	frappe.db.set_single_value("Global Defaults", "default_currency", currency)
	frappe.db.set_single_value("Global Defaults", "country", country)
	frappe.db.set_single_value("Stock Settings", "valuation_method", "FIFO")
	frappe.db.set_single_value("Stock Settings", "stock_uom", STOCK_UOM)
	frappe.db.commit()


def account_by_type(company: str, account_type: str | None = None, root_type: str | None = None, group: int = 0) -> str:
	filters = {"company": company, "is_group": group}
	if account_type:
		filters["account_type"] = account_type
	if root_type:
		filters["root_type"] = root_type
	name = frappe.db.get_value("Account", filters, "name")
	if not name:
		raise RuntimeError(f"Required account not found for {company}: {filters}")
	return str(name)


def ensure_accounts_and_warehouses(company: str, abbr: str, currency: str) -> dict[str, Any]:
	company_doc = frappe.get_doc("Company", company)
	receivable = company_doc.default_receivable_account or account_by_type(company, "Receivable")
	income = company_doc.default_income_account or account_by_type(company, root_type="Income")
	cogs = company_doc.default_expense_account or account_by_type(company, "Cost of Goods Sold")
	stock_adjustment = company_doc.stock_adjustment_account or account_by_type(company, "Stock Adjustment")
	default_inventory = company_doc.default_inventory_account or account_by_type(company, "Stock")
	cost_center = company_doc.cost_center or frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	if not cost_center:
		raise RuntimeError(f"No leaf Cost Center found for {company}")
	parent_stock_account = frappe.db.get_value("Account", {"company": company, "account_type": "Stock", "is_group": 1}, "name") or default_inventory
	warehouse_accounts: dict[str, str] = {}
	for logical in WAREHOUSE_LOGICAL_NAMES:
		account_name = f"Stock - {logical}"
		existing = frappe.db.get_value("Account", {"company": company, "account_name": account_name}, "name")
		if existing:
			warehouse_accounts[logical] = str(existing)
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"company": company,
				"parent_account": parent_stock_account,
				"is_group": 0,
				"root_type": "Asset",
				"report_type": "Balance Sheet",
				"account_type": "Stock",
				"account_currency": currency,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		warehouse_accounts[logical] = doc.name
	root_warehouse = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": "All Warehouses", "is_group": 1}, "name")
	if not root_warehouse:
		root_doc = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "All Warehouses", "company": company, "is_group": 1})
		root_doc.flags.ignore_permissions = True
		root_doc.insert()
		root_warehouse = root_doc.name
	network_parent = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": "Khit Me Network"}, "name")
	if not network_parent:
		network_doc = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Khit Me Network", "company": company, "is_group": 1, "parent_warehouse": root_warehouse})
		network_doc.flags.ignore_permissions = True
		network_doc.insert()
		network_parent = network_doc.name
	warehouses: dict[str, str] = {}
	for logical in WAREHOUSE_LOGICAL_NAMES:
		existing = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": logical}, "name")
		if existing:
			warehouses[logical] = str(existing)
			frappe.db.set_value("Warehouse", existing, "account", warehouse_accounts[logical])
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": logical,
				"company": company,
				"is_group": 0,
				"parent_warehouse": network_parent,
				"account": warehouse_accounts[logical],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		warehouses[logical] = doc.name
	for field, value in (
		("default_receivable_account", receivable),
		("default_income_account", income),
		("default_expense_account", cogs),
		("stock_adjustment_account", stock_adjustment),
		("default_inventory_account", default_inventory),
		("cost_center", cost_center),
	):
		if frappe.db.get_value("Company", company, field) != value:
			frappe.db.set_value("Company", company, field, value)
	frappe.db.commit()
	return {
		"receivable": receivable,
		"income": income,
		"cogs": cogs,
		"stock_adjustment": stock_adjustment,
		"default_inventory": default_inventory,
		"cost_center": cost_center,
		"warehouse_accounts": warehouse_accounts,
		"warehouses": warehouses,
	}


def ensure_cash_mode_of_payment(company: str) -> str:
	"""Provision `Mode of Payment = Cash` with a company account mapping so POS-style
	cash sales work on a fresh bench-loadtest site without running the Setup Wizard."""
	cash_account = frappe.db.get_value("Company", company, "default_cash_account")
	if not cash_account:
		cash_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name"
		)
	if not cash_account:
		raise RuntimeError(f"No leaf Cash account found for {company}")
	cash_account = str(cash_account)
	existing = frappe.db.exists("Mode of Payment", DEFAULT_PAYMENT_MODE)
	if existing:
		doc = frappe.get_doc("Mode of Payment", DEFAULT_PAYMENT_MODE)
		changed = False
		if doc.type != "Cash":
			doc.type = "Cash"
			changed = True
		if not doc.enabled:
			doc.enabled = 1
			changed = True
		if not any(row.company == company for row in doc.accounts):
			doc.append("accounts", {"company": company, "default_account": cash_account})
			changed = True
		if changed:
			doc.flags.ignore_permissions = True
			doc.save()
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": DEFAULT_PAYMENT_MODE,
				"type": "Cash",
				"enabled": 1,
				"accounts": [{"company": company, "default_account": cash_account}],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
	frappe.db.set_value("Company", company, "default_cash_account", cash_account)
	frappe.db.commit()
	return cash_account


def assert_no_existing_seed(company: str) -> None:
	sales = sql_scalar("SELECT COUNT(*) FROM `tabSales Invoice` WHERE company=%s", (company,)) or 0
	stock_entries = sql_scalar("SELECT COUNT(*) FROM `tabStock Entry` WHERE company=%s", (company,)) or 0
	seed_items = sql_scalar("SELECT COUNT(*) FROM `tabItem` WHERE name LIKE %s", ("KM-%",)) or 0
	seed_parties = sql_scalar("SELECT COUNT(*) FROM `tabCustomer` WHERE name LIKE %s", ("KM-CUST-%",)) or 0
	seed_parties += sql_scalar("SELECT COUNT(*) FROM `tabSupplier` WHERE name LIKE %s", ("KM-SUP-%",)) or 0
	if sales or stock_entries or seed_items or seed_parties:
		raise RuntimeError(
			f"Site already contains seeded or transactional data for {company} "
			f"(sales={sales}, stock_entries={stock_entries}, KM items={seed_items}, KM parties={seed_parties}). Use a fresh disposable site."
		)


def insert_master_rows(
	inserter: BulkInserter,
	stock_items: Sequence[CatalogItem],
	bundles: Sequence[BundleDefinition],
	customer_count: int,
	supplier_count: int,
	price_list: str,
	currency: str,
	party_defaults: dict[str, str],
	created: datetime,
	valid_from: date,
) -> dict[str, list[str]]:
	for idx, item in enumerate(stock_items):
		row_created = created + timedelta(microseconds=idx)
		inserter.add("tabItem", ITEM_COLUMNS, (item.item_code, row_created, row_created, "Administrator", "Administrator", 0, 0, "STO-ITEM-.YYYY.-", item.item_code, item.item_name, item.item_group, STOCK_UOM, 0, 1, 1, 1, item.cost_rate, "FIFO", item.cost_rate, item.brand, item.item_name))
		inserter.add("tabItem Barcode", ITEM_BARCODE_COLUMNS, (stable_hash("barcode", item.item_code), row_created, row_created, "Administrator", "Administrator", 0, 1, item.barcode, "EAN-13", STOCK_UOM, item.item_code, "barcodes", "Item"))
		inserter.add("tabUOM Conversion Detail", UOM_CONVERSION_COLUMNS, (stable_hash("uom", item.item_code), row_created, row_created, "Administrator", "Administrator", 0, 1, STOCK_UOM, 1, item.item_code, "uoms", "Item"))
		inserter.add("tabItem Price", ITEM_PRICE_COLUMNS, (stable_hash("price", item.item_code), row_created, row_created, "Administrator", "Administrator", 0, 0, item.item_code, STOCK_UOM, item.item_name, item.brand, price_list, 0, 1, currency, item.selling_rate, valid_from))
	bundle_offset = len(stock_items)
	for idx, bundle in enumerate(bundles):
		row_created = created + timedelta(microseconds=bundle_offset + idx)
		inserter.add("tabItem", ITEM_COLUMNS, (bundle.item_code, row_created, row_created, "Administrator", "Administrator", 0, 0, "STO-ITEM-.YYYY.-", bundle.item_code, bundle.item_name, "Bundles", STOCK_UOM, 0, 0, 1, 0, 0, "FIFO", 0, "Khit Me", bundle.item_name))
		inserter.add("tabItem Barcode", ITEM_BARCODE_COLUMNS, (stable_hash("barcode", bundle.item_code), row_created, row_created, "Administrator", "Administrator", 0, 1, bundle.barcode, "EAN-13", STOCK_UOM, bundle.item_code, "barcodes", "Item"))
		inserter.add("tabUOM Conversion Detail", UOM_CONVERSION_COLUMNS, (stable_hash("uom", bundle.item_code), row_created, row_created, "Administrator", "Administrator", 0, 1, STOCK_UOM, 1, bundle.item_code, "uoms", "Item"))
		inserter.add("tabItem Price", ITEM_PRICE_COLUMNS, (stable_hash("price", bundle.item_code), row_created, row_created, "Administrator", "Administrator", 0, 0, bundle.item_code, STOCK_UOM, bundle.item_name, "Khit Me", price_list, 0, 1, currency, bundle.selling_rate, valid_from))
		inserter.add("tabProduct Bundle", PRODUCT_BUNDLE_COLUMNS, (bundle.item_code, row_created, row_created, "Administrator", "Administrator", 0, 0, bundle.item_code, bundle.item_name, 0))
		for component_row_idx, (component_idx, component_qty) in enumerate(bundle.components, 1):
			component = stock_items[component_idx]
			inserter.add("tabProduct Bundle Item", PRODUCT_BUNDLE_ITEM_COLUMNS, (stable_hash("pbi", bundle.item_code, component_idx), row_created, row_created, "Administrator", "Administrator", 0, component_row_idx, component.item_code, component_qty, component.item_name, component.selling_rate, STOCK_UOM, bundle.item_code, "items", "Product Bundle"))
	customer_names = []
	for idx in range(customer_count):
		name = f"KM-CUST-{idx + 1:04d}"
		customer_names.append(name)
		inserter.add("tabCustomer", ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "customer_name", "customer_type", "customer_group", "territory", "disabled"], (name, created, created, "Administrator", "Administrator", 0, 0, f"Khit Customer {idx + 1:04d}", "Company", party_defaults["customer_group"], party_defaults["territory"], 0))
	supplier_names = []
	for idx in range(supplier_count):
		name = f"KM-SUP-{idx + 1:03d}"
		supplier_names.append(name)
		inserter.add("tabSupplier", ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "supplier_name", "supplier_group", "supplier_type", "country", "disabled"], (name, created, created, "Administrator", "Administrator", 0, 0, f"Khit Supplier {idx + 1:03d}", party_defaults["supplier_group"], "Company", DEFAULT_COUNTRY, 0))
	return {"customers": customer_names, "suppliers": supplier_names}


def warehouse_account(accounts: dict[str, Any], logical_idx: int) -> str:
	return accounts["warehouse_accounts"][WAREHOUSE_LOGICAL_NAMES[logical_idx]]


def add_gl_row(
	inserter: BulkInserter,
	series: SeriesAllocator,
	fiscal_map: FiscalYearMap,
	posting_dt: datetime,
	account: str,
	debit: float,
	credit: float,
	against: str,
	voucher_type: str,
	voucher_no: str,
	company: str,
	currency: str,
	cost_center: str,
	voucher_detail_no: str | None = None,
	party_type: str | None = None,
	party: str | None = None,
	against_voucher: str | None = None,
	is_opening: str = "No",
	remarks: str = "Benchmark seed entry",
) -> None:
	debit = money(debit)
	credit = money(credit)
	posting_date = posting_dt.date()
	against_voucher_type = voucher_type if party_type else None
	against_voucher_value = against_voucher or (voucher_no if party_type else None)
	row = (
		series.gl(posting_dt), posting_dt, posting_dt, "Administrator", "Administrator", 1, 0,
		posting_date, posting_date, fiscal_map.lookup(posting_date), posting_date, account, currency,
		against, party_type, party, voucher_type, voucher_no, voucher_detail_no,
		currency, against_voucher_type, against_voucher_value, 1, 1,
		debit, debit, debit, debit, credit, credit, credit, credit,
		cost_center, company, is_opening, "No", 0, 0, remarks,
	)
	inserter.add("tabGL Entry", GL_COLUMNS, row)


def apply_stock_movement(
	states: dict[tuple[int, int], FIFOQueue],
	inserter: BulkInserter,
	series: SeriesAllocator,
	fiscal_map: FiscalYearMap,
	item: CatalogItem,
	item_idx: int,
	warehouse_idx: int,
	warehouse_name: str,
	qty_delta: float,
	rate: float,
	posting_dt: datetime,
	voucher_type: str,
	voucher_no: str,
	voucher_detail_no: str,
	company: str,
	clock: MonotonicClock,
) -> dict[str, float]:
	posting_dt = clock.tick(posting_dt)
	state = states.setdefault((item_idx, warehouse_idx), FIFOQueue())
	before_qty, before_value = state.total_stock_and_value()
	incoming_rate = 0.0
	outgoing_rate = 0.0
	if qty_delta > 0:
		incoming_rate = float(rate) if rate else (before_value / before_qty if before_qty > 0 else item.cost_rate)
		state.add_stock(qty_delta, incoming_rate, posting_dt.date().isoformat())
	elif qty_delta < 0:
		_consumed, cost = state.remove_stock(abs(qty_delta))
		outgoing_rate = cost / abs(qty_delta)
	else:
		raise ValueError("qty_delta must not be zero")
	after_qty, after_value = state.total_stock_and_value()
	if after_qty < -0.000001 or after_value < -0.01:
		raise RuntimeError(
			"FIFO invariant violation BEFORE any bulk write: "
			f"{item.item_code} @ {warehouse_name} would go negative "
			f"(qty_after={after_qty}, value_after={after_value}) via {voucher_type} {voucher_no} "
			f"delta={qty_delta} at {posting_dt.isoformat()}"
		)
	value_difference = after_value - before_value
	valuation_rate = after_value / after_qty if after_qty else 0.0
	posting_date = posting_dt.date()
	row = (
		series.sle(posting_dt), posting_dt, posting_dt, "Administrator", "Administrator", 1, 0,
		item.item_code, warehouse_name, posting_date, posting_dt.time(), posting_dt,
		voucher_type, voucher_no, voucher_detail_no, r6(qty_delta), r6(after_qty),
		r6(incoming_rate), r6(outgoing_rate), r6(valuation_rate), r6(after_value), r6(value_difference),
		state.compact_json(), company, STOCK_UOM, fiscal_map.lookup(posting_date), 0, 0, 0, 0,
	)
	inserter.add("tabStock Ledger Entry", SLE_COLUMNS, row)
	return {
		"qty_before": before_qty,
		"qty_after": after_qty,
		"stock_value": after_value,
		"stock_value_difference": value_difference,
		"incoming_rate": incoming_rate,
		"outgoing_rate": outgoing_rate,
		"cost": abs(value_difference),
		"posting_dt": posting_dt,
	}


def process_receipt_event(
	event: InventoryEvent,
	inserter: BulkInserter,
	series: SeriesAllocator,
	fiscal_map: FiscalYearMap,
	states: dict[tuple[int, int], FIFOQueue],
	stock_items: Sequence[CatalogItem],
	warehouses: Sequence[str],
	accounts: dict[str, Any],
	company: str,
	currency: str,
	stats: dict[str, int],
	clock: MonotonicClock,
) -> None:
	posting_dt = event.posting_dt
	voucher_no = series.stock_entry(posting_dt)
	total_value = 0.0
	target_wh = warehouses[event.lines[0].warehouse_idx]
	for idx, operation in enumerate(event.lines, 1):
		item = stock_items[operation.item_idx]
		warehouse = warehouses[operation.warehouse_idx]
		detail_name = stable_hash("receipt", voucher_no, idx, length=12)
		rate = receipt_rate(item, event.period)
		result = apply_stock_movement(states, inserter, series, fiscal_map, item, operation.item_idx, operation.warehouse_idx, warehouse, operation.qty, rate, posting_dt, "Stock Entry", voucher_no, detail_name, company, clock)
		line_value = money(operation.qty * rate)
		total_value += line_value
		inserter.add("tabStock Entry Detail", STOCK_ENTRY_DETAIL_COLUMNS, (detail_name, posting_dt, posting_dt, "Administrator", "Administrator", 1, idx, item.barcode, None, warehouse, item.item_code, item.item_name, item.item_name, item.item_group, operation.qty, operation.qty, STOCK_UOM, STOCK_UOM, 1, rate, r6(result["incoming_rate"]), line_value, line_value, accounts["stock_adjustment"], accounts["cost_center"], r6(result["qty_before"]), 0, voucher_no, "items", "Stock Entry"))
		inventory_account = warehouse_account(accounts, operation.warehouse_idx)
		add_gl_row(inserter, series, fiscal_map, posting_dt, inventory_account, line_value, 0, accounts["stock_adjustment"], "Stock Entry", voucher_no, company, currency, accounts["cost_center"], detail_name, is_opening="Yes" if event.period == 0 else "No")
		add_gl_row(inserter, series, fiscal_map, posting_dt, accounts["stock_adjustment"], 0, line_value, inventory_account, "Stock Entry", voucher_no, company, currency, accounts["cost_center"], detail_name, is_opening="Yes" if event.period == 0 else "No")
		stats["receipt_lines"] += 1
	inserter.add("tabStock Entry", STOCK_ENTRY_COLUMNS, (voucher_no, posting_dt, posting_dt, "Administrator", "Administrator", 1, 0, company, "MAT-STE-.YYYY.-", "Material Receipt", "Material Receipt", 1, posting_dt.date(), posting_dt.time(), None, target_wh, 0, money(total_value), money(total_value), 0, "Seeded opening/purchase receipt", 100, money(total_value), "Yes" if event.period == 0 else "No"))
	stats["receipt_vouchers"] += 1


def process_transfer_event(
	event: InventoryEvent,
	inserter: BulkInserter,
	series: SeriesAllocator,
	fiscal_map: FiscalYearMap,
	states: dict[tuple[int, int], FIFOQueue],
	stock_items: Sequence[CatalogItem],
	warehouses: Sequence[str],
	accounts: dict[str, Any],
	company: str,
	currency: str,
	stats: dict[str, int],
	clock: MonotonicClock,
) -> None:
	posting_dt = event.posting_dt
	voucher_no = series.stock_entry(posting_dt)
	total_value = 0.0
	first_source = warehouses[int(event.lines[0].source_hub_idx)]
	first_target = warehouses[event.lines[0].warehouse_idx]
	for idx, operation in enumerate(event.lines, 1):
		item = stock_items[operation.item_idx]
		source_idx = int(operation.source_hub_idx)
		target_idx = operation.warehouse_idx
		source = warehouses[source_idx]
		target = warehouses[target_idx]
		detail_name = stable_hash("transfer", voucher_no, idx, length=12)
		out_result = apply_stock_movement(states, inserter, series, fiscal_map, item, operation.item_idx, source_idx, source, -operation.qty, 0, posting_dt, "Stock Entry", voucher_no, detail_name, company, clock)
		in_rate = out_result["outgoing_rate"]
		in_result = apply_stock_movement(states, inserter, series, fiscal_map, item, operation.item_idx, target_idx, target, operation.qty, in_rate, posting_dt, "Stock Entry", voucher_no, detail_name, company, clock)
		line_value = money(operation.qty * in_rate)
		total_value += line_value
		inserter.add("tabStock Entry Detail", STOCK_ENTRY_DETAIL_COLUMNS, (detail_name, posting_dt, posting_dt, "Administrator", "Administrator", 1, idx, item.barcode, source, target, item.item_code, item.item_name, item.item_name, item.item_group, operation.qty, operation.qty, STOCK_UOM, STOCK_UOM, 1, r6(in_rate), r6(in_rate), line_value, line_value, accounts["stock_adjustment"], accounts["cost_center"], r6(out_result["qty_before"]), 0, voucher_no, "items", "Stock Entry"))
		source_account = warehouse_account(accounts, source_idx)
		target_account = warehouse_account(accounts, target_idx)
		add_gl_row(inserter, series, fiscal_map, posting_dt, target_account, line_value, 0, source_account, "Stock Entry", voucher_no, company, currency, accounts["cost_center"], detail_name)
		add_gl_row(inserter, series, fiscal_map, posting_dt, source_account, 0, line_value, target_account, "Stock Entry", voucher_no, company, currency, accounts["cost_center"], detail_name)
		stats["transfer_lines"] += 1
	inserter.add("tabStock Entry", STOCK_ENTRY_COLUMNS, (voucher_no, posting_dt, posting_dt, "Administrator", "Administrator", 1, 0, company, "MAT-STE-.YYYY.-", "Material Transfer", "Material Transfer", 1, posting_dt.date(), posting_dt.time(), first_source, first_target, money(total_value), money(total_value), 0, 0, "Seeded hub-to-shop transfer", 100, money(total_value), "No"))
	stats["transfer_vouchers"] += 1


def process_sale_invoice(
	plan: InvoicePlan,
	inserter: BulkInserter,
	series: SeriesAllocator,
	fiscal_map: FiscalYearMap,
	states: dict[tuple[int, int], FIFOQueue],
	stock_items: Sequence[CatalogItem],
	bundles: Sequence[BundleDefinition],
	customer_names: Sequence[str],
	warehouses: Sequence[str],
	accounts: dict[str, Any],
	company: str,
	currency: str,
	price_list: str,
	party_defaults: dict[str, str],
	stats: dict[str, int],
	clock: MonotonicClock,
) -> None:
	posting_dt = plan.posting_dt
	posting_date = posting_dt.date()
	is_return = plan.is_return
	voucher_no = series.sale(posting_dt, is_return)
	customer = customer_names[plan.customer_idx]
	customer_name = f"Khit Customer {plan.customer_idx + 1:04d}"
	warehouse = warehouses[plan.warehouse_idx]
	sign = -1.0 if is_return else 1.0
	total_qty = 0.0
	grand_total = 0.0
	income_account = accounts["income"]
	for idx, line in enumerate(plan.lines, 1):
		detail_name = stable_hash("sii", voucher_no, idx, length=12)
		if line.kind == "bundle":
			bundle = bundles[line.index]
			line_item_code = bundle.item_code
			line_item_name = bundle.item_name
			line_group = "Bundles"
			line_brand = "Khit Me"
			line_barcode = bundle.barcode
			rate = bundle.selling_rate
			qty = sign * line.qty
			amount = money(qty * rate)
			total_qty += qty
			grand_total += amount
			inserter.add("tabSales Invoice Item", SALES_INVOICE_ITEM_COLUMNS, (detail_name, posting_dt, posting_dt, "Administrator", "Administrator", 1, idx, line_barcode, line_item_code, line_item_name, line_item_name, line_group, line_brand, r6(qty), STOCK_UOM, STOCK_UOM, 1, r6(qty), rate, rate, rate, amount, rate, amount, rate, rate, amount, rate, amount, income_account, accounts["cogs"], warehouse, 0, 0, accounts["cost_center"], voucher_no, "items", "Sales Invoice"))
			packed_idx = 0
			for component_idx, component_qty in bundle.components:
				packed_idx += 1
				component = stock_items[component_idx]
				movement_qty = sign * -1.0 * line.qty * component_qty
				result = apply_stock_movement(states, inserter, series, fiscal_map, component, component_idx, plan.warehouse_idx, warehouse, movement_qty, 0, posting_dt, "Sales Invoice", voucher_no, detail_name, company, clock)
				component_cost = money(abs(movement_qty) * (result["incoming_rate"] or result["outgoing_rate"] or component.cost_rate))
				if movement_qty < 0:
					add_gl_row(inserter, series, fiscal_map, posting_dt, accounts["cogs"], component_cost, 0, warehouse_account(accounts, plan.warehouse_idx), "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
					add_gl_row(inserter, series, fiscal_map, posting_dt, warehouse_account(accounts, plan.warehouse_idx), 0, component_cost, accounts["cogs"], "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
				else:
					add_gl_row(inserter, series, fiscal_map, posting_dt, warehouse_account(accounts, plan.warehouse_idx), component_cost, 0, accounts["cogs"], "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
					add_gl_row(inserter, series, fiscal_map, posting_dt, accounts["cogs"], 0, component_cost, warehouse_account(accounts, plan.warehouse_idx), "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
				packed_qty = sign * line.qty * component_qty
				inserter.add("tabPacked Item", PACKED_ITEM_COLUMNS, (stable_hash("packed", voucher_no, idx, packed_idx), posting_dt, posting_dt, "Administrator", "Administrator", 1, packed_idx, bundle.item_code, component.item_code, component.item_name, component.item_name, warehouse, 1, r6(packed_qty), component.selling_rate, STOCK_UOM, r6(result["incoming_rate"] or result["outgoing_rate"]), detail_name, voucher_no, "packed_items", "Sales Invoice"))
				stats["packed_items"] += 1
				stats["stock_sale_lines"] += 1
		else:
			item = stock_items[line.index]
			qty = sign * line.qty
			rate = item.selling_rate
			amount = money(qty * rate)
			total_qty += qty
			grand_total += amount
			movement_qty = sign * -1.0 * line.qty
			result = apply_stock_movement(states, inserter, series, fiscal_map, item, line.index, plan.warehouse_idx, warehouse, movement_qty, 0, posting_dt, "Sales Invoice", voucher_no, detail_name, company, clock)
			line_cost_rate = result["incoming_rate"] or result["outgoing_rate"]
			inserter.add("tabSales Invoice Item", SALES_INVOICE_ITEM_COLUMNS, (detail_name, posting_dt, posting_dt, "Administrator", "Administrator", 1, idx, item.barcode, item.item_code, item.item_name, item.item_name, item.item_group, item.brand, r6(qty), STOCK_UOM, STOCK_UOM, 1, r6(qty), rate, rate, rate, amount, rate, amount, rate, rate, amount, rate, amount, income_account, accounts["cogs"], warehouse, r6(line_cost_rate), r6(result["qty_before"]), accounts["cost_center"], voucher_no, "items", "Sales Invoice"))
			line_cost = money(abs(movement_qty) * line_cost_rate)
			if movement_qty < 0:
				add_gl_row(inserter, series, fiscal_map, posting_dt, accounts["cogs"], line_cost, 0, warehouse_account(accounts, plan.warehouse_idx), "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
				add_gl_row(inserter, series, fiscal_map, posting_dt, warehouse_account(accounts, plan.warehouse_idx), 0, line_cost, accounts["cogs"], "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
			else:
				add_gl_row(inserter, series, fiscal_map, posting_dt, warehouse_account(accounts, plan.warehouse_idx), line_cost, 0, accounts["cogs"], "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
				add_gl_row(inserter, series, fiscal_map, posting_dt, accounts["cogs"], 0, line_cost, warehouse_account(accounts, plan.warehouse_idx), "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], detail_name)
			stats["stock_sale_lines"] += 1
	grand_total = money(grand_total)
	abs_total = abs(grand_total)
	if grand_total >= 0:
		add_gl_row(inserter, series, fiscal_map, posting_dt, accounts["receivable"], grand_total, 0, income_account, "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], None, "Customer", customer, voucher_no)
		add_gl_row(inserter, series, fiscal_map, posting_dt, income_account, 0, grand_total, customer, "Sales Invoice", voucher_no, company, currency, accounts["cost_center"])
	else:
		add_gl_row(inserter, series, fiscal_map, posting_dt, accounts["receivable"], 0, abs_total, income_account, "Sales Invoice", voucher_no, company, currency, accounts["cost_center"], None, "Customer", customer, voucher_no)
		add_gl_row(inserter, series, fiscal_map, posting_dt, income_account, abs_total, 0, customer, "Sales Invoice", voucher_no, company, currency, accounts["cost_center"])
	due_date = posting_date + timedelta(days=14)
	ple_amount = grand_total
	inserter.add(
		"tabPayment Ledger Entry",
		PLE_COLUMNS,
		(
			series.ple(posting_dt), posting_dt, posting_dt, "Administrator", "Administrator", 1, 0,
			posting_date, company, "Receivable", accounts["receivable"], "Customer", customer, due_date,
			None, accounts["cost_center"], None, "Sales Invoice", voucher_no,
			"Sales Invoice", voucher_no, money(ple_amount), currency,
			money(ple_amount), 0, "Benchmark seed entry", None,
		),
	)
	stats["payment_ledger_entries"] += 1
	status = "Return" if is_return else "Unpaid"
	inserter.add("tabSales Invoice", SALES_INVOICE_COLUMNS, (voucher_no, posting_dt, posting_dt, "Administrator", "Administrator", 1, 0, company, "ACC-SINV-RET-.YYYY.-" if is_return else "ACC-SINV-.YYYY.-", customer, customer_name, posting_date, posting_dt.time(), 1, posting_date + timedelta(days=14), 0, 1 if is_return else 0, None, 1, currency, 1, price_list, currency, 1, 1, warehouse, r6(total_qty), grand_total, grand_total, grand_total, grand_total, 0, 0, grand_total, 0, 0, grand_total, grand_total, 0, grand_total, grand_total, accounts["receivable"], currency, "No", income_account, status, "Seeded benchmark sale", party_defaults["customer_group"], party_defaults["territory"], accounts["cost_center"]))
	stats["sales_invoices"] += 1
	stats["sales_invoice_items"] += len(plan.lines)
	if is_return:
		stats["return_invoices"] += 1


def insert_bins(
	inserter: BulkInserter,
	states: dict[tuple[int, int], FIFOQueue],
	stock_items: Sequence[CatalogItem],
	warehouses: Sequence[str],
	company: str,
	created: datetime,
) -> None:
	for warehouse_idx, warehouse in enumerate(warehouses):
		for item_idx, item in enumerate(stock_items):
			state = states.get((item_idx, warehouse_idx))
			qty, stock_value = state.total_stock_and_value() if state else (0.0, 0.0)
			valuation_rate = stock_value / qty if qty else 0.0
			name = stable_hash("bin", item.item_code, warehouse)
			inserter.add("tabBin", BIN_COLUMNS, (name, created, created, "Administrator", "Administrator", 0, 0, item.item_code, warehouse, r6(qty), 0, 0, 0, 0, 0, 0, 0, r6(qty), 0, STOCK_UOM, company, r6(valuation_rate), r6(stock_value)))


class StockClosingError(RuntimeError):
	pass


class StockClosingConflictError(StockClosingError):
	pass


class StockClosingStateError(StockClosingError):
	pass


def coerce_stock_closing_date(value: date | datetime | str) -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def calculate_stock_closing_cutoffs(start: date, to_date: date, final_voucher_date: date | None = None) -> list[date]:
	start = coerce_stock_closing_date(start)
	to_date = coerce_stock_closing_date(to_date)
	if to_date < start:
		raise ValueError("stock closing to_date must not precede start")
	final_cutoff = coerce_stock_closing_date(final_voucher_date) - timedelta(days=1) if final_voucher_date else to_date
	if final_cutoff < start or final_cutoff > to_date:
		raise ValueError("final voucher date implies a stock closing cutoff outside the report window")
	cutoffs: list[date] = []
	if (to_date - start).days + 1 > 30:
		cutoffs.append(to_date - timedelta(days=30))
	cutoffs.append(final_cutoff)
	return sorted(set(cutoffs))


def stock_closing_row_is_unscoped(row: dict[str, Any]) -> bool:
	return all(not row.get(field) for field in STOCK_CLOSING_SCOPE_FIELDS if field in row)


def decide_stock_closing_action(rows: Sequence[dict[str, Any]], cutoff: date) -> tuple[str, dict[str, Any] | None]:
	cutoff = coerce_stock_closing_date(cutoff)
	exact_unscoped: list[dict[str, Any]] = []
	exact_scoped: list[dict[str, Any]] = []
	later: list[dict[str, Any]] = []
	for row in rows:
		row_cutoff = coerce_stock_closing_date(row["to_date"])
		if row_cutoff == cutoff:
			if stock_closing_row_is_unscoped(row):
				exact_unscoped.append(row)
			else:
				exact_scoped.append(row)
		elif row_cutoff > cutoff:
			later.append(row)

	if len(exact_unscoped) > 1:
		names = ", ".join(str(row.get("name")) for row in exact_unscoped)
		raise StockClosingConflictError(f"Multiple unscoped Stock Closing Entries already exist for {cutoff}: {names}")

	if exact_unscoped:
		row = exact_unscoped[0]
		status = str(row.get("status") or "Draft")
		if status == "Completed":
			return "reuse", row
		if status in STOCK_CLOSING_ACTIVE_STATUSES:
			return "wait", row
		detail = str(row.get("error_message") or "").strip()
		suffix = f": {detail}" if detail else ""
		raise StockClosingStateError(f"Stock Closing Entry {row.get('name')} for {cutoff} is {status}{suffix}")

	if exact_scoped:
		names = ", ".join(str(row.get("name")) for row in exact_scoped)
		raise StockClosingConflictError(f"Cannot create an unscoped Stock Closing Entry for {cutoff}; scoped entries already exist for that cutoff: {names}")

	if later:
		descriptions = ", ".join(f"{row.get('name')} ({coerce_stock_closing_date(row['to_date'])})" for row in later)
		raise StockClosingConflictError(f"Cannot create Stock Closing Entry for {cutoff}; later closing entries already exist: {descriptions}")

	return "create", None


def stock_closing_status_fields() -> list[str]:
	fields = ["name", "to_date", "status"]
	meta = frappe.get_meta("Stock Closing Entry")
	if meta.has_field("error_message"):
		fields.append("error_message")
	return fields


def get_stock_closing_rows(company: str) -> list[dict[str, Any]]:
	meta = frappe.get_meta("Stock Closing Entry")
	fields = stock_closing_status_fields()
	for field in STOCK_CLOSING_SCOPE_FIELDS:
		if meta.has_field(field):
			fields.append(field)
	rows = frappe.db.get_values(
		"Stock Closing Entry",
		{"company": company, "docstatus": 1},
		fields,
		as_dict=True,
		order_by="to_date asc",
	)
	return [dict(row) for row in rows or []]



def create_sequential_stock_closings(
	company: str,
	cutoffs: Sequence[date],
	timeout_seconds: int = STOCK_CLOSING_TIMEOUT_SECONDS,
	poll_interval_seconds: int = STOCK_CLOSING_POLL_INTERVAL_SECONDS,
) -> dict[str, str]:
	"""Create chained closings synchronously with the streaming low-memory generator.

	The timeout/poll arguments remain for CLI/backward compatibility but are intentionally
	unused: this path never enqueues ERPNext's queue-long Stock Closing job.
	"""
	require_frappe()
	return create_streaming_sequential_stock_closings(company, cutoffs)


def choose_final_stock(states: dict[tuple[int, int], FIFOQueue], stock_items: Sequence[CatalogItem], warehouse_idx: int, count: int) -> list[int]:
	candidates = []
	for (item_idx, wh_idx), state in states.items():
		if wh_idx == warehouse_idx:
			qty, _value = state.total_stock_and_value()
			if qty >= 5:
				candidates.append((item_idx, qty))
	candidates.sort(key=lambda row: (row[0], row[1]))
	return [item_idx for item_idx, _qty in candidates[:count]]


def submit_final_vouchers(
	states: dict[tuple[int, int], FIFOQueue],
	stock_items: Sequence[CatalogItem],
	bundles: Sequence[BundleDefinition],
	customer_names: Sequence[str],
	warehouses: Sequence[str],
	accounts: dict[str, Any],
	company: str,
	currency: str,
	price_list: str,
	posting_date: date,
	stats: dict[str, int],
) -> dict[str, Any]:
	result: dict[str, Any] = {"sales": [], "returns": [], "transfers": [], "errors": []}
	shop_idx = 2
	hub_idx = 0
	item_candidates = choose_final_stock(states, stock_items, shop_idx, 80) or list(range(min(20, len(stock_items))))
	reserved_shop: dict[int, float] = defaultdict(float)
	reserved_hub: dict[int, float] = defaultdict(float)

	def available_qty(item_idx: int, warehouse_idx: int) -> float:
		state = states.get((item_idx, warehouse_idx))
		return state.total_stock_and_value()[0] if state else 0.0

	def pick_item(candidates: Sequence[int], warehouse_idx: int, qty_needed: float, reserved: dict[int, float]) -> int | None:
		for item_idx in candidates:
			if available_qty(item_idx, warehouse_idx) - reserved[item_idx] >= qty_needed:
				reserved[item_idx] += qty_needed
				return item_idx
		return None

	def pick_bundle() -> BundleDefinition | None:
		for candidate in bundles:
			if all(available_qty(component_idx, shop_idx) - reserved_shop[component_idx] >= component_qty for component_idx, component_qty in candidate.components):
				for component_idx, component_qty in candidate.components:
					reserved_shop[component_idx] += component_qty
				return candidate
		return None

	created_sales = []
	for idx in range(30):
		posting_time = time(12, idx)
		bundle = pick_bundle() if idx % 6 == 0 and bundles else None
		is_bundle_sale = bundle is not None
		if bundle:
			line = {"item_code": bundle.item_code, "qty": 1, "rate": bundle.selling_rate, "warehouse": warehouses[shop_idx], "uom": STOCK_UOM, "income_account": accounts["income"], "expense_account": accounts["cogs"], "cost_center": accounts["cost_center"]}
		else:
			qty_needed = float(1 + (idx % 2))
			item_idx = pick_item(item_candidates, shop_idx, qty_needed, reserved_shop)
			if item_idx is None:
				raise RuntimeError("No seeded Shop 1 stock remains for the final genuine sales vouchers")
			item = stock_items[item_idx]
			line = {"item_code": item.item_code, "qty": qty_needed, "rate": item.selling_rate, "warehouse": warehouses[shop_idx], "uom": STOCK_UOM, "income_account": accounts["income"], "expense_account": accounts["cogs"], "cost_center": accounts["cost_center"]}
		doc = frappe.get_doc({"doctype": "Sales Invoice", "naming_series": "ACC-SINV-.YYYY.-", "customer": customer_names[idx % len(customer_names)], "company": company, "posting_date": posting_date, "posting_time": posting_time, "set_posting_time": 1, "due_date": posting_date + timedelta(days=14), "currency": currency, "conversion_rate": 1, "selling_price_list": price_list, "price_list_currency": currency, "plc_conversion_rate": 1, "ignore_pricing_rule": 1, "update_stock": 1, "set_warehouse": warehouses[shop_idx], "debit_to": accounts["receivable"], "cost_center": accounts["cost_center"], "items": [line]})
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()
		created_sales.append(doc)
		result["sales"].append({"name": doc.name, "bundle": bool(is_bundle_sale)})
		stats["final_sales"] += 1
		if idx % 10 == 9:
			frappe.db.commit()
	for idx in range(5):
		source = created_sales[idx + 1]
		source_item = source.items[0]
		qty = -1 if float(source_item.qty) >= 1 else -float(source_item.qty)
		return_doc = frappe.get_doc({"doctype": "Sales Invoice", "naming_series": "ACC-SINV-RET-.YYYY.-", "customer": source.customer, "company": company, "posting_date": posting_date, "posting_time": time(13, idx), "set_posting_time": 1, "due_date": posting_date, "currency": currency, "conversion_rate": 1, "selling_price_list": price_list, "price_list_currency": currency, "plc_conversion_rate": 1, "ignore_pricing_rule": 1, "update_stock": 1, "is_return": 1, "return_against": source.name, "set_warehouse": source_item.warehouse, "debit_to": accounts["receivable"], "cost_center": accounts["cost_center"], "items": [{"item_code": source_item.item_code, "qty": qty, "rate": source_item.rate, "warehouse": source_item.warehouse, "uom": STOCK_UOM, "sales_invoice_item": source_item.name, "income_account": accounts["income"], "expense_account": accounts["cogs"], "cost_center": accounts["cost_center"]}]})
		return_doc.flags.ignore_permissions = True
		return_doc.insert()
		return_doc.submit()
		result["returns"].append({"name": return_doc.name, "return_against": source.name})
		stats["final_returns"] += 1
	transfer_candidates = choose_final_stock(states, stock_items, hub_idx, 80) or list(range(min(80, len(stock_items))))
	for idx in range(15):
		item_idx = pick_item(transfer_candidates, hub_idx, 1, reserved_hub)
		if item_idx is None:
			raise RuntimeError("No seeded Dist Hub 1 stock remains for the final genuine transfer vouchers")
		item = stock_items[item_idx]
		doc = frappe.get_doc({"doctype": "Stock Entry", "naming_series": "MAT-STE-.YYYY.-", "company": company, "stock_entry_type": "Material Transfer", "purpose": "Material Transfer", "posting_date": posting_date, "posting_time": time(14, idx), "set_posting_time": 1, "from_warehouse": warehouses[hub_idx], "to_warehouse": warehouses[shop_idx], "items": [{"item_code": item.item_code, "qty": 1, "s_warehouse": warehouses[hub_idx], "t_warehouse": warehouses[shop_idx], "uom": STOCK_UOM, "expense_account": accounts["stock_adjustment"], "cost_center": accounts["cost_center"]}]})
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()
		result["transfers"].append({"name": doc.name})
		stats["final_transfers"] += 1
		if idx % 5 == 4:
			frappe.db.commit()
	frappe.db.commit()
	return result


def build_item_search_index() -> None:
	frappe.db.set_single_value("Global Defaults", "enable_item_search_index", 1)
	frappe.db.commit()
	from erpnext.stock.doctype.item.item_search import ItemSearch, validate_sqlite_version

	validate_sqlite_version()
	search = ItemSearch()
	search.build_index(batch_size=1000)
	if not search.index_exists():
		raise RuntimeError("item_search.db was not created")


def write_manifest(manifest: dict[str, Any], output_path: str | None) -> str:
	errors = validate_manifest_schema(manifest)
	if errors:
		raise RuntimeError("internal manifest schema error: " + ", ".join(errors))
	if output_path:
		path = Path(output_path)
	else:
		path = Path(__file__).with_name("seed_manifest.json")
	try:
		path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
	except OSError:
		require_frappe()
		path = Path(frappe.get_site_path("private", "files")) / "seed_manifest.json"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
	return str(path)


def parse_date(value: str) -> date:
	return datetime.strptime(value, "%Y-%m-%d").date()


def run_seed(args: argparse.Namespace) -> dict[str, Any]:
	assert_safe_site(args.site, args.allow_non_bench_site)
	connect_site(args.site)
	config = SCALE_CONFIGS[args.scale]
	today = date.today()
	end = parse_date(args.end_date) if args.end_date else today - timedelta(days=1)
	start = parse_date(args.start_date) if args.start_date else end - timedelta(days=config.default_days - 1)
	if end < start:
		raise SystemExit("--end-date must not precede --start-date")
	if end >= today:
		raise SystemExit("--end-date must be before today so final real vouchers post after seeded history")
	final_date = min(end + timedelta(days=1), today)
	closing_cutoffs = calculate_stock_closing_cutoffs(start, end, final_date)
	print(f"Seeding {config.name} dataset on {args.site}: {start}..{end}", flush=True)
	stock_items = load_catalog(args.catalog_json, config.item_count, config.seed)
	if len(stock_items) != config.item_count:
		print(f"Catalog contains {len(stock_items)} items; preset target was {config.item_count}. Using catalog size.", flush=True)
	line_distribution = load_line_distribution(args.line_dist_json)
	bundles = build_bundles(stock_items, config.product_bundles, config.seed, args.catalog_json)
	groups = {item.item_group for item in stock_items} | {"Bundles"}
	brands = {item.brand for item in stock_items} | {"Khit Me"}
	party_defaults = ensure_base_masters(groups, brands, DEFAULT_CURRENCY, DEFAULT_PRICE_LIST)
	ensure_company(args.company, args.company_abbr, DEFAULT_CURRENCY, DEFAULT_COUNTRY, start, end + timedelta(days=1))
	accounts = ensure_accounts_and_warehouses(args.company, args.company_abbr, DEFAULT_CURRENCY)
	cash_account = ensure_cash_mode_of_payment(args.company)
	assert_no_existing_seed(args.company)
	created = datetime.combine(start, time(0, 0))
	inserter = BulkInserter(chunk_size=args.chunk_size)
	master_rows = insert_master_rows(inserter, stock_items, bundles, config.customers, config.suppliers, DEFAULT_PRICE_LIST, DEFAULT_CURRENCY, party_defaults, created, start)
	inserter.flush_all()
	print("Master rows inserted; calculating demand with Zipf distribution", flush=True)
	first_plans = SalesPlanGenerator(config, stock_items, bundles, line_distribution, start, end)
	demand = aggregate_sales_demand(first_plans.iter_invoices(), bundles, start, end, config.inventory_periods)
	inventory_events = build_inventory_events(demand, config, start, end, float(line_distribution["cold_item_annual_qty_threshold"]))
	print(f"Planned {len(inventory_events)} inventory vouchers from {len(demand)} demand buckets", flush=True)
	warehouse_names = [accounts["warehouses"][logical] for logical in WAREHOUSE_LOGICAL_NAMES]
	fiscal_map = FiscalYearMap()
	series = SeriesAllocator()
	clock = MonotonicClock()
	states: dict[tuple[int, int], FIFOQueue] = {}
	stats: dict[str, int] = defaultdict(int)
	second_plans = SalesPlanGenerator(config, stock_items, bundles, line_distribution, start, end).iter_invoices()
	next_sale = next(second_plans, None)
	for event_idx, event in enumerate(inventory_events, 1):
		while next_sale is not None and next_sale.posting_dt < event.posting_dt:
			process_sale_invoice(next_sale, inserter, series, fiscal_map, states, stock_items, bundles, master_rows["customers"], warehouse_names, accounts, args.company, DEFAULT_CURRENCY, DEFAULT_PRICE_LIST, party_defaults, stats, clock)
			if stats["sales_invoices"] % 10000 == 0:
				print(f"  sales {stats['sales_invoices']}/{config.sales_invoices}", flush=True)
			next_sale = next(second_plans, None)
		if event.kind == "receipt":
			process_receipt_event(event, inserter, series, fiscal_map, states, stock_items, warehouse_names, accounts, args.company, DEFAULT_CURRENCY, stats, clock)
		else:
			process_transfer_event(event, inserter, series, fiscal_map, states, stock_items, warehouse_names, accounts, args.company, DEFAULT_CURRENCY, stats, clock)
		if event_idx % 1000 == 0:
			print(f"  inventory vouchers {event_idx}/{len(inventory_events)}", flush=True)
	while next_sale is not None:
		process_sale_invoice(next_sale, inserter, series, fiscal_map, states, stock_items, bundles, master_rows["customers"], warehouse_names, accounts, args.company, DEFAULT_CURRENCY, DEFAULT_PRICE_LIST, party_defaults, stats, clock)
		if stats["sales_invoices"] % 10000 == 0:
			print(f"  sales {stats['sales_invoices']}/{config.sales_invoices}", flush=True)
		next_sale = next(second_plans, None)
	buffer_events = build_buffer_receipt_events(states, stock_items, end, config.inventory_periods)
	for event in buffer_events:
		process_receipt_event(event, inserter, series, fiscal_map, states, stock_items, warehouse_names, accounts, args.company, DEFAULT_CURRENCY, stats, clock)
	stats["buffer_receipt_vouchers"] += len(buffer_events)
	print(f"Added {len(buffer_events)} buffer receipt vouchers to keep k6 saleable stock healthy", flush=True)
	inserter.flush_all()
	print("Transactional rows inserted; writing Bin rows", flush=True)
	insert_bins(inserter, states, stock_items, warehouse_names, args.company, datetime.combine(end, time(23, 50)))
	inserter.flush_all()
	series.flush()
	frappe.db.commit()
	print("Creating sequential Stock Closing Entries with the synchronous streaming generator", flush=True)
	closing_names = create_sequential_stock_closings(args.company, closing_cutoffs)
	closing_name = closing_names[closing_cutoffs[-1].isoformat()]
	print("Building SQLite FTS5 item search index", flush=True)
	build_item_search_index()
	print("Submitting final 50 vouchers through genuine Frappe document hooks", flush=True)
	final_vouchers = submit_final_vouchers(states, stock_items, bundles, master_rows["customers"], warehouse_names, accounts, args.company, DEFAULT_CURRENCY, DEFAULT_PRICE_LIST, final_date, stats)
	popularity_order = sorted(range(len(stock_items)), key=lambda idx: stock_items[idx].popularity_weight or (1 / ((idx + 1) ** 1.15)), reverse=True)
	rank_of = {item_idx: rank for rank, item_idx in enumerate(popularity_order, 1)}
	counts = dict(inserter.counts)
	counts.update(
		{
			"sales_invoices_logical": stats["sales_invoices"],
			"return_invoices_logical": stats["return_invoices"],
			"final_vouchers": stats["final_sales"] + stats["final_returns"] + stats["final_transfers"],
		}
	)
	items_manifest = [
		{
			"item_code": item.item_code,
			"item_name": item.item_name,
			"barcode": item.barcode,
			"uom": STOCK_UOM,
			"rate": item.selling_rate,
			"rank": rank_of[idx],
		}
		for idx, item in enumerate(stock_items)
	]
	in_stock_items: dict[str, list[dict[str, Any]]] = {}
	for warehouse_idx, warehouse_name in enumerate(warehouse_names):
		rows: list[dict[str, Any]] = []
		for item_idx in popularity_order:
			state = states.get((item_idx, warehouse_idx))
			qty = state.total_stock_and_value()[0] if state else 0.0
			if qty >= MIN_IN_STOCK_QTY:
				item = stock_items[item_idx]
				rows.append({"item_code": item.item_code, "barcode": item.barcode, "uom": STOCK_UOM, "qty": r6(qty)})
			if len(rows) >= 2000:
				break
		in_stock_items[warehouse_name] = rows
	manifest = {
		"schema_version": 2,
		"seed_id": str(uuid.uuid4()),
		"seeded_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
		"site_name": args.site,
		"scale": config.name,
		"company": args.company,
		"company_abbr": args.company_abbr,
		"currency": DEFAULT_CURRENCY,
		"price_list": DEFAULT_PRICE_LIST,
		"payment_mode": DEFAULT_PAYMENT_MODE,
		"report_window": {
			"from_date": start.isoformat(),
			"to_date": end.isoformat(),
			"final_voucher_date": final_date.isoformat(),
		},
		"warehouses": accounts["warehouses"],
		"warehouse_names": warehouse_names,
		"warehouse_accounts": accounts["warehouse_accounts"],
		"accounts": {key: accounts[key] for key in ("receivable", "income", "cogs", "stock_adjustment", "default_inventory", "cost_center")},
		"cash_account": cash_account,
		"items": items_manifest,
		"item_summary": {
			"count": len(stock_items),
			"hot_samples": [stock_items[idx].item_code for idx in popularity_order[:10]],
			"cold_samples": [stock_items[idx].item_code for idx in popularity_order[-10:]],
			"bundle_items": [bundle.item_code for bundle in bundles],
		},
		"in_stock_items": in_stock_items,
		"customers": master_rows["customers"],
		"suppliers": master_rows["suppliers"],
		"counts": counts,
		"stock_closing_entries": closing_names,
		"generation": {
			"zipf_alpha": float(line_distribution["zipf_alpha"]),
			"bundle_probability": float(line_distribution["bundle_probability"]),
			"demand_buckets": len(demand),
			"inventory_events": len(inventory_events),
			"buffer_receipt_vouchers": len(buffer_events),
			"stock_closing_entry": closing_name,
		},
		"final_vouchers": final_vouchers,
	}
	manifest_path = write_manifest(manifest, args.manifest_output)
	print(f"Seed complete. Manifest: {manifest_path}", flush=True)
	print("Bulk row counts: " + json.dumps(dict(sorted(inserter.counts.items())), sort_keys=True), flush=True)
	print("Logical stats: " + json.dumps(dict(sorted(stats.items())), sort_keys=True), flush=True)
	return manifest


def seed(
	scale: str = "smoke",
	site: str = BENCH_SITE,
	allow_non_bench_site: bool | str = False,
	catalog_json: str | None = None,
	line_dist_json: str | None = None,
	start_date: str | None = None,
	end_date: str | None = None,
	company: str = DEFAULT_COMPANY,
	company_abbr: str = DEFAULT_COMPANY_ABBR,
	chunk_size: int | str = 10_000,
	manifest_output: str | None = None,
) -> dict[str, Any]:
	allow = allow_non_bench_site if isinstance(allow_non_bench_site, bool) else str(allow_non_bench_site).lower() in {"1", "true", "yes"}
	args = argparse.Namespace(
		site=site,
		scale=scale,
		allow_non_bench_site=allow,
		catalog_json=catalog_json,
		line_dist_json=line_dist_json,
		start_date=start_date,
		end_date=end_date,
		company=company,
		company_abbr=company_abbr,
		chunk_size=int(chunk_size),
		manifest_output=manifest_output,
	)
	return run_seed(args)


def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Seed the disposable Khitmee ERPNext performance benchmark dataset")
	parser.add_argument("--site", default=BENCH_SITE)
	parser.add_argument("--scale", choices=sorted(SCALE_CONFIGS), default="smoke")
	parser.add_argument("--allow-non-bench-site", action="store_true")
	parser.add_argument("--catalog-json")
	parser.add_argument("--line-dist-json")
	parser.add_argument("--start-date")
	parser.add_argument("--end-date")
	parser.add_argument("--company", default=DEFAULT_COMPANY)
	parser.add_argument("--company-abbr", default=DEFAULT_COMPANY_ABBR)
	parser.add_argument("--chunk-size", type=int, default=10_000)
	parser.add_argument("--manifest-output")
	return parser


def main(argv: Sequence[str] | None = None) -> int:
	args = build_arg_parser().parse_args(argv)
	try:
		run_seed(args)
	except Exception as exc:
		print(f"ERROR: {exc}", file=sys.stderr, flush=True)
		if os.environ.get("SEEDER_DEBUG"):
			raise
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
