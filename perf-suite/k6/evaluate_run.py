#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SEARCH_P95_THRESHOLD_MS = 300.0
SUBMIT_P95_THRESHOLD_MS = 1500.0
REPORT_P95_THRESHOLD_MS = 5000.0
HTTP_ERROR_RATE_THRESHOLD = 0.005
DB_CGROUP_HEADROOM_CAP_PERCENT = 85.0
BACKEND_CGROUP_HEADROOM_CAP_PERCENT = 75.0
FRONTEND_CGROUP_HEADROOM_CAP_PERCENT = 85.0
CGROUP_HEADROOM_CAPS = {
    "db": DB_CGROUP_HEADROOM_CAP_PERCENT,
    "backend": BACKEND_CGROUP_HEADROOM_CAP_PERCENT,
    "frontend": FRONTEND_CGROUP_HEADROOM_CAP_PERCENT,
}
REQUIRED_STRICT_REPORTS = ("Stock Balance", "Fast Gross Profit")
GIB = 1024 ** 3


@dataclass
class CheckResult:
    name: str
    observed: str
    threshold: str
    status: str
    detail: str = ""


@dataclass
class ContainerMemory:
    name: str
    peak_rss_bytes: float | None = None
    peak_pss_bytes: float | None = None
    peak_cgroup_bytes: float | None = None
    peak_kernel_memory_peak_bytes: float | None = None
    peak_cgroup_oom_events: float | None = None
    peak_swap_bytes: float | None = None
    peak_cgroup_percent: float | None = None
    cgroup_percent_evidence_missing: bool = False
    peak_oom_kills: float = 0.0
    memory_limit_bytes: float | None = None
    oom_evidence_missing: bool = False
    docker_oom_killed: bool = False
    docker_evidence_missing: bool = False
    sample_count: int = 0
    min_restart_count: float | None = None
    max_restart_count: float = 0.0
    observed_pids: set[str] = field(default_factory=set)
    restart_evidence_missing: bool = False


@dataclass
class MemoryAnalysis:
    row_count: int
    containers: dict[str, ContainerMemory] = field(default_factory=dict)
    fleet_peak_rss_bytes: float | None = None
    fleet_peak_pss_bytes: float | None = None
    total_oom_kills: float = 0.0
    error: str = ""


@dataclass
class Evaluation:
    verdict: str
    markdown: str
    checks: list[CheckResult]
    memory: MemoryAnalysis


def canonical_key(key):
    return re.sub(r"[^a-z0-9]+", "", str(key or "").lower())


def first_value(row, *names):
    for name in names:
        key = canonical_key(name)
        if key in row and row[key] not in (None, ""):
            return row[key], key
    return None, ""


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"true", "yes", "y"}:
        return 1.0
    if text.lower() in {"false", "no", "n"}:
        return 0.0
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    return float(match.group(0))


def parse_bytes(value, unit_hint="b"):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        unit = unit_hint.lower()
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.split("/")[0].strip() if "/" in text else text
        match = re.match(r"^([-+]?\d+(?:\.\d+)?)\s*([kmgtp]?i?b?|bytes?)?\s*$", text, re.IGNORECASE)
        if not match:
            return None
        number = float(match.group(1))
        unit = (match.group(2) or unit_hint or "b").lower()
    multipliers = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024 ** 2,
        "mb": 1024 ** 2,
        "mib": 1024 ** 2,
        "g": 1024 ** 3,
        "gb": 1024 ** 3,
        "gib": 1024 ** 3,
        "t": 1024 ** 4,
        "tb": 1024 ** 4,
        "tib": 1024 ** 4,
    }
    return number * multipliers.get(unit, 1)


def parse_bytes_field(value, key):
    hint = "b"
    if key.endswith("mb"):
        hint = "mb"
    elif key.endswith("gb"):
        hint = "gb"
    elif key.endswith("kb"):
        hint = "kb"
    return parse_bytes(value, hint)


def parse_usage_pair(value):
    if value is None or not isinstance(value, str) or "/" not in value:
        return None, None
    left, right = value.split("/", 1)
    return parse_bytes(left), parse_bytes(right)


def read_stats_rows(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return [], "container stats CSV has no header"
            rows = []
            for raw in reader:
                rows.append({canonical_key(key): value for key, value in raw.items()})
            return rows, ""
    except FileNotFoundError:
        return [], f"container stats CSV not found: {path}"
    except OSError as exc:
        return [], f"container stats CSV could not be read: {exc}"


def max_optional(current, candidate):
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


def observed_restarts(entry):
    if entry.sample_count <= 1:
        return entry.max_restart_count
    delta = entry.max_restart_count - (entry.min_restart_count or 0.0)
    pid_changes = max(0.0, float(len(entry.observed_pids) - 1))
    return max(delta, pid_changes)


def analyze_memory(stats_csv):
    rows, error = read_stats_rows(stats_csv)
    analysis = MemoryAnalysis(row_count=len(rows), error=error)
    if not rows:
        return analysis

    totals_by_timestamp = {}
    for row in rows:
        container, _ = first_value(row, "container", "container_name", "name", "service")
        container = str(container or "unknown")
        rss_raw, rss_key = first_value(
            row,
            "rss_bytes",
            "rss_mb",
            "rss_kb",
            "rss",
            "memory_rss_bytes",
            "memory_rss",
            "mem_rss_bytes",
            "mem_rss",
        )
        pss_raw, pss_key = first_value(
            row,
            "pss_bytes",
            "pss_mb",
            "pss_kb",
            "pss",
            "memory_pss_bytes",
            "memory_pss",
            "mem_pss_bytes",
            "mem_pss",
        )
        cgroup_raw, cgroup_key = first_value(row, "cgroup_memory_current_bytes", "memory_current_bytes")
        usage_raw, _ = first_value(row, "mem_usage", "memory_usage", "memory", "usage")
        limit_raw, limit_key = first_value(
            row,
            "cgroup_memory_max_bytes",
            "mem_limit_bytes",
            "memory_limit_bytes",
            "mem_limit",
            "memory_limit",
            "limit",
        )
        usage_bytes, pair_limit_bytes = parse_usage_pair(usage_raw)
        explicit_cgroup_bytes = parse_bytes_field(cgroup_raw, cgroup_key)
        explicit_limit_bytes = parse_bytes_field(limit_raw, limit_key)
        cgroup_bytes = explicit_cgroup_bytes if explicit_cgroup_bytes is not None else usage_bytes
        rss_bytes = parse_bytes_field(rss_raw, rss_key)
        if rss_bytes is None:
            rss_bytes = usage_bytes
        pss_bytes = parse_bytes_field(pss_raw, pss_key)
        limit_bytes = explicit_limit_bytes if explicit_limit_bytes is not None else pair_limit_bytes

        memory_peak_raw, memory_peak_key = first_value(
            row, "cgroup_memory_peak_bytes", "memory_peak_bytes", "kernel_memory_peak_bytes"
        )
        kernel_memory_peak_bytes = parse_bytes_field(memory_peak_raw, memory_peak_key)
        oom_events_raw, _ = first_value(row, "cgroup_oom_events", "oom_events")
        oom_events = parse_float(oom_events_raw)
        swap_raw, swap_key = first_value(row, "cgroup_swap_current_bytes", "memory_swap_current_bytes", "swap_current_bytes")
        swap_bytes = parse_bytes_field(swap_raw, swap_key)

        cgroup_percent = None
        if explicit_cgroup_bytes is not None and explicit_limit_bytes is not None and explicit_limit_bytes > 0:
            cgroup_percent = explicit_cgroup_bytes / explicit_limit_bytes * 100.0

        oom_raw, _ = first_value(row, "cgroup_oom_kill", "oom_kill", "oom_kills", "oom_killed", "oomkill", "oom")
        oom_kills = parse_float(oom_raw)
        docker_oom_raw, _ = first_value(row, "docker_oom_killed", "dockeroomkilled", "oomkilled")
        docker_oom_value = parse_float(docker_oom_raw)
        restart_raw, _ = first_value(row, "restart_count", "restarts", "container_restart_count", "restartcount")
        restart_value = parse_float(restart_raw)
        pid_raw, _ = first_value(row, "pid", "container_pid")
        pid_value = str(pid_raw).strip() if pid_raw is not None else ""
        timestamp, _ = first_value(row, "timestamp", "time", "ts", "sample_time")

        entry = analysis.containers.setdefault(container, ContainerMemory(name=container))
        entry.sample_count += 1
        entry.peak_rss_bytes = max_optional(entry.peak_rss_bytes, rss_bytes)
        entry.peak_pss_bytes = max_optional(entry.peak_pss_bytes, pss_bytes)
        entry.peak_cgroup_bytes = max_optional(entry.peak_cgroup_bytes, cgroup_bytes)
        entry.peak_kernel_memory_peak_bytes = max_optional(entry.peak_kernel_memory_peak_bytes, kernel_memory_peak_bytes)
        entry.peak_cgroup_oom_events = max_optional(entry.peak_cgroup_oom_events, oom_events)
        entry.peak_swap_bytes = max_optional(entry.peak_swap_bytes, swap_bytes)
        entry.peak_cgroup_percent = max_optional(entry.peak_cgroup_percent, cgroup_percent)
        if cgroup_percent is None:
            entry.cgroup_percent_evidence_missing = True
        entry.memory_limit_bytes = max_optional(entry.memory_limit_bytes, limit_bytes)
        if pid_value.isdigit() and int(pid_value) > 0:
            entry.observed_pids.add(pid_value)
        # Missing or unparsable ("NA") telemetry is evidence failure, never zero.
        if oom_kills is None:
            entry.oom_evidence_missing = True
        else:
            entry.peak_oom_kills = max(entry.peak_oom_kills, oom_kills)
        if docker_oom_value is None:
            entry.docker_evidence_missing = True
        elif docker_oom_value > 0:
            entry.docker_oom_killed = True
        if restart_value is None:
            entry.restart_evidence_missing = True
        else:
            entry.min_restart_count = (
                restart_value
                if entry.min_restart_count is None
                else min(entry.min_restart_count, restart_value)
            )
            entry.max_restart_count = max(entry.max_restart_count, restart_value)

        if timestamp:
            bucket = totals_by_timestamp.setdefault(str(timestamp), {"rss": 0.0, "pss": 0.0, "rss_seen": False, "pss_seen": False})
            if rss_bytes is not None:
                bucket["rss"] += rss_bytes
                bucket["rss_seen"] = True
            if pss_bytes is not None:
                bucket["pss"] += pss_bytes
                bucket["pss_seen"] = True

    if totals_by_timestamp:
        rss_totals = [bucket["rss"] for bucket in totals_by_timestamp.values() if bucket["rss_seen"]]
        pss_totals = [bucket["pss"] for bucket in totals_by_timestamp.values() if bucket["pss_seen"]]
        analysis.fleet_peak_rss_bytes = max(rss_totals) if rss_totals else None
        analysis.fleet_peak_pss_bytes = max(pss_totals) if pss_totals else None
    else:
        rss_values = [entry.peak_rss_bytes for entry in analysis.containers.values() if entry.peak_rss_bytes is not None]
        pss_values = [entry.peak_pss_bytes for entry in analysis.containers.values() if entry.peak_pss_bytes is not None]
        analysis.fleet_peak_rss_bytes = sum(rss_values) if rss_values else None
        analysis.fleet_peak_pss_bytes = sum(pss_values) if pss_values else None

    analysis.total_oom_kills = sum(entry.peak_oom_kills for entry in analysis.containers.values())
    return analysis


def service_role_for_container(container_name):
    tokens = [token for token in re.split(r"[^a-z0-9]+", str(container_name or "").lower()) if token]
    for service in CGROUP_HEADROOM_CAPS:
        if service in tokens:
            return service
    return ""


def service_headroom_evidence(memory):
    evidence = {
        service: {"peak_percent": None, "containers": [], "missing_evidence": False}
        for service in CGROUP_HEADROOM_CAPS
    }
    for entry in memory.containers.values():
        service = service_role_for_container(entry.name)
        if not service:
            continue
        item = evidence[service]
        item["containers"].append(entry.name)
        item["peak_percent"] = max_optional(item["peak_percent"], entry.peak_cgroup_percent)
        if entry.cgroup_percent_evidence_missing or entry.peak_cgroup_percent is None:
            item["missing_evidence"] = True
    return evidence


def load_k6_summary(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def metric_values(summary, metric_name):
    metrics = summary.get("metrics", summary)
    metric = metrics.get(metric_name) if isinstance(metrics, dict) else None
    if not isinstance(metric, dict):
        return None
    values = metric.get("values", metric)
    return values if isinstance(values, dict) else None


def numeric_metric(summary, metric_name, keys):
    values = metric_values(summary, metric_name)
    if values is None:
        return None
    for key in keys:
        if key in values:
            parsed = parse_float(values[key])
            if parsed is not None:
                return parsed
    return None


def metric_sample_count(summary, metric_name):
    if not metric_name:
        return None
    values = metric_values(summary, metric_name)
    if values is None:
        return None
    count = parse_float(values.get("count"))
    if count is not None:
        return count
    passes = parse_float(values.get("passes"))
    fails = parse_float(values.get("fails"))
    if passes is None and fails is None:
        return None
    return (passes or 0.0) + (fails or 0.0)


def parse_tagged_metric_key(metric_name):
    if not isinstance(metric_name, str) or "{" not in metric_name or not metric_name.endswith("}"):
        return "", {}
    base, tag_text = metric_name.split("{", 1)
    tags = {}
    for part in tag_text[:-1].split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, value = part.split(":", 1)
        elif "=" in part:
            key, value = part.split("=", 1)
        else:
            continue
        key = key.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            tags[key] = value
    return base, tags


def tagged_report_metric_names(summary, base_metric):
    metrics = summary.get("metrics", summary)
    if not isinstance(metrics, dict):
        return {}
    tagged = {}
    for metric_name in metrics:
        base, tags = parse_tagged_metric_key(metric_name)
        if base != base_metric:
            continue
        report = tags.get("report")
        if report:
            tagged.setdefault(report, metric_name)
    return tagged


def report_latency_breakdown(summary):
    end_to_end_metrics = tagged_report_metric_names(summary, "report_duration")
    server_metrics = tagged_report_metric_names(summary, "report_server_duration")
    rows = []
    for report in sorted(set(end_to_end_metrics) | set(server_metrics)):
        end_to_end_metric = end_to_end_metrics.get(report)
        server_metric = server_metrics.get(report)
        rows.append(
            {
                "report": report,
                "end_to_end_metric": end_to_end_metric,
                "server_metric": server_metric,
                "end_to_end_p95": numeric_metric(summary, end_to_end_metric, ["p(95)", "p95", "p95.0"])
                if end_to_end_metric
                else None,
                "end_to_end_count": metric_sample_count(summary, end_to_end_metric),
                "server_p95": numeric_metric(summary, server_metric, ["p(95)", "p95", "p95.0"])
                if server_metric
                else None,
                "server_count": metric_sample_count(summary, server_metric),
            }
        )
    return rows


def format_ms(value):
    return "no data" if value is None else f"{value:.1f} ms"


def format_n(value):
    return "no data" if value is None else str(int(value))


def format_ms_with_n(value, count):
    return f"{format_ms(value)} (n={format_n(count)})"


def format_rate(value):
    return "no data" if value is None else f"{value:.5f}"


def format_count(value):
    return "no data" if value is None else str(int(value))


def format_bytes(value):
    if value is None:
        return "no data"
    if value >= GIB:
        return f"{value / GIB:.2f} GiB"
    if value >= 1024 ** 2:
        return f"{value / (1024 ** 2):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value:.0f} B"


def format_percent(value):
    return "no data" if value is None else f"{value:.1f}%"


def format_mib(value):
    return "no data" if value is None else f"{value / (1024 ** 2):.1f}"


def markdown_escape(value):
    return str(value).replace("|", "\\|")


def add_threshold_check(
    checks,
    name,
    observed_value,
    observed_text,
    threshold_text,
    passes,
    missing_detail,
    missing_status="NO DATA",
    detail="",
):
    if observed_value is None:
        combined_detail = missing_detail if not detail else f"{missing_detail} {detail}"
        checks.append(CheckResult(name, observed_text, threshold_text, missing_status, combined_detail))
    elif passes:
        checks.append(CheckResult(name, observed_text, threshold_text, "PASS", detail))
    else:
        checks.append(CheckResult(name, observed_text, threshold_text, "FAIL", detail))


def server_duration_detail(summary, metric_name):
    server_p95 = numeric_metric(summary, metric_name, ["p(95)", "p95", "p95.0"])
    server_count = metric_sample_count(summary, metric_name)
    return f"Diagnostic only: {metric_name} p95 {format_ms(server_p95)} (server n={format_n(server_count)})."


def aggregate_latency_diagnostics(summary):
    rows = []
    for label, end_to_end_metric, server_metric in (
        ("Search", "search_duration", "search_server_duration"),
        ("Voucher submit", "submit_duration", "submit_server_duration"),
        ("Reports (aggregate)", "report_duration", "report_server_duration"),
    ):
        rows.append(
            {
                "label": label,
                "end_to_end_p95": numeric_metric(summary, end_to_end_metric, ["p(95)", "p95", "p95.0"]),
                "end_to_end_count": metric_sample_count(summary, end_to_end_metric),
                "server_p95": numeric_metric(summary, server_metric, ["p(95)", "p95", "p95.0"]),
                "server_count": metric_sample_count(summary, server_metric),
            }
        )
    return rows


def evaluate(summary, memory, run_label="", memory_budget_gb=None, require_all_metrics=True, strict=False):
    checks = []
    performance_missing_status = "NO DATA" if require_all_metrics else "SKIP"

    search_p95 = numeric_metric(summary, "search_duration", ["p(95)", "p95", "p95.0"])
    search_count = metric_sample_count(summary, "search_duration")
    add_threshold_check(
        checks,
        "Search end-to-end p95",
        search_p95,
        format_ms_with_n(search_p95, search_count),
        "< 300 ms",
        search_p95 is not None and search_p95 < SEARCH_P95_THRESHOLD_MS,
        "The k6 summary has no search_duration p(95) sample.",
        performance_missing_status,
        server_duration_detail(summary, "search_server_duration"),
    )

    submit_p95 = numeric_metric(summary, "submit_duration", ["p(95)", "p95", "p95.0"])
    submit_count = metric_sample_count(summary, "submit_duration")
    add_threshold_check(
        checks,
        "Voucher submit end-to-end p95",
        submit_p95,
        format_ms_with_n(submit_p95, submit_count),
        "< 1500 ms",
        submit_p95 is not None and submit_p95 < SUBMIT_P95_THRESHOLD_MS,
        "The k6 summary has no submit_duration p(95) sample.",
        performance_missing_status,
        server_duration_detail(summary, "submit_server_duration"),
    )

    report_p95 = numeric_metric(summary, "report_duration", ["p(95)", "p95", "p95.0"])
    report_count = metric_sample_count(summary, "report_duration")
    add_threshold_check(
        checks,
        "Report end-to-end p95",
        report_p95,
        format_ms_with_n(report_p95, report_count),
        "< 5000 ms",
        report_p95 is not None and report_p95 < REPORT_P95_THRESHOLD_MS,
        "The k6 summary has no report_duration p(95) sample; run report_stress for full evidence.",
        performance_missing_status,
        server_duration_detail(summary, "report_server_duration"),
    )

    failed_rate = numeric_metric(summary, "http_req_failed", ["rate", "value"])
    add_threshold_check(
        checks,
        "HTTP failure rate",
        failed_rate,
        format_rate(failed_rate),
        "< 0.005",
        failed_rate is not None and failed_rate < HTTP_ERROR_RATE_THRESHOLD,
        "The k6 summary has no http_req_failed rate.",
    )

    error_count = numeric_metric(summary, "errors", ["count", "value"])
    add_threshold_check(
        checks,
        "Logical / validation errors",
        error_count,
        format_count(error_count),
        "= 0",
        error_count is not None and error_count == 0,
        "The k6 summary has no errors counter.",
    )

    if memory.row_count:
        missing_oom = sorted(
            entry.name
            for entry in memory.containers.values()
            if entry.oom_evidence_missing or entry.docker_evidence_missing
        )
        docker_killed = sorted(entry.name for entry in memory.containers.values() if entry.docker_oom_killed)
        if missing_oom:
            checks.append(
                CheckResult(
                    "Container OOM kills",
                    "missing telemetry",
                    "= 0",
                    "FAIL",
                    "OOM evidence is missing or unparsable (NA) for containers: "
                    + ", ".join(missing_oom)
                    + ". Missing telemetry is never treated as zero.",
                )
            )
        elif docker_killed or memory.total_oom_kills > 0:
            detail_parts = []
            if memory.total_oom_kills > 0:
                detail_parts.append("Any OOM kill invalidates the memory-size experiment.")
            if docker_killed:
                detail_parts.append("Docker-reported OOM kills in containers: " + ", ".join(docker_killed) + ".")
            checks.append(
                CheckResult("Container OOM kills", str(int(memory.total_oom_kills)), "= 0", "FAIL", " ".join(detail_parts))
            )
        else:
            checks.append(CheckResult("Container OOM kills", "0", "= 0", "PASS"))

        missing_restarts = sorted(
            entry.name for entry in memory.containers.values() if entry.restart_evidence_missing
        )
        peak_restarts = max((observed_restarts(entry) for entry in memory.containers.values()), default=0.0)
        if missing_restarts:
            checks.append(
                CheckResult(
                    "Container restarts",
                    "missing telemetry",
                    "= 0",
                    "FAIL",
                    "restart_count telemetry is missing or unparsable (NA) for containers: "
                    + ", ".join(missing_restarts)
                    + ". Missing telemetry is never treated as zero.",
                )
            )
        elif peak_restarts > 0:
            checks.append(
                CheckResult(
                    "Container restarts",
                    str(int(peak_restarts)),
                    "= 0",
                    "FAIL",
                    "A container restart invalidates the memory-size experiment (crash or cgroup eviction).",
                )
            )
        else:
            checks.append(CheckResult("Container restarts", "0", "= 0", "PASS"))
    else:
        detail = memory.error or "No container statistics rows were parsed."
        checks.append(CheckResult("Container OOM kills", "no data", "= 0", "NO DATA", detail))
        checks.append(CheckResult("Container restarts", "no data", "= 0", "NO DATA", detail))

    headroom_evidence = service_headroom_evidence(memory)
    for service, cap_percent in CGROUP_HEADROOM_CAPS.items():
        evidence = headroom_evidence[service]
        peak_percent = evidence["peak_percent"]
        missing_evidence = not evidence["containers"] or evidence["missing_evidence"] or peak_percent is None
        threshold = f"< {cap_percent:.1f}%"
        check_name = f"{service} cgroup headroom"
        if strict:
            if missing_evidence:
                checks.append(
                    CheckResult(
                        check_name,
                        format_percent(peak_percent),
                        threshold,
                        "FAIL",
                        "Strict mode requires real cgroup_memory_current_bytes / cgroup_memory_max_bytes telemetry for "
                        f"{service}; missing or unparsable cgroup telemetry is a failure.",
                    )
                )
            elif peak_percent >= cap_percent:
                checks.append(
                    CheckResult(
                        check_name,
                        format_percent(peak_percent),
                        threshold,
                        "FAIL",
                        f"{service} peak cgroup usage leaves insufficient headroom under its container memory limit.",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        check_name,
                        format_percent(peak_percent),
                        threshold,
                        "PASS",
                        "True cgroup telemetry only; RSS is never used for this headroom gate.",
                    )
                )
        else:
            if missing_evidence:
                detail = (
                    f"Informational only: no complete cgroup current/max telemetry for {service}; "
                    "strict mode fails closed on this evidence gap."
                )
            elif peak_percent >= cap_percent:
                detail = "Informational only in non-strict mode; this row would FAIL under --strict."
            else:
                detail = "Informational only in non-strict mode; --strict enforces this headroom gate."
            checks.append(CheckResult(check_name, format_percent(peak_percent), threshold, "SKIP", detail))

    report_rows = report_latency_breakdown(summary)
    strict_report_evidence = strict and (metric_values(summary, "report_duration") is not None or require_all_metrics)
    report_threshold = "< 5000 ms (n >= 1)" if strict_report_evidence else "< 5000 ms"
    for row in report_rows:
        if not row["end_to_end_metric"]:
            continue
        tagged_report_p95 = row["end_to_end_p95"]
        sample_count = row["end_to_end_count"]
        count_passes = not strict_report_evidence or (sample_count is not None and sample_count >= 1)
        passes = tagged_report_p95 is not None and tagged_report_p95 < REPORT_P95_THRESHOLD_MS and count_passes
        strict_detail = (
            " Strict report evidence requires n >= 1 and p95 < 5000 ms for both Stock Balance and Fast Gross Profit."
            if strict_report_evidence
            else ""
        )
        add_threshold_check(
            checks,
            f"{row['report']} end-to-end p95",
            tagged_report_p95,
            format_ms_with_n(tagged_report_p95, sample_count),
            report_threshold,
            passes,
            f"The k6 summary has no p(95) sample for tagged report_duration metric {row['end_to_end_metric']}.{strict_detail}",
            detail=f"Diagnostic only: report_server_duration p95 {format_ms(row['server_p95'])} (server n={format_n(row['server_count'])}).{strict_detail}",
        )

    if strict_report_evidence:
        reports_with_end_to_end = {row["report"] for row in report_rows if row["end_to_end_metric"]}
        for report in REQUIRED_STRICT_REPORTS:
            if report in reports_with_end_to_end:
                continue
            checks.append(
                CheckResult(
                    f"{report} end-to-end p95",
                    "no data",
                    "< 5000 ms (n >= 1)",
                    "FAIL",
                    "Strict report evaluation requires positive tagged report_duration samples (n >= 1) "
                    "for both Stock Balance and Fast Gross Profit.",
                )
            )

    verdict = "GO" if all(check.status in {"PASS", "SKIP"} for check in checks) else "NO-GO"
    markdown = render_markdown(summary, memory, checks, verdict, run_label, memory_budget_gb)
    return Evaluation(verdict=verdict, markdown=markdown, checks=checks, memory=memory)


def render_markdown(summary, memory, checks, verdict, run_label="", memory_budget_gb=None):
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Khitmee ERPNext 2 GB vs 4 GB Go / No-Go Scorecard",
        "",
        f"Generated: {generated}",
    ]
    if run_label:
        lines.append(f"Run: `{markdown_escape(run_label)}`")
    if memory_budget_gb:
        lines.append(f"Configured memory budget for this run: {memory_budget_gb:g} GiB")
    lines.extend([
        "",
        f"## Verdict: **{verdict}**",
        "",
    ])
    if verdict == "GO":
        if any(check.status == "SKIP" for check in checks):
            lines.append("All applicable latency, correctness, HTTP-error, and OOM-kill checks passed. SKIP rows are outside this scenario and are not evidence for those capabilities.")
        else:
            lines.append("All required latency, correctness, HTTP-error, and OOM-kill checks passed. This run is acceptable evidence for the tested memory configuration.")
    else:
        lines.append("This run is not acceptable go-live evidence. Every FAIL must be fixed; every NO DATA row means the evidence is incomplete rather than passed.")
    lines.extend([
        "",
        "## Pass/fail checks",
        "",
        "| Check | Observed | Threshold | Result | Notes |",
        "|---|---:|---:|:---:|---|",
    ])
    for check in checks:
        lines.append(
            f"| {markdown_escape(check.name)} | {markdown_escape(check.observed)} | {markdown_escape(check.threshold)} | **{check.status}** | {markdown_escape(check.detail)} |"
        )

    report_rows = report_latency_breakdown(summary)
    if report_rows:
        lines.extend([
            "",
            "## Per-report latency breakdown",
            "",
            "| Report | End-to-end p95 | End-to-end n | Server p95 | Server n | 5 s gate |",
            "|---|---:|---:|---:|---:|:---:|",
        ])
        for row in report_rows:
            end_to_end_p95 = row["end_to_end_p95"]
            if end_to_end_p95 is None:
                gate = "NO DATA"
            else:
                gate = "PASS" if end_to_end_p95 < REPORT_P95_THRESHOLD_MS else "FAIL"
            lines.append(
                "| {} | {} | {} | {} | {} | **{}** |".format(
                    markdown_escape(row["report"]),
                    format_ms(end_to_end_p95),
                    format_n(row["end_to_end_count"]),
                    format_ms(row["server_p95"]),
                    format_n(row["server_count"]),
                    gate,
                )
            )

    lines.extend([
        "",
        "## Server-duration diagnostics",
        "",
        "Server-duration rows exclude the configured simulated Singapore RTT. They are diagnostic evidence only and do not alter the strict end-to-end threshold verdicts above.",
        "",
        "| Operation | End-to-end p95 | End-to-end n | Server p95 | Server n |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in aggregate_latency_diagnostics(summary):
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                markdown_escape(row["label"]),
                format_ms(row["end_to_end_p95"]),
                format_n(row["end_to_end_count"]),
                format_ms(row["server_p95"]),
                format_n(row["server_count"]),
            )
        )

    lines.extend([
        "",
        "## Container memory evidence",
        "",
    ])
    if memory.row_count:
        lines.extend([
            "| Container | Peak Cgroup (MiB) | Cgroup % | Peak RSS (MiB) | Peak PSS (MiB) | Limit (MiB) | memory.peak (MiB) | OOM Events | OOM Kills | Peak Swap (MiB) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        ordered = sorted(
            memory.containers.values(),
            key=lambda item: item.peak_cgroup_bytes if item.peak_cgroup_bytes is not None else (item.peak_rss_bytes or 0),
            reverse=True,
        )
        for container in ordered:
            lines.append(
                "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    markdown_escape(container.name),
                    format_mib(container.peak_cgroup_bytes),
                    format_percent(container.peak_cgroup_percent),
                    format_mib(container.peak_rss_bytes),
                    format_mib(container.peak_pss_bytes),
                    format_mib(container.memory_limit_bytes),
                    format_mib(container.peak_kernel_memory_peak_bytes),
                    format_n(container.peak_cgroup_oom_events),
                    int(container.peak_oom_kills),
                    format_mib(container.peak_swap_bytes),
                )
            )
        lines.extend([
            "",
            f"Fleet peak RSS: **{format_bytes(memory.fleet_peak_rss_bytes)}**",
            "",
            f"Fleet peak PSS: **{format_bytes(memory.fleet_peak_pss_bytes)}**",
        ])
    else:
        lines.append(f"No usable container memory samples were available. {markdown_escape(memory.error)}")

    evidence_bytes = memory.fleet_peak_pss_bytes if memory.fleet_peak_pss_bytes is not None else memory.fleet_peak_rss_bytes
    lines.extend([
        "",
        "## 2 GB / 4 GB memory-budget readout",
        "",
        "This table compares the observed fleet peak with both candidate budgets. It is a memory-headroom readout, not a substitute for rerunning the benchmark under each cgroup limit.",
        "",
        "| Candidate budget | Observed fleet peak | Headroom | Fits observed peak? |",
        "|---:|---:|---:|:---:|",
    ])
    for budget_gb in (2, 4):
        budget_bytes = budget_gb * GIB
        if evidence_bytes is None:
            headroom = "no data"
            fits = "NO DATA"
        else:
            headroom = format_bytes(budget_bytes - evidence_bytes) if evidence_bytes <= budget_bytes else f"-{format_bytes(evidence_bytes - budget_bytes)}"
            fits = "YES" if evidence_bytes < budget_bytes else "NO"
        lines.append(f"| {budget_gb} GiB | {format_bytes(evidence_bytes)} | {headroom} | {fits} |")

    sale_count = numeric_metric(summary, "sale_vouchers", ["count", "value"])
    transfer_count = numeric_metric(summary, "transfer_vouchers", ["count", "value"])
    lines.extend([
        "",
        "## Workload counters",
        "",
        f"- Successful sale vouchers: **{format_count(sale_count)}**",
        f"- Successful transfer vouchers: **{format_count(transfer_count)}**",
        "",
        "End-to-end durations include the configured simulated Singapore RTT. Companion `*_server_duration` trends in the k6 summary exclude that artificial RTT and are the right values for server-only comparisons.",
        "",
    ])
    return "\n".join(lines)


def evaluate_files(k6_summary_path, stats_csv_path, run_label="", memory_budget_gb=None, require_all_metrics=True, strict=False):
    summary = load_k6_summary(k6_summary_path)
    memory = analyze_memory(stats_csv_path)
    return evaluate(
        summary,
        memory,
        run_label=run_label,
        memory_budget_gb=memory_budget_gb,
        require_all_metrics=require_all_metrics,
        strict=strict,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the Khitmee k6/container Go-No-Go scorecard.")
    parser.add_argument("--k6-summary", required=True, help="Path to k6 --summary-export JSON")
    parser.add_argument("--stats-csv", required=True, help="Path to container stats CSV")
    parser.add_argument("--output", required=True, help="Markdown report output path")
    parser.add_argument("--run-label", default="", help="Human-readable run label")
    parser.add_argument("--memory-budget-gb", type=float, default=None, help="Memory budget configured for this run")
    parser.add_argument("--strict", action="store_true", help="Exit 1 unless the verdict is GO")
    parser.add_argument(
        "--allow-scenario-skips",
        action="store_true",
        help="Treat missing search/submit/report trends as SKIP for scenarios that do not exercise them",
    )
    args = parser.parse_args(argv)

    evaluation = evaluate_files(
        args.k6_summary,
        args.stats_csv,
        run_label=args.run_label,
        memory_budget_gb=args.memory_budget_gb,
        require_all_metrics=not args.allow_scenario_skips,
        strict=args.strict,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(evaluation.markdown, encoding="utf-8")
    print(f"Verdict: {evaluation.verdict}")
    print(f"Scorecard: {output}")
    return 0 if evaluation.verdict == "GO" or not args.strict else 1


if __name__ == "__main__":
    sys.exit(main())
