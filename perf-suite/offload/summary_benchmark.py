#!/usr/bin/env python3
"""Benchmark a small incrementally maintainable MariaDB daily-summary read model.

The table and trigger-free maintenance statements are created only in the isolated
bench-loadtest database. This is a workload/cost proof of concept; it does not
claim ERPNext Gross Profit semantic parity because FIFO return folding and bundle
rules remain owned by ERPNext's report implementation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

import pymysql
import pymysql.cursors

from offload_spec import json_default
from probe_mariadb import connect_for_site, status_delta, status_snapshot

SUMMARY_TABLE = "khitmee_offload_daily_summary"

CREATE_SUMMARY_SQL = f"""
CREATE TABLE `{SUMMARY_TABLE}` (
  `posting_date` date NOT NULL,
  `warehouse` varchar(140) NOT NULL,
  `invoice_count` bigint NOT NULL DEFAULT 0,
  `line_count` bigint NOT NULL DEFAULT 0,
  `qty` decimal(21,9) NOT NULL DEFAULT 0,
  `selling_amount` decimal(21,9) NOT NULL DEFAULT 0,
  `buying_amount_estimate` decimal(21,9) NOT NULL DEFAULT 0,
  `gross_profit_estimate` decimal(21,9) NOT NULL DEFAULT 0,
  PRIMARY KEY (`posting_date`, `warehouse`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

BUILD_SUMMARY_SQL = f"""
INSERT INTO `{SUMMARY_TABLE}` (
  `posting_date`, `warehouse`, `invoice_count`, `line_count`, `qty`,
  `selling_amount`, `buying_amount_estimate`, `gross_profit_estimate`
)
SELECT
  si.posting_date,
  sii.warehouse,
  COUNT(DISTINCT si.name) AS invoice_count,
  COUNT(*) AS line_count,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
FROM `tabSales Invoice` AS si
JOIN `tabSales Invoice Item` AS sii ON sii.parent = si.name
WHERE si.company = %s
  AND si.docstatus = 1
  AND si.is_opening != 'Yes'
  AND si.posting_date BETWEEN %s AND %s
GROUP BY si.posting_date, sii.warehouse
"""

RAW_RANGE_SQL = """
SELECT
  si.posting_date,
  sii.warehouse,
  COUNT(DISTINCT si.name) AS invoice_count,
  COUNT(*) AS line_count,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
FROM `tabSales Invoice` AS si
JOIN `tabSales Invoice Item` AS sii ON sii.parent = si.name
WHERE si.company = %s
  AND si.docstatus = 1
  AND si.is_opening != 'Yes'
  AND si.posting_date BETWEEN %s AND %s
GROUP BY si.posting_date, sii.warehouse
ORDER BY si.posting_date, sii.warehouse
"""

SUMMARY_RANGE_SQL = f"""
SELECT
  posting_date,
  warehouse,
  SUM(invoice_count) AS invoice_count,
  SUM(line_count) AS line_count,
  SUM(qty) AS qty,
  SUM(selling_amount) AS selling_amount,
  SUM(buying_amount_estimate) AS buying_amount_estimate,
  SUM(gross_profit_estimate) AS gross_profit_estimate
FROM `{SUMMARY_TABLE}`
WHERE posting_date BETWEEN %s AND %s
GROUP BY posting_date, warehouse
ORDER BY posting_date, warehouse
"""

MAINTENANCE_UPSERT_SQL = f"""
INSERT INTO `{SUMMARY_TABLE}` (
  `posting_date`, `warehouse`, `invoice_count`, `line_count`, `qty`,
  `selling_amount`, `buying_amount_estimate`, `gross_profit_estimate`
)
VALUES (%s, %s, 0, 1, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  `line_count` = `line_count` + 1,
  `qty` = `qty` + VALUES(`qty`),
  `selling_amount` = `selling_amount` + VALUES(`selling_amount`),
  `buying_amount_estimate` = `buying_amount_estimate` + VALUES(`buying_amount_estimate`),
  `gross_profit_estimate` = `gross_profit_estimate` + VALUES(`gross_profit_estimate`)
"""


def execute_timed(cursor, sql: str, params: tuple[Any, ...] = (), fetch: bool = False) -> dict[str, Any]:
    before = status_snapshot(cursor)
    start = time.perf_counter()
    cursor.execute(sql, params)
    affected_rows = cursor.rowcount
    rows = cursor.fetchall() if fetch else None
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    after = status_snapshot(cursor)
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "rows": rows,
        "row_count": len(rows) if rows is not None else affected_rows,
        "status_delta": status_delta(before, after),
    }


def compare_rows(left, right) -> dict[str, Any]:
    mismatches = []
    if len(left) != len(right):
        mismatches.append({"kind": "row_count", "raw": len(left), "summary": len(right)})
    for index, (lrow, rrow) in enumerate(zip(left, right)):
        if tuple(lrow) != tuple(rrow):
            ltuple = tuple(str(value) for value in lrow)
            rtuple = tuple(str(value) for value in rrow)
            mismatches.append({"kind": "value", "row": index, "raw": ltuple, "summary": rtuple})
            break
    return {"passed": not mismatches, "mismatches": mismatches[:5]}


def table_size(cursor) -> dict[str, Any]:
    cursor.execute(
        "SELECT table_rows, data_length, index_length FROM information_schema.TABLES "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (SUMMARY_TABLE,),
    )
    row = cursor.fetchone()
    if not row:
        return {}
    return {"table_rows_estimate": row[0], "data_bytes": row[1], "index_bytes": row[2], "total_bytes": row[1] + row[2]}


def benchmark_maintenance_upserts(cursor, rows: int) -> dict[str, Any]:
    cursor.execute(
        "SELECT posting_date, warehouse, qty, selling_amount, buying_amount_estimate, gross_profit_estimate "
        f"FROM `{SUMMARY_TABLE}` ORDER BY posting_date DESC, warehouse LIMIT 1"
    )
    sample = cursor.fetchone()
    if not sample:
        raise RuntimeError("summary table is empty; cannot benchmark maintenance upserts")
    posting_date, warehouse, qty, selling, buying, gross = sample
    batch_rows = max(1, int(rows))
    params = (posting_date, warehouse, qty, selling, buying, gross)
    before = status_snapshot(cursor)
    start = time.perf_counter()
    for _ in range(batch_rows):
        cursor.execute(MAINTENANCE_UPSERT_SQL, params)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    after = status_snapshot(cursor)
    return {
        "rows": batch_rows,
        "elapsed_ms": round(elapsed_ms, 3),
        "avg_ms_per_line": round(elapsed_ms / batch_rows, 6),
        "status_delta": status_delta(before, after),
        "note": "Trigger-free line-maintenance primitive cost on one hot summary row; invoice_count maintenance is intentionally excluded from this PoC.",
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n", encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites-path", default="/home/frappe/frappe-bench/sites")
    parser.add_argument("--company", required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--maintenance-rows", type=int, default=1000)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--keep-table", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    site, conn = connect_for_site(Path(args.sites_path))
    try:
        cursor = conn.cursor(pymysql.cursors.Cursor)
        cursor.execute(f"DROP TABLE IF EXISTS `{SUMMARY_TABLE}`")
        cursor.execute(CREATE_SUMMARY_SQL)
        build = execute_timed(cursor, BUILD_SUMMARY_SQL, (args.company, args.from_date, args.to_date))
        conn.commit()
        raw = execute_timed(cursor, RAW_RANGE_SQL, (args.company, args.from_date, args.to_date), fetch=True)
        summary = execute_timed(cursor, SUMMARY_RANGE_SQL, (args.from_date, args.to_date), fetch=True)
        parity = compare_rows(raw["rows"], summary["rows"])
        conn.autocommit(False)
        maintenance = benchmark_maintenance_upserts(cursor, args.maintenance_rows)
        conn.rollback()  # maintenance microbenchmark must not mutate the immutable baseline
        conn.autocommit(True)
        size = table_size(cursor)
        if not args.keep_table:
            cursor.execute(f"DROP TABLE IF EXISTS `{SUMMARY_TABLE}`")
            conn.commit()
        cursor.close()
    finally:
        conn.close()

    payload = {
        "schema_version": 1,
        "engine": "mariadb-summary-poc",
        "site": site,
        "company": args.company,
        "from_date": args.from_date,
        "to_date": args.to_date,
        "table": SUMMARY_TABLE,
        "build": build,
        "raw_range_query": {key: value for key, value in raw.items() if key != "rows"},
        "summary_range_query": {key: value for key, value in summary.items() if key != "rows"},
        "parity": parity,
        "maintenance": maintenance,
        "size": size,
        "correctness_scope": [
            "Parity is exact only for the additive canonical financial facts in this benchmark query.",
            "ERPNext Gross Profit FIFO, returns, bundles, permissions, and reposting semantics are outside this lightweight read-model PoC.",
        ],
    }
    write_json(Path(args.json_out), payload)
    print(json.dumps({
        "build_ms": build["elapsed_ms"],
        "raw_ms": raw["elapsed_ms"],
        "summary_ms": summary["elapsed_ms"],
        "rows": summary["row_count"],
        "parity": parity["passed"],
        "maintenance_avg_ms_per_line": maintenance["avg_ms_per_line"],
    }, sort_keys=True))
    return 0 if parity["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
