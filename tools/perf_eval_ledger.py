#!/usr/bin/env python3
"""Reproducible perf/memory evaluation ledger for khitmee-perf k6 artifacts.

Pure Python 3 standard library.  Missing evidence is represented as null/N/A;
this module never invents a zero, latency, or memory value.
"""
import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCENARIOS = {
    "sustained": "sustained",
    "burst-2x": "burst-2x",
    "reports-30d": "reports",
    "reports-1d": "reports-daily",
}

LATENCY_KEYS = (
    "search_p95_ms",
    "submit_p95_ms",
    "report_p95_ms",
    "stock_balance_p95_ms",
    "fast_gross_profit_p95_ms",
    "search_server_p95_ms",
    "submit_server_p95_ms",
    "report_server_p95_ms",
)
CORRECTNESS_KEYS = (
    "http_fail_rate",
    "errors",
    "oom_kills",
    "restarts",
    "search_samples",
    "submit_samples",
    "report_samples",
)
MEMORY_KEYS = (
    "fleet_peak_rss_mib",
    "fleet_peak_pss_mib",
    "backend_peak_rss_mib",
    "backend_peak_pss_mib",
    "backend_peak_cgroup_mib",
    "backend_peak_pct",
    "db_peak_rss_mib",
    "db_peak_pss_mib",
    "db_peak_cgroup_mib",
    "db_peak_pct",
    "frontend_peak_rss_mib",
    "frontend_peak_pss_mib",
    "frontend_peak_cgroup_mib",
    "frontend_peak_pct",
)
METRIC_KEYS = LATENCY_KEYS + CORRECTNESS_KEYS + MEMORY_KEYS
HEADROOM_THRESHOLDS = {"db": 85.0, "backend": 75.0, "frontend": 85.0}
REPORT_KEY_BY_NAME = {
    "Stock Balance": "stock_balance_p95_ms",
    "Fast Gross Profit": "fast_gross_profit_p95_ms",
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return None
        return float(value)
    text = str(value).strip()
    lowered = text.lower()
    if not text or lowered in {"no data", "n/a", "na", "null", "none", "missing telemetry"}:
        return None
    if lowered in {"true", "yes", "y"}:
        return 1.0
    if lowered in {"false", "no", "n"}:
        return 0.0
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group(0)) if match else None


def clean_cell(value):
    return str(value or "").strip().replace("**", "").replace("`", "").strip()


def split_markdown_row(line):
    text = line.strip()
    if not text.startswith("|") or not text.endswith("|"):
        return None
    cells = []
    buf = []
    escaped = False
    for char in text[1:-1]:
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append(clean_cell("".join(buf)))
            buf = []
        else:
            buf.append(char)
    cells.append(clean_cell("".join(buf)))
    return cells


def parse_size_mib(value):
    if value is None:
        return None
    text = clean_cell(value)
    match = re.match(r"^([-+]?\d+(?:\.\d+)?)\s*(gib|gb|mib|mb|kib|kb|b|bytes?)?$", text, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "mib").lower()
    if unit in {"gib", "gb"}:
        return number * 1024.0
    if unit in {"kib", "kb"}:
        return number / 1024.0
    if unit in {"b", "byte", "bytes"}:
        return number / (1024.0 * 1024.0)
    return number


def empty_metrics():
    return {key: None for key in METRIC_KEYS}


def observed_ms(value):
    return parse_float(value)


def parse_scorecard(path):
    """Parse one rendered scorecard without treating absent evidence as zero."""
    path = Path(path)
    result = {
        "path": str(path),
        "run_label": None,
        "verdict": None,
        "metrics": empty_metrics(),
        "headroom_checks": [],
    }
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        result["error"] = "missing"
        return result
    except OSError as exc:
        result["error"] = str(exc)
        return result

    check_names = {
        "Search end-to-end p95": "search_p95_ms",
        "Voucher submit end-to-end p95": "submit_p95_ms",
        "Report end-to-end p95": "report_p95_ms",
        "HTTP failure rate": "http_fail_rate",
        "Logical / validation errors": "errors",
        "Container OOM kills": "oom_kills",
        "Container restarts": "restarts",
        "Stock Balance end-to-end p95": "stock_balance_p95_ms",
        "Fast Gross Profit end-to-end p95": "fast_gross_profit_p95_ms",
        "Search server p95": "search_server_p95_ms",
        "Voucher submit server p95": "submit_server_p95_ms",
        "Report server p95": "report_server_p95_ms",
    }
    containers = {}
    in_memory_table = False
    memory_header = None

    for line in lines:
        verdict = re.match(r"^##\s+Verdict:\s*\*\*(GO|NO-GO)\*\*", line.strip())
        if verdict:
            result["verdict"] = verdict.group(1)
        run = re.match(r"^Run:\s*`([^`]+)`", line.strip())
        if run:
            result["run_label"] = run.group(1)

        fleet_rss = re.match(r"^Fleet peak RSS:\s*\*\*([^*]+)\*\*", line.strip())
        if fleet_rss:
            result["metrics"]["fleet_peak_rss_mib"] = parse_size_mib(fleet_rss.group(1))
        fleet_pss = re.match(r"^Fleet peak PSS:\s*\*\*([^*]+)\*\*", line.strip())
        if fleet_pss:
            result["metrics"]["fleet_peak_pss_mib"] = parse_size_mib(fleet_pss.group(1))

        cells = split_markdown_row(line)
        if cells and cells[0] == "Container" and len(cells) >= 6 and any("peak rss" in cell.lower() for cell in cells):
            in_memory_table = True
            memory_header = [cell.lower() for cell in cells]
            continue
        if in_memory_table and (not cells or not line.strip().startswith("|")):
            in_memory_table = False
        if not cells or len(cells) < 2 or cells[0].startswith("---"):
            continue

        name = cells[0]
        if name in check_names and len(cells) >= 2:
            result["metrics"][check_names[name]] = observed_ms(cells[1])
            continue
        if not in_memory_table and len(cells) >= 4 and "headroom" in name.lower():
            result["headroom_checks"].append({
                "name": name,
                "observed": cells[1],
                "threshold": cells[2],
                "status": cells[3],
                "notes": cells[4] if len(cells) >= 5 else "",
                "source": "scorecard",
            })
            continue
        if name in REPORT_KEY_BY_NAME and len(cells) >= 2 and "end-to-end" not in name.lower():
            # Per-report breakdown table row: report, end-to-end p95, server p95, gate.
            result["metrics"][REPORT_KEY_BY_NAME[name]] = observed_ms(cells[1])
            continue
        if in_memory_table and len(cells) >= 6:
            header = memory_header or []

            def column_index(*needles):
                for index, heading in enumerate(header):
                    if all(needle in heading for needle in needles):
                        return index
                return None

            rss_index = column_index("peak rss")
            pss_index = column_index("peak pss")
            cgroup_index = column_index("cgroup", "mib")
            pct_index = column_index("cgroup", "%")
            if pct_index is None:
                pct_index = column_index("peak cgroup memory")
            row = {
                "rss_mib": parse_size_mib(cells[rss_index]) if rss_index is not None and rss_index < len(cells) else None,
                "pss_mib": parse_size_mib(cells[pss_index]) if pss_index is not None and pss_index < len(cells) else None,
                "cgroup_mib": parse_size_mib(cells[cgroup_index]) if cgroup_index is not None and cgroup_index < len(cells) else None,
                "pct": parse_float(cells[pct_index]) if pct_index is not None and pct_index < len(cells) else None,
            }
            containers[name] = row


    for role in ("backend", "db", "frontend"):
        exact = f"khitmee-perf-{role}-1"
        row = containers.get(exact)
        if row is None:
            row = next((value for key, value in containers.items() if key.endswith(f"-{role}-1")), None)
        if row:
            result["metrics"][f"{role}_peak_rss_mib"] = row["rss_mib"]
            result["metrics"][f"{role}_peak_pss_mib"] = row["pss_mib"]
            result["metrics"][f"{role}_peak_cgroup_mib"] = row.get("cgroup_mib")
            result["metrics"][f"{role}_peak_pct"] = row["pct"]
    result["containers"] = containers
    return result


def metric_values(payload, metric_name):
    metrics = payload.get("metrics", payload) if isinstance(payload, dict) else {}
    metric = metrics.get(metric_name) if isinstance(metrics, dict) else None
    if not isinstance(metric, dict):
        return None
    values = metric.get("values", metric)
    return values if isinstance(values, dict) else None


def numeric_metric(payload, metric_name, keys=("p(95)", "p95", "p95.0")):
    values = metric_values(payload, metric_name)
    if values is None:
        return None
    for key in keys:
        if key in values:
            parsed = parse_float(values[key])
            if parsed is not None:
                return parsed
    return None


def parse_tagged_metric_key(metric_name):
    if not isinstance(metric_name, str) or "{" not in metric_name or not metric_name.endswith("}"):
        return "", {}
    base, tag_text = metric_name.split("{", 1)
    tags = {}
    for part in tag_text[:-1].split(","):
        if ":" in part:
            key, value = part.split(":", 1)
        elif "=" in part:
            key, value = part.split("=", 1)
        else:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        tags[key.strip().lower()] = value
    return base, tags


def parse_k6_summary_payload(payload):
    parsed = empty_metrics()
    parsed["search_p95_ms"] = numeric_metric(payload, "search_duration")
    parsed["submit_p95_ms"] = numeric_metric(payload, "submit_duration")
    parsed["report_p95_ms"] = numeric_metric(payload, "report_duration")
    parsed["search_server_p95_ms"] = numeric_metric(payload, "search_server_duration")
    parsed["submit_server_p95_ms"] = numeric_metric(payload, "submit_server_duration")
    parsed["report_server_p95_ms"] = numeric_metric(payload, "report_server_duration")
    parsed["search_samples"] = numeric_metric(payload, "search_duration", ("count", "sample_count"))
    parsed["submit_samples"] = numeric_metric(payload, "submit_duration", ("count", "sample_count"))
    parsed["report_samples"] = numeric_metric(payload, "report_duration", ("count", "sample_count"))
    parsed["http_fail_rate"] = numeric_metric(payload, "http_req_failed", ("rate", "value"))
    parsed["errors"] = numeric_metric(payload, "errors", ("count", "value"))

    metrics = payload.get("metrics", payload) if isinstance(payload, dict) else {}
    if isinstance(metrics, dict):
        for metric_name in sorted(metrics):
            base, tags = parse_tagged_metric_key(metric_name)
            if base not in {"report_duration", "report_server_duration"}:
                continue
            target = REPORT_KEY_BY_NAME.get(tags.get("report", ""))
            if not target:
                continue
            if base == "report_server_duration":
                target = target.replace("_p95_ms", "_server_p95_ms")
                if target not in parsed:
                    continue
            value = numeric_metric(payload, metric_name)
            if value is not None:
                parsed[target] = value
    return parsed


def parse_k6_summary(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return parse_k6_summary_payload(json.load(handle))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return empty_metrics()


def canonical_mapping(mapping):
    if not isinstance(mapping, dict):
        return {}
    return {canonical_key(key): value for key, value in mapping.items()}


def first_value_with_key(row, *names):
    for name in names:
        key = canonical_key(name)
        if key in row and row[key] not in (None, ""):
            return key, row[key]
    return None, None


def memory_mib(value, key=""):
    parsed = parse_float(value)
    if parsed is None:
        return None
    canonical = canonical_key(key)
    if "bytes" in canonical:
        return parsed / (1024.0 * 1024.0)
    if "kib" in canonical or canonical.endswith("kb"):
        return parsed / 1024.0
    if "gib" in canonical or canonical.endswith("gb"):
        return parsed * 1024.0
    if "mib" not in canonical and "mb" not in canonical and abs(parsed) >= 1024.0 * 1024.0:
        return parsed / (1024.0 * 1024.0)
    return parsed


def memory_value(row, value_kind, *names):
    key, value = first_value_with_key(row, *names)
    if key is None:
        return None
    if value_kind == "mib":
        return memory_mib(value, key)
    return parse_float(value)


def extract_container_memory(row):
    row = canonical_mapping(row)
    return {
        "rss_mib": memory_value(row, "mib", "peak_rss_mib", "peak_rss_bytes", "rss_mib", "rss_bytes", "peak_rss"),
        "pss_mib": memory_value(row, "mib", "peak_pss_mib", "peak_pss_bytes", "pss_mib", "pss_bytes", "peak_pss"),
        "cgroup_mib": memory_value(
            row, "mib",
            "peak_cgroup_mib", "peak_cgroup_bytes", "cgroup_mib", "cgroup_bytes",
            "cgroup_memory_current_bytes", "memory_current_bytes", "peak_cgroup",
        ),
        "pct": memory_value(
            row, "float",
            "peak_cgroup_percent", "peak_cgroup_pct", "cgroup_percent", "cgroup_memory_percent",
            "peak_pct", "pct",
        ),
    }


def mapping_value(mapping, *names):
    return first_value_with_key(canonical_mapping(mapping), *names)[1]


def container_items_from(value):
    if isinstance(value, dict):
        for name, row in value.items():
            if isinstance(row, dict):
                yield str(name), row
    elif isinstance(value, list):
        for row in value:
            if not isinstance(row, dict):
                continue
            name = mapping_value(row, "name", "container", "container_name", "service")
            if name:
                yield str(name), row


def summary_container_items(payload):
    sources = [payload]
    for key in ("memory", "container_memory", "memory_evidence", "cgroup_memory"):
        nested = mapping_value(payload, key)
        if isinstance(nested, dict):
            sources.append(nested)
    seen = set()
    for source in sources:
        containers = mapping_value(source, "containers", "container_stats", "container_memory")
        if containers is not None:
            for name, row in container_items_from(containers):
                key = (name, id(row))
                if key not in seen:
                    seen.add(key)
                    yield name, row
        if isinstance(source, dict):
            for name, row in source.items():
                if container_role(name) and isinstance(row, dict):
                    key = (str(name), id(row))
                    if key not in seen:
                        seen.add(key)
                        yield str(name), row


def normalize_headroom_check(name, value, source):
    if isinstance(value, dict):
        row = canonical_mapping(value)
        status = first_value_with_key(row, "status", "result", "verdict", "outcome")[1]
        ok = first_value_with_key(row, "ok", "passed", "pass")[1]
        if status is None and ok is not None:
            status = "PASS" if parse_float(ok) == 1.0 else "FAIL"
        return {
            "name": str(first_value_with_key(row, "name", "check", "container", "role")[1] or name),
            "observed": first_value_with_key(row, "observed", "value", "pct", "percent")[1],
            "threshold": first_value_with_key(row, "threshold", "limit", "max")[1],
            "status": str(status or "missing telemetry"),
            "notes": first_value_with_key(row, "notes", "detail", "message")[1] or "",
            "source": source,
        }
    return {"name": str(name), "observed": None, "threshold": None, "status": str(value), "notes": "", "source": source}


def normalize_headroom_checks(value, source):
    checks = []
    if isinstance(value, list):
        for index, row in enumerate(value):
            checks.append(normalize_headroom_check(f"headroom-{index}", row, source))
    elif isinstance(value, dict):
        row = canonical_mapping(value)
        if any(key in row for key in ("status", "result", "verdict", "outcome", "ok", "passed", "pass")):
            checks.append(normalize_headroom_check(mapping_value(value, "name", "check") or "headroom", value, source))
        else:
            for name, entry in value.items():
                checks.append(normalize_headroom_check(name, entry, source))
    elif value not in (None, ""):
        checks.append(normalize_headroom_check("headroom", value, source))
    return checks


def summary_headroom_checks(payload):
    checks = []
    evidence = False
    sources = [("summary", payload)]
    nested_memory = mapping_value(payload, "memory")
    if isinstance(nested_memory, dict):
        sources.append(("summary.memory", nested_memory))
    for source, mapping in sources:
        for key in ("headroom_gates", "headroom_checks", "cgroup_headroom", "cgroup_headroom_gates"):
            canonical = canonical_mapping(mapping)
            canonical_key_name = canonical_key(key)
            if canonical_key_name in canonical:
                evidence = True
                checks.extend(normalize_headroom_checks(canonical[canonical_key_name], source))
    return checks, evidence


def parse_summary_memory_payload(payload):
    """Normalize optional evaluate_run memory/headroom JSON without guessing absent evidence."""
    parsed = empty_metrics()
    if not isinstance(payload, dict):
        return parsed, [], False

    evidence = False
    sources = [payload]
    nested_memory = mapping_value(payload, "memory")
    if isinstance(nested_memory, dict):
        sources.append(nested_memory)
    for source in sources:
        row = canonical_mapping(source)
        for role in HEADROOM_THRESHOLDS:
            for suffix in ("peak_rss_mib", "peak_pss_mib", "peak_cgroup_mib", "peak_pct"):
                key, value = first_value_with_key(row, f"{role}_{suffix}")
                if key is None:
                    continue
                parsed_value = memory_mib(value, key) if suffix.endswith("_mib") else parse_float(value)
                if parsed_value is not None:
                    parsed[f"{role}_{suffix}"] = parsed_value
                    evidence = True

    for name, row in summary_container_items(payload):
        role = container_role(name)
        if not role:
            continue
        memory = extract_container_memory(row)
        evidence = True
        if memory["rss_mib"] is not None:
            parsed[f"{role}_peak_rss_mib"] = memory["rss_mib"]
        if memory["pss_mib"] is not None:
            parsed[f"{role}_peak_pss_mib"] = memory["pss_mib"]
        if memory["cgroup_mib"] is not None:
            parsed[f"{role}_peak_cgroup_mib"] = memory["cgroup_mib"]
        if memory["pct"] is not None:
            parsed[f"{role}_peak_pct"] = memory["pct"]

    checks, headroom_evidence = summary_headroom_checks(payload)
    return parsed, checks, evidence or headroom_evidence


def load_json_optional(path):
    try:
        return load_json_file(path)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def canonical_key(key):
    return re.sub(r"[^a-z0-9]+", "", str(key or "").lower())


def first_value(row, *names):
    for name in names:
        key = canonical_key(name)
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def container_role(name):
    text = str(name or "")
    if text == "khitmee-perf-backend-1" or text.endswith("-backend-1"):
        return "backend"
    if text == "khitmee-perf-db-1" or text.endswith("-db-1"):
        return "db"
    if text == "khitmee-perf-frontend-1" or text.endswith("-frontend-1"):
        return "frontend"
    return None


def parse_container_stats(path):
    """Parse raw stats CSV. Fleet peaks are concurrent timestamp sums, not sums of peaks.

    RSS/PSS are process-memory views while cgroup memory.current is the accounting
    value enforced by the kernel.  They are intentionally kept separate; cgroup
    utilization is derived from memory.current whenever that evidence exists.
    """
    parsed = empty_metrics()
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            raw_rows = list(reader)
    except (FileNotFoundError, OSError):
        return parsed
    if not raw_rows:
        return parsed

    rows = [{canonical_key(key): value for key, value in row.items()} for row in raw_rows]
    containers = {}
    totals = {}
    any_oom_missing = False
    any_restart_missing = False
    docker_oom_seen = False

    for row in rows:
        name = first_value(row, "container", "container_name", "name", "service")
        if name is None:
            continue
        name = str(name)
        entry = containers.setdefault(name, {
            "rss": None, "pss": None, "cgroup": None, "pct": None, "oom": 0.0,
            "restart_min": None, "restart_max": 0.0, "pids": set(), "samples": 0,
        })
        entry["samples"] += 1

        rss = None
        if first_value(row, "rss_kb") is not None:
            rss = parse_float(first_value(row, "rss_kb"))
            rss = rss / 1024.0 if rss is not None else None
        elif first_value(row, "rss_bytes") is not None:
            rss = parse_float(first_value(row, "rss_bytes"))
            rss = rss / (1024.0 * 1024.0) if rss is not None else None
        else:
            rss = parse_float(first_value(row, "rss_mb", "rss"))
        pss = None
        if first_value(row, "pss_kb") is not None:
            pss = parse_float(first_value(row, "pss_kb"))
            pss = pss / 1024.0 if pss is not None else None
        elif first_value(row, "pss_bytes") is not None:
            pss = parse_float(first_value(row, "pss_bytes"))
            pss = pss / (1024.0 * 1024.0) if pss is not None else None
        else:
            pss = parse_float(first_value(row, "pss_mb", "pss"))
        if rss is not None:
            entry["rss"] = rss if entry["rss"] is None else max(entry["rss"], rss)
        if pss is not None:
            entry["pss"] = pss if entry["pss"] is None else max(entry["pss"], pss)

        cgroup = None
        if first_value(row, "cgroup_memory_current_bytes", "memory_current_bytes") is not None:
            cgroup_bytes = parse_float(first_value(row, "cgroup_memory_current_bytes", "memory_current_bytes"))
            cgroup = cgroup_bytes / (1024.0 * 1024.0) if cgroup_bytes is not None else None
        elif first_value(row, "cgroup_memory_current_kb", "memory_current_kb") is not None:
            cgroup_kb = parse_float(first_value(row, "cgroup_memory_current_kb", "memory_current_kb"))
            cgroup = cgroup_kb / 1024.0 if cgroup_kb is not None else None
        if cgroup is not None:
            entry["cgroup"] = cgroup if entry["cgroup"] is None else max(entry["cgroup"], cgroup)

        limit_bytes = parse_float(first_value(row, "cgroup_memory_max_bytes", "memory_limit_bytes", "mem_limit"))
        limit_mib = limit_bytes / (1024.0 * 1024.0) if limit_bytes else None
        explicit_pct = parse_float(first_value(row, "cgroup_percent", "cgroup_memory_percent", "mem_percent"))
        pct = None
        if cgroup is not None and limit_mib:
            pct = cgroup / limit_mib * 100.0
        elif rss is not None and limit_mib:
            pct = rss / limit_mib * 100.0
        elif explicit_pct is not None:
            pct = explicit_pct
        if pct is not None:
            entry["pct"] = pct if entry["pct"] is None else max(entry["pct"], pct)

        oom = parse_float(first_value(row, "cgroup_oom_kill", "oom_kill", "oom_kills"))
        if oom is None:
            any_oom_missing = True
        else:
            entry["oom"] = max(entry["oom"], oom)
        docker_oom = parse_float(first_value(row, "docker_oom_killed", "docker_oom"))
        if docker_oom and docker_oom > 0:
            docker_oom_seen = True
        restart = parse_float(first_value(row, "restart_count", "restarts"))
        if restart is None:
            any_restart_missing = True
        else:
            entry["restart_min"] = restart if entry["restart_min"] is None else min(entry["restart_min"], restart)
            entry["restart_max"] = max(entry["restart_max"], restart)
        pid = first_value(row, "pid", "container_pid")
        if pid and str(pid).isdigit() and int(pid) > 0:
            entry["pids"].add(str(pid))

        timestamp = first_value(row, "timestamp", "time", "ts")
        if timestamp is not None:
            bucket = totals.setdefault(str(timestamp), {"rss": 0.0, "pss": 0.0, "rss_seen": False, "pss_seen": False})
            if rss is not None:
                bucket["rss"] += rss
                bucket["rss_seen"] = True
            if pss is not None:
                bucket["pss"] += pss
                bucket["pss_seen"] = True

    for role in ("backend", "db", "frontend"):
        selected = containers.get(f"khitmee-perf-{role}-1")
        if selected is None:
            selected = next((value for name, value in containers.items() if container_role(name) == role), None)
        if selected:
            parsed[f"{role}_peak_rss_mib"] = selected["rss"]
            parsed[f"{role}_peak_pss_mib"] = selected["pss"]
            parsed[f"{role}_peak_cgroup_mib"] = selected["cgroup"]
            parsed[f"{role}_peak_pct"] = selected["pct"]

    if totals:
        rss_values = [bucket["rss"] for bucket in totals.values() if bucket["rss_seen"]]
        pss_values = [bucket["pss"] for bucket in totals.values() if bucket["pss_seen"]]
        parsed["fleet_peak_rss_mib"] = max(rss_values) if rss_values else None
        parsed["fleet_peak_pss_mib"] = max(pss_values) if pss_values else None
    else:
        rss_values = [value["rss"] for value in containers.values() if value["rss"] is not None]
        pss_values = [value["pss"] for value in containers.values() if value["pss"] is not None]
        parsed["fleet_peak_rss_mib"] = sum(rss_values) if rss_values else None
        parsed["fleet_peak_pss_mib"] = sum(pss_values) if pss_values else None

    if not any_oom_missing:
        total_oom = sum(value["oom"] for value in containers.values())
        parsed["oom_kills"] = max(total_oom, 1.0 if docker_oom_seen else 0.0)
    if not any_restart_missing:
        restarts = []
        for value in containers.values():
            if value["samples"] <= 1:
                observed = value["restart_max"]
            else:
                delta = value["restart_max"] - (value["restart_min"] or 0.0)
                observed = max(delta, float(max(0, len(value["pids"]) - 1)))
            restarts.append(observed)
        parsed["restarts"] = max(restarts, default=0.0)
    return parsed

def overlay_non_null(base, overlay):
    for key, value in overlay.items():
        if key in METRIC_KEYS and value is not None:
            base[key] = value
    return base


def existing_path(*candidates):
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def collect_scenario(results_dir, prefix, scenario):
    suffix = SCENARIOS[scenario]
    base = Path(results_dir) / f"{prefix}-{suffix}"
    scorecard_path = Path(str(base) + "-scorecard.md")
    summary_path = existing_path(Path(str(base) + "-k6-summary.json"), Path(str(base) + "-summary.json"))
    stats_path = existing_path(Path(str(base) + "-container-stats.csv"))

    scorecard = parse_scorecard(scorecard_path) if scorecard_path.exists() else {
        "path": str(scorecard_path), "run_label": None, "verdict": None, "metrics": empty_metrics(),
        "headroom_checks": [], "error": "missing",
    }
    metrics = dict(scorecard["metrics"])
    headroom_checks = list(scorecard.get("headroom_checks") or [])
    headroom_telemetry_in_summary = False
    if summary_path:
        summary_payload = load_json_optional(summary_path)
        if summary_payload is not None:
            overlay_non_null(metrics, parse_k6_summary_payload(summary_payload))
            summary_memory, summary_checks, summary_evidence = parse_summary_memory_payload(summary_payload)
            overlay_non_null(metrics, summary_memory)
            headroom_checks.extend(summary_checks)
            headroom_telemetry_in_summary = summary_evidence
        else:
            overlay_non_null(metrics, parse_k6_summary(summary_path))
    if stats_path:
        stats_metrics = parse_container_stats(stats_path)
        for telemetry_key in ("oom_kills", "restarts"):
            if stats_metrics.get(telemetry_key) is None:
                metrics[telemetry_key] = None
        overlay_non_null(metrics, stats_metrics)

    artifacts = {}
    missing = []
    for kind, path in (("scorecard", scorecard_path), ("k6_summary", summary_path), ("container_stats", stats_path)):
        if path and Path(path).exists():
            artifacts[kind] = str(path)
        else:
            missing.append(kind)
    return {
        "verdict": scorecard.get("verdict"),
        "run_label": scorecard.get("run_label"),
        "metrics": metrics,
        "artifacts": artifacts,
        "missing_artifacts": missing,
        "headroom_checks": headroom_checks,
        "headroom_telemetry_in_summary": headroom_telemetry_in_summary,
    }


def mean_present(scenarios, metric, names=None):
    values = []
    for name, scenario in scenarios.items():
        if names and name not in names:
            continue
        value = scenario["metrics"].get(metric)
        if value is not None:
            values.append(float(value))
    return sum(values) / len(values) if values else None


def max_present(scenarios, metric):
    values = [float(scenario["metrics"].get(metric)) for scenario in scenarios.values() if scenario["metrics"].get(metric) is not None]
    return max(values) if values else None


def round_or_none(value, digits=3):
    return None if value is None else round(float(value), digits)


def representative_metrics(entry):
    scenarios = entry["scenarios"]
    latency = {
        "search_p95_mean": round_or_none(mean_present(scenarios, "search_p95_ms")),
        "submit_p95_mean": round_or_none(mean_present(scenarios, "submit_p95_ms")),
        "report_p95_mean": round_or_none(mean_present(scenarios, "report_p95_ms", {"reports-30d", "reports-1d"})),
        "stock_balance_p95_mean": round_or_none(mean_present(scenarios, "stock_balance_p95_ms", {"reports-30d", "reports-1d"})),
        "fast_gross_profit_p95_mean": round_or_none(mean_present(scenarios, "fast_gross_profit_p95_ms", {"reports-30d", "reports-1d"})),
    }
    memory = {key: round_or_none(max_present(scenarios, key)) for key in MEMORY_KEYS}
    return {"latency_ms": latency, "memory_mib": memory}


def metric_unit(key):
    if key.endswith("_ms") or key.endswith("_mean"):
        return "ms"
    if key.endswith("_mib"):
        return "MiB"
    if key.endswith("_pct"):
        return "%"
    if key == "http_fail_rate":
        return "rate"
    return "count"


def signed(value, unit):
    if value is None:
        return "N/A"
    precision = 5 if unit == "rate" else 1 if unit in {"ms", "MiB", "%"} else 0
    return f"{value:+.{precision}f} {unit}"


def compare_entries(base, head):
    """Positive deltas are savings/improvements; negative deltas are regressions."""
    scenario_deltas = {}
    improvements = []
    regressions = []
    for scenario in SCENARIOS:
        row = {}
        for key in METRIC_KEYS:
            before = base["scenarios"].get(scenario, {}).get("metrics", {}).get(key)
            after = head["scenarios"].get(scenario, {}).get("metrics", {}).get(key)
            if before is None or after is None:
                row[key] = None
                continue
            delta = float(before) - float(after)
            row[key] = round(delta, 6)
            if delta > 1e-9:
                improvements.append(f"{scenario} {key} improved by {delta:.3f} {metric_unit(key)}")
            elif delta < -1e-9:
                regressions.append(f"{scenario} {key} regressed by {-delta:.3f} {metric_unit(key)}")
        scenario_deltas[scenario] = row

    base_rep = representative_metrics(base)
    head_rep = representative_metrics(head)
    latency_saved = {}
    memory_saved = {}
    aggregate_improvements = []
    aggregate_regressions = []
    for key in base_rep["latency_ms"]:
        before, after = base_rep["latency_ms"][key], head_rep["latency_ms"][key]
        delta = None if before is None or after is None else round(before - after, 3)
        latency_saved[key] = delta
        if delta is not None and delta > 1e-9:
            aggregate_improvements.append(f"{key} improved by {delta:.3f} ms")
        elif delta is not None and delta < -1e-9:
            aggregate_regressions.append(f"{key} regressed by {-delta:.3f} ms")
    for key in base_rep["memory_mib"]:
        before, after = base_rep["memory_mib"][key], head_rep["memory_mib"][key]
        delta = None if before is None or after is None else round(before - after, 3)
        memory_saved[key] = delta
        if delta is not None and delta > 1e-9:
            aggregate_improvements.append(f"{key} improved by {delta:.3f} {metric_unit(key)}")
        elif delta is not None and delta < -1e-9:
            aggregate_regressions.append(f"{key} regressed by {-delta:.3f} {metric_unit(key)}")

    return {
        "base_pass_id": base["pass_id"],
        "head_pass_id": head["pass_id"],
        "base_profile": base["profile"],
        "head_profile": head["profile"],
        "aggregate": {
            "latency_ms_saved": latency_saved,
            "memory_mib_saved": memory_saved,
        },
        "scenario_deltas": scenario_deltas,
        "improvements": aggregate_improvements + improvements,
        "regressions": aggregate_regressions + regressions,
        "regression_count": len(aggregate_regressions) + len(regressions),
        "positive_delta_means_improvement": True,
    }


def load_json_file(path):
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def scenario_requests_headroom(evidence):
    return bool(evidence.get("headroom_telemetry_in_summary") or evidence.get("headroom_checks"))


def normalize_check_status(value):
    """Normalize Markdown-decorated scorecard statuses such as **PASS**."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(value or "")).upper()


def evaluate_run_headroom_failures(scenario, checks):
    failures = []
    for check in checks or []:
        status = normalize_check_status(check.get("status"))
        if status not in {"PASS", "PASSED", "OK", "GO", "TRUE"}:
            name = check.get("name") or "unnamed"
            failures.append(f"{scenario} evaluate_run headroom check '{name}' is {status or 'missing'}, not PASS")
    return failures


def headroom_gate_failures(scenario, evidence):
    metrics = evidence.get("metrics", {})
    failures = []
    for role, threshold in HEADROOM_THRESHOLDS.items():
        cgroup_key = f"{role}_peak_cgroup_mib"
        pct_key = f"{role}_peak_pct"
        cgroup_value = parse_float(metrics.get(cgroup_key))
        pct_value = parse_float(metrics.get(pct_key))
        if cgroup_value is None:
            failures.append(f"{scenario} {cgroup_key} telemetry is missing")
        if pct_value is None:
            failures.append(f"{scenario} {pct_key} telemetry is missing")
        elif pct_value >= threshold:
            failures.append(f"{scenario} {pct_key} is {pct_value:.1f}%, not below {threshold:.1f}%")
    failures.extend(evaluate_run_headroom_failures(scenario, evidence.get("headroom_checks")))
    return failures


def strict_gate_failures(scenarios, parity_pass, safety_pass, k6_all_zero, require_headroom=False):
    """Return every reason a pass cannot be published as strict GO evidence."""
    failures = []
    for scenario in SCENARIOS:
        evidence = scenarios.get(scenario, {})
        if evidence.get("verdict") != "GO":
            failures.append(f"{scenario} verdict is {evidence.get('verdict') or 'missing'}, not GO")
        missing = evidence.get("missing_artifacts") or []
        if missing:
            failures.append(f"{scenario} missing artifacts: {', '.join(missing)}")
        metrics = evidence.get("metrics", {})
        latency_keys = ["search_p95_ms", "submit_p95_ms"]
        sample_keys = ["search_samples", "submit_samples"]
        if scenario in {"reports-30d", "reports-1d"}:
            latency_keys.append("report_p95_ms")
            sample_keys.append("report_samples")
        for key in latency_keys:
            if metrics.get(key) is None:
                failures.append(f"{scenario} {key} k6 evidence is missing")
        for key in sample_keys:
            value = metrics.get(key)
            if value is None or float(value) <= 0:
                failures.append(f"{scenario} {key} k6 sample evidence is missing or zero")
        for key in ("oom_kills", "restarts"):
            value = metrics.get(key)
            if value is None:
                failures.append(f"{scenario} {key} telemetry is missing")
            elif float(value) != 0.0:
                failures.append(f"{scenario} {key} is {int(float(value))}, not 0")
        if require_headroom or scenario_requests_headroom(evidence):
            failures.extend(headroom_gate_failures(scenario, evidence))
    if parity_pass is not True:
        failures.append("parity_pass is not true")
    if safety_pass is not True:
        failures.append("safety_pass is not true")
    if k6_all_zero is not True:
        failures.append("one or more k6 commands exited nonzero")
    return failures


def evaluate_strict_verdict(scenarios, parity_pass, safety_pass, k6_all_zero, require_headroom=False):
    failures = strict_gate_failures(
        scenarios,
        parity_pass,
        safety_pass,
        k6_all_zero,
        require_headroom=require_headroom,
    )
    return {
        "strict_go": not failures,
        "strict_verdict": "GO" if not failures else "NO-GO",
        "strict_gate_failures": failures,
    }


def build_entry(pass_id, label, profile, results_dir, prefix, profile_json=None, parity_pass=None,
                notes="", previous=None, safety_pass=False, k6_all_zero=True, require_headroom=False):
    scenarios = {
        scenario: collect_scenario(results_dir, prefix, scenario)
        for scenario in SCENARIOS
    }
    go_count = sum(1 for scenario in scenarios.values() if scenario["verdict"] == "GO")
    missing_count = sum(1 for scenario in scenarios.values() if scenario["verdict"] not in {"GO", "NO-GO"})
    strict = evaluate_strict_verdict(
        scenarios,
        parity_pass,
        safety_pass,
        k6_all_zero,
        require_headroom=require_headroom,
    )
    gate_failures = strict["strict_gate_failures"]
    strict_go = strict["strict_go"]
    strict_verdict = strict["strict_verdict"]
    headroom_required = bool(require_headroom or any(scenario_requests_headroom(scenario) for scenario in scenarios.values()))

    profiler = None
    profiler_error = None
    if profile_json:
        try:
            profiler = load_json_file(profile_json)
        except (OSError, json.JSONDecodeError) as exc:
            profiler_error = f"{type(exc).__name__}: {exc}"

    entry = {
        "schema_version": 2,
        "pass_id": pass_id,
        "profile": profile,
        "label": label,
        "prefix": prefix,
        "recorded_at": utc_now(),
        "parity_pass": parity_pass,
        "safety_pass": bool(safety_pass),
        "k6_all_zero": bool(k6_all_zero),
        "notes": notes or "",
        "scenarios": scenarios,
        "score": {
            "scenarios_go": go_count,
            "scenarios_total": len(SCENARIOS),
            "scenarios_missing": missing_count,
            "strict_go": strict_go,
            "strict_verdict": strict_verdict,
            "strict_gate_failures": gate_failures,
            "headroom_required": headroom_required,
            "fraction_go": round(go_count / len(SCENARIOS), 4),
            "honesty_note": "fraction_go is progress telemetry only; strict_verdict is NO-GO unless every fail-closed evidence gate passes.",
        },
        "profile_json_path": str(profile_json) if profile_json else None,
        "profiler": profiler,
        "profiler_error": profiler_error,
    }
    entry["representative"] = representative_metrics(entry)
    if previous is not None:
        entry["delta_vs_previous"] = compare_entries(previous, entry)
    return entry

def load_ledger(path):
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "passes": []}
    data = load_json_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("passes"), list):
        raise ValueError(f"ledger has unexpected shape: {path}")
    data.setdefault("schema_version", 1)
    return data


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def save_ledger(path, data):
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


def md_escape(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def display_value(key, value):
    if value is None:
        return "N/A"
    if key in LATENCY_KEYS:
        return f"{float(value):.1f}"
    if key in MEMORY_KEYS:
        return f"{float(value):.1f}"
    if key == "http_fail_rate":
        return f"{float(value):.5f}"
    return str(int(float(value)))


def column_name(entry):
    return f"{entry['pass_id']} ({entry['profile']})"


def render_markdown(data):
    entries = data.get("passes", [])
    lines = [
        "# Overnight Perf + Memory Eval Ledger",
        "",
        f"Generated: {utc_now()}",
        "",
        "Strict rule: a pass is GO only when every scenario verdict, artifact, telemetry, parity, safety and k6-exit gate passes. Numeric savings are progress telemetry and never hide a NO-GO.",
        "",
        "Headroom gates evaluate scenario-sampled cgroup memory.current / memory.max during k6 workload windows (lifetime container memory.peak is reported separately in post-run evidence and includes pre-k6 profiler/setup subprocesses). Historical schema v1 passes (pre-Pass-2) predated safety_pass, k6_all_zero, and cgroup headroom gates and are explicitly labeled legacy v1.",
        "",
        "## Go/No-Go Matrix",
        "",
        "| Pass | Profile | Label | sustained | burst-2x | reports-30d | reports-1d | GO count | Strict verdict |",
        "|---|---|---|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]
    for entry in entries:
        row = [md_escape(entry["pass_id"]), md_escape(entry["profile"]), md_escape(entry.get("label", ""))]
        row.extend(entry["scenarios"][scenario]["verdict"] or "N/A" for scenario in SCENARIOS)
        score = entry["score"]
        strict_verdict = score["strict_verdict"]
        if int(entry.get("schema_version", 1)) < 2:
            strict_verdict = f"{score['strict_verdict']} (legacy v1)"
        row.extend([f"{score['scenarios_go']}/{score['scenarios_total']}", strict_verdict])
        lines.append("| " + " | ".join(row) + " |")

    lines.extend(["", "## Strict Gate Failure Reasons", ""])
    any_failures = False
    for entry in entries:
        failures = entry.get("score", {}).get("strict_gate_failures") or []
        if not failures:
            continue
        any_failures = True
        lines.append(f"- **{md_escape(column_name(entry))}**")
        lines.extend(f"  - {md_escape(failure)}" for failure in failures)
    if not any_failures:
        lines.append("No strict gate failures recorded.")

    columns = [md_escape(column_name(entry)) for entry in entries]
    header = "| Metric | " + " | ".join(columns) + " |" if entries else "| Metric | N/A |"
    divider = "|---|" + "---:|" * len(entries) if entries else "|---|---:|"
    lines.extend(["", "## Latency Table", "", "Values are end-to-end p95 milliseconds; N/A means the artifact did not contain that evidence.", "", header, divider])
    for scenario in SCENARIOS:
        for key in LATENCY_KEYS:
            row = [f"{scenario} {key}"]
            row.extend(display_value(key, entry["scenarios"][scenario]["metrics"].get(key)) for entry in entries)
            lines.append("| " + " | ".join(row) + " |")

    lines.extend(["", "## Memory Table", "", "Values are MiB except `*_pct`, which is peak cgroup utilization percent.", "", header, divider])
    for scenario in SCENARIOS:
        for key in MEMORY_KEYS:
            row = [f"{scenario} {key}"]
            row.extend(display_value(key, entry["scenarios"][scenario]["metrics"].get(key)) for entry in entries)
            lines.append("| " + " | ".join(row) + " |")

    lines.extend([
        "", "## Pass-over-Pass Deltas", "",
        "Positive saved values are improvements. Negative saved values are regressions and are explicitly marked REGRESSION.",
        "",
        "| Pass | Compared with | Search ms saved | Submit ms saved | Report ms saved | Fleet PSS MiB saved | Regression flags |",
        "|---|---|---:|---:|---:|---:|---|",
    ])
    for entry in entries:
        delta = entry.get("delta_vs_previous")
        if not delta:
            lines.append(f"| {md_escape(column_name(entry))} | baseline | N/A | N/A | N/A | N/A | baseline |")
            continue
        agg = delta["aggregate"]
        regressions = "<br>".join("REGRESSION " + md_escape(item) for item in delta["regressions"][:8]) or "none"
        if len(delta["regressions"]) > 8:
            regressions += f"<br>REGRESSION +{len(delta['regressions']) - 8} more"
        values = [
            md_escape(column_name(entry)),
            md_escape(delta["base_pass_id"]),
            signed(agg["latency_ms_saved"].get("search_p95_mean"), "ms"),
            signed(agg["latency_ms_saved"].get("submit_p95_mean"), "ms"),
            signed(agg["latency_ms_saved"].get("report_p95_mean"), "ms"),
            signed(agg["memory_mib_saved"].get("fleet_peak_pss_mib"), "MiB"),
            regressions,
        ]
        lines.append("| " + " | ".join(values) + " |")

    lines.extend(["", "## Run Metadata", "", "| Pass | Prefix | Parity pass | Safety pass | k6 exit zero | Profiler | Notes |", "|---|---|---|---|---|---|---|"])
    for entry in entries:
        parity = "N/A" if entry.get("parity_pass") is None else str(bool(entry["parity_pass"])).lower()
        is_legacy_v1 = int(entry.get("schema_version", 1)) < 2
        if is_legacy_v1 and "safety_pass" not in entry:
            safety = "N/A (legacy v1)"
        elif entry.get("safety_pass") is None:
            safety = "N/A"
        else:
            safety = str(bool(entry["safety_pass"])).lower()
        if is_legacy_v1 and "k6_all_zero" not in entry:
            k6_zero = "N/A (legacy v1)"
        elif entry.get("k6_all_zero") is None:
            k6_zero = "N/A"
        else:
            k6_zero = str(bool(entry["k6_all_zero"])).lower()
        profiler = entry.get("profile_json_path") or "N/A"
        if entry.get("profiler_error"):
            profiler += " (unreadable)"
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            md_escape(column_name(entry)), md_escape(entry.get("prefix", "")), parity,
            safety, k6_zero, md_escape(profiler), md_escape(entry.get("notes", "")),
        ))
    lines.append("")
    return "\n".join(lines)


def same_key(entry, pass_id, profile):
    return entry.get("pass_id") == pass_id and entry.get("profile") == profile


def upsert_pass(data, entry):
    passes = data.setdefault("passes", [])
    for index, existing in enumerate(passes):
        if same_key(existing, entry["pass_id"], entry["profile"]):
            entry["recorded_at"] = existing.get("recorded_at", entry["recorded_at"])
            entry["updated_at"] = utc_now()
            passes[index] = entry
            return "updated"
    passes.append(entry)
    return "inserted"


def previous_for_pass(passes, pass_id, profile):
    index = next((i for i, entry in enumerate(passes) if same_key(entry, pass_id, profile)), len(passes))
    for candidate in reversed(passes[:index]):
        if candidate.get("profile") == profile and candidate.get("pass_id") != pass_id:
            return candidate
    return None


def recompute_pass_deltas(data):
    """Recompute chain deltas after idempotent replacement of any earlier pass."""
    previous_by_profile = {}
    for entry in data.get("passes", []):
        entry.pop("delta_vs_previous", None)
        previous = previous_by_profile.get(entry.get("profile"))
        if previous is not None and previous.get("pass_id") != entry.get("pass_id"):
            entry["delta_vs_previous"] = compare_entries(previous, entry)
        previous_by_profile[entry.get("profile")] = entry


def record_command(args):
    data = load_ledger(args.ledger)
    previous = previous_for_pass(data.get("passes", []), args.pass_id, args.profile)
    entry = build_entry(
        pass_id=args.pass_id,
        label=args.label,
        profile=args.profile,
        results_dir=args.results_dir,
        prefix=args.prefix,
        profile_json=args.profile_json,
        parity_pass=args.parity_pass,
        notes=args.notes,
        previous=previous,
        safety_pass=args.safety_pass,
        k6_all_zero=not args.k6_exit_nonzero,
        require_headroom=args.require_headroom,
    )
    action = upsert_pass(data, entry)
    recompute_pass_deltas(data)
    save_ledger(args.ledger, data)
    atomic_write_text(args.markdown, render_markdown(data))
    score = entry["score"]
    print(
        f"recorded {action}: {entry['pass_id']} ({entry['profile']}) "
        f"strict={score['strict_verdict']} go={score['scenarios_go']}/{score['scenarios_total']} "
        f"ledger={args.ledger} markdown={args.markdown}"
    )
    if entry.get("profiler_error"):
        print(f"WARNING: profiler JSON was not readable: {entry['profiler_error']}", file=sys.stderr)
    return 0


def entries_for(passes, pass_id, profile=None):
    return [entry for entry in passes if entry.get("pass_id") == pass_id and (profile is None or entry.get("profile") == profile)]


def render_compare(base, head):
    delta = compare_entries(base, head)
    lines = [
        f"compare {base['pass_id']} -> {head['pass_id']} profile={head['profile']}",
        f"strict verdict: {base['score']['strict_verdict']} {base['score']['scenarios_go']}/{base['score']['scenarios_total']} -> "
        f"{head['score']['strict_verdict']} {head['score']['scenarios_go']}/{head['score']['scenarios_total']}",
        "aggregate saved (positive = improvement):",
    ]
    for key, value in delta["aggregate"]["latency_ms_saved"].items():
        lines.append(f"  {key}: {signed(value, 'ms')}")
    for key, value in delta["aggregate"]["memory_mib_saved"].items():
        lines.append(f"  {key}: {signed(value, metric_unit(key))}")
    lines.append("improvements:")
    if delta["improvements"]:
        lines.extend("  IMPROVEMENT " + item for item in delta["improvements"])
    else:
        lines.append("  none")
    lines.append("regressions:")
    if delta["regressions"]:
        lines.extend("  REGRESSION " + item for item in delta["regressions"])
    else:
        lines.append("  none")
    return "\n".join(lines)


def compare_command(args):
    data = load_ledger(args.ledger)
    passes = data.get("passes", [])
    base_entries = entries_for(passes, args.base, args.profile)
    head_entries = entries_for(passes, args.head, args.profile)
    if not base_entries or not head_entries:
        print(f"compare failed: missing pass entries for base={args.base} or head={args.head}", file=sys.stderr)
        return 2
    sections = []
    for head in head_entries:
        base = next((entry for entry in base_entries if entry.get("profile") == head.get("profile")), None)
        if base is not None:
            sections.append(render_compare(base, head))
    if not sections:
        print("compare failed: base and head have no common memory profile", file=sys.stderr)
        return 2
    print("\n\n".join(sections))
    return 0


def parse_bool(text):
    value = str(text).strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser():
    parser = argparse.ArgumentParser(description="Record and compare reproducible khitmee-perf eval passes.")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="record one pass and regenerate the Markdown ledger")
    record.add_argument("--ledger", required=True)
    record.add_argument("--markdown", required=True)
    record.add_argument("--pass-id", required=True)
    record.add_argument("--label", required=True)
    record.add_argument("--profile", required=True, choices=("4gb", "2gb"))
    record.add_argument("--results-dir", required=True)
    record.add_argument("--prefix", "--run-prefix", dest="prefix", required=True)
    record.add_argument("--profile-json", default=None)
    record.add_argument("--parity-pass", type=parse_bool, default=None)
    record.add_argument("--safety-pass", dest="safety_pass", action="store_true", default=False,
                        help="certify that the post-run container/service/live-stack safety checks all passed")
    record.add_argument("--no-safety-pass", dest="safety_pass", action="store_false",
                        help="explicitly mark post-run safety as failed or unverified")
    record.add_argument("--k6-exit-nonzero", action="store_true", default=False,
                        help="record that at least one k6 command exited nonzero")
    record.add_argument("--require-headroom", dest="require_headroom", action="store_true", default=False,
                        help="fail closed unless db/backend/frontend cgroup headroom is present and below strict thresholds")
    record.add_argument("--no-require-headroom", dest="require_headroom", action="store_false",
                        help=argparse.SUPPRESS)
    record.add_argument("--notes", default="")
    record.set_defaults(func=record_command)

    compare = sub.add_parser("compare", help="print compact pass-over-pass improvements and regressions")
    compare.add_argument("--ledger", required=True)
    compare.add_argument("--base", required=True)
    compare.add_argument("--head", required=True)
    compare.add_argument("--profile", choices=("4gb", "2gb"), default=None)
    compare.set_defaults(func=compare_command)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
