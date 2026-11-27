#!/usr/bin/env python3
"""Load offload snapshot CSV files into DuckDB, benchmark canonical queries, and verify parity."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time as time_type
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

from offload_spec import CANONICAL_QUERIES, NULL_TOKEN, QUERIES_BY_KEY, TABLES_BY_KEY, json_default, query_parameters

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised in integration, not unit tests
    duckdb = None


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def proc_memory() -> dict[str, float | None]:
    result = {"vmrss_mib": None, "vmhwm_mib": None}
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    result["vmrss_mib"] = float(line.split()[1]) / 1024.0
                elif line.startswith("VmHWM:"):
                    result["vmhwm_mib"] = float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return result


def proc_cpu_seconds() -> float:
    times = os.times()
    return float(times.user + times.system)


def cpu_delta(before: float, after: float) -> float:
    return round(after - before, 6)


def memory_delta(before: dict[str, float | None], after: dict[str, float | None]) -> dict[str, float | None]:
    rss_before = before.get("vmrss_mib")
    rss_after = after.get("vmrss_mib")
    return {
        "vmrss_before_mib": round(rss_before, 3) if rss_before is not None else None,
        "vmrss_after_mib": round(rss_after, 3) if rss_after is not None else None,
        "vmrss_delta_mib": round(rss_after - rss_before, 3) if rss_before is not None and rss_after is not None else None,
        "vmhwm_after_mib": round(after.get("vmhwm_mib"), 3) if after.get("vmhwm_mib") is not None else None,
    }


def csv_load_sql(table_key: str, csv_path: Path, target_sql: str) -> str:
    spec = TABLES_BY_KEY[table_key]
    return (
        f"{target_sql} AS SELECT * FROM read_csv({sql_literal(str(csv_path))}, "
        f"columns={spec.duckdb_columns_expression()}, header=true, nullstr={sql_literal(NULL_TOKEN)}, "
        "quote='\"', escape='\"')"
    )


def load_full(conn, extract_dir: Path, tables: list[str]) -> dict[str, Any]:
    stats = {}
    for table_key in tables:
        csv_path = extract_dir / f"{table_key}.csv.gz"
        before = proc_memory()
        cpu_before = proc_cpu_seconds()
        start = time.perf_counter()
        conn.execute(csv_load_sql(table_key, csv_path, f"CREATE OR REPLACE TABLE {table_key}"))
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        rows = conn.execute(f"SELECT COUNT(*) FROM {table_key}").fetchone()[0]
        after = proc_memory()
        cpu_after = proc_cpu_seconds()
        stats[table_key] = {
            "rows": rows,
            "load_ms": round(elapsed_ms, 3),
            "cpu_seconds": cpu_delta(cpu_before, cpu_after),
            "input_bytes": csv_path.stat().st_size,
            "memory": memory_delta(before, after),
        }
    return stats


def apply_delta(conn, extract_dir: Path, tables: list[str]) -> dict[str, Any]:
    stats = {}
    for table_key in tables:
        csv_path = extract_dir / f"{table_key}.csv.gz"
        temp = f"delta_{table_key}"
        before = proc_memory()
        cpu_before = proc_cpu_seconds()
        start = time.perf_counter()
        conn.execute(csv_load_sql(table_key, csv_path, f"CREATE OR REPLACE TEMP TABLE {temp}"))
        table_exists = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_key],
        ).fetchone()[0]
        if not table_exists:
            raise RuntimeError(f"delta table {table_key} requires an existing full DuckDB baseline")
        conn.execute(f"DELETE FROM {table_key} WHERE name IN (SELECT name FROM {temp})")
        conn.execute(f"INSERT INTO {table_key} SELECT * FROM {temp}")
        rows_changed = conn.execute(f"SELECT COUNT(*) FROM {temp}").fetchone()[0]
        rows_total = conn.execute(f"SELECT COUNT(*) FROM {table_key}").fetchone()[0]
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        after = proc_memory()
        cpu_after = proc_cpu_seconds()
        stats[table_key] = {
            "rows_changed": rows_changed,
            "rows_total": rows_total,
            "apply_ms": round(elapsed_ms, 3),
            "cpu_seconds": cpu_delta(cpu_before, cpu_after),
            "input_bytes": csv_path.stat().st_size,
            "memory": memory_delta(before, after),
        }
    return stats


def run_duckdb_queries(conn, company: str, from_date: str, to_date: str, query_keys: list[str]) -> dict[str, Any]:
    results = {}
    selected = [QUERIES_BY_KEY[key] for key in query_keys] if query_keys else list(CANONICAL_QUERIES)
    for query in selected:
        params = query_parameters(query.key, company, from_date, to_date)
        timings = []
        rows = None
        columns = None
        memory_rows = []
        cpu_rows = []
        for _ in range(2):
            before = proc_memory()
            cpu_before = proc_cpu_seconds()
            start = time.perf_counter()
            cursor = conn.execute(query.duckdb_sql, params)
            rows = cursor.fetchall()
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            after = proc_memory()
            cpu_after = proc_cpu_seconds()
            timings.append(elapsed_ms)
            memory_rows.append(memory_delta(before, after))
            cpu_rows.append(cpu_delta(cpu_before, cpu_after))
            columns = [column[0] for column in cursor.description]
        results[query.key] = {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "cold_ms": round(timings[0], 3),
            "warm_ms": round(timings[1], 3),
            "memory_cold": memory_rows[0],
            "memory_warm": memory_rows[1],
            "cpu_cold_seconds": cpu_rows[0],
            "cpu_warm_seconds": cpu_rows[1],
            "params": params,
        }
    return results


def values_equal(serialized: Any, typed: Any) -> bool:
    """Use the DuckDB result type to decode the MariaDB JSON value."""
    if isinstance(typed, (Decimal, int, float)) and not isinstance(typed, bool):
        if isinstance(serialized, bool):
            return False
        try:
            return Decimal(str(serialized)) == Decimal(str(typed))
        except InvalidOperation:
            return False
    if isinstance(typed, (date, datetime, time_type)):
        return serialized == typed.isoformat()
    return type(serialized) is type(typed) and serialized == typed


def compare_result_rows(mariadb_rows: list[list[Any]], duckdb_rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    mismatches = []
    if len(mariadb_rows) != len(duckdb_rows):
        mismatches.append({"kind": "row_count", "mariadb": len(mariadb_rows), "duckdb": len(duckdb_rows)})
    for index, (mrow, drow) in enumerate(zip(mariadb_rows, duckdb_rows)):
        if len(mrow) != len(drow):
            mismatches.append({"kind": "width", "row": index, "mariadb": len(mrow), "duckdb": len(drow)})
            continue
        for column_index, (mval, dval) in enumerate(zip(mrow, drow)):
            if not values_equal(mval, dval):
                mismatches.append(
                    {
                        "kind": "value",
                        "row": index,
                        "column": column_index,
                        "mariadb": json_default(mval),
                        "duckdb": json_default(dval),
                    }
                )
                break
        if len(mismatches) >= 5:
            break
    return {"passed": not mismatches, "mismatches": mismatches, "rows_compared": min(len(mariadb_rows), len(duckdb_rows))}


def verify_parity(mariadb_path: Path, duckdb_results: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(mariadb_path.read_text(encoding="utf-8"))
    comparisons = {}
    for key, duckdb_result in duckdb_results.items():
        mariadb_result = payload.get("queries", {}).get(key)
        if not mariadb_result:
            comparisons[key] = {"passed": False, "mismatches": [{"kind": "missing_mariadb_result"}], "rows_compared": 0}
            continue
        comparisons[key] = compare_result_rows(mariadb_result.get("rows", []), duckdb_result.get("rows", []))
    return comparisons


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# DuckDB offload proof-of-concept benchmark",
        "",
        f"- mode: `{report['mode']}`",
        f"- company: `{report['company']}`",
        f"- date window: `{report['from_date']}` to `{report['to_date']}`",
        f"- DuckDB database bytes: {report.get('duckdb_file_bytes', 0)}",
        "",
        "| Query | MariaDB cold ms | MariaDB warm ms | DuckDB cold ms | DuckDB warm ms | Rows | Parity |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    mariadb = report.get("mariadb", {})
    for key, query in report["queries"].items():
        mrow = mariadb.get(key, {})
        parity = report.get("parity", {}).get(key, {})
        lines.append(
            f"| {key} | {mrow.get('cold_ms', 'N/A')} | {mrow.get('warm_ms', 'N/A')} | "
            f"{query.get('cold_ms')} | {query.get('warm_ms')} | {query.get('row_count')} | "
            f"{'PASS' if parity.get('passed') else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "`gross_profit_estimate` is a transport/model parity measure, not a replacement for ERPNext Gross Profit semantics.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract-dir", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--mariadb-results", default=None)
    parser.add_argument("--query", action="append", choices=sorted(QUERIES_BY_KEY), dest="queries")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--memory-limit", default="512MB")
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--markdown-out", default=None)
    return parser.parse_args(argv)


def working_database_copy(database_path: Path) -> Path:
    work_path = database_path.with_name(f".{database_path.name}.work-{os.getpid()}")
    for candidate in (work_path, Path(str(work_path) + ".wal")):
        if candidate.exists():
            candidate.unlink()
    if database_path.exists():
        try:
            shutil.copy2(database_path, work_path)
        except Exception:
            cleanup_working_database(work_path)
            raise
    return work_path


def cleanup_working_database(work_path: Path) -> None:
    for candidate in (work_path, Path(str(work_path) + ".wal")):
        if candidate.exists():
            candidate.unlink()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if duckdb is None:
        raise SystemExit("python package 'duckdb' is required on the manager/reader side; install it in a local benchmark venv")
    extract_dir = Path(args.extract_dir)
    manifest = json.loads((extract_dir / "manifest.json").read_text(encoding="utf-8"))
    tables = manifest.get("table_order") or sorted(TABLES_BY_KEY)
    database_path = Path(args.database)
    if database_path.is_symlink():
        database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest.get("mode") == "delta" and not database_path.exists():
        raise SystemExit("delta mode requires an existing full DuckDB baseline; copy a full-run database and pass it with --database")
    lock_path = Path(str(database_path) + ".lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        work_path = working_database_copy(database_path)
        try:
            conn = duckdb.connect(str(work_path))
            try:
                conn.execute(f"PRAGMA threads={int(args.threads)}")
                conn.execute(f"SET memory_limit={sql_literal(args.memory_limit)}")
                if manifest.get("mode") == "delta":
                    load_stats = apply_delta(conn, extract_dir, tables)
                else:
                    load_stats = load_full(conn, extract_dir, tables)
                conn.execute("CHECKPOINT")
                query_keys = args.queries or []
                query_results = run_duckdb_queries(conn, args.company, args.from_date, args.to_date, query_keys)
            finally:
                conn.close()

            mariadb_payload = {}
            parity = {}
            if args.mariadb_results:
                mariadb_payload = json.loads(Path(args.mariadb_results).read_text(encoding="utf-8")).get("queries", {})
                parity = verify_parity(Path(args.mariadb_results), query_results)

            report = {
                "schema_version": 1,
                "engine": "duckdb",
                "duckdb_version": duckdb.__version__,
                "mode": manifest.get("mode"),
                "manifest": manifest,
                "company": args.company,
                "from_date": args.from_date,
                "to_date": args.to_date,
                "threads": args.threads,
                "memory_limit": args.memory_limit,
                "load": load_stats,
                "queries": query_results,
                "mariadb": mariadb_payload,
                "parity": parity,
                "parity_passed": bool(parity) and all(row.get("passed") for row in parity.values()),
                "duckdb_file_bytes": work_path.stat().st_size if work_path.exists() else 0,
            }
            write_json(Path(args.json_out), report)
            if args.markdown_out:
                write_markdown(Path(args.markdown_out), report)
            if not parity or report["parity_passed"]:
                os.replace(work_path, database_path)
            print(json.dumps({
                "queries": {key: {"cold_ms": row["cold_ms"], "warm_ms": row["warm_ms"], "rows": row["row_count"]} for key, row in query_results.items()},
                "parity_passed": report["parity_passed"],
                "duckdb_file_bytes": report["duckdb_file_bytes"],
            }, sort_keys=True))
            return 0 if not parity or report["parity_passed"] else 2
        finally:
            cleanup_working_database(work_path)


if __name__ == "__main__":
    raise SystemExit(main())
