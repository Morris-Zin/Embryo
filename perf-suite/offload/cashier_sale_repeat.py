#!/usr/bin/env python3
"""Rollback-safe repeated cashier sale latency probe for the isolated perf bench."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import sys
import time
from typing import Any

PROJECT = "khitmee-perf"
SCHEMA_VERSION = "cashier-sale-repeat-v1"


def percentile(sorted_values: list[float], pct: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[low]
    fraction = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def stats(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 3),
        "p50_ms": round(percentile(ordered, 50), 3),
        "p95_ms": round(percentile(ordered, 95), 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "samples_ms": [round(value, 3) for value in samples],
    }


def connect(site: str | None):
    import frappe

    if not site:
        configs = sorted(Path.cwd().glob("*/site_config.json"))
        if len(configs) != 1:
            raise RuntimeError(f"expected exactly one site below {Path.cwd()}, found {len(configs)}")
        site = configs[0].parent.name
    lowered = str(site).lower()
    if lowered == "khitmee.localhost" or lowered.endswith(".khitmee.localhost"):
        raise RuntimeError(f"refusing live-stack site {site!r}")
    frappe.init(site=site, sites_path=str(Path.cwd()))
    frappe.connect()
    if getattr(frappe.conf, "db_host", None) != "db":
        frappe.destroy()
        raise RuntimeError("refusing non-isolated database host; expected service name 'db'")
    frappe.set_user("Administrator")
    return frappe, site


def resolve_warehouse(frappe, logical_name: str) -> str:
    exact = frappe.db.get_value("Warehouse", {"name": logical_name}, "name")
    if exact:
        return str(exact)
    by_label = frappe.db.get_value("Warehouse", {"warehouse_name": logical_name}, "name")
    if by_label:
        return str(by_label)
    row = frappe.db.sql("SELECT name FROM `tabWarehouse` WHERE is_group = 0 AND disabled = 0 ORDER BY name LIMIT 1")
    if not row:
        raise RuntimeError(f"no leaf warehouse found for {logical_name}")
    return str(row[0][0])


def build_payload(items_pool: list[dict[str, Any]], line_count: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed + line_count)
    if len(items_pool) >= line_count:
        chosen = rng.sample(items_pool, line_count)
    else:
        chosen = [items_pool[idx % len(items_pool)] for idx in range(line_count)]
    return [
        {
            "item_code": str(row.get("item_code") or row.get("name")),
            "qty": rng.randint(1, 3),
            "uom": str(row.get("stock_uom") or row.get("uom") or "Nos"),
        }
        for row in chosen
    ]


def clear_state(frappe) -> None:
    frappe.db.rollback()
    cache = getattr(getattr(frappe, "local", None), "document_cache", None)
    if hasattr(cache, "clear"):
        cache.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default=None)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.iterations < 3:
        raise SystemExit("--iterations must be at least 3")

    frappe = None
    try:
        frappe, site = connect(args.site)
        import khitmee_ops.api.vouchers as vouchers_mod

        shop1 = resolve_warehouse(frappe, "Shop 1")
        customer = str(frappe.db.get_value("Customer", {"disabled": 0}, "name", order_by="name"))
        price_list = str(frappe.db.get_value("Price List", {"enabled": 1, "selling": 1}, "name", order_by="name"))
        payment_mode = (
            frappe.db.get_value("Mode of Payment", {"enabled": 1, "type": "Cash"}, "name")
            or frappe.db.get_value("Mode of Payment", {"enabled": 1}, "name")
        )
        items_pool = frappe.db.sql(
            "SELECT name AS item_code, stock_uom FROM `tabItem` "
            "WHERE disabled = 0 AND has_variants = 0 AND is_stock_item = 1 "
            "ORDER BY name LIMIT 250",
            as_dict=True,
        )
        if not items_pool:
            raise RuntimeError("fixture has no stock items")

        original_commit = frappe.db.commit

        def guarded_commit(*_args, **_kwargs):
            raise RuntimeError("commit guard tripped during rollback-only cashier repeat")

        frappe.db.commit = guarded_commit
        cases: dict[str, Any] = {}
        try:
            for line_count in (10, 50):
                items = build_payload(items_pool, line_count, args.seed)
                warmup_ms: list[float] = []
                for index in range(args.warmups):
                    start = time.perf_counter()
                    vouchers_mod.submit_sale(
                        f"bench-sale-repeat-{line_count}-w{index}-{args.seed}",
                        shop1,
                        customer,
                        price_list,
                        json.dumps(items),
                        payment_mode=payment_mode,
                        printer_type="voucher",
                    )
                    warmup_ms.append(round((time.perf_counter() - start) * 1000.0, 3))
                    clear_state(frappe)

                samples: list[float] = []
                for index in range(args.iterations):
                    start = time.perf_counter()
                    vouchers_mod.submit_sale(
                        f"bench-sale-repeat-{line_count}-{index}-{args.seed}",
                        shop1,
                        customer,
                        price_list,
                        json.dumps(items),
                        payment_mode=payment_mode,
                        printer_type="voucher",
                    )
                    samples.append((time.perf_counter() - start) * 1000.0)
                    clear_state(frappe)
                result = stats(samples)
                result["warmup_ms"] = warmup_ms
                result["line_count"] = line_count
                cases[f"sale_{line_count}_lines"] = result
        finally:
            frappe.db.commit = original_commit
            clear_state(frappe)

        payload = {
            "schema_version": SCHEMA_VERSION,
            "project": PROJECT,
            "site": site,
            "arm": args.arm,
            "seed": args.seed,
            "iterations": args.iterations,
            "warmups": args.warmups,
            "warehouse": shop1,
            "cases": cases,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"arm": args.arm, "cases": {k: {"p50_ms": v["p50_ms"], "p95_ms": v["p95_ms"]} for k, v in cases.items()}}, sort_keys=True))
        return 0
    finally:
        if frappe is not None:
            try:
                frappe.db.rollback()
            except Exception:
                pass
            frappe.destroy()


if __name__ == "__main__":
    sys.exit(main())
