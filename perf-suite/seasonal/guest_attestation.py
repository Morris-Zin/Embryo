#!/usr/bin/env python3
"""Attest the ADR 0004 dedicated guest and whole-guest cgroup envelope.

A correctly configured cgroup slice on a larger host is useful diagnostic
evidence, but it is not qualification evidence. Qualification requires both
the ADR parent slice and a dedicated guest whose online CPU, memory and swap
shape match the 2 vCPU / 2 GiB VPS being represented.
"""

import argparse
import json
import pathlib
import subprocess
import sys

SERVICES = ("db", "backend", "cashier", "queue", "scheduler", "redis-cache", "redis-queue", "websocket", "frontend")
EXPECTED_CPU_MAX = "200000 100000"
EXPECTED_MEMORY_MAX = 2 * 1024**3
REQUIRED_ONLINE_CPUS = 2
EXPECTED_ASSIGNED_MEMORY_MIB = 2048.0
# MemTotal is the kernel-visible remainder after firmware/hypervisor/kernel
# reservation, so it never equals the nominal assignment exactly. A +/-10%
# envelope accepts normal reservation overhead while rejecting larger hosts
# such as 8 CPU / 16 GiB and materially undersized guests.
ASSIGNED_MEMORY_TOLERANCE_RATIO = 0.10


def read_text(path):
    return pathlib.Path(path).read_text(encoding="utf-8").strip()


def expand_cpu_spec(spec):
    cpus = set()
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            cpus.update(range(int(first), int(last) + 1))
        elif part.isdigit():
            cpus.add(int(part))
    return cpus


def cpuset_count(value):
    return len(expand_cpu_spec(value))


def online_cpu_count():
    try:
        spec = pathlib.Path("/sys/devices/system/cpu/online").read_text(encoding="utf-8").strip()
        return len(expand_cpu_spec(spec)), spec
    except (OSError, ValueError):
        return 0, ""


def guest_fact_problems(facts):
    problems = []
    online = int(facts.get("online_cpu_count") or 0)
    nproc_count = int(facts.get("nproc_count") or 0)
    if online != REQUIRED_ONLINE_CPUS:
        problems.append(f"guest has {online} online CPUs; dedicated qualification requires exactly {REQUIRED_ONLINE_CPUS}")
    if nproc_count != REQUIRED_ONLINE_CPUS:
        problems.append(f"nproc reports {nproc_count} CPUs; dedicated qualification requires exactly {REQUIRED_ONLINE_CPUS}")
    memory_mib = float(facts.get("guest_mem_total_bytes") or 0) / 1024**2
    tolerance = EXPECTED_ASSIGNED_MEMORY_MIB * ASSIGNED_MEMORY_TOLERANCE_RATIO
    low = EXPECTED_ASSIGNED_MEMORY_MIB - tolerance
    high = EXPECTED_ASSIGNED_MEMORY_MIB + tolerance
    if not low <= memory_mib <= high:
        problems.append(
            f"guest MemTotal is {memory_mib:.1f} MiB; dedicated qualification requires about 2048 MiB "
            f"within +/-10% ({low:.1f}..{high:.1f} MiB) for kernel/hypervisor reservation"
        )
    if int(facts.get("guest_swap_total_bytes") or 0) != 0:
        problems.append("guest swap is enabled; dedicated qualification requires swap to be disabled")
    return problems


def guest_facts():
    cpus = 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            cpus = sum(1 for line in handle if line.startswith("processor"))
    except OSError:
        pass
    meminfo = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, rest = line.split(":", 1)
                meminfo[key] = int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    online_count, online_spec = online_cpu_count()
    try:
        nproc = subprocess.check_output(["nproc"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        nproc = ""
    try:
        nproc_count = int(nproc or "0")
    except ValueError:
        nproc_count = 0
    return {
        "guest_visible_cpu_count": cpus,
        "online_cpu_count": online_count,
        "online_cpu_spec": online_spec,
        "nproc": nproc,
        "nproc_count": nproc_count,
        "guest_mem_total_bytes": meminfo.get("MemTotal", 0),
        "guest_mem_total_mib": meminfo.get("MemTotal", 0) / 1024**2,
        "guest_swap_total_bytes": meminfo.get("SwapTotal", 0),
        "required_online_cpus": REQUIRED_ONLINE_CPUS,
        "expected_assigned_memory_mib": EXPECTED_ASSIGNED_MEMORY_MIB,
        "assigned_memory_tolerance_ratio": ASSIGNED_MEMORY_TOLERANCE_RATIO,
    }


def docker_json(args):
    result = subprocess.run(["docker", *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("docker " + args[0] + " failed: " + (result.stderr or result.stdout)[:240])
    return json.loads(result.stdout)


def container_cgroup(pid):
    relative = ""
    with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                relative = parts[2]
                break
    return str(pathlib.Path("/sys/fs/cgroup" + relative)) if relative else ""


def service_membership(project, parent):
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + project],
        text=True, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("cannot list project containers for guest membership attestation")
    rows = []
    problems = []
    for container_id in filter(None, result.stdout.splitlines()):
        infos = docker_json(["inspect", container_id])
        info = infos[0] if isinstance(infos, list) else infos
        service = (info.get("Config") or {}).get("Labels", {}).get("com.docker.compose.service")
        if service not in SERVICES:
            continue
        state = info.get("State") or {}
        path = container_cgroup(int(state.get("Pid") or 0)) if state.get("Running") else ""
        member = bool(path) and (path == parent or path.startswith(parent.rstrip("/") + "/"))
        rows.append({"service": service, "id": (info.get("Id") or "")[:12], "cgroup_path": path, "member": member})
        if not member:
            problems.append(service + " is not inside the attested parent cgroup")
    missing = sorted(set(SERVICES) - {row["service"] for row in rows})
    for service in missing:
        problems.append("missing running service for membership attestation: " + service)
    return rows, problems


def attest(parent="/sys/fs/cgroup/khitmeeperf.slice", project="khitmee-perf", fact_reader=None, membership_reader=None):
    problems = []
    root = pathlib.Path(parent)
    values = {"parent": parent}
    if not root.is_dir():
        problems.append("ADR parent cgroup does not exist: " + parent)
    else:
        try:
            cpuset = read_text(root / "cpuset.cpus.effective")
            cpu_max = read_text(root / "cpu.max")
            memory_max = int(read_text(root / "memory.max"))
            swap_max = read_text(root / "memory.swap.max")
            values.update({
                "cpuset_cpus_effective": cpuset,
                "cpuset_cpu_count": cpuset_count(cpuset),
                "cpu_max": cpu_max,
                "memory_max_bytes": memory_max,
                "memory_swap_max": swap_max,
            })
            if values["cpuset_cpu_count"] != 2:
                problems.append("parent cpuset does not expose exactly two CPUs")
            if cpu_max != EXPECTED_CPU_MAX:
                problems.append("parent cpu.max is not 200000 100000")
            if memory_max != EXPECTED_MEMORY_MAX:
                problems.append("parent memory.max is not 2147483648")
            if swap_max != "0":
                problems.append("parent memory.swap.max is not 0")
        except (OSError, ValueError) as exc:
            problems.append("cannot read parent cgroup envelope: " + str(exc))
    fact_reader = fact_reader or guest_facts
    membership_reader = membership_reader or service_membership
    facts = fact_reader()
    guest_problems = guest_fact_problems(facts)
    try:
        members, membership_problems = membership_reader(project, parent)
    except Exception as exc:
        members, membership_problems = [], [str(exc)]
    problems.extend(membership_problems)
    parent_slice_passed = not problems
    dedicated_guest_passed = not guest_problems
    passed = parent_slice_passed and dedicated_guest_passed
    values.update(facts)
    values.update({
        "schema_version": "seasonal-guest-attestation-v2",
        "project": project,
        "service_membership": members,
        "service_membership_verified": not membership_problems,
        "parent_slice_passed": parent_slice_passed,
        "dedicated_guest_passed": dedicated_guest_passed,
        "guest_problems": guest_problems,
        "attestation": "qualifying" if passed else "diagnostic",
        "passed": passed,
        "problems": problems + guest_problems,
    })
    return values


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--parent", default="/sys/fs/cgroup/khitmeeperf.slice")
    parser.add_argument("--project", default="khitmee-perf")
    args = parser.parse_args(argv)
    result = attest(args.parent, args.project)
    pathlib.Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
