#!/usr/bin/env python3
"""Measure scoped Fast Gross Profit latency at fixed invoice-parent cardinalities.

Runs only inside the isolated khitmee-perf backend. The deterministic invoice
scope is selected once per process from the restored fixture, then each
cardinality reuses a stable prefix so paired MariaDB arms execute the same SQL
shape, parameters, and application revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

PROJECT = "khitmee-perf"
FORBIDDEN_PROJECT = "khitmee-erpnext"
SCHEMA_VERSION = "fast-gp-invoice-scaling-v1"
DEFAULT_COUNTS = (100, 1000, 10000)


def validate_target_site(site: str) -> str:
    value = str(site or "").strip()
    lowered = value.lower()
    if not value or lowered == "khitmee.localhost" or lowered.endswith(".khitmee.localhost"):
        raise RuntimeError(f"refusing live-stack site {value!r}")
    return value


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


def latency_stats(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in samples)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 3),
        "p50_ms": round(percentile(ordered, 50.0), 3),
        "p95_ms": round(percentile(ordered, 95.0), 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
    }


def connect_frappe(site: str | None):
    import frappe

    if not site:
        configs = sorted(Path.cwd().glob("*/site_config.json"))
        if len(configs) != 1:
            raise RuntimeError(f"expected exactly one site below {Path.cwd()}, found {len(configs)}")
        site = configs[0].parent.name
    site = validate_target_site(site)
    frappe.init(site=site, sites_path=str(Path.cwd()))
    frappe.connect()
    if getattr(frappe.conf, "db_host", None) != "db":
        frappe.destroy()
        raise RuntimeError("refusing non-isolated database host; expected service name 'db'")
    frappe.set_user("Administrator")
    return frappe, site


def global_variables(frappe) -> dict[str, str]:
    rows = frappe.db.sql("SHOW GLOBAL VARIABLES")
    wanted = {
        "version",
        "version_compile_machine",
        "transaction_isolation",
        "innodb_flush_log_at_trx_commit",
        "innodb_flush_method",
        "innodb_snapshot_isolation",
        "innodb_buffer_pool_size",
        "innodb_buffer_pool_load_at_startup",
        "innodb_buffer_pool_dump_at_shutdown",
        "innodb_buffer_pool_dump_pct",
        "max_connections",
        "sync_binlog",
        "binlog_format",
    }
    return {str(key): str(value) for key, value in rows if str(key).lower() in wanted}


def wait_for_buffer_pool_preload(frappe, timeout_seconds: int = 90) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        rows = frappe.db.sql("SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_load_status'")
        last_status = str(rows[0][1]) if rows else ""
        lowered = last_status.lower()
        if not last_status or "load completed" in lowered or "not started" in lowered:
            return last_status
        time.sleep(2)
    raise RuntimeError(f"InnoDB buffer pool preload did not complete within {timeout_seconds}s: {last_status}")


def buffer_pool_status(frappe) -> dict[str, str]:
    rows = frappe.db.sql(
        "SHOW GLOBAL STATUS WHERE Variable_name IN (%s, %s, %s, %s, %s, %s, %s)",
        (
            "Innodb_buffer_pool_pages_total",
            "Innodb_buffer_pool_pages_data",
            "Innodb_buffer_pool_pages_free",
            "Innodb_buffer_pool_pages_dirty",
            "Innodb_buffer_pool_read_requests",
            "Innodb_buffer_pool_reads",
            "Innodb_buffer_pool_load_status",
        ),
    )
    return {str(key): str(value) for key, value in rows}


def select_invoice_scope(frappe, company: str, from_date: str, to_date: str, maximum: int) -> list[str]:
    rows = frappe.db.sql(
        """
        SELECT si.name
        FROM `tabSales Invoice` AS si
        WHERE si.company = %s
          AND si.docstatus = 1
          AND si.is_opening != 'Yes'
          AND si.posting_date BETWEEN %s AND %s
          AND EXISTS (
            SELECT 1 FROM `tabSales Invoice Item` AS sii WHERE sii.parent = si.name
          )
        ORDER BY si.posting_date DESC, si.posting_time DESC, si.name DESC
        LIMIT %s
        """,
        (company, from_date, to_date, int(maximum)),
    )
    return [str(row[0]) for row in rows]


def clear_frappe_caches(frappe) -> None:
    cache = getattr(getattr(frappe, "local", None), "document_cache", None)
    if hasattr(cache, "clear"):
        cache.clear()
    frappe.db.rollback()


def parse_counts(value: str) -> list[int]:
    counts = []
    for token in value.split(","):
        count = int(token.strip())
        if count < 1 or count > 100000:
            raise ValueError("invoice counts must be between 1 and 100000")
        counts.append(count)
    if not counts or len(set(counts)) != len(counts):
        raise ValueError("invoice counts must be non-empty and unique")
    return sorted(counts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default=None)
    parser.add_argument("--company", default=None)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--warehouse", default=None)
    parser.add_argument("--invoice-counts", default=",".join(str(count) for count in DEFAULT_COUNTS))
    parser.add_argument("--iterations", type=int, default=9)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.iterations < 3:
        raise SystemExit("--iterations must be at least 3 for a useful p95")
    if args.warmups < 0 or args.warmups > 3:
        raise SystemExit("--warmups must be between 0 and 3")
    counts = parse_counts(args.invoice_counts)
    frappe = None
    try:
        frappe, site = connect_frappe(args.site)
        preload_status = wait_for_buffer_pool_preload(frappe)
        from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import run_detail_gross_profit

        company = args.company
        if not company:
            company = str(frappe.db.get_value("Company", {"is_group": 0}, "name", order_by="name"))
        maximum = max(counts)
        selected_invoices = select_invoice_scope(frappe, company, args.from_date, args.to_date, maximum)
        if len(selected_invoices) < maximum:
            raise RuntimeError(
                f"fixture has only {len(selected_invoices)} scoped invoices, fewer than requested maximum {maximum}"
            )

        filters: dict[str, Any] = {
            "company": company,
            "from_date": args.from_date,
            "to_date": args.to_date,
            "group_by": "Invoice",
        }
        if args.warehouse:
            filters["warehouse"] = args.warehouse

        buffer_pool_before_cases = buffer_pool_status(frappe)
        cases: dict[str, Any] = {}
        for count in counts:
            invoice_ids = selected_invoices[:count]
            warmup_ms: list[float] = []
            row_count = 0
            for _ in range(args.warmups):
                start = time.perf_counter()
                _columns, rows = run_detail_gross_profit(dict(filters), invoice_ids=invoice_ids, enforce_budget=False)
                warmup_ms.append(round((time.perf_counter() - start) * 1000.0, 3))
                row_count = len(rows or [])
                clear_frappe_caches(frappe)

            samples: list[float] = []
            rows = []
            for _ in range(args.iterations):
                start = time.perf_counter()
                _columns, rows = run_detail_gross_profit(dict(filters), invoice_ids=invoice_ids, enforce_budget=False)
                samples.append(round((time.perf_counter() - start) * 1000.0, 3))
                row_count = len(rows or [])
                clear_frappe_caches(frappe)

            stats = latency_stats(samples)
            row_hash = hashlib.sha256(
                json.dumps(rows or [], sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            stats.update(
                {
                    "invoice_parent_count": count,
                    "row_count": row_count,
                    "warmup_ms": warmup_ms,
                    "samples_ms": samples,
                    "row_hash_sha256": row_hash,
                    "first_invoice": invoice_ids[0],
                    "last_invoice": invoice_ids[-1],
                }
            )
            cases[str(count)] = stats

        payload = {
            "schema_version": SCHEMA_VERSION,
            "project": PROJECT,
            "arm": args.arm,
            "site": site,
            "company": company,
            "from_date": args.from_date,
            "to_date": args.to_date,
            "warehouse": args.warehouse,
            "iterations": args.iterations,
            "warmups": args.warmups,
            "invoice_scope_sha256": None,
            "database_variables": global_variables(frappe),
            "buffer_pool_preload_status": preload_status,
            "cache_state": {
                "frappe_document_cache_cleared_between_iterations": True,
                "transactions_rolled_back_between_iterations": True,
                "warmup_iterations_per_case": args.warmups,
                "buffer_pool_preload_completed_before_measurement": True,
            },
            "buffer_pool_status_before_cases": buffer_pool_before_cases,
            "buffer_pool_status_before_exit": buffer_pool_status(frappe),
            "cases": cases,
        }
        payload["invoice_scope_sha256"] = hashlib.sha256("\n".join(selected_invoices).encode("utf-8")).hexdigest()
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({
            "arm": args.arm,
            "cases": {key: {"p50_ms": row["p50_ms"], "p95_ms": row["p95_ms"], "rows": row["row_count"]} for key, row in cases.items()},
        }, sort_keys=True))
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
