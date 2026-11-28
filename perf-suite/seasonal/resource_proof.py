#!/usr/bin/env python3
"""Verify and record effective Docker CPU/memory limits for the isolated seasonal stack."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

PROJECT = "khitmee-perf"
SERVICES = ("db", "backend", "cashier", "queue", "scheduler", "redis-cache", "redis-queue", "websocket", "frontend", "configurator")
RUNNING_SERVICES = SERVICES[:-1]
# The ADR 0004 guest profile allocates per-service memory safety ceilings
# totaling 1728 MiB for the nine long-running workload containers, leaving at
# least 256 MiB of the 2 GiB guest for the kernel, runtime, page cache and the
# short-lived configurator. Child CPU quota is intentionally unlimited; the
# parent slice is the authoritative two-CPU boundary and child cpu_shares are
# only relative weights.
WORKLOAD_MEMORY_BUDGET_MIB = 1728
GIB = 1024 ** 3
EXPECTED_MEMORY_MIB = {
    "configurator": 64,
    "db": 544,
    "backend": 224,
    "cashier": 416,
    "queue": 256,
    "scheduler": 96,
    "redis-cache": 64,
    "redis-queue": 32,
    "websocket": 72,
    "frontend": 24,
}
EXPECTED_CPU_SHARES = {
    "configurator": 1024,
    "db": 4050,
    "backend": 1024,
    "cashier": 4050,
    "queue": 416,
    "scheduler": 212,
    "redis-cache": 1703,
    "redis-queue": 1284,
    "websocket": 311,
    "frontend": 1024,
}
EXPECTED_CGROUP_PARENT = "khitmeeperf.slice"


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




def cgroup_limits(info):
    """Read the kernel-enforced cgroup v2 memory/CPU limits for a running container."""
    state = info.get("State") or {}
    pid = int(state.get("Pid") or 0)
    if not state.get("Running") or pid <= 0:
        return {"cgroup_memory_max_bytes": None, "cgroup_cpu_limit": None, "cgroup_cpu_max_text": "", "cgroup_path": ""}
    try:
        relative = ""
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[0] == "0":
                    relative = parts[2]
                    break
        if not relative:
            return {"cgroup_memory_max_bytes": None, "cgroup_cpu_limit": None, "cgroup_cpu_max_text": "", "cgroup_path": ""}
        root = Path("/sys/fs/cgroup" + relative)
        memory_text = (root / "memory.max").read_text(encoding="utf-8").strip()
        cpu_text = (root / "cpu.max").read_text(encoding="utf-8").strip().split()
        memory = None if memory_text == "max" else int(memory_text)
        cpu = None
        if len(cpu_text) >= 2 and cpu_text[0] != "max":
            cpu = int(cpu_text[0]) / int(cpu_text[1])
        return {"cgroup_memory_max_bytes": memory, "cgroup_cpu_limit": cpu, "cgroup_cpu_max_text": " ".join(cpu_text), "cgroup_path": str(root)}
    except (OSError, ValueError, IndexError):
        return {"cgroup_memory_max_bytes": None, "cgroup_cpu_limit": None, "cgroup_cpu_max_text": "", "cgroup_path": ""}


def proof():
    containers = inspect_containers()
    rows = []
    problems = []
    memory_total = 0
    workload_memory_total = 0
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
        restart_count = None
        restart_evidence_ok = "RestartCount" in info
        if restart_evidence_ok:
            try:
                restart_count = int(info.get("RestartCount"))
            except (TypeError, ValueError):
                restart_evidence_ok = False
        row = {
            "service": service,
            "container": info.get("Name", "").lstrip("/"),
            "id": info.get("Id") or "",
            "running": bool(state.get("Running")),
            "started_at": state.get("StartedAt") or "",
            "pid": int(state.get("Pid") or 0),
            "memory_limit_bytes": memory,
            "cpu_limit": cpus,
            "cpu_shares": int(host.get("CpuShares") or 0),
            "cgroup_parent": host.get("CgroupParent") or "",
            "restart_count": restart_count,
            "restart_evidence_ok": restart_evidence_ok,
            "oom_killed": bool(state.get("OOMKilled")),
            "image": (info.get("Config") or {}).get("Image"),
        }
        row.update(cgroup_limits(info))
        rows.append(row)
        if service in RUNNING_SERVICES and not row["running"]:
            problems.append(service + " is not running")
        if not restart_evidence_ok:
            problems.append(service + " is missing top-level Docker RestartCount evidence")
        if memory <= 0:
            problems.append(service + " has no memory limit")
        expected_memory = EXPECTED_MEMORY_MIB[service] * 1024**2
        if memory != expected_memory:
            problems.append(service + f" memory limit is {memory}, expected ADR guest safety ceiling {expected_memory}")
        if nano != 0:
            problems.append(service + " has a child CPU quota; ADR guest children must rely on the parent quota")
        if row["cgroup_parent"] != EXPECTED_CGROUP_PARENT:
            problems.append(service + " is not a child of " + EXPECTED_CGROUP_PARENT)
        if row["cpu_shares"] != EXPECTED_CPU_SHARES[service]:
            problems.append(service + f" cpu shares are {row['cpu_shares']}, expected {EXPECTED_CPU_SHARES[service]}")
        if service in RUNNING_SERVICES and row["running"]:
            if row.get("cgroup_memory_max_bytes") != memory:
                problems.append(service + " cgroup memory.max does not match the configured memory limit")
            if not str(row.get("cgroup_cpu_max_text") or "").split()[:1] == ["max"]:
                problems.append(service + " child cgroup cpu.max is not unlimited inside the parent slice")
        memory_total += memory
        if service in RUNNING_SERVICES:
            workload_memory_total += memory
        cpu_total += cpus
    measurement_problems = list(problems)
    budget_problems = []
    workload_budget_bytes = WORKLOAD_MEMORY_BUDGET_MIB * 1024**2
    if workload_memory_total > workload_budget_bytes:
        budget_problems.append(
            f"long-running container memory limits total {workload_memory_total} bytes, "
            f"above the {WORKLOAD_MEMORY_BUDGET_MIB} MiB workload budget after 256 MiB minimum OS/runtime headroom"
        )
    return {
        "passed": not measurement_problems and not budget_problems,
        "measurement_passed": not measurement_problems,
        "within_budget": not budget_problems,
        "project": PROJECT,
        "memory_limit_total_bytes": memory_total,
        "memory_limit_total_mib": memory_total / 1024**2,
        "workload_memory_limit_total_bytes": workload_memory_total,
        "workload_memory_limit_total_mib": workload_memory_total / 1024**2,
        "workload_memory_budget_mib": WORKLOAD_MEMORY_BUDGET_MIB,
        "os_headroom_mib": 256,
        "cpu_limit_total": cpu_total,
        "services": rows,
        "measurement_problems": measurement_problems,
        "budget_problems": budget_problems,
        "problems": measurement_problems + budget_problems,
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
