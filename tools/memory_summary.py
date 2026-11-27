#!/usr/bin/env python3
"""Summarize khitmee-perf memory/CPU collector CSVs without external dependencies."""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

MIB = 1024.0 * 1024.0


def parse_float(value):
    try:
        if value in (None, "", "NA"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def mib_from_bytes(value):
    return None if value is None else value / MIB


def mib_from_kb(value):
    return None if value is None else value / 1024.0


def rounded(value, digits=2):
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(value, digits)


def read_rows(path, start=None, end=None):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if not row.get("timestamp") or not row.get("container"):
                continue
            try:
                ts = parse_time(row["timestamp"])
            except (TypeError, ValueError):
                continue
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            row["_ts"] = ts
            rows.append(row)
    return rows


def mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def peak(values):
    values = [value for value in values if value is not None]
    return max(values) if values else None


def cpu_percent(rows):
    ordered = sorted(rows, key=lambda row: row["_ts"])
    usage_before = None
    time_before = None
    throttled_before = None
    usage_delta = 0.0
    throttle_delta = 0.0
    elapsed = 0.0
    for row in ordered:
        usage = parse_float(row.get("cgroup_cpu_usage_usec"))
        throttled = parse_float(row.get("cgroup_cpu_throttled_usec"))
        ts = row["_ts"]
        if usage_before is not None and usage is not None and ts > time_before and usage >= usage_before:
            usage_delta += usage - usage_before
            elapsed += ts - time_before
            if throttled_before is not None and throttled is not None and throttled >= throttled_before:
                throttle_delta += throttled - throttled_before
        if usage is not None:
            usage_before = usage
            time_before = ts
            throttled_before = throttled
    if elapsed <= 0:
        return None, None
    return usage_delta / (elapsed * 1_000_000.0) * 100.0, throttle_delta / max(usage_delta, 1.0) * 100.0


def summarize_container(rows):
    cpu, throttle_ratio = cpu_percent(rows)
    return {
        "samples": len(rows),
        "rss_mib_avg": rounded(mean([mib_from_kb(parse_float(row.get("rss_kb"))) for row in rows])),
        "rss_mib_peak": rounded(peak([mib_from_kb(parse_float(row.get("rss_kb"))) for row in rows])),
        "pss_mib_avg": rounded(mean([mib_from_kb(parse_float(row.get("pss_kb"))) for row in rows])),
        "pss_mib_peak": rounded(peak([mib_from_kb(parse_float(row.get("pss_kb"))) for row in rows])),
        "private_dirty_mib_avg": rounded(mean([mib_from_kb(parse_float(row.get("private_dirty_kb"))) for row in rows])),
        "private_dirty_mib_peak": rounded(peak([mib_from_kb(parse_float(row.get("private_dirty_kb"))) for row in rows])),
        "cgroup_current_mib_avg": rounded(mean([mib_from_bytes(parse_float(row.get("cgroup_memory_current_bytes"))) for row in rows])),
        "cgroup_current_mib_peak": rounded(peak([mib_from_bytes(parse_float(row.get("cgroup_memory_current_bytes"))) for row in rows])),
        "cgroup_peak_mib_max": rounded(peak([mib_from_bytes(parse_float(row.get("cgroup_memory_peak_bytes"))) for row in rows])),
        "anon_mib_avg": rounded(mean([mib_from_bytes(parse_float(row.get("cgroup_memory_anon_bytes"))) for row in rows])),
        "file_mib_avg": rounded(mean([mib_from_bytes(parse_float(row.get("cgroup_memory_file_bytes"))) for row in rows])),
        "kernel_mib_avg": rounded(mean([mib_from_bytes(parse_float(row.get("cgroup_memory_kernel_bytes"))) for row in rows])),
        "slab_mib_avg": rounded(mean([mib_from_bytes(parse_float(row.get("cgroup_memory_slab_bytes"))) for row in rows])),
        "cpu_percent_avg": rounded(cpu),
        "cpu_throttled_percent_of_usage": rounded(throttle_ratio),
        "oom_kill_max": rounded(peak([parse_float(row.get("cgroup_oom_kill")) for row in rows])),
        "restart_count_max": rounded(peak([parse_float(row.get("restart_count")) for row in rows])),
    }


def fleet_by_timestamp(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["_ts"]].append(row)
    points = []
    for ts, group in grouped.items():
        def total(key, converter):
            values = [converter(parse_float(row.get(key))) for row in group]
            if any(value is None for value in values):
                return None
            return sum(values)
        points.append({
            "ts": ts,
            "pss_mib": total("pss_kb", mib_from_kb),
            "private_dirty_mib": total("private_dirty_kb", mib_from_kb),
            "rss_mib": total("rss_kb", mib_from_kb),
            "cgroup_mib": total("cgroup_memory_current_bytes", mib_from_bytes),
        })
    return sorted(points, key=lambda point: point["ts"])


def summarize_fleet(rows):
    points = fleet_by_timestamp(rows)
    cpu_rows = defaultdict(list)
    for row in rows:
        cpu_rows[row["container"]].append(row)
    total_cpu = 0.0
    any_cpu = False
    for container_rows in cpu_rows.values():
        cpu, _throttle = cpu_percent(container_rows)
        if cpu is not None:
            total_cpu += cpu
            any_cpu = True
    return {
        "timestamps": len(points),
        "rss_mib_avg": rounded(mean([point["rss_mib"] for point in points])),
        "rss_mib_peak": rounded(peak([point["rss_mib"] for point in points])),
        "pss_mib_avg": rounded(mean([point["pss_mib"] for point in points])),
        "pss_mib_peak": rounded(peak([point["pss_mib"] for point in points])),
        "private_dirty_mib_avg": rounded(mean([point["private_dirty_mib"] for point in points])),
        "private_dirty_mib_peak": rounded(peak([point["private_dirty_mib"] for point in points])),
        "cgroup_mib_avg": rounded(mean([point["cgroup_mib"] for point in points])),
        "cgroup_mib_peak": rounded(peak([point["cgroup_mib"] for point in points])),
        "cpu_percent_avg": rounded(total_cpu) if any_cpu else None,
    }


def summarize(path, start=None, end=None):
    rows = read_rows(path, start=start, end=end)
    containers = defaultdict(list)
    for row in rows:
        containers[row["container"]].append(row)
    return {
        "schema_version": 1,
        "input": str(path),
        "sample_count": len(rows),
        "window": {
            "start": datetime.fromtimestamp(min((row["_ts"] for row in rows), default=0)).isoformat() if rows else None,
            "end": datetime.fromtimestamp(max((row["_ts"] for row in rows), default=0)).isoformat() if rows else None,
        },
        "fleet": summarize_fleet(rows),
        "containers": {name: summarize_container(container_rows) for name, container_rows in sorted(containers.items())},
    }


def markdown(summary):
    lines = [
        "| container | PSS avg MiB | PSS peak MiB | private peak MiB | cgroup avg MiB | cgroup peak MiB | CPU avg % | OOM | restarts |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in summary["containers"].items():
        lines.append(
            "| {name} | {pss_avg} | {pss_peak} | {private_peak} | {cg_avg} | {cg_peak} | {cpu} | {oom} | {restarts} |".format(
                name=name,
                pss_avg=row["pss_mib_avg"],
                pss_peak=row["pss_mib_peak"],
                private_peak=row["private_dirty_mib_peak"],
                cg_avg=row["cgroup_current_mib_avg"],
                cg_peak=row["cgroup_current_mib_peak"],
                cpu=row["cpu_percent_avg"],
                oom=row["oom_kill_max"],
                restarts=row["restart_count_max"],
            )
        )
    fleet = summary["fleet"]
    lines.extend([
        "",
        "Fleet: PSS avg {pss_avg} MiB, PSS peak {pss_peak} MiB, private peak {private_peak} MiB, cgroup peak {cg_peak} MiB, CPU avg {cpu}%.".format(
            pss_avg=fleet["pss_mib_avg"],
            pss_peak=fleet["pss_mib_peak"],
            private_peak=fleet["private_dirty_mib_peak"],
            cg_peak=fleet["cgroup_mib_peak"],
            cpu=fleet["cpu_percent_avg"],
        ),
    ])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--start", help="inclusive ISO timestamp")
    parser.add_argument("--end", help="inclusive ISO timestamp")
    parser.add_argument("--json-out")
    parser.add_argument("--markdown-out")
    args = parser.parse_args(argv)
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    summary = summarize(Path(args.csv), start=start, end=end)
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8", newline="\n")
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown(summary), encoding="utf-8", newline="\n")
    if not args.json_out:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
