"""Bracket a real k6 run with SQL snapshots, cgroup counters and guest sampling."""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time

import telemetry_profiler as telemetry


def block_identity():
    result = {}
    for link in Path("/sys/dev/block").iterdir():
        path = link.resolve(strict=True)
        disk = path.parent if (path / "partition").exists() else path
        sequence = disk / "diskseq"
        if sequence.is_file():
            result[link.name] = [str(path), path.stat().st_ino, sequence.read_text().strip()]
    return result


def cgroup_identity(path):
    stat = path.stat()
    return [str(path.resolve()), stat.st_dev, stat.st_ino]


def io_boundary(path):
    devices = {}
    for line in (path / "io.stat").read_text().splitlines():
        device, *fields = line.split()
        counters = {}
        for field in fields:
            name, sep, value = field.partition("=")
            if name in telemetry.IO_FIELDS:
                if sep != "=" or not value.isdigit() or name in counters:
                    raise ValueError("invalid io.stat counter")
                counters[name] = int(value)
        if device in devices or set(counters) != set(telemetry.IO_FIELDS):
            raise ValueError("incomplete io.stat device counters")
        devices[device] = counters
    return {"devices":devices}


def account_first_io(target, group_start, group_end, blocks_start, blocks_end):
    """io.stat creates a device row on its first I/O in a live cgroup."""
    start, end = target["start"], target["end"]
    before, after = (s.get("io", {}).get("devices") for s in (start, end))
    if (not isinstance(before, dict) or not isinstance(after, dict) or not set(before) < set(after)
            or target.get("identity_changes") or group_start != group_end
            or start.get("cgroup_path") != group_start[0] or end.get("cgroup_path") != group_start[0]
            or any("io.stat" in s.get("missing_files", []) for s in (start, end))):
        return {}
    new = sorted(set(after) - set(before))
    if any(device not in blocks_start or blocks_start[device] != blocks_end.get(device)
           or not blocks_start[device][2].isdigit() or int(blocks_start[device][2]) <= 0
           or set(after[device]) != set(telemetry.IO_FIELDS)
           or any(type(count) is not int or count < 0 for count in after[device].values())
           for device in new):
        return {}
    # Only the derived start view uses zeros. Retain the actual observed boundaries
    # and the independent device and cgroup lifecycle witnesses in the final report.
    target["start"] = dict(start, io={"devices": {**before, **{
        d:dict.fromkeys(telemetry.IO_FIELDS, 0) for d in new}}})
    return {"new_devices":new,"cgroup_identity":group_start,"blocks":{d:blocks_start[d] for d in new},
            "observed_start":before,"observed_end":after}


def kernel():
    ticks = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    disk = Path("/sys/class/block/vda/stat").read_text().split()
    return {"busy_ticks": sum(ticks) - ticks[3] - ticks[4], "total_ticks": sum(ticks),
            "memory_bytes": (int(mem["MemTotal"].split()[0]) - int(mem["MemAvailable"].split()[0])) * 1024,
            "read_bytes": int(disk[2]) * 512, "written_bytes": int(disk[6]) * 512}


def observe(project):
    os.environ["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    os.environ.pop("DOCKER_CONTEXT", None)
    state_dir = Path("/var/lib/khitmee-lab-arms") / project
    state = json.loads((state_dir / "state.json").read_bytes())
    active = subprocess.check_output(["docker", "ps", "--format", '{{.Label "com.docker.compose.project"}}']).decode()
    if state["namespace"] != project or set(active.splitlines()) != {project} or socket.gethostname() != "khitmee-lab":
        raise ValueError("expected sole running restored guest arm")
    schema = json.loads((Path("/var/lib/docker/volumes") / (project + "-sites")
                         / "_data/bench-loadtest.localhost/site_config.json").read_bytes())["db_name"]
    collector = telemetry.LinuxCollector(project, ["all"])
    collector.discover()
    def sql_snapshot():
        snap = collector.sql_snapshot()
        snap["digest"]["rows"] = [r for r in snap["digest"]["rows"] if r["SCHEMA_NAME"] == schema]
        return snap
    sql_before = sql_snapshot()
    if not sql_before["digest"]["supported"]:
        raise ValueError("no starting statement digest snapshot")
    collector.observe()
    paths = {k: Path(t["samples"][0]["cgroup_path"]) for k, t in collector.targets.items()}
    groups_start = {k:cgroup_identity(p) for k,p in paths.items()}
    blocks_start = block_identity()
    for k,path in paths.items():
        collector.targets[k]["samples"][0]["io"] = io_boundary(path)
    host_start = kernel()
    samples = [host_start["memory_bytes"]]
    started, start_clock = telemetry.utc_now(), time.monotonic()
    print(json.dumps({"ready": started}), flush=True)
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0.5)
        if r:
            if sys.stdin.readline().strip() != "stop":
                raise ValueError("supervisor stopped without a completed k6 run")
            break
        samples.append(kernel()["memory_bytes"])
        for key, path in paths.items():
            collector.targets[key]["samples"].append({"memory": {"current_bytes": int((path / "memory.current").read_text())}})
    ended, duration = telemetry.utc_now(), time.monotonic() - start_clock
    host_end = kernel()
    collector.observe()
    blocks_end = block_identity()
    for k,path in paths.items():
        collector.targets[k]["samples"][-1]["io"] = io_boundary(path)
    sql_after = sql_snapshot()
    first_io = {}
    for key,t in collector.targets.items():
        t["start"], t["end"] = t["samples"][0], t["samples"][-1]
        evidence = account_first_io(t, groups_start[key], cgroup_identity(paths[key]), blocks_start, blocks_end)
        if evidence:
            first_io[key] = evidence
    raw = {"project": project, "target_scope": list(paths), "targets": collector.targets, "started_at": started,
           "ended_at": ended, "duration_seconds": duration, "measured_duration_seconds": duration,
           "sql": {"enabled": True, "start": sql_before, "end": sql_after}}
    report = telemetry.build_report(raw)
    report["cgroup_first_io"] = first_io
    report["kernel"] = {"before": host_start, "after": host_end, "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
                        "memory_peak_sampled_bytes": max(*samples, host_end["memory_bytes"]),
                        "memory_sample_count": len(samples) + 1, "interval_seconds": 0.5}
    report["sql"]["boundary_utc"] = [sql_before["collected_at"], sql_after["collected_at"]]
    # Retain actual digest deltas and boundary counters for an independent calculation.
    report["sql"]["boundary_status"] = [sql_before["status"], sql_after["status"]]
    print(json.dumps(report, allow_nan=False), flush=True)
