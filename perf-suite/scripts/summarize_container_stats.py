#!/usr/bin/env python3
"""Summarize cumulative cgroup CPU and disk-I/O counters from a stats CSV."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def number(value):
    try:
        if value in (None, "", "NA"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def summarize(path: Path) -> dict:
    rows_by_container = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows_by_container[row.get("container", "unknown")].append(row)

    containers = {}
    for name, rows in sorted(rows_by_container.items()):
        rows.sort(key=lambda row: row.get("timestamp", ""))
        first, last = rows[0], rows[-1]
        times = [parse_time(row.get("timestamp")) for row in rows]
        times = [value for value in times if value]
        elapsed = (times[-1] - times[0]).total_seconds() if len(times) >= 2 else 0

        def delta(column):
            start = number(first.get(column))
            end = number(last.get(column))
            if start is None or end is None or end < start:
                return None
            return end - start

        memory_values = [number(row.get("cgroup_memory_current_bytes")) for row in rows]
        memory_values = [value for value in memory_values if value is not None]
        peak_values = [number(row.get("cgroup_memory_peak_bytes")) for row in rows]
        peak_values = [value for value in peak_values if value is not None]
        containers[name] = {
            "samples": len(rows),
            "elapsed_seconds": round(elapsed, 3),
            "memory_avg_bytes": round(sum(memory_values) / len(memory_values)) if memory_values else None,
            "memory_peak_bytes": round(max(peak_values)) if peak_values else (round(max(memory_values)) if memory_values else None),
            "cpu_usage_seconds": round((delta("cgroup_cpu_usage_usec") or 0) / 1_000_000, 6),
            "cpu_throttled_seconds": round((delta("cgroup_cpu_throttled_usec") or 0) / 1_000_000, 6),
            "cpu_throttled_periods": int(delta("cgroup_cpu_nr_throttled") or 0),
            "io_read_bytes": int(delta("cgroup_io_read_bytes") or 0),
            "io_write_bytes": int(delta("cgroup_io_write_bytes") or 0),
            "io_read_ops": int(delta("cgroup_io_read_ops") or 0),
            "io_write_ops": int(delta("cgroup_io_write_ops") or 0),
            "restart_count_last": number(last.get("restart_count")),
            "docker_oom_killed_last": last.get("docker_oom_killed"),
        }
    return {"input": str(path), "containers": containers}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    result = summarize(args.csv_path)
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
