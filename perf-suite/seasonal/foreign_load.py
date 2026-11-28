#!/usr/bin/env python3
"""Record running Docker containers outside the isolated khitmee-perf project."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

PROJECT = "khitmee-perf"
# khitmee-erpnext is the permanent live project that shard rules forbid us to
# stop. It is recorded for context, but it is not treated as a concurrent
# benchmark/foreign load. Every other non-perf container invalidates timing.
BASELINE_PROJECTS = {"khitmee-erpnext"}


def docker(args):
    result = subprocess.run(["docker", *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[:300])
    return result.stdout


def record():
    ids = [line for line in docker(["ps", "-q"]).splitlines() if line]
    containers = []
    if ids:
        for info in json.loads(docker(["inspect", *ids])):
            labels = (info.get("Config") or {}).get("Labels") or {}
            containers.append({
                "id": (info.get("Id") or "")[:12],
                "name": (info.get("Name") or "").lstrip("/"),
                "image": (info.get("Config") or {}).get("Image") or "",
                "project": labels.get("com.docker.compose.project") or "",
                "status": (info.get("State") or {}).get("Status") or "",
            })
    other = [row for row in containers if row["project"] != PROJECT]
    baseline = [row for row in other if row["project"] in BASELINE_PROJECTS]
    foreign = [row for row in other if row["project"] not in BASELINE_PROJECTS]
    return {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project_excluded": PROJECT,
        "baseline_projects_excluded_from_foreign_load": sorted(BASELINE_PROJECTS),
        "all_running_count": len(containers),
        "other_running_count": len(other),
        "other_running_containers": sorted(other, key=lambda row: row["name"]),
        "baseline_count": len(baseline),
        "baseline_containers": sorted(baseline, key=lambda row: row["name"]),
        "foreign_count": len(foreign),
        "foreign_containers": sorted(foreign, key=lambda row: row["name"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = record()
    except Exception as exc:
        result = {"foreign_count": -1, "foreign_containers": [], "error": str(exc)}
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("foreign_count", -1) >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
