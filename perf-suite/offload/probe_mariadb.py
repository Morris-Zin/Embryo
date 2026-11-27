#!/usr/bin/env python3
"""Run canonical offload benchmark queries against isolated MariaDB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

import pymysql
import pymysql.cursors

from export_snapshot import discover_site
from offload_spec import CANONICAL_QUERIES, QUERIES_BY_KEY, json_default, query_parameters

STATUS_KEYS = (
    "Innodb_rows_read",
    "Innodb_data_reads",
    "Innodb_pages_read",
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
    "Created_tmp_tables",
    "Created_tmp_disk_tables",
    "Sort_merge_passes",
    "Bytes_sent",
)


def status_snapshot(cursor) -> dict[str, int]:
    try:
        cursor.execute("SHOW SESSION STATUS")
        rows = cursor.fetchall()
    except Exception:
        return {}
    result = {}
    for key, value in rows:
        if key in STATUS_KEYS:
            try:
                result[key] = int(value)
            except (TypeError, ValueError):
                pass
    return result


def status_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in STATUS_KEYS if key in before or key in after}


def connect_for_site(sites_path: Path):
    import json as json_module

    site, config_path = discover_site(sites_path)
    site_config = json_module.loads(config_path.read_text(encoding="utf-8"))
    if site_config.get("db_host", "db") != "db":
        raise RuntimeError("probe only runs against the isolated compose service named db")
    conn = pymysql.connect(
        host="db",
        user=site_config.get("db_user", site_config["db_name"]),
        password=site_config["db_password"],
        database=site_config["db_name"],
        autocommit=True,
        cursorclass=pymysql.cursors.SSCursor,
        charset="utf8mb4",
    )
    return site, conn


def run_query(cursor, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    timings = []
    rows = None
    columns = None
    deltas = []
    for _ in range(2):
        before = status_snapshot(cursor)
        start = time.perf_counter()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        after = status_snapshot(cursor)
        timings.append(elapsed_ms)
        deltas.append(status_delta(before, after))
        columns = [column[0] for column in cursor.description]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "cold_ms": round(timings[0], 3),
        "warm_ms": round(timings[1], 3),
        "status_delta_cold": deltas[0],
        "status_delta_warm": deltas[1],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites-path", default="/home/frappe/frappe-bench/sites")
    parser.add_argument("--company", required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--query", action="append", choices=sorted(QUERIES_BY_KEY), dest="queries")
    parser.add_argument("--json-out", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = [QUERIES_BY_KEY[key] for key in args.queries] if args.queries else list(CANONICAL_QUERIES)
    site, conn = connect_for_site(Path(args.sites_path))
    try:
        cursor = conn.cursor()
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        results = {}
        for query in selected:
            params = query_parameters(query.key, args.company, args.from_date, args.to_date)
            outcome = run_query(cursor, query.mariadb_sql, params)
            outcome["params"] = params
            results[query.key] = outcome
        cursor.close()
    finally:
        conn.close()

    payload = {
        "schema_version": 1,
        "engine": "mariadb",
        "site": site,
        "company": args.company,
        "from_date": args.from_date,
        "to_date": args.to_date,
        "queries": results,
    }
    write_json(Path(args.json_out), payload)
    print(json.dumps({key: {"cold_ms": row["cold_ms"], "warm_ms": row["warm_ms"], "rows": row["row_count"]} for key, row in results.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
