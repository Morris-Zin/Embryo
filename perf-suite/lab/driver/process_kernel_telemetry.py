#!/usr/bin/env python3
"""Dev-only per-process and kernel OOM telemetry for one isolated lab arm.

The explicit integration contract is::

    handle = process_kernel_telemetry.start(target, message, folder, interval=1.0)
    try:
        run_the_measured_workload()
    finally:
        result = process_kernel_telemetry.stop(handle)

``target`` and ``message`` are the same pinned SSH/arm objects accepted by
``seasonal_telemetry.measure``. ``folder`` is the trusted host artifact directory.
The collector is read-only inside the guest apart from journal/process reads; its only
write is the host-side ``process-kernel-telemetry.json`` artifact. The default scope is
only the backend service. Process snapshots map kernel thread IDs to their owning
process, command, parent, cgroup and RSS. Kernel records are collected from the start
journal cursor through stop. The artifact intentionally does not claim HTTP-route
causality: neither procfs nor kernel OOM records contain a route or request ID.
"""
import copy
from datetime import datetime, timezone, timedelta
import math
from pathlib import Path
import re
import sys
import threading
import time

HERE = Path(__file__).resolve().parent

import seasonal_driver
import seasonal_telemetry


SCHEMA = "khitmee.lab-process-kernel-telemetry/v1"
ARTIFACT_NAME = "process-kernel-telemetry.json"
DEFAULT_SERVICES = ("backend",)
CURSOR_RE = re.compile(r"[A-Za-z0-9_;=.-]{8,512}\Z")
SENSITIVE_ARG_RE = re.compile(r"(password|passwd|secret|token|api[-_]?key|authorization|cookie)", re.I)
SENSITIVE_VALUE_MARKERS = seasonal_driver.adapter.SENSITIVE_VALUE_MARKERS


GUEST_ADDENDUM = r'''
PROCESS_KERNEL_SENSITIVE_ARG_RE = re.compile(r"(password|passwd|secret|token|api[-_]?key|authorization|cookie)", re.I)
PROCESS_KERNEL_CURSOR_RE = re.compile(r"[A-Za-z0-9_;=.-]{8,512}\Z")


def process_kernel_cursor():
    output = command("journalctl", "-k", "-n", "0", "--show-cursor", "--no-pager").decode(errors="replace")
    cursor = None
    for line in output.splitlines():
        if line.startswith("-- cursor: "):
            cursor = line.partition(": ")[2].strip()
    require(cursor and PROCESS_KERNEL_CURSOR_RE.fullmatch(cursor), "kernel journal cursor unavailable")
    return cursor


def process_kernel_sanitize_args(args):
    if args is None:
        return [], False
    cleaned = []
    redact_next = False
    truncated = len(args) > 32
    for raw in list(args)[:32]:
        arg = str(raw).replace("\x00", " ")[:240]
        if redact_next:
            cleaned.append("<redacted>")
            redact_next = False
            continue
        lower = arg.lower()
        if lower in ("--password", "--passwd", "--token", "--secret", "--api-key", "--authorization"):
            cleaned.append(arg)
            redact_next = True
        elif PROCESS_KERNEL_SENSITIVE_ARG_RE.search(lower):
            if "=" in arg:
                cleaned.append(arg.split("=", 1)[0] + "=<redacted>")
            else:
                cleaned.append("<redacted>")
        else:
            cleaned.append(arg)
    return cleaned, truncated


def process_kernel_status(path):
    fields = {}
    for line in Path(path).read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator and key in ("Name", "Tgid", "Pid", "PPid", "Uid", "State", "VmSize", "VmRSS", "RssAnon", "RssFile", "RssShmem"):
            fields[key] = value.strip()
    return fields


def process_kernel_int(value):
    try:
        return int(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def process_kernel_bytes(fields, key):
    value = process_kernel_int(fields.get(key))
    return value * 1024 if value is not None else None


def process_kernel_cmdline(pid, comm):
    try:
        raw = Path("/proc") / str(pid) / "cmdline"
        args = [piece.decode(errors="replace") for piece in raw.read_bytes().split(b"\0") if piece]
    except OSError:
        args = []
    if not args:
        args = [comm]
    return process_kernel_sanitize_args(args)


def process_kernel_start_time(pid):
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text()
        close = text.rfind(")")
        require(close > 0, "bad stat")
        parts = text[close + 2:].split()
        return int(parts[19]) if len(parts) > 19 else None
    except (OSError, ValueError, IndexError):
        return None


def process_kernel_process(pid, container_pid):
    pid = int(pid)
    try:
        root = Path("/proc") / str(pid)
        status = process_kernel_status(root / "status")
        comm = status.get("Name") or "<unknown>"
        cmdline, cmdline_truncated = process_kernel_cmdline(pid, comm)
        uid = process_kernel_int(status.get("Uid"))
        threads = []
        try:
            task_ids = sorted(int(task.name) for task in (root / "task").iterdir() if task.name.isdigit())
        except OSError:
            task_ids = []
        for tid in task_ids:
            try:
                task_status = process_kernel_status(root / "task" / str(tid) / "status")
                threads.append({"tid": tid,
                                "tgid": process_kernel_int(task_status.get("Tgid")),
                                "comm": task_status.get("Name") or comm,
                                "state": task_status.get("State")})
            except OSError:
                continue
        return {"pid": pid,
                "tgid": process_kernel_int(status.get("Tgid")) or pid,
                "ppid": process_kernel_int(status.get("PPid")),
                "uid": uid,
                "comm": comm,
                "state": status.get("State"),
                "cmdline": cmdline,
                "cmdline_truncated": cmdline_truncated,
                "container_init": pid == container_pid,
                "start_time_ticks": process_kernel_start_time(pid),
                "vm_size_bytes": process_kernel_bytes(status, "VmSize"),
                "rss_bytes": process_kernel_bytes(status, "VmRSS"),
                "rss_anon_bytes": process_kernel_bytes(status, "RssAnon"),
                "rss_file_bytes": process_kernel_bytes(status, "RssFile"),
                "rss_shmem_bytes": process_kernel_bytes(status, "RssShmem"),
                "threads": threads}
    except OSError:
        return {"pid": pid, "missing": True, "threads": []}


def process_kernel_service_map(project, names, expected=None):
    services = {}
    for service in names:
        name = project + "-" + service + "-1"
        info = inspect(name)
        require((info.get("Config") or {}).get("Labels", {}).get("com.docker.compose.project") == project,
                "wrong process telemetry container")
        state = info.get("State") or {}
        require(state.get("Running") is True, "process telemetry service not running")
        pid = state.get("Pid")
        require(isinstance(pid, int) and pid > 0, "missing container init pid")
        if expected and service in expected:
            old = expected[service]
            require(info.get("Id") == old.get("container_id"), "container identity changed")
            require(pid == old.get("container_pid"), "container init pid changed")
            require(state.get("StartedAt") == old.get("started_at"), "container start identity changed")
        cgroup_rel = None
        for line in (Path("/proc") / str(pid) / "cgroup").read_text().splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                cgroup_rel = parts[2]
        require(cgroup_rel and ".." not in cgroup_rel.split("/"), "missing container cgroup")
        cgroup = "/sys/fs/cgroup" + cgroup_rel
        require((Path(cgroup) / "cgroup.procs").is_file(), "missing cgroup process list")
        cmd, cmd_truncated = process_kernel_sanitize_args((info.get("Config") or {}).get("Cmd") or [])
        entrypoint, entrypoint_truncated = process_kernel_sanitize_args((info.get("Config") or {}).get("Entrypoint") or [])
        services[service] = {"container_name": name,
                             "container_id": info.get("Id"),
                             "image_id": info.get("Image"),
                             "container_pid": pid,
                             "started_at": state.get("StartedAt"),
                             "restart_count": info.get("RestartCount"),
                             "oom_killed": state.get("OOMKilled"),
                             "cgroup_path": cgroup,
                             "command": cmd,
                             "command_truncated": cmd_truncated,
                             "entrypoint": entrypoint,
                             "entrypoint_truncated": entrypoint_truncated}
    return services


def process_kernel_service_names(message):
    names = message.get("process_kernel_service_names") or ["backend"]
    require(isinstance(names, list) and names and len(set(names)) == len(names)
            and all(isinstance(name, str) and name in TELEMETRY_TARGETS for name in names),
            "invalid process telemetry services")
    return names


def process_kernel_expected_services(message):
    expected = message.get("process_kernel_services")
    require(isinstance(expected, dict) and expected, "process telemetry service map missing")
    for service, meta in expected.items():
        require(service in TELEMETRY_TARGETS and isinstance(meta, dict), "invalid process telemetry service map")
    return expected


def process_kernel_key_values(path):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    values[parts[0]] = int(parts[1])
                except ValueError:
                    pass
    except OSError:
        pass
    return values


def process_kernel_read_int(path):
    try:
        text = Path(path).read_text().strip()
        return None if text == "max" else int(text)
    except (OSError, ValueError):
        return None


def process_kernel_sample_target(service_info):
    cgroup = service_info["cgroup_path"]
    procs = []
    try:
        pids = [int(line) for line in (Path(cgroup) / "cgroup.procs").read_text().splitlines() if line.strip().isdigit()]
    except OSError as exc:
        raise ValueError("missing cgroup process sample") from exc
    for pid in sorted(set(pids)):
        procs.append(process_kernel_process(pid, service_info["container_pid"]))
    events = process_kernel_key_values(Path(cgroup) / "memory.events")
    return {"cgroup_path": cgroup,
            "memory_current_bytes": process_kernel_read_int(Path(cgroup) / "memory.current"),
            "memory_max_bytes": process_kernel_read_int(Path(cgroup) / "memory.max"),
            "memory_events": events,
            "processes": procs}


def process_kernel_journal_records(start_cursor):
    require(isinstance(start_cursor, str) and PROCESS_KERNEL_CURSOR_RE.fullmatch(start_cursor),
            "invalid kernel journal start cursor")
    output = command("journalctl", "-k", "--after-cursor", start_cursor, "-o", "json", "--no-pager",
                     timeout=180).decode(errors="replace")
    wanted = ("__REALTIME_TIMESTAMP", "__MONOTONIC_TIMESTAMP", "__CURSOR", "_BOOT_ID",
              "_HOSTNAME", "SYSLOG_IDENTIFIER", "PRIORITY", "MESSAGE")
    records = []
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append({key: record[key] for key in wanted if key in record})
    return records


def process_kernel_start(message):
    project, current = validate_telemetry_window(message)
    names = process_kernel_service_names(message)
    services = process_kernel_service_map(project, names)
    print(json.dumps({"collected_at": telemetry.utc_now(), "target": current, "namespace": project,
                      "page_size_bytes": os.sysconf("SC_PAGE_SIZE"), "ticks_per_second": os.sysconf("SC_CLK_TCK"),
                      "journal_cursor": process_kernel_cursor(), "services": services}, allow_nan=False))


def process_kernel_sample(message):
    project, current = validate_telemetry_window(message)
    expected = process_kernel_expected_services(message)
    services = process_kernel_service_map(project, list(expected), expected)
    targets = {service: process_kernel_sample_target(services[service]) for service in services}
    print(json.dumps({"collected_at": telemetry.utc_now(), "target": current, "namespace": project,
                      "targets": targets}, allow_nan=False))


def process_kernel_stop(message):
    project, current = validate_telemetry_window(message)
    expected = process_kernel_expected_services(message)
    services = process_kernel_service_map(project, list(expected), expected)
    start_cursor = message.get("process_kernel_start_cursor")
    records = process_kernel_journal_records(start_cursor)
    end_cursor = records[-1].get("__CURSOR") if records else start_cursor
    print(json.dumps({"collected_at": telemetry.utc_now(), "target": current, "namespace": project,
                      "services": services,
                      "kernel": {"start_cursor": start_cursor, "end_cursor": end_cursor,
                                 "record_count": len(records), "records": records}}, allow_nan=False))
'''


GUEST_SCRIPT = seasonal_telemetry.GUEST_SCRIPT + GUEST_ADDENDUM

OOM_INVOKED_RE = re.compile(
    r"(?P<comm>.+?) invoked oom-killer: gfp_mask=(?P<gfp_mask>[^,]+), order=(?P<order>\d+), oom_score_adj=(?P<adj>-?\d+)")
OOM_CPU_RE = re.compile(r"CPU: \d+ PID: (?P<pid>\d+) Comm: (?P<comm>\S+)")
MEMCG_USAGE_RE = re.compile(r"memory: usage (?P<usage>\d+)kB, limit (?P<limit>\d+)kB, failcnt (?P<failcnt>\d+)")
MEMCG_STATS_RE = re.compile(r"Memory cgroup stats for (?P<cgroup>\S+):")
TASK_ROW_RE = re.compile(
    r"\[\s*(?P<pid>\d+)\]\s+(?P<uid>\d+)\s+(?P<tgid>\d+)\s+(?P<total_vm>\d+)\s+(?P<rss>\d+)"
    r"\s+(?P<rss_anon>\d+)\s+(?P<rss_file>\d+)\s+(?P<rss_shmem>\d+)\s+(?P<pgtables_bytes>\d+)"
    r"\s+(?P<swapents>\d+)\s+(?P<oom_score_adj>-?\d+)\s+(?P<name>\S+)")
OOM_KILL_RE = re.compile(r"oom-kill:(?P<fields>.+)")
KILLED_RE = re.compile(
    r"Memory cgroup out of memory: Killed process (?P<pid>\d+) \((?P<comm>.*?)\) total-vm:(?P<total_vm>\d+)kB, "
    r"anon-rss:(?P<anon_rss>\d+)kB, file-rss:(?P<file_rss>\d+)kB, shmem-rss:(?P<shmem_rss>\d+)kB, "
    r"UID:(?P<uid>\d+) pgtables:(?P<pagetables>\d+)kB oom_score_adj:(?P<adj>-?\d+)")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_from_unix_us(value):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(microseconds=int(value))).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso_us(value):
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)
    except ValueError:
        return None


def kb_to_bytes(value):
    return int(value) * 1024 if value is not None else None


def sanitize_text(text, limit=1000):
    text = str(text).replace("\x00", " ")[:limit]
    for key in ("ADMIN_PASSWORD", "DB_PASSWORD", "MYSQL_ROOT_PASSWORD"):
        text = re.sub(key + r"\s*=\s*[^\s,;]+", key + "=<redacted>", text, flags=re.I)
    for marker in SENSITIVE_VALUE_MARKERS:
        if marker.endswith("PRIVATE KEY"):
            text = text.replace(marker, "<redacted-private-key-marker>")
    return text



def _int_group(match, name):
    return int(match.group(name)) if match.group(name) is not None else None


def parse_oom_key_values(text):
    values = {}
    for piece in text.split(","):
        key, separator, value = piece.partition("=")
        if separator:
            values[key] = value
    return values


def parse_oom_events(records):
    events = []
    current = None
    for index, record in enumerate(records or []):
        message = str((record or {}).get("MESSAGE") or "")
        realtime = (record or {}).get("__REALTIME_TIMESTAMP")
        invoked = OOM_INVOKED_RE.search(message)
        if invoked:
            if current is not None:
                current["complete"] = False
                events.append(current)
            current = {"invocation": dict(invoked.groupdict()),
                       "invoked_at_us": int(realtime) if str(realtime or "").isdigit() else None,
                       "raw_record_indexes": [index], "tasks": []}
            if current["invoked_at_us"] is not None:
                current["invoked_at"] = iso_from_unix_us(current["invoked_at_us"])
            continue
        if current is None:
            continue
        current["raw_record_indexes"].append(index)
        cpu = OOM_CPU_RE.search(message)
        if cpu and "allocator" not in current:
            current["allocator"] = {"tid": int(cpu.group("pid")), "comm": cpu.group("comm"),
                                    "timestamp_us": int(realtime) if str(realtime or "").isdigit() else None}
            if current["allocator"]["timestamp_us"] is not None:
                current["allocator"]["timestamp"] = iso_from_unix_us(current["allocator"]["timestamp_us"])
            continue
        usage = MEMCG_USAGE_RE.search(message)
        if usage:
            current["memory_cgroup"] = {"usage_bytes": kb_to_bytes(_int_group(usage, "usage")),
                                        "limit_bytes": kb_to_bytes(_int_group(usage, "limit")),
                                        "failcnt": _int_group(usage, "failcnt"),
                                        "usage_reported_kib": _int_group(usage, "usage"),
                                        "limit_reported_kib": _int_group(usage, "limit")}
            continue
        stats = MEMCG_STATS_RE.search(message)
        if stats:
            current["memory_cgroup_path"] = stats.group("cgroup")
            continue
        task = TASK_ROW_RE.search(message)
        if task:
            row = {key: _int_group(task, key) for key in
                   ("pid", "uid", "tgid", "total_vm", "rss", "rss_anon", "rss_file", "rss_shmem",
                    "pgtables_bytes", "swapents", "oom_score_adj")}
            row["name"] = task.group("name")
            current["tasks"].append(row)
            continue
        kill = OOM_KILL_RE.search(message)
        if kill:
            fields = parse_oom_key_values(kill.group("fields"))
            current["oom_kill"] = fields
            if fields.get("pid", "").isdigit():
                current["victim_selection"] = {"pid": int(fields["pid"]), "comm": fields.get("task"),
                                               "uid": int(fields["uid"]) if fields.get("uid", "").isdigit() else None,
                                               "task_memcg": fields.get("task_memcg"),
                                               "oom_memcg": fields.get("oom_memcg"),
                                               "constraint": fields.get("constraint"),
                                               "timestamp_us": int(realtime) if str(realtime or "").isdigit() else None}
                if current["victim_selection"]["timestamp_us"] is not None:
                    current["victim_selection"]["timestamp"] = iso_from_unix_us(current["victim_selection"]["timestamp_us"])
            continue
        killed = KILLED_RE.search(message)
        if killed:
            current["victim"] = {"pid": _int_group(killed, "pid"), "comm": killed.group("comm"),
                                 "uid": _int_group(killed, "uid"),
                                 "total_vm_bytes": kb_to_bytes(_int_group(killed, "total_vm")),
                                 "rss_anon_bytes": kb_to_bytes(_int_group(killed, "anon_rss")),
                                 "rss_file_bytes": kb_to_bytes(_int_group(killed, "file_rss")),
                                 "rss_shmem_bytes": kb_to_bytes(_int_group(killed, "shmem_rss")),
                                 "pgtables_bytes": kb_to_bytes(_int_group(killed, "pagetables")),
                                 "oom_score_adj": _int_group(killed, "adj"),
                                 "timestamp_us": int(realtime) if str(realtime or "").isdigit() else None}
            rss_values = [current["victim"]["rss_anon_bytes"] or 0,
                          current["victim"]["rss_file_bytes"] or 0,
                          current["victim"]["rss_shmem_bytes"] or 0]
            current["victim"]["rss_bytes"] = sum(rss_values)
            if current["victim"]["timestamp_us"] is not None:
                current["victim"]["timestamp"] = iso_from_unix_us(current["victim"]["timestamp_us"])
            current["complete"] = True
            events.append(current)
            current = None
    if current is not None:
        current["complete"] = False
        events.append(current)
    return events


def normalized_cgroup(path):
    if not isinstance(path, str):
        return None
    prefix = "/sys/fs/cgroup"
    return path[len(prefix):] if path.startswith(prefix) else path


def process_role(process, service):
    if not process or not service:
        return None
    if process.get("pid") == service.get("container_pid"):
        return "container-init"
    if process.get("ppid") == service.get("container_pid"):
        return "container-child"
    return "container-descendant-or-unverified-parent"


def _sample_process_hits(samples, pid):
    hits = []
    for sample in samples or []:
        sample_us = parse_iso_us(sample.get("collected_at"))
        for service, target in (sample.get("targets") or {}).items():
            for process in target.get("processes") or []:
                if process.get("pid") == pid or process.get("tgid") == pid:
                    hits.append((sample_us or 0, sample.get("collected_at"), service, process))
    return hits


def _sample_thread_hits(samples, tid):
    hits = []
    for sample in samples or []:
        sample_us = parse_iso_us(sample.get("collected_at"))
        for service, target in (sample.get("targets") or {}).items():
            for process in target.get("processes") or []:
                for thread in process.get("threads") or []:
                    if thread.get("tid") == tid:
                        hits.append((sample_us or 0, sample.get("collected_at"), service, process, thread))
    return hits


def _choose_hit(hits, event_us):
    if not hits:
        return None, None
    before = [hit for hit in hits if event_us is not None and hit[0] <= event_us]
    if before:
        return max(before, key=lambda hit: hit[0]), "before-or-at-event"
    after = [hit for hit in hits if event_us is not None and hit[0] > event_us]
    if after:
        return min(after, key=lambda hit: hit[0]), "after-event"
    return hits[-1], "unknown-time-order"


def slim_process(process, service):
    if process is None:
        return None
    keys = ("pid", "tgid", "ppid", "uid", "comm", "state", "cmdline", "cmdline_truncated",
            "container_init", "start_time_ticks", "vm_size_bytes", "rss_bytes", "rss_anon_bytes",
            "rss_file_bytes", "rss_shmem_bytes", "missing")
    result = {key: process.get(key) for key in keys if key in process}
    result["thread_count"] = len(process.get("threads") or [])
    result["process_role"] = process_role(process, service)
    return result


def attribute_oom_event(event, samples, services):
    victim_selection = event.get("victim_selection") or {}
    victim = event.get("victim") or {}
    cgroup = victim_selection.get("task_memcg") or victim_selection.get("oom_memcg") or event.get("memory_cgroup_path")
    service_name = None
    service = None
    for name, candidate in (services or {}).items():
        candidate_cgroup = normalized_cgroup(candidate.get("cgroup_path"))
        if cgroup and candidate_cgroup == cgroup:
            service_name, service = name, candidate
            break
        container_id = candidate.get("container_id") or ""
        if cgroup and container_id and container_id in cgroup:
            service_name, service = name, candidate
            break

    event_us = (event.get("victim_selection") or {}).get("timestamp_us") or event.get("invoked_at_us")
    allocator = dict(event.get("allocator") or {})
    tid = allocator.get("tid")
    hit = relation = None
    if isinstance(tid, int):
        hit, relation = _choose_hit(_sample_thread_hits(samples, tid), event_us)
    if hit:
        _sample_us, sampled_at, hit_service, process, thread = hit
        allocator.update({"resolution": "direct-thread-sample" if relation == "before-or-at-event" else "thread-seen-after-event",
                          "sample_relation": relation, "sampled_at": sampled_at, "service": hit_service,
                          "thread": dict(thread), "owning_process": slim_process(process, (services or {}).get(hit_service))})
    else:
        allocator.update({"resolution": "unresolved-thread-not-in-process-samples", "owning_process": None})

    victim_out = copy.deepcopy(victim)
    victim_pid = victim.get("pid") or victim_selection.get("pid")
    victim_hit = victim_relation = None
    if isinstance(victim_pid, int):
        victim_hit, victim_relation = _choose_hit(_sample_process_hits(samples, victim_pid), event_us)
    if victim_hit:
        _sample_us, sampled_at, hit_service, process = victim_hit
        victim_out.update({"sample_relation": victim_relation, "sampled_at": sampled_at, "service": hit_service,
                           "process": slim_process(process, (services or {}).get(hit_service))})
    else:
        victim_out.update({"process": None, "sample_relation": "not-observed-in-process-samples"})
    for task in event.get("tasks") or []:
        if task.get("pid") == victim_pid:
            victim_out["kernel_task_row"] = dict(task)
            break
    return {"complete": event.get("complete") is True,
            "invoked_at": event.get("invoked_at"), "invoked_at_us": event.get("invoked_at_us"),
            "memory_cgroup": event.get("memory_cgroup"), "memory_cgroup_path": event.get("memory_cgroup_path"),
            "cgroup": cgroup, "service": service_name, "container_id": (service or {}).get("container_id"),
            "container_started_at": (service or {}).get("started_at"), "container_pid": (service or {}).get("container_pid"),
            "allocator": allocator, "victim_selection": victim_selection, "victim": victim_out,
            "tasks": event.get("tasks") or [], "raw_record_indexes": event.get("raw_record_indexes") or []}


def build_evidence(start, end, samples, duration_seconds, sample_error=None, interval_seconds=None):
    missing = []
    if sample_error is not None:
        missing.append({"scope": "process", "item": "interval_sample", "reason": str(sample_error),
                        "impact": "thread-to-process attribution may be incomplete"})
    if not samples:
        missing.append({"scope": "process", "item": "interval_sample", "reason": "no process sample completed",
                        "impact": "allocator thread ownership cannot be established"})
    kernel = (end or {}).get("kernel") or {}
    records = kernel.get("records") or []
    if kernel.get("record_count") != len(records):
        missing.append({"scope": "kernel", "item": "journal_records", "reason": "journal record count mismatch",
                        "impact": "kernel event evidence is incomplete"})
    events = parse_oom_events(records)
    services = (start or {}).get("services") or {}
    attributed = [attribute_oom_event(event, samples, services) for event in events]
    for index, event in enumerate(attributed):
        if not event["complete"]:
            missing.append({"scope": "kernel", "item": "oom_event_complete", "reason": f"OOM event {index} is truncated",
                            "impact": "victim or allocator fields may be missing"})
        if event["allocator"].get("resolution") != "direct-thread-sample":
            missing.append({"scope": "process", "item": "allocator_thread_owner",
                            "reason": f"OOM event {index}: " + str(event["allocator"].get("resolution")),
                            "impact": "allocator thread cannot be exactly assigned to a process"})
    evidence = {
        "schema": SCHEMA,
        "complete": not missing,
        "target": (end or {}).get("target") or (start or {}).get("target"),
        "window": {"started_at": (start or {}).get("collected_at"), "ended_at": (end or {}).get("collected_at"),
                   "duration_seconds": duration_seconds, "interval_seconds": interval_seconds},
        "collection_scope": {"services": list(services), "dev_only": True,
                              "route_attribution": "not-collected-in-procfs-or-kernel-oom-records"},
        "journal": {"start_cursor": kernel.get("start_cursor") or (start or {}).get("journal_cursor"),
                    "end_cursor": kernel.get("end_cursor"), "record_count": len(records)},
        "oom_events": attributed,
        "coverage": {"missing": missing, "assumptions": [
            {"scope": "process", "item": "observer_effect",
             "reason": "each interval sample enters the guest over SSH and reads procfs/cgroup files",
             "impact": "use dev-only like-for-like windows; do not treat this as zero-cost production telemetry"},
            {"scope": "route", "item": "route_attribution",
             "reason": "procfs and kernel OOM records do not contain HTTP routes or request IDs",
             "impact": "external route events can only be correlated by time unless application instrumentation adds a process/thread request marker"}]},
        "raw": {"start": start, "end": end, "process_samples": samples,
                "kernel_records": [{key: sanitize_text(value) if key == "MESSAGE" else value
                                    for key, value in record.items()} for record in records]},
    }
    problems = [item["item"] for item in missing]
    return evidence, problems


class ProcessKernelMeasurement:
    def __init__(self, target, message, folder, interval, services):
        self.target = target
        self.message = copy.deepcopy(message)
        self.folder = folder
        self.interval = interval
        self.services = tuple(services)
        self.result = {}
        self.samples = []
        self.sample_error = None
        self.stop_event = threading.Event()
        self.thread = None
        self.started = None
        self.start_boundary = None
        self.sample_message = None
        self.stop_message = None
        self.finished = False

    def _remote(self, operation, timeout=60, message=None):
        payload = dict(self.message if message is None else message)
        return seasonal_driver.remote(self.target, payload, GUEST_SCRIPT,
                                      operation + "(message)", timeout=timeout)

    def _sample_loop(self):
        sequence = 0
        while not self.stop_event.is_set():
            try:
                sample = self._remote("process_kernel_sample", timeout=45, message=self.sample_message)
                sample["sequence"] = sequence
                self.samples.append(sample)
            except Exception as exc:
                self.sample_error = exc
                return
            sequence += 1
            if self.stop_event.wait(self.interval):
                return

    def start(self):
        if self.thread is not None or self.start_boundary is not None:
            raise RuntimeError("process/kernel telemetry already started")
        self.started = time.monotonic()
        payload = copy.deepcopy(self.message)
        payload["process_kernel_service_names"] = list(self.services)
        self.start_boundary = self._remote("process_kernel_start", timeout=90, message=payload)
        service_map = {}
        for service, meta in (self.start_boundary.get("services") or {}).items():
            service_map[service] = {"container_id": meta.get("container_id"),
                                    "container_pid": meta.get("container_pid"),
                                    "started_at": meta.get("started_at")}
        self.sample_message = copy.deepcopy(self.message)
        self.sample_message["process_kernel_services"] = service_map
        self.thread = threading.Thread(target=self._sample_loop, name="process-kernel-telemetry-sample", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.finished:
            return self.result
        if self.start_boundary is None:
            raise RuntimeError("process/kernel telemetry was not started")
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        self.stop_message = copy.deepcopy(self.sample_message or self.message)
        self.stop_message["process_kernel_start_cursor"] = self.start_boundary.get("journal_cursor")
        end = self._remote("process_kernel_stop", timeout=120, message=self.stop_message)
        duration = time.monotonic() - self.started
        evidence, problems = build_evidence(self.start_boundary, end, list(self.samples), duration,
                                            self.sample_error, self.interval)
        reference = seasonal_driver.artifact(self.folder, ARTIFACT_NAME, evidence)
        self.result.update({"artifact": reference["artifact"], "sha256": reference["sha256"],
                            "complete": evidence["complete"], "oom_event_count": len(evidence["oom_events"]),
                            "oom_events": evidence["oom_events"], "coverage": evidence["coverage"]})
        self.finished = True
        if problems:
            detail = "incomplete process/kernel telemetry: " + ", ".join(problems) + "; evidence retained in " + ARTIFACT_NAME
            if self.sample_error is not None:
                if hasattr(self.sample_error, "add_note"):
                    self.sample_error.add_note(detail)
                raise self.sample_error
            raise ValueError(detail)
        return self.result

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.stop()
        except Exception as telemetry_error:
            if exc_type is not None:
                if hasattr(exc, "add_note"):
                    exc.add_note("process/kernel telemetry finalization also failed: " + str(telemetry_error))
                return False
            raise
        return False


def _validate_inputs(target, message, folder, interval, services):
    seasonal_telemetry._validate_inputs(target, message, folder, interval)
    if not (isinstance(services, (tuple, list)) and services and len(set(services)) == len(services)
            and all(isinstance(service, str) and service in seasonal_telemetry.TARGET_SERVICES for service in services)):
        raise ValueError("process telemetry services must be unique deployed service names")
    if interval > 10:
        raise ValueError("process telemetry is dev-only; use an interval of at most ten seconds")


def start(target, message, folder, *, interval=1.0, services=DEFAULT_SERVICES):
    """Start process sampling and return the handle required by ``stop``.

    The caller must invoke ``stop`` exactly once after the measured workload, including
    failure paths. ``services`` defaults to ``(\"backend\",)`` to keep the probe small.
    """
    folder = Path(folder).resolve(strict=True)
    interval = float(interval)
    services = tuple(services)
    _validate_inputs(target, message, folder, interval, services)
    measurement = ProcessKernelMeasurement(target, message, folder, interval, services)
    return measurement.start()


def stop(handle):
    """Stop a handle returned by ``start`` and write ``process-kernel-telemetry.json``."""
    if not isinstance(handle, ProcessKernelMeasurement):
        raise TypeError("stop requires a handle returned by process_kernel_telemetry.start")
    return handle.stop()


def measure(target, message, folder, *, interval=1.0, services=DEFAULT_SERVICES):
    """Context-manager convenience wrapper around the explicit start/stop hooks."""
    folder = Path(folder).resolve(strict=True)
    interval = float(interval)
    services = tuple(services)
    _validate_inputs(target, message, folder, interval, services)
    return ProcessKernelMeasurement(target, message, folder, interval, services)
