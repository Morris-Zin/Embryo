#!/usr/bin/env python3
"""Dev-only, read-only cgroup and SQL telemetry profiler for the isolated perf stack.

This module intentionally uses only the Python standard library. It must run on the
Linux/WSL host that owns the Docker daemon; it never writes to containers or the DB.
"""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree

SCHEMA = "khitmee-perf.telemetry/v1"
RAW_SCHEMA = "khitmee.telemetry.raw/v1"
CGROUP_ROOT = Path("/sys/fs/cgroup")
CPU_COUNTERS = ("usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec")
IO_FIELDS = ("rbytes", "wbytes", "rios", "wios", "dbytes", "dios")
DIGEST_COLUMNS = (
    "SCHEMA_NAME", "DIGEST", "DIGEST_TEXT", "COUNT_STAR", "SUM_TIMER_WAIT",
    "MIN_TIMER_WAIT", "AVG_TIMER_WAIT", "MAX_TIMER_WAIT", "SUM_LOCK_TIME",
    "SUM_ROWS_AFFECTED", "SUM_ROWS_SENT", "SUM_ROWS_EXAMINED",
    "SUM_CREATED_TMP_TABLES", "SUM_CREATED_TMP_DISK_TABLES",
    "SUM_SELECT_FULL_JOIN", "SUM_SORT_ROWS", "SUM_NO_INDEX_USED",
    "SUM_NO_GOOD_INDEX_USED", "FIRST_SEEN", "LAST_SEEN", "QUANTILE_95",
)

BASE_SQL_PROBE = """SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_VARIABLES
WHERE VARIABLE_NAME IN ('VERSION','SERVER_UUID','PERFORMANCE_SCHEMA');
SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS
WHERE VARIABLE_NAME IN ('UPTIME','UPTIME_SINCE_FLUSH_STATUS','QUESTIONS','COM_SELECT','COM_INSERT','COM_UPDATE',
'COM_DELETE','INNODB_ROW_LOCK_WAITS','INNODB_ROW_LOCK_TIME','INNODB_ROW_LOCK_TIME_AVG',
'INNODB_ROW_LOCK_TIME_MAX','INNODB_DEADLOCKS','INNODB_DATA_READ','INNODB_DATA_WRITTEN',
'INNODB_OS_LOG_WRITTEN');
SELECT COLUMN_NAME FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='performance_schema'
AND TABLE_NAME='events_statements_summary_by_digest';
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_key_value_text(text):
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            number = parse_int(parts[1])
            if number is not None:
                values[parts[0]] = number
    return values


def parse_io_stat(text):
    devices = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or ":" not in parts[0]:
            continue
        values = {field: 0 for field in IO_FIELDS}
        for token in parts[1:]:
            key, separator, raw = token.partition("=")
            if separator and key in values:
                number = parse_int(raw)
                if number is not None:
                    values[key] = number
        devices[parts[0]] = values
    return devices


def parse_mariadb_xml_resultsets(output):
    """Parse concatenated mariadb --xml resultset documents into row dictionaries."""
    documents = []
    marker = '<?xml version="1.0"?>'
    for piece in output.split(marker)[1:]:
        text = marker + piece
        root = ElementTree.fromstring(text)
        rows = []
        for row in root.findall("row"):
            values = {}
            for field in row.findall("field"):
                nil = field.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
                values[field.attrib.get("name", "")] = None if nil else (field.text or "")
            rows.append(values)
        documents.append(rows)
    return documents


def counter_delta(start, end, scope, counter, resets):
    start_value = parse_int(start)
    end_value = parse_int(end)
    if start_value is None or end_value is None:
        return None
    if end_value < start_value:
        resets.append({"scope": scope, "counter": counter, "start": start_value, "end": end_value,
                       "impact": counter + " delta is invalid"})
        return None
    return end_value - start_value


def percent(part, whole):
    if part is None or whole in (None, 0):
        return None
    return part / whole * 100.0


def finite(value):
    return isinstance(value, (int, float)) and value == value and value not in (float("inf"), float("-inf"))


def ranking_entries(values):
    total = sum(item["value"] for item in values if finite(item["value"]))
    ordered = sorted(values, key=lambda item: item["value"], reverse=True)
    for index, item in enumerate(ordered, 1):
        item["rank"] = index
        item["share_of_measured_total"] = (item["value"] / total) if total > 0 else None
    return ordered


def digest_key(row):
    return (str(row.get("SCHEMA_NAME") or ""), str(row.get("DIGEST") or row.get("DIGEST_TEXT") or ""))


def numeric_delta(start_row, end_row, field, scope, resets):
    return counter_delta(start_row.get(field), end_row.get(field), scope, "digest." + field, resets)


def analyze_digest(start, end, missing, resets, assumptions):
    start_digest = start.get("digest") or {}
    end_digest = end.get("digest") or {}
    if not start_digest.get("supported") or not end_digest.get("supported"):
        reason = end_digest.get("reason") or start_digest.get("reason") or "digest support unavailable"
        missing.append({"scope": "sql", "item": "sql_statement_digest", "reason": reason,
                        "impact": "SQL statements cannot be attributed or ranked by digest time."})
        return {"supported": False, "reason": reason, "top_by_time": [], "rows_missing_at_end": 0,
                "rows_new_at_end": 0, "resets": 0}

    start_rows = {digest_key(row): row for row in start_digest.get("rows", [])}
    end_rows = {digest_key(row): row for row in end_digest.get("rows", [])}
    rows_missing_at_end = len(set(start_rows) - set(end_rows))
    rows_new_at_end = len(set(end_rows) - set(start_rows))
    reset_count = 0
    if rows_missing_at_end:
        missing.append({"scope": "sql", "item": "sql_statement_digest_continuity",
                        "reason": f"{rows_missing_at_end} digest rows present at start were absent at end",
                        "impact": "Some digest deltas are unavailable; truncation, eviction, or reset cannot be distinguished."})
    if rows_new_at_end:
        assumptions.append({"scope": "sql", "item": "sql_statement_digest_new_rows",
                            "reason": f"{rows_new_at_end} digest rows appeared during the window",
                            "impact": "Their cumulative counters are treated as window-local because no start row existed."})

    analyzed = []
    for key, end_row in end_rows.items():
        start_row = start_rows.get(key)
        start_exists = start_row is not None
        if start_row is None:
            start_row = {field: 0 for field in (
                "COUNT_STAR", "SUM_TIMER_WAIT", "SUM_ROWS_EXAMINED", "SUM_ROWS_SENT",
                "SUM_ROWS_AFFECTED", "SUM_NO_INDEX_USED", "SUM_CREATED_TMP_DISK_TABLES")}
        scope = "sql digest " + (key[1] or "<unknown>")
        if start_exists and start_row.get("FIRST_SEEN") != end_row.get("FIRST_SEEN"):
            resets.append({"scope": scope, "counter": "digest.FIRST_SEEN",
                           "start": start_row.get("FIRST_SEEN"), "end": end_row.get("FIRST_SEEN"),
                           "impact": "digest row was reset or replaced; cumulative delta is invalid"})
            reset_count += 1
            continue
        before = len(resets)
        count_delta = numeric_delta(start_row, end_row, "COUNT_STAR", scope, resets)
        timer_delta = numeric_delta(start_row, end_row, "SUM_TIMER_WAIT", scope, resets)
        rows_examined = numeric_delta(start_row, end_row, "SUM_ROWS_EXAMINED", scope, resets)
        rows_sent = numeric_delta(start_row, end_row, "SUM_ROWS_SENT", scope, resets)
        rows_affected = numeric_delta(start_row, end_row, "SUM_ROWS_AFFECTED", scope, resets)
        no_index = numeric_delta(start_row, end_row, "SUM_NO_INDEX_USED", scope, resets)
        tmp_disk = numeric_delta(start_row, end_row, "SUM_CREATED_TMP_DISK_TABLES", scope, resets)
        if len(resets) != before:
            reset_count += 1
            continue
        if count_delta is None or timer_delta is None:
            continue
        time_ms = timer_delta / 1_000_000_000.0
        analyzed.append({
            "schema_name": end_row.get("SCHEMA_NAME"),
            "digest": end_row.get("DIGEST"),
            "digest_text": end_row.get("DIGEST_TEXT"),
            "count_delta": count_delta,
            "time_ms_delta": time_ms,
            "avg_ms_per_execution": (time_ms / count_delta) if count_delta else None,
            "rows_examined_delta": rows_examined,
            "rows_sent_delta": rows_sent,
            "rows_affected_delta": rows_affected,
            "rows_examined_per_row_sent": ((rows_examined / rows_sent) if rows_sent else None),
            "no_index_used_delta": no_index,
            "created_tmp_disk_tables_delta": tmp_disk,
            "first_seen_start": start_row.get("FIRST_SEEN"),
            "last_seen_end": end_row.get("LAST_SEEN"),
        })
    analyzed.sort(key=lambda row: row["time_ms_delta"], reverse=True)
    if reset_count:
        missing.append({"scope": "sql", "item": "sql_statement_digest_counter_reset",
                        "reason": f"{reset_count} digest rows had decreasing counters",
                        "impact": "Those statements were excluded from digest rankings."})
    return {"supported": True, "reason": None, "top_by_time": analyzed,
            "rows_missing_at_end": rows_missing_at_end, "rows_new_at_end": rows_new_at_end,
            "resets": reset_count}


def timestamp_seconds(start, end):
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (last - first).total_seconds()


def memory_peak(samples):
    values = [((item.get("memory") or {}).get("current_bytes")) for item in samples if item]
    values = [parse_int(value) for value in values]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def analyze_target(name, target, missing, resets, identity_changes):
    scope = "target:" + name
    for change in target.get("identity_changes", []):
        identity_changes.append({"target": name, **change})
    start = target.get("start") or {}
    end = target.get("end") or {}
    samples = target.get("samples") or [start, end]
    target_missing = []
    if target.get("missing_at_start"):
        target_missing.append({"scope": scope, "item": "target", "reason": target["missing_at_start"],
                               "impact": "No cgroup measurement exists for this target."})
    for filename in (target.get("coverage") or {}).get("missing_files", []):
        target_missing.append({"scope": scope, "item": filename,
                               "reason": "cgroup file missing or unreadable",
                               "impact": "Measurements depending on this file are unavailable or incomplete."})
    missing.extend(target_missing)

    cpu_start = start.get("cpu") or {}
    cpu_end = end.get("cpu") or {}
    cpu = {"valid": True}
    for field in CPU_COUNTERS:
        delta = counter_delta(cpu_start.get(field), cpu_end.get(field), scope, "cpu." + field, resets)
        if field.endswith("_usec"):
            cpu[field.replace("_usec", "_delta_seconds")] = (delta / 1_000_000.0) if delta is not None else None
        else:
            cpu[field + "_delta"] = delta
        if delta is None and field in ("usage_usec", "throttled_usec"):
            cpu["valid"] = False
            if cpu_start.get(field) is None or cpu_end.get(field) is None:
                missing.append({"scope": scope, "item": "cpu.stat:" + field,
                                "reason": "counter missing at window boundary",
                                "impact": "CPU cost cannot be ranked for this target."})

    mem_start = start.get("memory") or {}
    mem_end = end.get("memory") or {}
    peak = memory_peak(samples)
    limit = parse_int(mem_end.get("max_bytes") if mem_end.get("max_bytes") is not None else mem_start.get("max_bytes"))
    events_start = mem_start.get("events") or {}
    events_end = mem_end.get("events") or {}
    oom = counter_delta(events_start.get("oom"), events_end.get("oom"), scope, "memory.events.oom", resets)
    oom_kill = counter_delta(events_start.get("oom_kill"), events_end.get("oom_kill"), scope, "memory.events.oom_kill", resets)
    memory = {
        "start_bytes": parse_int(mem_start.get("current_bytes")),
        "end_bytes": parse_int(mem_end.get("current_bytes")),
        "peak_observed_bytes": peak,
        "limit_bytes": limit,
        "limit_unlimited": bool(mem_end.get("max_unlimited") or mem_start.get("max_unlimited")),
        "peak_observed_percent_of_limit": percent(peak, limit),
        "swap_end_bytes": parse_int(mem_end.get("swap_current_bytes")),
        "oom_delta": oom,
        "oom_kill_delta": oom_kill,
    }
    if peak is None:
        missing.append({"scope": scope, "item": "memory.current", "reason": "no readable memory samples",
                        "impact": "Memory cost cannot be ranked for this target."})

    io_start = (start.get("io") or {}).get("devices")
    io_end = (end.get("io") or {}).get("devices")
    io = {"valid": True, "devices": {}, "device_coverage_changed": False}
    if io_start is None or io_end is None:
        io["valid"] = False
        missing.append({"scope": scope, "item": "io.stat", "reason": "counter missing at window boundary",
                        "impact": "Block IO cost cannot be ranked for this target."})
    else:
        stable_devices = set(io_start) & set(io_end)
        if set(io_start) != set(io_end):
            io["device_coverage_changed"] = True
            missing.append({"scope": scope, "item": "io.stat device continuity",
                            "reason": "block device rows differ between window boundaries",
                            "impact": "Only stable device rows are included in IO deltas."})
        for device in sorted(stable_devices):
            values = {}
            for field in IO_FIELDS:
                values[field + "_delta"] = counter_delta(io_start[device].get(field), io_end[device].get(field),
                                                         scope + "/" + device, "io." + field, resets)
                if values[field + "_delta"] is None:
                    io["valid"] = False
            io["devices"][device] = values
        for field in IO_FIELDS:
            per_device = [values[field + "_delta"] for values in io["devices"].values()
                          if values[field + "_delta"] is not None]
            io[field + "_delta"] = sum(per_device) if per_device else None
        if not stable_devices:
            io["valid"] = False

    return {
        "requested": target.get("requested"),
        "service": target.get("service"),
        "container_name": target.get("name"),
        "identity": target.get("identity"),
        "counter_span_seconds": timestamp_seconds(start.get("timestamp"), end.get("timestamp")),
        "sample_count": target.get("sample_count", len(samples)),
        "cgroup_path": (target.get("coverage") or {}).get("cgroup_path"),
        "cpu": cpu,
        "memory": memory,
        "io": io,
    }


def add_ranking(rankings, metric, unit, values):
    rankings.append({"metric": metric, "unit": unit, "entries": ranking_entries(values)})


def build_hypotheses(target_metrics, sql_metrics, rankings):
    hypotheses = []

    def evidence(metric, subject, value, unit, extra=None):
        item = {"metric": metric, "subject": subject, "value": value, "unit": unit}
        if extra:
            item.update(extra)
        return item

    cpu_ranking = next((item for item in rankings if item["metric"] == "cpu_usage_delta_seconds"), None)
    if cpu_ranking and len(cpu_ranking["entries"]) > 1 and cpu_ranking["entries"]:
        top = cpu_ranking["entries"][0]
        if top["share_of_measured_total"] is not None and top["share_of_measured_total"] >= 0.6 and top["value"] > 0.25:
            hypotheses.append({
                "id": "cpu-dominance-" + top["target"],
                "confidence": "medium",
                "statement": f"{top['target']} consumed {top['share_of_measured_total'] * 100:.1f}% of measured target CPU time in this window.",
                "evidence": [evidence("cpu_usage_delta_seconds", top["target"], top["value"], "cpu_seconds",
                                      {"share_of_measured_total": top["share_of_measured_total"]})],
                "next_checks": ["Correlate this target with the workload phase and inspect its hottest SQL or Python path."],
            })

    for target, metrics in target_metrics.items():
        memory = metrics["memory"]
        ratio = memory.get("peak_observed_percent_of_limit")
        if ratio is not None and ratio >= 80.0:
            hypotheses.append({
                "id": "memory-pressure-" + target,
                "confidence": "medium",
                "statement": f"{target} reached {ratio:.1f}% of its cgroup memory limit during the window.",
                "evidence": [
                    evidence("memory_peak_observed_bytes", target, memory.get("peak_observed_bytes"), "bytes"),
                    evidence("memory_limit_bytes", target, memory.get("limit_bytes"), "bytes"),
                    evidence("memory_oom_kill_delta", target, memory.get("oom_kill_delta"), "count"),
                ],
                "next_checks": ["Inspect allocator/workspace growth before raising limits; memory peak alone does not prove a leak."],
            })
        throttled = metrics["cpu"].get("throttled_delta_seconds")
        if throttled and throttled > 0:
            hypotheses.append({
                "id": "cpu-throttling-" + target,
                "confidence": "medium",
                "statement": f"{target} was CPU-throttled for {throttled:.3f} seconds; its cgroup CPU envelope may shape the measurement.",
                "evidence": [evidence("cpu_throttled_delta_seconds", target, throttled, "cpu_seconds")],
                "next_checks": ["Compare cpu.max with the intended experiment envelope before interpreting latency."],
            })
        io = metrics["io"]
        read_bytes = io.get("rbytes_delta")
        write_bytes = io.get("wbytes_delta")
        if write_bytes and write_bytes > 1024 * 1024 and write_bytes >= 2 * (read_bytes or 0):
            hypotheses.append({
                "id": "write-dominated-io-" + target,
                "confidence": "low",
                "statement": f"{target} block IO was write-dominated in this window.",
                "evidence": [
                    evidence("io_write_bytes_delta", target, write_bytes, "bytes"),
                    evidence("io_read_bytes_delta", target, read_bytes or 0, "bytes"),
                ],
                "next_checks": ["Correlate with flush, redo-log, temporary-table, or artifact-write phases before tuning IO."],
            })

    locks = sql_metrics.get("lock_waits") or {}
    lock_count = locks.get("row_lock_waits_delta")
    lock_ms = locks.get("row_lock_time_ms_delta")
    if lock_count:
        hypotheses.append({
            "id": "innodb-row-lock-waits",
            "confidence": "medium",
            "statement": f"InnoDB row lock waits increased by {lock_count}; queued row access contributed measured wait time.",
            "evidence": [
                evidence("innodb_row_lock_waits_delta", "mariadb", lock_count, "count"),
                evidence("innodb_row_lock_time_delta_ms", "mariadb", lock_ms or 0, "milliseconds"),
                evidence("innodb_deadlocks_delta", "mariadb", locks.get("deadlocks_delta") or 0, "count"),
            ],
            "next_checks": ["Capture concurrent transaction shapes and digest text for write statements in the same window."],
        })

    statements = (sql_metrics.get("statements") or {}).get("top_by_time") or []
    if statements:
        top = statements[0]
        rows_sent = top.get("rows_sent_delta") or 0
        rows_examined = top.get("rows_examined_delta") or 0
        if rows_examined >= 1000 and (not rows_sent or rows_examined / max(rows_sent, 1) >= 100):
            hypotheses.append({
                "id": "digest-scan-ratio-" + str(top.get("digest") or "unknown"),
                "confidence": "low",
                "statement": "The top digest by measured time examined at least 100 rows per returned row; indexing or filter selectivity may dominate SQL time.",
                "evidence": [
                    evidence("sql_digest_time_delta_ms", top.get("digest"), top.get("time_ms_delta"), "milliseconds"),
                    evidence("sql_digest_rows_examined_delta", top.get("digest"), rows_examined, "rows"),
                    evidence("sql_digest_rows_sent_delta", top.get("digest"), rows_sent, "rows"),
                    evidence("sql_digest_no_index_used_delta", top.get("digest"), top.get("no_index_used_delta") or 0, "count"),
                ],
                "next_checks": ["Run EXPLAIN on the digest shape with representative constants; do not tune from digest text alone."],
            })
    return hypotheses


def build_report(raw, include_samples=False):
    missing = []
    resets = []
    identity_changes = []
    assumptions = list(raw.get("assumptions", []))
    targets_raw = raw.get("targets") or {}
    target_metrics = {}
    for name in sorted(targets_raw):
        target_metrics[name] = analyze_target(name, targets_raw[name], missing, resets, identity_changes)

    sql_raw = raw.get("sql") or {}
    sql_enabled = sql_raw.get("enabled", True)
    sql_metrics = {"enabled": sql_enabled}
    if sql_enabled:
        assumptions.append({"scope": "sql", "item": "observer_effect",
                            "reason": "SQL boundary snapshots execute the mariadb client inside the db container cgroup just outside the cgroup boundaries",
                            "impact": "Boundary alignment is not atomic; asynchronous probe work can cross into the window, so compare like-for-like runs."})
        start = sql_raw.get("start") or {}
        end = sql_raw.get("end") or {}
        for error in list(sql_raw.get("errors", [])) + list(start.get("errors", [])) + list(end.get("errors", [])):
            missing.append({"scope": "sql", "item": "sql_probe", "reason": str(error),
                            "impact": "Some SQL telemetry is unavailable."})
        start_server = start.get("server") or {}
        end_server = end.get("server") or {}
        server_uuid_start = start_server.get("SERVER_UUID")
        server_uuid_end = end_server.get("SERVER_UUID")
        if server_uuid_start and server_uuid_end and server_uuid_start != server_uuid_end:
            identity_changes.append({"target": "mariadb", "field": "server_uuid", "before": server_uuid_start,
                                     "after": server_uuid_end, "at": raw.get("ended_at")})
        start_status = start.get("status") or {}
        end_status = end.get("status") or {}
        uptime_delta = counter_delta(start_status.get("UPTIME"), end_status.get("UPTIME"), "sql", "sql.UPTIME", resets)
        flush_uptime_delta = counter_delta(start_status.get("UPTIME_SINCE_FLUSH_STATUS"),
                                           end_status.get("UPTIME_SINCE_FLUSH_STATUS"),
                                           "sql", "sql.UPTIME_SINCE_FLUSH_STATUS", resets)
        questions_delta = counter_delta(start_status.get("QUESTIONS"), end_status.get("QUESTIONS"), "sql", "sql.QUESTIONS", resets)
        lock_waits = {}
        for status_name, output_name in (
                ("INNODB_ROW_LOCK_WAITS", "row_lock_waits_delta"),
                ("INNODB_ROW_LOCK_TIME", "row_lock_time_ms_delta"),
                ("INNODB_DEADLOCKS", "deadlocks_delta")):
            lock_waits[output_name] = counter_delta(start_status.get(status_name), end_status.get(status_name),
                                                    "sql", "sql." + status_name, resets)
            if lock_waits[output_name] is None:
                missing.append({"scope": "sql", "item": status_name,
                                "reason": "global status counter missing at window boundary",
                                "impact": "InnoDB lock-wait deltas are incomplete."})
        statements = analyze_digest(start, end, missing, resets, assumptions)
        sql_metrics.update({
            "server": {"version": end_server.get("VERSION") or start_server.get("VERSION"),
                       "performance_schema": end_server.get("PERFORMANCE_SCHEMA") or start_server.get("PERFORMANCE_SCHEMA"),
                       "server_uuid_present": bool(server_uuid_end or server_uuid_start)},
            "uptime_seconds_delta": uptime_delta,
            "uptime_since_flush_status_seconds_delta": flush_uptime_delta,
            "questions_delta": questions_delta,
            "lock_waits": lock_waits,
            "statements": statements,
        })

    rankings = []
    add_ranking(rankings, "cpu_usage_delta_seconds", "cpu_seconds", [
        {"target": name, "value": metrics["cpu"].get("usage_delta_seconds")}
        for name, metrics in target_metrics.items()
        if metrics["cpu"].get("valid") and finite(metrics["cpu"].get("usage_delta_seconds"))
    ])
    add_ranking(rankings, "cpu_throttled_delta_seconds", "cpu_seconds", [
        {"target": name, "value": metrics["cpu"].get("throttled_delta_seconds")}
        for name, metrics in target_metrics.items()
        if metrics["cpu"].get("valid") and finite(metrics["cpu"].get("throttled_delta_seconds"))
    ])
    add_ranking(rankings, "memory_peak_observed_bytes", "bytes", [
        {"target": name, "value": metrics["memory"].get("peak_observed_bytes")}
        for name, metrics in target_metrics.items()
        if finite(metrics["memory"].get("peak_observed_bytes"))
    ])
    add_ranking(rankings, "io_read_bytes_delta", "bytes", [
        {"target": name, "value": metrics["io"].get("rbytes_delta")}
        for name, metrics in target_metrics.items()
        if metrics["io"].get("valid") and finite(metrics["io"].get("rbytes_delta"))
    ])
    add_ranking(rankings, "io_write_bytes_delta", "bytes", [
        {"target": name, "value": metrics["io"].get("wbytes_delta")}
        for name, metrics in target_metrics.items()
        if metrics["io"].get("valid") and finite(metrics["io"].get("wbytes_delta"))
    ])
    if sql_enabled:
        statements = (sql_metrics.get("statements") or {}).get("top_by_time") or []
        add_ranking(rankings, "sql_digest_time_delta_ms", "milliseconds", [
            {"digest": item.get("digest"), "schema_name": item.get("schema_name"),
             "digest_text": item.get("digest_text"), "value": item.get("time_ms_delta")}
            for item in statements if finite(item.get("time_ms_delta"))
        ])
        locks = sql_metrics.get("lock_waits") or {}
        add_ranking(rankings, "innodb_row_lock_waits_delta", "count", [
            {"target": "mariadb", "value": locks.get("row_lock_waits_delta")}
        ] if finite(locks.get("row_lock_waits_delta")) else [])
        add_ranking(rankings, "innodb_row_lock_time_delta_ms", "milliseconds", [
            {"target": "mariadb", "value": locks.get("row_lock_time_ms_delta")}
        ] if finite(locks.get("row_lock_time_ms_delta")) else [])

    if resets or identity_changes:
        status = "failed"
    elif missing:
        status = "incomplete"
    else:
        status = "complete"

    report = {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "project": raw.get("project"),
        "target_scope": raw.get("target_scope", sorted(targets_raw)),
        "status": status,
        "window": {"started_at": raw.get("started_at"), "ended_at": raw.get("ended_at"),
                   "duration_seconds": raw.get("duration_seconds"),
                   "measured_duration_seconds": raw.get("measured_duration_seconds", raw.get("duration_seconds"))},
        "coverage": {"missing": missing, "resets": resets, "identity_changes": identity_changes,
                     "assumptions": assumptions},
        "targets": target_metrics,
        "sql": sql_metrics,
        "rankings": rankings,
        "hypotheses": build_hypotheses(target_metrics, sql_metrics, rankings),
    }
    if include_samples:
        report["samples"] = {name: target.get("samples", []) for name, target in targets_raw.items()}
    return report


class CollectorError(RuntimeError):
    pass


class Docker:
    def run(self, args, input_text=None, timeout=15):
        try:
            if input_text is None:
                completed = subprocess.run(["docker", *args], stdin=subprocess.DEVNULL, text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=timeout, check=False)
            else:
                completed = subprocess.run(["docker", *args], input=input_text, text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollectorError(f"docker command failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            raise CollectorError(detail)
        return completed.stdout

    def inspect(self, names):
        if not names:
            return []
        try:
            return json.loads(self.run(["inspect", *names]))
        except json.JSONDecodeError as exc:
            raise CollectorError("docker inspect returned invalid JSON") from exc


class LinuxCollector:
    def __init__(self, project, targets, include_sql=True, docker=None):
        self.project = project
        self.requested_targets = targets
        self.include_sql = include_sql
        self.docker = docker or Docker()
        self.targets = {}
        self.last_identity = {}

    def project_containers(self):
        output = self.docker.run(["ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"])
        ids = [line.strip() for line in output.splitlines() if line.strip()]
        infos = self.docker.inspect(ids) if ids else []
        return [info for info in infos
                if (info.get("Config") or {}).get("Labels", {}).get("com.docker.compose.project") == self.project]

    def discover(self):
        infos = self.project_containers()
        if self.requested_targets == ["all"]:
            selected = [info for info in infos if (info.get("State") or {}).get("Running")]
        else:
            selected = []
            for requested in self.requested_targets:
                matches = []
                for info in infos:
                    labels = (info.get("Config") or {}).get("Labels") or {}
                    name = (info.get("Name") or "").lstrip("/")
                    service = labels.get("com.docker.compose.service")
                    if requested in (service, name) or name == f"{self.project}-{requested}-1":
                        matches.append(info)
                if len(matches) > 1:
                    raise CollectorError(f"target {requested!r} is ambiguous")
                if matches:
                    selected.append(matches[0])
                else:
                    self.targets[requested] = {"requested": requested, "missing_at_start": "no matching container in project",
                                               "identity_changes": [], "samples": [], "coverage": {"missing_files": []}}
        for info in selected:
            labels = (info.get("Config") or {}).get("Labels") or {}
            service = labels.get("com.docker.compose.service") or (info.get("Name") or "").lstrip("/")
            key = service
            if key in self.targets:
                key = (info.get("Name") or "").lstrip("/")
            self.targets[key] = self.new_target(info, key)

    def identity_from(self, info):
        state = info.get("State") or {}
        return {"container_id": info.get("Id"), "image_id": info.get("Image"),
                "started_at": state.get("StartedAt"), "pid": state.get("Pid")}

    def new_target(self, info, requested):
        name = (info.get("Name") or "").lstrip("/")
        labels = (info.get("Config") or {}).get("Labels") or {}
        identity = self.identity_from(info)
        self.last_identity[name] = dict(identity)
        return {"requested": requested, "service": labels.get("com.docker.compose.service") or requested,
                "name": name, "identity": identity, "identity_changes": [], "samples": [],
                "coverage": {"missing_files": []}}

    def inspect_target(self, target):
        name = target.get("name")
        if not name:
            return None
        try:
            infos = self.docker.inspect([name])
        except CollectorError:
            infos = []
        if not infos:
            before = self.last_identity.get(name) or {}
            if before.get("container_id") is not None:
                target["identity_changes"].append({"field": "container_exists", "before": True, "after": False,
                                                   "at": utc_now()})
                self.last_identity[name] = {"container_id": None, "image_id": None, "started_at": None, "pid": 0}
            return None
        info = infos[0]
        current = self.identity_from(info)
        before = self.last_identity.get(name) or {}
        for field, current_value in current.items():
            if before.get(field) != current_value:
                target["identity_changes"].append({"field": field, "before": before.get(field),
                                                   "after": current_value, "at": utc_now()})
        self.last_identity[name] = dict(current)
        return info

    def observe_target(self, target):
        if target.get("missing_at_start"):
            return
        info = self.inspect_target(target)
        if info is None:
            target["samples"].append(empty_sample())
            return
        identity = self.identity_from(info)
        sample = read_container_sample(identity.get("container_id"), identity.get("pid"))
        target["samples"].append(sample)
        coverage = target["coverage"]
        if sample.get("cgroup_path") and not coverage.get("cgroup_path"):
            coverage["cgroup_path"] = sample.get("cgroup_path")
        for filename in sample.get("missing_files", []):
            if filename not in coverage["missing_files"]:
                coverage["missing_files"].append(filename)

    def observe(self):
        targets = list(self.targets.values())
        if len(targets) <= 1:
            for target in targets:
                self.observe_target(target)
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
            list(executor.map(self.observe_target, targets))

    def mariadb_xml_query(self, container, sql):
        output = self.docker.run(
            ["exec", "-i", container, "sh", "-c", 'mariadb --xml -uroot -p"$MYSQL_ROOT_PASSWORD"'],
            input_text=sql, timeout=20)
        return parse_mariadb_xml_resultsets(output)

    def sql_snapshot(self):
        try:
            db_container = None
            for info in self.project_containers():
                labels = (info.get("Config") or {}).get("Labels") or {}
                if labels.get("com.docker.compose.service") == "db" and (info.get("State") or {}).get("Running"):
                    db_container = (info.get("Name") or "").lstrip("/")
                    break
            if not db_container:
                raise CollectorError("running db container not found")
            resultsets = self.mariadb_xml_query(db_container, BASE_SQL_PROBE)
            if len(resultsets) != 3:
                raise CollectorError(f"expected 3 SQL resultsets, received {len(resultsets)}")
            server = {str(row.get("VARIABLE_NAME") or "").upper(): row.get("VARIABLE_VALUE")
                      for row in resultsets[0]}
            status = {str(row.get("VARIABLE_NAME") or "").upper(): row.get("VARIABLE_VALUE")
                      for row in resultsets[1]}
            available = {str(row.get("COLUMN_NAME") or "").upper() for row in resultsets[2]}
            performance_schema = str(server.get("PERFORMANCE_SCHEMA") or "").upper()
            digest = {"supported": False, "reason": None, "rows": []}
            if performance_schema not in ("ON", "1", "YES", "TRUE"):
                digest["reason"] = "performance_schema is " + (performance_schema or "unknown")
            elif "COUNT_STAR" not in available or "SUM_TIMER_WAIT" not in available:
                digest["reason"] = "digest table or required timing columns are unavailable"
            else:
                wanted = [name for name in DIGEST_COLUMNS if name in available]
                digest_sql = ("SELECT " + ",".join("`" + name + "`" for name in wanted) +
                              " FROM performance_schema.events_statements_summary_by_digest;\n")
                digest_resultsets = self.mariadb_xml_query(db_container, digest_sql)
                digest = {"supported": True, "reason": None,
                          "rows": digest_resultsets[0] if digest_resultsets else []}
            snapshot = {"server": server, "status": status, "digest": digest, "errors": []}
        except (CollectorError, ElementTree.ParseError, KeyError, TypeError) as exc:
            snapshot = {"server": {}, "status": {},
                        "digest": {"supported": False, "reason": "SQL probe failed", "rows": []},
                        "errors": [str(exc)]}
        snapshot["collected_at"] = utc_now()
        return snapshot

    def collect(self, duration, interval, include_samples=True, sleeper=time.sleep, clock=time.monotonic):
        self.discover()
        started_at = utc_now()
        sql_start = self.sql_snapshot() if self.include_sql else None
        self.observe()
        window_clock_start = clock()
        deadline = window_clock_start + duration
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleeper(min(interval, remaining))
            self.observe()
        measured_duration = clock() - window_clock_start
        sql_end = self.sql_snapshot() if self.include_sql else None
        ended_at = utc_now()
        targets = {}
        for name, target in self.targets.items():
            samples = target.get("samples", [])
            copied = dict(target)
            copied["start"] = samples[0] if samples else None
            copied["end"] = samples[-1] if samples else None
            copied["samples"] = list(samples) if include_samples else [copied["start"], copied["end"]]
            copied["sample_count"] = len(samples)
            targets[name] = copied
        return {"schema": RAW_SCHEMA, "project": self.project, "target_scope": list(self.requested_targets),
                "started_at": started_at, "ended_at": ended_at,
                "duration_seconds": duration, "measured_duration_seconds": measured_duration, "targets": targets,
                "sql": {"enabled": self.include_sql, "start": sql_start or {}, "end": sql_end or {}, "errors": []}}


def empty_sample():
    return {"timestamp": utc_now(), "cgroup_path": None, "missing_files": ["cgroup"],
            "cpu": {}, "memory": {"events": {}}, "io": {"devices": None}}


def read_text(path):
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return None


def resolve_cgroup_path(container_id, pid):
    pid_number = parse_int(pid)
    if pid_number and pid_number > 0:
        cgroup_text = read_text(Path("/proc") / str(pid_number) / "cgroup")
        if cgroup_text:
            for line in cgroup_text.splitlines():
                parts = line.split(":", 2)
                if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
                    candidate = CGROUP_ROOT / parts[2].lstrip("/")
                    if (candidate / "cgroup.procs").exists():
                        return candidate
    container_id = str(container_id or "")
    candidates = [
        CGROUP_ROOT / "system.slice" / f"docker-{container_id}.scope",
        CGROUP_ROOT / "docker" / container_id,
        CGROUP_ROOT / "system.slice" / f"containerd-{container_id}.scope",
        CGROUP_ROOT / "containerd" / container_id,
    ]
    for candidate in candidates:
        if (candidate / "cgroup.procs").exists():
            return candidate
    # Bounded fallback for runtimes that do not use the standard systemd slice.
    try:
        for root, dirs, _files in os.walk(CGROUP_ROOT):
            depth = len(Path(root).relative_to(CGROUP_ROOT).parts)
            if depth >= 5:
                dirs[:] = []
                continue
            for dirname in list(dirs):
                if dirname in (container_id, f"docker-{container_id}.scope", f"containerd-{container_id}.scope"):
                    candidate = Path(root) / dirname
                    if (candidate / "cgroup.procs").exists():
                        return candidate
    except OSError:
        pass
    return None


def parse_memory_max(text):
    if text is None:
        return None, False
    stripped = text.strip()
    if stripped == "max":
        return None, True
    return parse_int(stripped), False


def read_container_sample(container_id, pid):
    path = resolve_cgroup_path(container_id, pid)
    if path is None:
        return empty_sample()
    missing = []
    cpu = {}
    cpu_text = read_text(path / "cpu.stat")
    if cpu_text is None:
        missing.append("cpu.stat")
    else:
        cpu = parse_key_value_text(cpu_text)
    cpu_max = read_text(path / "cpu.max")
    if cpu_max is None:
        missing.append("cpu.max")

    memory = {}
    memory_current = read_text(path / "memory.current")
    if memory_current is None:
        missing.append("memory.current")
    else:
        memory["current_bytes"] = parse_int(memory_current)
    memory_max = read_text(path / "memory.max")
    if memory_max is None:
        missing.append("memory.max")
        memory["max_bytes"] = None
        memory["max_unlimited"] = False
    else:
        memory["max_bytes"], memory["max_unlimited"] = parse_memory_max(memory_max)
    memory_peak = read_text(path / "memory.peak")
    memory["peak_bytes"] = parse_int(memory_peak) if memory_peak is not None else None
    memory_swap = read_text(path / "memory.swap.current")
    memory["swap_current_bytes"] = parse_int(memory_swap) if memory_swap is not None else None
    memory_events = read_text(path / "memory.events")
    if memory_events is None:
        missing.append("memory.events")
        memory["events"] = {}
    else:
        memory["events"] = parse_key_value_text(memory_events)

    io_text = read_text(path / "io.stat")
    if io_text is None:
        missing.append("io.stat")
        devices = None
    else:
        devices = parse_io_stat(io_text)
    return {"timestamp": utc_now(), "cgroup_path": str(path), "missing_files": missing,
            "cpu": cpu, "cpu_max": cpu_max.strip() if cpu_max else None,
            "memory": memory, "io": {"devices": devices}}


def parse_targets(value):
    targets = []
    for item in value.split(","):
        item = item.strip()
        if item and item not in targets:
            targets.append(item)
    if not targets:
        raise argparse.ArgumentTypeError("at least one target is required")
    return targets


def make_parser():
    parser = argparse.ArgumentParser(
        prog="khitmee-telemetry",
        description="Dev-only read-only profiler for scoped khitmee-perf containers.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    profile = subcommands.add_parser("profile", help="collect a profiling window and emit JSON")
    profile.add_argument("--project", default="khitmee-perf", help="Compose project label to scope collection")
    profile.add_argument("--targets", type=parse_targets, default="db,backend",
                         help="comma-separated Compose services/container names, or 'all' for running project containers")
    profile.add_argument("--duration", type=float, default=10.0, help="profiling window seconds (default: 10)")
    profile.add_argument("--interval", type=float, default=1.0, help="cgroup sample interval seconds (default: 1)")
    profile.add_argument("--output", type=Path, help="also write the JSON report to this path")
    profile.add_argument("--include-samples", action="store_true", help="include raw per-interval samples in the JSON")
    profile.add_argument("--no-sql", action="store_true", help="explicitly exclude SQL digest and InnoDB lock telemetry")
    profile.add_argument("--strict", action="store_true", help="exit 2 when coverage is incomplete; failed measurements always exit 3")
    return parser


def validate_args(args):
    if args.project != "khitmee-perf":
        raise ValueError("dev-only profiler refuses projects other than khitmee-perf")
    if not (0.1 <= args.duration <= 300.0):
        raise ValueError("--duration must be between 0.1 and 300 seconds")
    if not (0.1 <= args.interval <= 60.0):
        raise ValueError("--interval must be between 0.1 and 60 seconds")


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        collector = LinuxCollector(args.project, args.targets, include_sql=not args.no_sql)
        raw = collector.collect(args.duration, args.interval, include_samples=True)
        report = build_report(raw, include_samples=args.include_samples)
    except (CollectorError, ValueError) as exc:
        print(f"khitmee-telemetry: {exc}", file=sys.stderr)
        return 2
    output = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    print(output, end="")
    if report["status"] == "failed":
        return 3
    if report["status"] == "incomplete" and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
