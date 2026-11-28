#!/usr/bin/env python3
"""Verify and record effective Docker CPU/memory limits for the isolated seasonal stack."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

PROJECT = "khitmee-perf"
SERVICES = ("db", "backend", "queue", "scheduler", "redis-cache", "redis-queue", "websocket", "frontend", "configurator")
RUNNING_SERVICES = SERVICES[:-1]
GIB = 1024 ** 3


def docker_json(args):
    result = subprocess.run(["docker", *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("docker " + args[0] + " failed: " + (result.stderr or result.stdout)[:240])
    return json.loads(result.stdout)


def inspect_containers():
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT],
        text=True, capture_output=True,
    )
    if ids.returncode:
        raise RuntimeError("cannot list khitmee-perf containers")
    items = []
    for container_id in filter(None, ids.stdout.splitlines()):
        data = docker_json(["inspect", container_id])
        if isinstance(data, list):
            items.extend(data)
        else:
            items.append(data)
    return items


def proof():
    containers = inspect_containers()
    rows = []
    problems = []
    memory_total = 0
    cpu_total = 0.0
    for service in SERVICES:
        matches = [c for c in containers if (c.get("Config") or {}).get("Labels", {}).get("com.docker.compose.service") == service]
        if not matches:
            problems.append("missing container for service " + service)
            continue
        # Compose can leave an old container during replacement; use the newest one.
        info = sorted(matches, key=lambda item: item.get("Created") or "")[-1]
        host = info.get("HostConfig") or {}
        state = info.get("State") or {}
        memory = int(host.get("Memory") or 0)
        nano = int(host.get("NanoCpus") or 0)
        cpus = nano / 1e9
        row = {
            "service": service,
            "container": info.get("Name", "").lstrip("/"),
            "id": (info.get("Id") or "")[:12],
            "running": bool(state.get("Running")),
            "memory_limit_bytes": memory,
            "cpu_limit": cpus,
            "restart_count": int(state.get("RestartCount") or 0),
            "oom_killed": bool(state.get("OOMKilled")),
            "image": (info.get("Config") or {}).get("Image"),
        }
        rows.append(row)
        if service in RUNNING_SERVICES and not row["running"]:
            problems.append(service + " is not running")
        if memory <= 0:
            problems.append(service + " has no memory limit")
        if nano <= 0:
            problems.append(service + " has no CPU limit")
        memory_total += memory
        cpu_total += cpus
    if memory_total > 2 * GIB:
        problems.append(f"container memory limits total {memory_total} bytes, above 2 GiB")
    if cpu_total > 2.0000001:
        problems.append(f"container CPU limits total {cpu_total:.3f}, above 2 vCPU")
    return {
        "passed": not problems,
        "project": PROJECT,
        "memory_limit_total_bytes": memory_total,
        "memory_limit_total_mib": memory_total / 1024**2,
        "cpu_limit_total": cpu_total,
        "services": rows,
        "problems": problems,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = proof()
    except Exception as exc:
        result = {"passed": False, "problems": [str(exc)], "project": PROJECT}
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
