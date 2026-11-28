#!/usr/bin/env python3
"""Fast (~10-15s), rollback-safe inner-loop profiler targeting khitmee-perf.

Measures:
- 10-line, 50-line, and 200-line counter sale submission (cProfile top hotspots,
  SQL query count, total SQL time, wall ms, RSS delta — rolled back cleanly).
- 50-line direct transfer submission (rolled back cleanly).
- search_items warm latency (p50/p95).
- Fast Gross Profit Warehouse, Monthly, and Invoice-detail (first 50 rows)
  latency + RSS delta.
- `--compare baseline.json candidate.json` renders a Markdown delta table.

Strictly refuses `khitmee-erpnext` or live-stack sites (`khitmee.localhost`).
"""

from __future__ import annotations

import argparse
import cProfile
import json
import math
from pathlib import Path
import pstats
import random
import re
import subprocess
import sys
import time
from typing import Any

PROJECT = "khitmee-perf"
FORBIDDEN_PROJECT = "khitmee-erpnext"
BENCH_PYTHON = "/home/frappe/frappe-bench/env/bin/python"
BENCH_SITES = "/home/frappe/frappe-bench/sites"
SCHEMA_VERSION = "perf-bench-inner-v1"
SALE_LINE_COUNTS = (10, 50, 200)
TRANSFER_LINE_COUNT = 50
INVOICE_DETAIL_PAGE_LIMIT = 50


class SafetyGuardError(RuntimeError):
    """Raised when a target project, site, DB host, or commit violates isolation rules."""


def validate_target_project(project: str | None) -> str:
    value = str(project or "").strip()
    if not value or value == FORBIDDEN_PROJECT or value != PROJECT:
        raise SafetyGuardError(
            f"refusing benchmark target project {value!r}; only isolated {PROJECT!r} is allowed"
        )
    return value


def validate_target_site(site: str | None) -> str:
    value = str(site or "").strip()
    if not value:
        raise SafetyGuardError("site name is required")
    lowered = value.lower()
    if lowered == "khitmee.localhost" or lowered.endswith(".khitmee.localhost"):
        raise SafetyGuardError(f"refusing live-stack site {value!r}")
    return value


def validate_db_host(db_host: str | None) -> str:
    value = str(db_host or "").strip()
    if value != "db":
        raise SafetyGuardError(
            f"unexpected database host {value!r}; perf_bench_inner only runs inside isolated {PROJECT}"
        )
    return value


def build_exec_command(project: str, inner_argv: list[str]) -> list[str]:
    validate_target_project(project)
    backend = f"{project}-backend-1"
    return [
        "docker",
        "exec",
        "-i",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-w",
        BENCH_SITES,
        backend,
        BENCH_PYTHON,
        "-",
        "--inside-backend",
        *inner_argv,
    ]


def build_sale_payload(
    items_pool: list[dict[str, Any]],
    line_count: int,
    warehouse: str,
    customer: str,
    price_list: str,
    payment_mode: str | None = None,
    seed: int = 20261128,
) -> dict[str, Any]:
    if line_count < 1:
        raise ValueError("line_count must be >= 1")
    if not items_pool:
        raise ValueError("items_pool must not be empty")
    rng = random.Random(seed + line_count)
    items: list[dict[str, Any]] = []
    pool_len = len(items_pool)
    if pool_len >= line_count:
        chosen = rng.sample(items_pool, line_count)
    else:
        chosen = [items_pool[idx % pool_len] for idx in range(line_count)]
    for base in chosen:
        item_code = str(base.get("item_code") or base.get("name") or "")
        uom = str(base.get("stock_uom") or base.get("uom") or "Nos")
        items.append({"item_code": item_code, "qty": rng.randint(1, 3), "uom": uom})
    return {
        "warehouse": warehouse,
        "customer": customer,
        "price_list": price_list,
        "payment_mode": payment_mode,
        "items": items,
    }


def build_transfer_payload(
    items_pool: list[dict[str, Any]],
    line_count: int,
    source_warehouse: str,
    target_warehouse: str,
    seed: int = 20261128,
) -> dict[str, Any]:
    if line_count < 1:
        raise ValueError("line_count must be >= 1")
    if not items_pool:
        raise ValueError("items_pool must not be empty")
    if source_warehouse == target_warehouse:
        raise ValueError("source_warehouse and target_warehouse must differ")
    rng = random.Random(seed + line_count + 99)
    items: list[dict[str, Any]] = []
    pool_len = len(items_pool)
    for idx in range(line_count):
        base = items_pool[idx % pool_len]
        item_code = str(base.get("item_code") or base.get("name") or "")
        uom = str(base.get("stock_uom") or base.get("uom") or "Nos")
        items.append({"item_code": item_code, "qty": rng.randint(1, 2), "uom": uom})
    return {
        "source": source_warehouse,
        "target": target_warehouse,
        "items": items,
    }


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        raise ValueError("cannot compute percentile of empty list")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_vals[lo]
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def compute_latency_stats(samples_ms: list[float]) -> dict[str, Any]:
    """Aggregate latency samples in milliseconds; returns None metrics when empty."""
    cleaned = sorted(float(x) for x in (samples_ms or []))
    if not cleaned:
        return {
            "count": 0,
            "min_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "mean_ms": None,
        }
    return {
        "count": len(cleaned),
        "min_ms": round(cleaned[0], 3),
        "p50_ms": round(_percentile(cleaned, 50.0), 3),
        "p95_ms": round(_percentile(cleaned, 95.0), 3),
        "max_ms": round(cleaned[-1], 3),
        "mean_ms": round(sum(cleaned) / len(cleaned), 3),
    }


_SQL_WS_RE = re.compile(r"\s+")
_SQL_SINGLE_QUOTED_RE = re.compile(r"'(?:''|\\'|[^'])*'")
_SQL_PERCENT_S_RE = re.compile(r"%s")
_SQL_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_SQL_IN_LIST_RE = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.IGNORECASE)


def normalize_sql_template(query: Any, max_len: int = 160) -> str:
    """Normalize a SQL query into a compact template for hotspot aggregation."""
    text = _SQL_WS_RE.sub(" ", str(query or "").strip())
    if not text:
        return "<empty>"
    text = _SQL_SINGLE_QUOTED_RE.sub("?", text)
    text = _SQL_PERCENT_S_RE.sub("?", text)
    text = _SQL_NUMBER_RE.sub("?", text)
    text = _SQL_IN_LIST_RE.sub("IN (?...)", text)
    if max_len > 3 and len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def extract_cprofile_hotspots(
    profiler: cProfile.Profile,
    limit: int = 10,
    sort_by: str = "cumtime",
) -> list[dict[str, Any]]:
    stats = pstats.Stats(profiler)
    rows: list[dict[str, Any]] = []
    for (filename, lineno, func_name), (cc, nc, tt, ct, _callers) in stats.stats.items():
        rows.append(
            {
                "function": func_name,
                "filename": Path(filename).name if filename else "",
                "lineno": int(lineno),
                "calls": int(nc),
                "primitive_calls": int(cc),
                "tottime_ms": round(tt * 1000.0, 3),
                "cumtime_ms": round(ct * 1000.0, 3),
            }
        )
    if sort_by == "tottime":
        rows.sort(key=lambda item: (-item["tottime_ms"], -item["cumtime_ms"], item["function"]))
    else:
        rows.sort(key=lambda item: (-item["cumtime_ms"], -item["tottime_ms"], item["function"]))
    return rows[: max(1, int(limit))]


def current_rss_mib() -> float | None:
    """Return current process RSS in MiB (Linux /proc/self/status or resource fallback)."""
    proc_status = Path("/proc/self/status")
    if proc_status.is_file():
        try:
            for line in proc_status.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return round(int(parts[1]) / 1024.0, 3)
        except OSError:
            pass
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(usage / 1024.0, 3)
    except Exception:
        return None


class CommitGuard:
    """Block any accidental commit during rollback-only profiling."""

    def __init__(self, frappe: Any):
        self.frappe = frappe
        self.original = None

    def __enter__(self) -> CommitGuard:
        self.original = self.frappe.db.commit

        def _guarded_commit(*args: Any, **kwargs: Any) -> None:
            raise SafetyGuardError("commit guard tripped during rollback-only perf_bench_inner run")

        self.frappe.db.commit = _guarded_commit
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.frappe.db.commit = self.original
        return False


class SqlProfiler:
    """Count SQL queries, accumulate wall time, and aggregate normalized SQL templates."""

    def __init__(self, frappe: Any):
        self.frappe = frappe
        self.original = None
        self.count = 0
        self.total_ms = 0.0
        self.templates: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> SqlProfiler:
        self.original = self.frappe.db.sql

        def _wrapped_sql(*args: Any, **kwargs: Any) -> Any:
            query = args[0] if args else kwargs.get("query", "")
            t0 = time.perf_counter()
            try:
                return self.original(*args, **kwargs)
            finally:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                self.count += 1
                self.total_ms += elapsed_ms
                tpl = normalize_sql_template(query)
                bucket = self.templates.get(tpl)
                if bucket is None:
                    self.templates[tpl] = {"count": 1, "total_ms": elapsed_ms}
                else:
                    bucket["count"] += 1
                    bucket["total_ms"] += elapsed_ms

        self.frappe.db.sql = _wrapped_sql
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.frappe.db.sql = self.original
        return False

    def top_templates(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = [
            {
                "template": tpl,
                "count": int(meta["count"]),
                "total_ms": round(float(meta["total_ms"]), 3),
                "avg_ms": round(float(meta["total_ms"]) / int(meta["count"]), 3),
            }
            for tpl, meta in self.templates.items()
        ]
        rows.sort(key=lambda item: (-item["total_ms"], -item["count"], item["template"]))
        return rows[: max(1, int(limit))]


def _extract_comparable_metrics(report: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """Flatten a benchmark report into {metric_label: {wall_ms, sql_count, sql_ms, rss_delta_mib}}."""
    out: dict[str, dict[str, float | None]] = {}
    benchmarks = report.get("benchmarks") or {}
    for key, val in benchmarks.items():
        if not isinstance(val, dict):
            continue
        if "p50_ms" in val:
            out[f"{key} (p50)"] = {
                "wall_ms": val.get("p50_ms"),
                "sql_count": val.get("avg_sql_count"),
                "sql_ms": val.get("avg_sql_ms"),
                "rss_delta_mib": val.get("rss_delta_mib"),
            }
            out[f"{key} (p95)"] = {
                "wall_ms": val.get("p95_ms"),
                "sql_count": None,
                "sql_ms": None,
                "rss_delta_mib": None,
            }
        else:
            out[key] = {
                "wall_ms": val.get("wall_ms"),
                "sql_count": val.get("sql_count"),
                "sql_ms": val.get("sql_ms"),
                "rss_delta_mib": val.get("rss_delta_mib"),
            }
    return out


def compare_benchmarks(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    base_metrics = _extract_comparable_metrics(baseline)
    cand_metrics = _extract_comparable_metrics(candidate)
    rows: list[dict[str, Any]] = []
    for name in sorted(set(base_metrics) | set(cand_metrics)):
        b = base_metrics.get(name) or {}
        c = cand_metrics.get(name) or {}
        b_ms = b.get("wall_ms")
        c_ms = c.get("wall_ms")
        delta_ms = round(c_ms - b_ms, 3) if (b_ms is not None and c_ms is not None) else None
        delta_pct = round(((c_ms - b_ms) / b_ms) * 100.0, 2) if (b_ms is not None and c_ms is not None and b_ms > 0) else None
        b_sql = b.get("sql_count")
        c_sql = c.get("sql_count")
        delta_sql = (c_sql - b_sql) if (b_sql is not None and c_sql is not None) else None
        rows.append(
            {
                "benchmark": name,
                "baseline_ms": b_ms,
                "candidate_ms": c_ms,
                "delta_ms": delta_ms,
                "delta_pct": delta_pct,
                "baseline_sql": b_sql,
                "candidate_sql": c_sql,
                "delta_sql": delta_sql,
                "baseline_rss_delta_mib": b.get("rss_delta_mib"),
                "candidate_rss_delta_mib": c.get("rss_delta_mib"),
            }
        )
    return rows


def _fmt_num(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _fmt_delta(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    if isinstance(value, float):
        return f"{sign}{value:.2f}{suffix}"
    return f"{sign}{value}{suffix}"


def render_comparison_markdown(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    rows = compare_benchmarks(baseline, candidate)
    lines = [
        "| Benchmark | Baseline (ms) | Candidate (ms) | Delta (ms) | Delta (%) | SQL (base -> cand) | RSS Delta MiB (base -> cand) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        sql_col = (
            f"{row['baseline_sql']} -> {row['candidate_sql']}"
            if row["baseline_sql"] is not None or row["candidate_sql"] is not None
            else "-"
        )
        rss_col = (
            f"{_fmt_num(row['baseline_rss_delta_mib'])} -> {_fmt_num(row['candidate_rss_delta_mib'])}"
            if row["baseline_rss_delta_mib"] is not None or row["candidate_rss_delta_mib"] is not None
            else "-"
        )
        lines.append(
            f"| {row['benchmark']} | {_fmt_num(row['baseline_ms'])} | {_fmt_num(row['candidate_ms'])} "
            f"| {_fmt_delta(row['delta_ms'])} | {_fmt_delta(row['delta_pct'], '%')} | {sql_col} | {rss_col} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live inner-backend execution (inside khitmee-perf-backend-1 only)
# ---------------------------------------------------------------------------


def _connect_frappe(site: str | None = None) -> tuple[Any, str]:
    import frappe

    if not site:
        configs = sorted(Path.cwd().glob("*/site_config.json"))
        if len(configs) != 1:
            raise SafetyGuardError(f"expected exactly one site below {Path.cwd()}, found {len(configs)}")
        site = configs[0].parent.name
    validate_target_site(site)
    frappe.init(site=site, sites_path=str(Path.cwd()))
    frappe.connect()
    try:
        validate_db_host(getattr(frappe.conf, "db_host", None))
    except Exception:
        frappe.destroy()
        raise
    frappe.set_user("Administrator")
    return frappe, site


def _resolve_warehouse(frappe: Any, logical_name: str) -> str:
    exact = frappe.db.get_value("Warehouse", {"name": logical_name}, "name")
    if exact:
        return str(exact)
    by_label = frappe.db.get_value("Warehouse", {"warehouse_name": logical_name}, "name")
    if by_label:
        return str(by_label)
    rows = frappe.db.sql(
        "SELECT name FROM `tabWarehouse` WHERE is_group = 0 AND disabled = 0 ORDER BY name LIMIT 1"
    )
    if not rows:
        raise RuntimeError(f"no leaf warehouse found for {logical_name}")
    return str(rows[0][0])


def _profile_rollback_action(frappe: Any, savepoint: str, fn: Any, top_n: int = 8) -> dict[str, Any]:
    """Profile one write action under CommitGuard and roll back cleanly via frappe.db.rollback()."""
    _ = savepoint
    rss_before = current_rss_mib()
    profiler = cProfile.Profile()
    sql_prof = SqlProfiler(frappe)
    t0 = time.perf_counter()
    try:
        with sql_prof:
            profiler.enable()
            try:
                fn()
            finally:
                profiler.disable()
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        rss_after = current_rss_mib()
        rss_delta = round(rss_after - rss_before, 3) if (rss_before is not None and rss_after is not None) else None
        return {
            "wall_ms": wall_ms,
            "sql_count": sql_prof.count,
            "sql_ms": round(sql_prof.total_ms, 3),
            "top_sql_templates": sql_prof.top_templates(limit=top_n),
            "rss_before_mib": rss_before,
            "rss_after_mib": rss_after,
            "rss_delta_mib": rss_delta,
            "cprofile_top": extract_cprofile_hotspots(profiler, limit=top_n, sort_by="cumtime"),
            "cprofile_self_top": extract_cprofile_hotspots(profiler, limit=top_n, sort_by="tottime"),
        }
    finally:
        frappe.db.rollback()
        cache = getattr(getattr(frappe, "local", None), "document_cache", None)
        if hasattr(cache, "clear"):
            cache.clear()


def run_inside_backend(args: argparse.Namespace) -> int:
    frappe = None
    try:
        frappe, site = _connect_frappe(args.site)
        import khitmee_ops.api.search as search_mod
        import khitmee_ops.api.vouchers as vouchers_mod
        from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import execute as fast_gp_execute

        shop1 = _resolve_warehouse(frappe, "Shop 1")
        shop2 = _resolve_warehouse(frappe, "Shop 2")
        dist1 = _resolve_warehouse(frappe, "Dist Hub 1")
        company = str(frappe.db.get_value("Warehouse", shop1, "company") or "Khit Me Distribution")
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

        benchmarks: dict[str, Any] = {}
        with CommitGuard(frappe):
            for line_count in SALE_LINE_COUNTS:
                payload = build_sale_payload(
                    items_pool,
                    line_count,
                    shop1,
                    customer,
                    price_list,
                    payment_mode=payment_mode,
                    seed=args.seed,
                )
                cid = f"bench-sale-{line_count}-{args.seed}"
                benchmarks[f"sale_{line_count}_lines"] = _profile_rollback_action(
                    frappe,
                    f"bench_sale_{line_count}",
                    lambda p=payload, c=cid: vouchers_mod.submit_sale(
                        c,
                        p["warehouse"],
                        p["customer"],
                        p["price_list"],
                        json.dumps(p["items"]),
                        payment_mode=p["payment_mode"],
                        printer_type="voucher",
                    ),
                )

            transfer_payload = build_transfer_payload(
                items_pool,
                TRANSFER_LINE_COUNT,
                dist1,
                shop2 if shop2 != dist1 else shop1,
                seed=args.seed,
            )
            tid = f"bench-transfer-{TRANSFER_LINE_COUNT}-{args.seed}"
            benchmarks[f"transfer_{TRANSFER_LINE_COUNT}_lines"] = _profile_rollback_action(
                frappe,
                "bench_transfer_50",
                lambda p=transfer_payload, c=tid: vouchers_mod.submit_transfer(
                    c,
                    p["source"],
                    p["target"],
                    "direct",
                    outgoing_stock_entry=None,
                    items=json.dumps(p["items"]),
                    printer_type="voucher",
                ),
            )

            # Warm search_items latency
            sample_term = str(items_pool[0]["item_code"])[:6] if items_pool else "KM-"
            search_mod.search_items(q=sample_term, warehouse=shop1, price_list=price_list, limit=15)
            search_samples: list[float] = []
            rss_before_search = current_rss_mib()
            search_profiler = cProfile.Profile()
            search_sql_prof = SqlProfiler(frappe)
            with search_sql_prof:
                search_profiler.enable()
                try:
                    for _ in range(args.search_iterations):
                        t0 = time.perf_counter()
                        search_mod.search_items(q=sample_term, warehouse=shop1, price_list=price_list, limit=15)
                        search_samples.append((time.perf_counter() - t0) * 1000.0)
                finally:
                    search_profiler.disable()
            rss_after_search = current_rss_mib()
            search_stats = compute_latency_stats(search_samples)
            iters = max(1, len(search_samples))
            search_stats["avg_sql_count"] = round(search_sql_prof.count / iters, 2)
            search_stats["avg_sql_ms"] = round(search_sql_prof.total_ms / iters, 3)
            search_stats["top_sql_templates"] = search_sql_prof.top_templates(limit=8)
            search_stats["cprofile_top"] = extract_cprofile_hotspots(search_profiler, limit=8, sort_by="cumtime")
            search_stats["cprofile_self_top"] = extract_cprofile_hotspots(search_profiler, limit=8, sort_by="tottime")
            search_stats["rss_delta_mib"] = (
                round(rss_after_search - rss_before_search, 3)
                if (rss_before_search is not None and rss_after_search is not None)
                else None
            )
            benchmarks["search_items_warm"] = search_stats

            # Fast Gross Profit: Warehouse, Monthly, Invoice-detail (first 50 rows)
            gp_cases = (
                ("fast_gp_warehouse", {"company": company, "from_date": args.from_date, "to_date": args.to_date, "group_by": "Warehouse"}),
                ("fast_gp_monthly", {"company": company, "from_date": args.from_date, "to_date": args.to_date, "group_by": "Monthly"}),
                (
                    "fast_gp_invoice_detail_50",
                    {
                        "company": company,
                        "from_date": args.from_date,
                        "to_date": args.to_date,
                        "warehouse": shop1,
                        "group_by": "Invoice",
                        "page_length": INVOICE_DETAIL_PAGE_LIMIT,
                    },
                ),
            )
            for label, flt in gp_cases:
                rss_before = current_rss_mib()
                gp_profiler = cProfile.Profile()
                sql_prof = SqlProfiler(frappe)
                t0 = time.perf_counter()
                with sql_prof:
                    gp_profiler.enable()
                    try:
                        gp_result = fast_gp_execute(frappe._dict(dict(flt)))
                        rows = gp_result[1] if isinstance(gp_result, (list, tuple)) and len(gp_result) >= 2 else []
                    finally:
                        gp_profiler.disable()
                wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                rss_after = current_rss_mib()
                if label == "fast_gp_invoice_detail_50":
                    rows = list(rows or [])[:INVOICE_DETAIL_PAGE_LIMIT]
                benchmarks[label] = {
                    "wall_ms": wall_ms,
                    "sql_count": sql_prof.count,
                    "sql_ms": round(sql_prof.total_ms, 3),
                    "top_sql_templates": sql_prof.top_templates(limit=8),
                    "cprofile_top": extract_cprofile_hotspots(gp_profiler, limit=8, sort_by="cumtime"),
                    "cprofile_self_top": extract_cprofile_hotspots(gp_profiler, limit=8, sort_by="tottime"),
                    "row_count": len(rows or []),
                    "rss_before_mib": rss_before,
                    "rss_after_mib": rss_after,
                    "rss_delta_mib": (
                        round(rss_after - rss_before, 3)
                        if (rss_before is not None and rss_after is not None)
                        else None
                    ),
                }

        frappe.db.rollback()
        result = {
            "schema_version": SCHEMA_VERSION,
            "project": PROJECT,
            "site": site,
            "company": company,
            "from_date": args.from_date,
            "to_date": args.to_date,
            "benchmarks": benchmarks,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        if frappe is not None:
            try:
                frappe.db.rollback()
            except Exception:
                pass
            frappe.destroy()


def host_exec(args: argparse.Namespace, argv: list[str]) -> int:
    backend = f"{PROJECT}-backend-1"
    try:
        project = subprocess.check_output(
            ["docker", "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.project" }}', backend],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        print(f"perf_bench_inner requires the isolated {backend} container", file=sys.stderr)
        return 3
    try:
        validate_target_project(project)
    except SafetyGuardError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    inner_argv: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in ("--output",):
            skip_next = True
            continue
        if token.startswith("--output="):
            continue
        inner_argv.append(token)

    command = build_exec_command(project, inner_argv)
    source = Path(__file__).read_bytes()
    proc = subprocess.run(command, input=source, stdout=subprocess.PIPE, stderr=None)
    if proc.returncode != 0:
        sys.stdout.buffer.write(proc.stdout)
        return proc.returncode
    if args.output:
        Path(args.output).write_bytes(proc.stdout)
    sys.stdout.buffer.write(proc.stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast rollback-safe inner-loop profiler for khitmee-perf")
    parser.add_argument("--site", default=None)
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument("--from-date", default="2026-10-27")
    parser.add_argument("--to-date", default="2026-11-26")
    parser.add_argument("--search-iterations", type=int, default=15)
    parser.add_argument("--output", default=None, help="write JSON benchmark result to file")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE_JSON", "CANDIDATE_JSON"), help="render Markdown delta table")
    parser.add_argument("--inside-backend", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    if args.compare:
        baseline = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        candidate = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        sys.stdout.write(render_comparison_markdown(baseline, candidate))
        return 0
    if args.inside_backend:
        return run_inside_backend(args)
    return host_exec(args, raw_argv)


if __name__ == "__main__":
    sys.exit(main())
