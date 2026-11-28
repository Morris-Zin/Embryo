#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-khitmee-perf}" EXPECTED_COUNT="${EXPECTED_COUNT:-10}" python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
import time

project_name = os.environ.get("PROJECT_NAME", "khitmee-perf")
expected_count = int(os.environ.get("EXPECTED_COUNT", "10"))
slice_name = "khitmeeperf.slice"
slice_path = Path("/sys/fs/cgroup") / slice_name
expected_cpu_max = "200000 100000"
expected_memory_max = "2147483648"
expected_swap_max = "0"

# Docker cpu_shares inputs in profile-2gb-guest.yaml are converted by runc to
# these cgroup-v2 cpu.weight values. backend is the ADR general pool.
expected_services = {
    "configurator": {"memory_mib": 64, "weight": 100, "shares": 1024},
    "db": {"memory_mib": 544, "weight": 300, "shares": 4050},
    "backend": {"memory_mib": 224, "weight": 100, "shares": 1024},
    "cashier": {"memory_mib": 416, "weight": 300, "shares": 4050},
    "queue": {"memory_mib": 256, "weight": 50, "shares": 416},
    "scheduler": {"memory_mib": 96, "weight": 30, "shares": 212},
    "redis-cache": {"memory_mib": 64, "weight": 150, "shares": 1703},
    "redis-queue": {"memory_mib": 32, "weight": 120, "shares": 1284},
    "websocket": {"memory_mib": 72, "weight": 40, "shares": 311},
    "frontend": {"memory_mib": 24, "weight": 100, "shares": 1024},
}

def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)

def read_text(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""

def expand_cpuset(spec):
    cpus = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            cpus.update(range(int(first), int(last) + 1))
        else:
            cpus.add(int(part))
    return cpus

def scope_procs(scope_path):
    try:
        return {int(line) for line in (scope_path / "cgroup.procs").read_text(encoding="utf-8").splitlines() if line.strip()}
    except FileNotFoundError:
        return set()

if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
    fail("cgroup v2 is not mounted at /sys/fs/cgroup")
if not slice_path.is_dir():
    fail(f"{slice_path} does not exist; run guest_cgroup_setup.sh first")

cpu_max = read_text(slice_path / "cpu.max")
memory_max = read_text(slice_path / "memory.max")
swap_max = read_text(slice_path / "memory.swap.max")
cpuset_effective = read_text(slice_path / "cpuset.cpus.effective")
cpuset_cpus = expand_cpuset(cpuset_effective) if cpuset_effective else set()
online_spec = read_text(Path("/sys/devices/system/cpu/online"))
online_cpus = expand_cpuset(online_spec) if online_spec else set()
dedicated_mode = bool(cpuset_cpus) and cpuset_cpus == online_cpus
mode = "dedicated-guest" if dedicated_mode else "exclusive-cpuset"

print(f"parent={slice_path}")
print(f"mode={mode}")
print(f"cpuset.cpus.effective={cpuset_effective}")
print(f"cpu.max={cpu_max}")
print(f"memory.max={memory_max}")
print(f"memory.swap.max={swap_max}")

parent_errors = []
if len(cpuset_cpus) != 2:
    parent_errors.append(f"cpuset.cpus.effective must contain exactly two logical CPUs, got {cpuset_effective!r}")
if cpu_max != expected_cpu_max:
    parent_errors.append(f"cpu.max is {cpu_max!r}, expected {expected_cpu_max!r}")
if memory_max != expected_memory_max:
    parent_errors.append(f"memory.max is {memory_max!r}, expected {expected_memory_max!r}")
if swap_max != expected_swap_max:
    parent_errors.append(f"memory.swap.max is {swap_max!r}, expected {expected_swap_max!r}")

if not dedicated_mode:
    cgroup_root = Path("/sys/fs/cgroup")
    for sibling in (cgroup_root / "init.scope", cgroup_root / "system.slice", cgroup_root / "user.slice", cgroup_root / "non-systemd"):
        effective = read_text(sibling / "cpuset.cpus.effective")
        if not effective:
            parent_errors.append(f"{sibling} has no readable cpuset.cpus.effective")
            continue
        overlap = sorted(cpuset_cpus & expand_cpuset(effective))
        if overlap:
            parent_errors.append(f"{sibling} overlaps exclusive guest CPUs {overlap}: {effective}")

    # The named scopes cover systemd and WSL interop, but fail closed if any other
    # populated top-level cgroup can still schedule on the guest CPUs.
    for sibling in cgroup_root.iterdir():
        if not sibling.is_dir() or sibling.name == slice_name:
            continue
        effective = read_text(sibling / "cpuset.cpus.effective")
        if not effective:
            continue
        overlap = sorted(cpuset_cpus & expand_cpuset(effective))
        if not overlap:
            continue
        populated = False
        for path in (sibling, *sibling.rglob("*")):
            if path.is_dir() and scope_procs(path):
                populated = True
                break
        if populated:
            parent_errors.append(f"populated cgroup {sibling} overlaps exclusive guest CPUs {overlap}: {effective}")
if parent_errors:
    fail("; ".join(parent_errors))

try:
    id_output = subprocess.check_output(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project_name}", "--format", "{{.ID}}"],
        text=True,
        stderr=subprocess.PIPE,
    )
except (OSError, subprocess.CalledProcessError) as exc:
    fail(f"docker ps failed: {exc}")
container_ids = [line.strip() for line in id_output.splitlines() if line.strip()]
if not container_ids:
    fail(f"no containers matching {project_name!r} are present")
if len(container_ids) != expected_count:
    fail(f"found {len(container_ids)} containers matching {project_name!r}, expected {expected_count}")

try:
    inspect_output = subprocess.check_output(["docker", "inspect", *container_ids], text=True, stderr=subprocess.PIPE)
    inspected = json.loads(inspect_output)
except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
    fail(f"docker inspect failed: {exc}")

def service_name(container_name):
    name = container_name.lstrip("/")
    prefix = project_name + "-"
    if name.startswith(prefix):
        name = name[len(prefix):]
    if len(name) > 2 and name[-2] == "-" and name[-1].isdigit():
        name = name[:-2]
    return name

def proc_stat_pids():
    children = {}
    states = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            after_comm = stat.rsplit(")", 1)[1].split()
            state = after_comm[0]
            ppid = int(after_comm[1])
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            continue
        states[pid] = state
        children.setdefault(ppid, []).append(pid)
    return children, states

def process_tree(root):
    children, states = proc_stat_pids()
    result = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in result or pid not in states:
            continue
        result.add(pid)
        stack.extend(children.get(pid, []))
    return result


service_counts = {}
for container in inspected:
    service_counts[service_name(container.get("Name", ""))] = service_counts.get(service_name(container.get("Name", "")), 0) + 1
count_failures = []
for service in sorted(expected_services):
    count = service_counts.get(service, 0)
    if count != 1:
        count_failures.append(f"{service}: found {count}, expected exactly 1")
unexpected = sorted(set(service_counts) - set(expected_services))
for service in unexpected:
    count_failures.append(f"{service}: unexpected service")
if count_failures:
    fail("container cardinality mismatch: " + "; ".join(count_failures))

failures = []
running_checked = 0
for container in inspected:
    full_id = container.get("Id", "")
    name = container.get("Name", "")
    service = service_name(name)
    state = container.get("State") or {}
    host_config = container.get("HostConfig") or {}
    status = state.get("Status", "unknown")
    pid = int(state.get("Pid") or 0)
    expected = expected_services.get(service)
    problems = []

    if expected is None:
        problems.append("unexpected service for ADR-0004 guest topology")
        expected = {"memory_mib": None, "weight": None, "shares": None}
    if host_config.get("CgroupParent") != slice_name:
        problems.append(f"cgroup_parent={host_config.get('CgroupParent')!r}")
    if int(host_config.get("NanoCpus") or 0) != 0:
        problems.append(f"NanoCpus={host_config.get('NanoCpus')}")
    expected_memory_bytes = (expected["memory_mib"] or 0) * 1024 * 1024
    if expected["memory_mib"] is not None and int(host_config.get("Memory") or 0) != expected_memory_bytes:
        problems.append(f"Memory={host_config.get('Memory')}, expected {expected_memory_bytes}")
    if expected["shares"] is not None and int(host_config.get("CpuShares") or 0) != expected["shares"]:
        problems.append(f"CpuShares={host_config.get('CpuShares')}, expected {expected['shares']}")
    if service == "configurator":
        if status != "exited" or int(state.get("ExitCode") or 0) != 0:
            problems.append(f"configurator status={status!r} exit_code={state.get('ExitCode')!r}, expected one successful exit")
    elif status != "running" or pid <= 0:
        problems.append(f"long-lived service status={status!r} pid={pid}, expected exactly one running instance")

    membership = "not-live"
    pid_count = 0
    weight = "not-live"
    child_cpu_max = "not-live"
    child_memory_max = "not-live"
    if status == "running" and pid > 0:
        scope_path = slice_path / f"docker-{full_id}.scope"
        if not scope_path.is_dir():
            problems.append(f"live scope {scope_path} is missing")
        else:
            weight = read_text(scope_path / "cpu.weight")
            child_cpu_max = read_text(scope_path / "cpu.max")
            child_memory_max = read_text(scope_path / "memory.max")
            child_cpuset = read_text(scope_path / "cpuset.cpus.effective")
            if expected["weight"] is not None and weight != str(expected["weight"]):
                problems.append(f"cpu.weight={weight}, expected {expected['weight']}")
            if not child_cpu_max.startswith("max "):
                problems.append(f"child cpu.max={child_cpu_max!r}, expected unlimited 'max ...'")
            if expected["memory_mib"] is not None and child_memory_max != str(expected_memory_bytes):
                problems.append(f"child memory.max={child_memory_max}, expected {expected_memory_bytes}")
            if expand_cpuset(child_cpuset) != cpuset_cpus:
                problems.append(f"child cpuset.cpus.effective={child_cpuset!r}, expected {cpuset_effective!r}")

            tree = process_tree(pid)
            members = scope_procs(scope_path)
            missing = tree - members
            if missing:
                time.sleep(0.2)
                tree = process_tree(pid)
                members = scope_procs(scope_path)
                missing = tree - members
            if missing:
                problems.append(f"host PIDs outside container scope: {sorted(missing)}")
            pid_count = len(tree)
            membership = str(scope_path)
            running_checked += 1

    if problems:
        failures.append(f"{name} ({service}): " + "; ".join(problems))
        print(f"FAIL container={name} service={service} status={status} problems={'; '.join(problems)}")
    else:
        print(
            f"OK container={name} service={service} status={status} cgroup={membership} "
            f"pids={pid_count} cpu.weight={weight} cpu.max={child_cpu_max} memory.max={child_memory_max}"
        )

if failures:
    fail(f"{len(failures)} container(s) are outside or inconsistent with {slice_name}")
expected_running = len(expected_services) - 1
if running_checked != expected_running:
    fail(f"checked {running_checked} running process trees, expected {expected_running}")
print(f"PASS[{mode}]: {len(container_ids)} containers belong to {slice_name} ({running_checked} running process trees verified)")
PY
