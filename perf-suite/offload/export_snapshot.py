#!/usr/bin/env python3
"""Export a consistent MariaDB snapshot for the offload proof of concept.

This script is intentionally executed inside the isolated khitmee-perf backend
container. It reads database credentials only from the site's own config file,
uses one repeatable-read snapshot for every selected table, and writes gzip CSV
files plus a manifest that can be transported to a manager/shop machine.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time as time_module
from decimal import Decimal
from typing import Any, Iterable

import pymysql
import pymysql.cursors

from offload_spec import NULL_TOKEN, TABLE_SPECS, TABLES_BY_KEY, TableSpec, format_timedelta, json_default

PROTECTED_SITE_PARTS = ("khitmee.localhost", "khitmee.com")
EXPECTED_SITE = "bench-loadtest.localhost"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def csv_value(value: Any) -> str:
    if value is None:
        return NULL_TOKEN
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return format_timedelta(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def canonical_row_bytes(row: Iterable[Any]) -> bytes:
    return json.dumps([json_default(value) for value in row], separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class XorRowHash:
    def __init__(self) -> None:
        self.value = 0
        self.rows = 0

    def add(self, row: Iterable[Any]) -> None:
        digest = hashlib.sha256(canonical_row_bytes(row)).digest()
        self.value ^= int.from_bytes(digest, "big")
        self.rows += 1

    def hexdigest(self) -> str:
        return f"{self.value:064x}"


def discover_site(sites_path: Path) -> tuple[str, Path]:
    configs = sorted(sites_path.glob("*/site_config.json"))
    if len(configs) != 1:
        raise RuntimeError(f"expected exactly one site config below {sites_path}, found {len(configs)}")
    site = configs[0].parent.name
    lowered = site.lower()
    if site != EXPECTED_SITE or any(part in lowered for part in PROTECTED_SITE_PARTS):
        raise RuntimeError(f"refusing non-benchmark or protected site {site}")
    return site, configs[0]


def connect(site_config: dict[str, Any]):
    if site_config.get("db_host", "db") != "db":
        raise RuntimeError("exporter only runs against the isolated compose service named db")
    return pymysql.connect(
        host="db",
        user=site_config.get("db_user", site_config["db_name"]),
        password=site_config["db_password"],
        database=site_config["db_name"],
        autocommit=False,
        cursorclass=pymysql.cursors.SSCursor,
        charset="utf8mb4",
    )


def start_consistent_snapshot(conn) -> dict[str, Any]:
    cursor = conn.cursor(pymysql.cursors.Cursor)
    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
    snapshot = {"started_at": utc_now()}
    try:
        cursor.execute("SELECT @@gtid_current_pos")
        row = cursor.fetchone()
        snapshot["gtid_current_pos"] = row[0] if row else ""
    except Exception as exc:  # GTID is optional evidence, not a consistency primitive here.
        snapshot["gtid_current_pos"] = ""
        snapshot["gtid_error"] = type(exc).__name__
    try:
        cursor.execute("SELECT NOW(6)")
        row = cursor.fetchone()
        snapshot["database_now"] = json_default(row[0]) if row else None
    except Exception:
        snapshot["database_now"] = None
    cursor.close()
    return snapshot


def export_table(conn, spec: TableSpec, output_dir: Path, modified_after: str | None = None) -> dict[str, Any]:
    output_path = output_dir / f"{spec.key}.csv.gz"
    where = "`modified` > %s" if modified_after else None
    params = (modified_after,) if modified_after else ()
    sql = spec.select_sql(where)
    hasher = XorRowHash()
    start = time_module.perf_counter()
    rows = 0

    cursor = conn.cursor()
    cursor.execute(sql, params)
    with gzip.open(output_path, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(spec.column_names)
        while True:
            batch = cursor.fetchmany(2000)
            if not batch:
                break
            for row in batch:
                hasher.add(row)
                writer.writerow([csv_value(value) for value in row])
                rows += 1
    cursor.close()

    elapsed_ms = (time_module.perf_counter() - start) * 1000.0
    return {
        "table": spec.key,
        "source_table": spec.source_name,
        "file": output_path.name,
        "rows": rows,
        "bytes": output_path.stat().st_size,
        "elapsed_ms": round(elapsed_ms, 3),
        "xor_sha256": hasher.hexdigest(),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites-path", default="/home/frappe/frappe-bench/sites")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--table", action="append", choices=sorted(TABLES_BY_KEY), dest="tables")
    parser.add_argument("--modified-after", default=None, help="incremental export lower bound; uses overlap/reconciliation outside this script")
    parser.add_argument("--mode", choices=("full", "delta"), default="full")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sites_path = Path(args.sites_path)
    site, config_path = discover_site(sites_path)
    site_config = json.loads(config_path.read_text(encoding="utf-8"))
    selected = [TABLES_BY_KEY[key] for key in args.tables] if args.tables else list(TABLE_SPECS)
    if args.mode == "full" and args.modified_after:
        raise SystemExit("--modified-after requires --mode delta")
    if args.mode == "delta" and not args.modified_after:
        raise SystemExit("--mode delta requires --modified-after")

    conn = connect(site_config)
    started = time_module.perf_counter()
    try:
        snapshot = start_consistent_snapshot(conn)
        table_stats = [export_table(conn, spec, output_dir, args.modified_after) for spec in selected]
        conn.rollback()
    finally:
        conn.close()

    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "mode": args.mode,
        "modified_after": args.modified_after,
        "site": site,
        "snapshot": snapshot,
        "tables": table_stats,
        "table_order": [spec.key for spec in selected],
        "elapsed_ms": round((time_module.perf_counter() - started) * 1000.0, 3),
        "null_token": NULL_TOKEN,
        "notes": [
            "All tables were read inside one MariaDB REPEATABLE READ consistent snapshot.",
            "The CSV export is a proof-of-concept transport format, not a proposed production format.",
        ],
    }
    write_json(output_dir / "manifest.json", manifest)
    print(json.dumps({"rows": sum(row["rows"] for row in table_stats), "bytes": sum(row["bytes"] for row in table_stats), "elapsed_ms": manifest["elapsed_ms"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
