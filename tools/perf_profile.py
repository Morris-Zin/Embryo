#!/usr/bin/env python3
"""Compact read-only Frappe report/search profiler for khitmee-perf.

This file is importable on a plain host Python for unit tests.  Frappe is imported
lazily only when profiling is executed inside the benchmark backend container.
"""
import argparse
import cProfile
import importlib
import io
import json
import math
import pstats
import re
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_WAREHOUSE = "Shop 1 - KMD"
WAREHOUSE_LOGICAL_NAMES = ("Dist Hub 1", "Dist Hub 2", "Shop 1", "Shop 2", "Shop 3", "Shop 4")
DEFAULT_PRICE_LIST = "Standard Selling"
DEFAULT_COMPANY = None
DATE_30D_FROM = "2026-10-27"
DATE_1D_FROM = "2026-11-26"
DATE_TO = "2026-11-26"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def truncate_text(value, max_length=160):
    text = str(value or "")
    if max_length < 4:
        return text[:max_length]
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def normalize_sql_template(query, max_length=160):
    """Normalize whitespace and redact literal values before truncation.

    String literals and numeric literals become `?`; backtick-quoted identifiers stay
    intact because they are schema shape, not user data.  The bounded result is safe
    to aggregate and never carries query parameter values.
    """
    text = str(query or "")
    string_pattern = "'(?:''|\\\\.|[^'\\\\])*'|\"(?:\\\\.|[^\"\\\\])*\""
    text = re.sub(string_pattern, "?", text)
    text = re.sub(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])", "?", text)
    text = re.sub(r"\s+", " ", text).strip()
    return truncate_text(text, max_length)


def bytes_to_mib(value):
    return None if value is None else float(value) / (1024.0 * 1024.0)


def round_or_none(value, digits=3):
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(float(value), digits)


def read_proc_status(path="/proc/self/status"):
    result = {"vmrss_kb": None, "vmhwm_kb": None}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    result["vmrss_kb"] = parse_float(line)
                elif line.startswith("VmHWM:"):
                    result["vmhwm_kb"] = parse_float(line)
    except OSError:
        pass
    return result


def parse_float(value):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def proc_memory_snapshot():
    raw = read_proc_status()
    return {
        "vmrss_mib": None if raw["vmrss_kb"] is None else raw["vmrss_kb"] / 1024.0,
        "vmhwm_mib": None if raw["vmhwm_kb"] is None else raw["vmhwm_kb"] / 1024.0,
    }


def percentile_nearest_rank(values, percentile):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(len(ordered) * percentile / 100.0))
    return ordered[rank - 1]


def summarize_timings(values):
    values = [float(value) for value in values]
    if not values:
        return {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "count": len(values),
        "avg_ms": round_or_none(sum(values) / len(values)),
        "p95_ms": round_or_none(percentile_nearest_rank(values, 95)),
        "max_ms": round_or_none(max(values)),
    }


def short_location(filename, line, function_name):
    parts = str(filename).replace("\\", "/").split("/")
    short = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return truncate_text(f"{short}:{line}({function_name})", 120)


def cprofile_top_functions(profile, limit=10):
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream)
    rows = []
    for (filename, line, function_name), values in stats.stats.items():
        primitive_calls, total_calls, self_seconds, cumulative_seconds, _callers = values
        rows.append({
            "function": short_location(filename, line, function_name),
            "calls": int(total_calls),
            "primitive_calls": int(primitive_calls),
            "self_ms": round_or_none(self_seconds * 1000.0),
            "cumulative_ms": round_or_none(cumulative_seconds * 1000.0),
        })
    rows.sort(key=lambda row: row["cumulative_ms"] or 0.0, reverse=True)
    return rows[:limit]


def allocation_sites(snapshot, limit=5):
    rows = []
    for stat in snapshot.statistics("lineno")[:limit]:
        rows.append({
            "site": truncate_text(str(stat.traceback[0]), 120),
            "size_mib": round_or_none(bytes_to_mib(stat.size)),
            "count": stat.count,
        })
    return rows


class SqlMonitor:
    """Bounded MariaDB query telemetry through a frappe.db.sql monkey-patch."""

    def __init__(self, db):
        self.db = db
        self.count = 0
        self.total_ms = 0.0
        self.templates = {}
        self._restore = None

    def record(self, query, elapsed_ms):
        self.count += 1
        self.total_ms += elapsed_ms
        template = normalize_sql_template(query)
        row = self.templates.setdefault(template, {"template": template, "count": 0, "total_ms": 0.0, "max_ms": 0.0})
        row["count"] += 1
        row["total_ms"] += elapsed_ms
        row["max_ms"] = max(row["max_ms"], elapsed_ms)

    def __enter__(self):
        original = getattr(self.db, "sql")

        def instance_wrapper(query, *args, **kwargs):
            start = time.perf_counter()
            try:
                return original(query, *args, **kwargs)
            finally:
                self.record(query, (time.perf_counter() - start) * 1000.0)

        try:
            setattr(self.db, "sql", instance_wrapper)
        except (AttributeError, TypeError):
            cls = type(self.db)
            class_original = cls.sql

            def class_wrapper(instance, query, *args, **kwargs):
                if instance is not self.db:
                    return class_original(instance, query, *args, **kwargs)
                start = time.perf_counter()
                try:
                    return original(query, *args, **kwargs)
                finally:
                    self.record(query, (time.perf_counter() - start) * 1000.0)

            setattr(cls, "sql", class_wrapper)
            self._restore = lambda: setattr(cls, "sql", class_original)
        else:
            self._restore = lambda: setattr(self.db, "sql", original)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._restore:
            self._restore()
        return False

    def summary(self):
        templates = list(self.templates.values())
        for row in templates:
            row["total_ms"] = round_or_none(row["total_ms"])
            row["max_ms"] = round_or_none(row["max_ms"])
        templates.sort(key=lambda row: (row["total_ms"] or 0.0, row["max_ms"] or 0.0), reverse=True)
        return {
            "sql_count": self.count,
            "sql_ms": round_or_none(self.total_ms),
            "top_sql_templates": templates[:5],
        }


def memory_delta(before, after, traced_peak_bytes, snapshot):
    before_rss = before.get("vmrss_mib")
    after_rss = after.get("vmrss_mib")
    observed = [value for value in (before_rss, after_rss) if value is not None]
    return {
        "vmrss_before_mib": round_or_none(before_rss),
        "vmrss_after_mib": round_or_none(after_rss),
        "vmrss_delta_mib": round_or_none(after_rss - before_rss) if before_rss is not None and after_rss is not None else None,
        "vmrss_peak_mib": round_or_none(max(observed)) if observed else None,
        "vmhwm_before_mib": round_or_none(before.get("vmhwm_mib")),
        "vmhwm_after_mib": round_or_none(after.get("vmhwm_mib")),
        "vmrss_peak_note": "vmrss_peak_mib is the max of before/after samples; vmhwm_after_mib is the kernel high-water mark.",
        "tracemalloc_peak_mib": round_or_none(bytes_to_mib(traced_peak_bytes)),
        "top_allocation_sites": allocation_sites(snapshot, 5),
    }


def measure_uninstrumented_wall(fn, runs=2):
    """Measure plain report execution without SQL monkey-patch, cProfile or tracemalloc.

    The first execution is cold relative to this probe and the second is the warm
    repeat.  No profiler or allocator tracing is active in either timing.
    """
    timings = []
    value = None
    for _ in range(max(2, int(runs))):
        start = time.perf_counter()
        value = fn()
        timings.append((time.perf_counter() - start) * 1000.0)
    return value, {
        "cold_first_ms": round_or_none(timings[0]),
        "warm_second_ms": round_or_none(timings[1]),
        "runs_ms": [round_or_none(value) for value in timings],
        "instrumentation": "none",
    }


def measure_call(fn, frappe, use_cprofile=True):
    before = proc_memory_snapshot()
    tracemalloc.start()
    profiler = cProfile.Profile() if use_cprofile else None
    wall_start = time.perf_counter()
    with SqlMonitor(frappe.db) as monitor:
        if profiler:
            profiler.enable()
        try:
            value = fn()
        finally:
            if profiler:
                profiler.disable()
    wall_ms = (time.perf_counter() - wall_start) * 1000.0
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    snapshot = tracemalloc.take_snapshot()
    tracemalloc.stop()
    after = proc_memory_snapshot()

    result = {
        "wall_ms": round_or_none(wall_ms),
        "memory": memory_delta(before, after, peak_bytes, snapshot),
        "tracemalloc_current_mib": round_or_none(bytes_to_mib(current_bytes)),
        "python_cpu_top10": cprofile_top_functions(profiler, 10) if profiler else [],
    }
    result.update(monitor.summary())
    return value, result


def extract_rows(result):
    if isinstance(result, dict) and isinstance(result.get("result"), list):
        return result["result"]
    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], list):
        return result[1]
    if isinstance(result, list):
        return result
    return []


def json_encode(payload, frappe):
    try:
        return frappe.as_json(payload)
    except (AttributeError, TypeError, ValueError):
        return json.dumps(payload, default=str, separators=(",", ":"))


def measure_json_payload(result, frappe):
    start = time.perf_counter()
    encoded = json_encode({"message": result}, frappe)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return {
        "rows": len(extract_rows(result)),
        "json_serialize_ms": round_or_none(elapsed_ms),
        "payload_bytes": len(str(encoded).encode("utf-8")),
    }


def discover_site():
    configs = sorted(Path.cwd().glob("*/site_config.json"))
    if len(configs) != 1:
        raise RuntimeError(f"expected exactly one site_config.json below {Path.cwd()}, found {len(configs)}")
    return configs[0].parent.name


def connect_frappe(site=None):
    frappe = importlib.import_module("frappe")
    site = site or discover_site()
    lowered = site.lower()
    if lowered == "khitmee.localhost" or lowered.endswith(".khitmee.localhost"):
        raise RuntimeError(f"refusing live-stack site {site}")
    frappe.init(site=site, sites_path=str(Path.cwd()))
    frappe.connect()
    if getattr(frappe.conf, "db_host", None) != "db":
        frappe.destroy()
        raise RuntimeError("unexpected database host; perf_profile only runs in the isolated khitmee-perf backend")
    if hasattr(frappe, "set_user"):
        frappe.set_user("Administrator")
    frappe.db.sql("SET SESSION TRANSACTION READ ONLY")
    frappe.db.sql("START TRANSACTION READ ONLY")
    return frappe, site


def company_for_warehouse(frappe, warehouse, explicit=None):
    if explicit:
        return explicit
    company = frappe.get_value("Warehouse", warehouse, "company")
    if not company:
        raise RuntimeError(f"could not determine company for warehouse {warehouse}")
    return company


def run_query_report(frappe, report_name, filters):
    module = importlib.import_module("frappe.desk.query_report")
    return module.run(report_name, filters=filters, ignore_prepared_report=True)


def preload_report_modules():
    for name in (
        "frappe.desk.query_report",
        "erpnext.stock.report.stock_balance.stock_balance",
        "khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit",
    ):
        try:
            importlib.import_module(name)
        except ImportError:
            # query_report.run produces the authoritative error if the report is absent.
            pass


def profile_one_report(frappe, report_name, filters):
    def invoke():
        return run_query_report(frappe, report_name, dict(filters))
    raw, uninstrumented = measure_uninstrumented_wall(invoke)
    _instrumented_raw, telemetry = measure_call(invoke, frappe, use_cprofile=True)
    telemetry["uninstrumented_wall"] = uninstrumented
    telemetry.update(measure_json_payload(raw, frappe))
    telemetry["report_name"] = report_name
    telemetry["filters"] = {key: str(value) for key, value in filters.items() if key in {"company", "warehouse", "from_date", "to_date", "group_by"}}
    return telemetry


def resolve_warehouse(frappe, warehouse):
    """Resolve seeded logical names such as 'Shop 1' to ERPNext's suffixed names."""
    if frappe.db.exists("Warehouse", warehouse):
        return warehouse
    rows = frappe.get_list(
        "Warehouse",
        filters={"warehouse_name": warehouse, "disabled": 0},
        pluck="name",
        limit_page_length=5,
    )
    if len(rows) == 1:
        return rows[0]
    rows = frappe.get_list(
        "Warehouse",
        filters={"name": ["like", warehouse + " - %"], "disabled": 0},
        pluck="name",
        limit_page_length=5,
    )
    if len(rows) == 1:
        return rows[0]
    raise RuntimeError(f"could not resolve a unique enabled warehouse for logical name {warehouse!r}")


def warehouse_key(logical_name):
    return re.sub(r"[^a-z0-9]+", "_", logical_name.lower()).strip("_")


def profile_warehouse_sweep(frappe, args):
    """Measure the 30d report tail across every seeded warehouse."""
    sweep = {}
    for logical in WAREHOUSE_LOGICAL_NAMES:
        warehouse = resolve_warehouse(frappe, logical)
        company = company_for_warehouse(frappe, warehouse, args.company)
        base_filters = {
            "company": company,
            "warehouse": warehouse,
            "from_date": args.from_date_30d,
            "to_date": args.to_date,
        }
        prefix = warehouse_key(logical)
        sweep[f"{prefix}_stock_balance_30d"] = profile_one_report(frappe, "Stock Balance", base_filters)
        for group_by in ("Invoice", "Item Code"):
            fast_filters = dict(base_filters)
            fast_filters["group_by"] = group_by
            key = f"{prefix}_fast_gross_profit_{group_by.lower().replace(' ', '_')}_30d"
            sweep[key] = profile_one_report(frappe, "Fast Gross Profit", fast_filters)
    return sweep


def profile_reports(frappe, args):
    preload_report_modules()
    company = company_for_warehouse(frappe, args.warehouse, args.company)
    windows = {
        "30d": (args.from_date_30d, args.to_date),
        "1d": (args.from_date_1d, args.to_date),
    }
    output = {}
    for window, (from_date, to_date) in windows.items():
        stock_filters = {
            "company": company,
            "warehouse": args.warehouse,
            "from_date": from_date,
            "to_date": to_date,
        }
        output[f"stock_balance_{window}"] = profile_one_report(frappe, "Stock Balance", stock_filters)
        for group_by in ("Invoice", "Item Code"):
            fast_filters = dict(stock_filters)
            fast_filters["group_by"] = group_by
            key = f"fast_gross_profit_{group_by.lower().replace(' ', '_')}_{window}"
            output[key] = profile_one_report(frappe, "Fast Gross Profit", fast_filters)
    if args.warehouse_sweep:
        output["warehouse_sweep_30d"] = profile_warehouse_sweep(frappe, args)
    return output


def discover_search_terms(frappe, count=10):
    rows = frappe.db.sql(
        "SELECT item_code FROM `tabItem` WHERE disabled = 0 ORDER BY item_code LIMIT " + str(int(count)),
        as_list=True,
    )
    terms = [str(row[0]) for row in rows if row and row[0]]
    fallback = ["KM-00001", "KM-00002", "KM-00003", "KM-00004", "KM-00005",
                "KM-00006", "KM-00007", "KM-00008", "KM-00009", "KM-00010"]
    for term in fallback:
        if len(terms) >= count:
            break
        if term not in terms:
            terms.append(term)
    return terms[:count]


def profile_search(frappe, args):
    search_module = importlib.import_module("khitmee_ops.api.search")
    if args.search_terms:
        terms = [term.strip() for term in args.search_terms.split(",") if term.strip()][:10]
    else:
        terms = discover_search_terms(frappe, 10)
    timings = []
    result_counts = []

    def batch():
        for term in terms:
            start = time.perf_counter()
            value = search_module.search_items(
                q=term,
                warehouse=args.warehouse,
                price_list=args.price_list,
                limit=15,
            )
            timings.append((time.perf_counter() - start) * 1000.0)
            result_counts.append(len(value.get("items", [])) if isinstance(value, dict) else 0)

    _value, telemetry = measure_call(batch, frappe, use_cprofile=True)
    summary = summarize_timings(timings)
    telemetry.update(summary)
    telemetry["terms"] = terms
    telemetry["term_count"] = len(terms)
    telemetry["result_rows_total"] = sum(result_counts)
    return telemetry


def format_human_summary(result):
    lines = [
        "perf_profile mode={} site={} read_only={} warehouse={}".format(
            result.get("mode"), result.get("site"), str(result.get("read_only")), result.get("warehouse")
        )
    ]
    report_rows = []
    for name, row in (result.get("reports") or {}).items():
        if isinstance(row, dict) and "wall_ms" in row:
            report_rows.append((name, row))
        elif isinstance(row, dict):
            report_rows.extend((f"{name}/{child_name}", child) for child_name, child in row.items())
    for name, row in report_rows:
        memory = row.get("memory", {})
        uninstrumented = row.get("uninstrumented_wall", {})
        lines.append(
            "{}: wall={:.1f}ms cold={}ms warm={}ms sql={:.1f}ms/{}q rows={} payload={}B rssΔ={}MiB trace={}MiB".format(
                name,
                row.get("wall_ms") or 0.0,
                uninstrumented.get("cold_first_ms") if uninstrumented.get("cold_first_ms") is not None else "N/A",
                uninstrumented.get("warm_second_ms") if uninstrumented.get("warm_second_ms") is not None else "N/A",
                row.get("sql_ms") or 0.0,
                row.get("sql_count") or 0,
                row.get("rows") if row.get("rows") is not None else "N/A",
                row.get("payload_bytes") if row.get("payload_bytes") is not None else "N/A",
                memory.get("vmrss_delta_mib") if memory.get("vmrss_delta_mib") is not None else "N/A",
                memory.get("tracemalloc_peak_mib") if memory.get("tracemalloc_peak_mib") is not None else "N/A",
            )
        )
    search = result.get("search")
    if search:
        memory = search.get("memory", {})
        lines.append(
            "search: avg={}ms p95={}ms sql={}q trace={}MiB terms={}".format(
                search.get("avg_ms"), search.get("p95_ms"), search.get("sql_count"),
                memory.get("tracemalloc_peak_mib"), search.get("term_count"),
            )
        )
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description="Compact read-only khitmee-perf report/search profiler")
    parser.add_argument("--mode", choices=("all", "reports", "report-memory", "search"), default="all")
    parser.add_argument("--json-out", default=None, help="write structured JSON here; '-' writes JSON to stdout and human summary to stderr")
    parser.add_argument("--site", default=None)
    parser.add_argument("--warehouse", default=DEFAULT_WAREHOUSE)
    parser.add_argument("--company", default=DEFAULT_COMPANY)
    parser.add_argument("--price-list", default=DEFAULT_PRICE_LIST)
    parser.add_argument("--search-terms", default=None, help="optional comma-separated override for exactly reproducible search terms")
    parser.add_argument("--warehouse-sweep", dest="warehouse_sweep", action="store_true", default=True,
                        help="sweep all six seeded warehouses for 30d Stock Balance and Fast Gross Profit (default)")
    parser.add_argument("--no-warehouse-sweep", dest="warehouse_sweep", action="store_false",
                        help="only profile --warehouse")
    parser.add_argument("--from-date-30d", default=DATE_30D_FROM)
    parser.add_argument("--from-date-1d", default=DATE_1D_FROM)
    parser.add_argument("--to-date", default=DATE_TO)
    return parser


def write_json_output(path, payload):
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path == "-":
        sys.stdout.write(text)
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")


def run(args):
    frappe = None
    site = args.site
    try:
        frappe, site = connect_frappe(site)
        result = {
            "schema_version": 1,
            "generated_at": utc_now(),
            "mode": args.mode,
            "site": site,
            "warehouse": args.warehouse,
            "company": company_for_warehouse(frappe, args.warehouse, args.company),
            "read_only": True,
            "date_windows": {
                "30d": {"from_date": args.from_date_30d, "to_date": args.to_date},
                "1d": {"from_date": args.from_date_1d, "to_date": args.to_date},
            },
            "notes": [
                "All work ran inside a READ ONLY transaction; no write mode is provided.",
                "End-to-end HTTP network latency is intentionally absent because this is an in-process backend profile.",
            ],
        }
        if args.mode in {"all", "reports", "report-memory"}:
            result["reports"] = profile_reports(frappe, args)
        if args.mode in {"all", "search"}:
            result["search"] = profile_search(frappe, args)
        frappe.db.rollback()
        return result
    finally:
        if frappe is not None:
            try:
                frappe.db.rollback()
            except Exception:
                pass
            frappe.destroy()


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = run(args)
    human = format_human_summary(result)
    if args.json_out == "-":
        print(human, file=sys.stderr)
        write_json_output("-", result)
    else:
        print(human)
        if args.json_out:
            write_json_output(args.json_out, result)
            print(f"json: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
