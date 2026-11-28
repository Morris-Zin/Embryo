#!/usr/bin/env python3
"""Trusted reusable telemetry window for one deployed seasonal lab arm.

The host-side context manager is intentionally small: callers run the workload inside
``with measure(...) as result:`` and this module owns only read-only guest counter
collection plus retention of the raw evidence artifact.  A successful exit populates
the result dictionary; discontinuous or missing measurement data raises instead of
substituting partial values.
"""
import copy
import math
from pathlib import Path
import re
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
sys.path.insert(0, str(LAB / "telemetry"))

import seasonal_driver
import telemetry_profiler as telemetry


SCHEMA = "khitmee.lab-arm-telemetry/v1"
ARTIFACT_NAME = "arm-telemetry.json"
TARGET_SERVICES = ("backend", "db", "scheduler", "queue", "websocket", "frontend",
                   "redis-cache", "redis-queue")
TARGET_IDENTITY_FIELDS = ("hostname", "virtualization", "boot_id", "machine_id_sha256",
                          "vcpus", "memory_bytes", "swap_bytes")
NAMESPACE_RE = re.compile(r"khitmee-arm-[0-9a-f]{16}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _guest_script():
    probe = seasonal_driver.smoke.bundled_module(
        "telemetry_profiler", LAB / "telemetry" / "telemetry_profiler.py")
    return probe + r'''
telemetry = telemetry_profiler
TELEMETRY_TARGETS = ("backend", "db", "scheduler", "queue", "websocket", "frontend",
                     "redis-cache", "redis-queue")


def telemetry_resource_snapshot():
    try:
        with open("/proc/stat") as stat_file:
            ticks = list(map(int, stat_file.readline().split()[1:9]))
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable"):
                mem[key] = int(value.split()[0]) * 1024
        disk_stat = Path("/sys/class/block/vda/stat").read_text().split()
        require("MemTotal" in mem and "MemAvailable" in mem and len(disk_stat) > 6,
                "missing whole-guest resource counter")
        return {"cpu_ticks": sum(ticks) - ticks[3] - ticks[4],
                "memory_bytes": mem["MemTotal"] - mem["MemAvailable"],
                "disk_bytes": (int(disk_stat[2]) + int(disk_stat[6])) * 512}
    except (OSError, IndexError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "missing whole-guest resource counter":
            raise
        raise ValueError("missing whole-guest resource counter") from exc


def validate_telemetry_window(message):
    os.environ["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    os.environ.pop("DOCKER_CONTEXT", None)
    current = current_target()
    require(current["hostname"] == "khitmee-lab" and current["virtualization"] == "kvm"
            and current["vcpus"] == 2 and current["swap_bytes"] == 0
            and command("docker", "info", "--format", "{{.Name}}").strip() == b"khitmee-lab",
            "dedicated guest identity mismatch")
    expected = message.get("target") or {}
    require(all(current[key] == expected.get(key) for key in current), "guest identity changed")
    project = message.get("namespace")
    require(isinstance(project, str) and re.fullmatch(r"khitmee-arm-[0-9a-f]{16}", project),
            "invalid arm")
    require(not availability(project), "another guest project is running; exclusive window required")
    directory = Path("/var/lib/khitmee-lab-arms") / project
    state = json.loads((directory / "state.json").read_bytes())
    require(state.get("namespace") == project and state.get("fixture_sha256") == message.get("fixture_sha256")
            and state.get("restored_from_immutable_fixture") is True, "deployed project state missing or invalid")
    return project, current


def telemetry_service_states(project):
    services = {}
    for service in TELEMETRY_TARGETS:
        name = project + "-" + service + "-1"
        try:
            info = inspect(name)
        except RuntimeError:
            services[service] = {"missing": True}
            continue
        state = info.get("State") or {}
        services[service] = {"id": info.get("Id"), "started_at": state.get("StartedAt"),
                             "restart_count": info.get("RestartCount"),
                             "oom_killed": state.get("OOMKilled"),
                             "running": state.get("Running")}
    return services


def telemetry_collector(project):
    collector = telemetry.LinuxCollector(project, list(TELEMETRY_TARGETS))
    collector.discover()
    collector.observe()
    return collector


def telemetry_sql_snapshot(project, collector):
    sites = Path("/var/lib/docker/volumes") / (project + "-sites") / "_data"
    schema = json.loads((sites / SITE / "site_config.json").read_bytes())["db_name"]
    require(re.fullmatch("[_a-zA-Z0-9]+", schema), "unexpected site database name")
    snapshot = collector.sql_snapshot()
    digest = snapshot.get("digest") or {}
    digest["rows"] = [row for row in digest.get("rows", []) if row.get("SCHEMA_NAME") == schema]
    snapshot["digest"] = digest
    snapshot["site_schema"] = schema
    return snapshot


def telemetry_boundary(message):
    project, current = validate_telemetry_window(message)
    collector = telemetry_collector(project)
    print(json.dumps({"collected_at": telemetry.utc_now(), "target": current, "namespace": project,
                      "resource": telemetry_resource_snapshot(),
                      "ticks_per_second": os.sysconf("SC_CLK_TCK"),
                      "services": telemetry_service_states(project),
                      "targets": collector.targets,
                      "sql": telemetry_sql_snapshot(project, collector)}, allow_nan=False))


def telemetry_sample(message):
    project, current = validate_telemetry_window(message)
    requested = message.get("telemetry_cgroups") or {}
    require(isinstance(requested, dict), "invalid cgroup memory sample request")
    memory = {}
    for service, cgroup in requested.items():
        require(service in TELEMETRY_TARGETS and isinstance(cgroup, str)
                and cgroup.startswith("/sys/fs/cgroup/") and ".." not in cgroup.split("/"),
                "invalid cgroup memory sample request")
        try:
            memory[service] = int((Path(cgroup) / "memory.current").read_text().strip())
        except (OSError, ValueError) as exc:
            raise ValueError("missing cgroup memory sample") from exc
    print(json.dumps({"collected_at": telemetry.utc_now(), "target": current, "namespace": project,
                      "resource": telemetry_resource_snapshot(),
                      "cgroup_memory_bytes": memory}, allow_nan=False))
'''


GUEST_SCRIPT = _guest_script()


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _first_sample(entry):
    samples = (entry or {}).get("samples") or []
    return samples[0] if samples else {}


def _identity_change(service, field, before, after, at):
    return {"target": service, "field": field, "before": before, "after": after, "at": at}


def _compare_identity(service, before, after, at, changes):
    if before is None and after is None:
        return
    if before is None or after is None:
        changes.append(_identity_change(service, "container_exists", before is not None,
                                        after is not None, at))
        return
    for field in ("container_id", "image_id", "started_at", "pid"):
        if before.get(field) != after.get(field):
            changes.append(_identity_change(service, field, before.get(field), after.get(field), at))


def _merge_targets(start, end, samples):
    points = [start, *samples, end]
    merged = {}
    issues = []
    for service in TARGET_SERVICES:
        changes = []
        collected = []
        missing_files = []
        cgroup_path = None
        previous = None
        start_entry = (start.get("targets") or {}).get(service) or {}
        end_entry = (end.get("targets") or {}).get(service) or {}
        for index, point in enumerate(points):
            has_identity = "targets" in point
            entry = (point.get("targets") or {}).get(service) or {}
            sample = _first_sample(entry)
            memory_only = (point.get("cgroup_memory_bytes") or {}).get(service)
            if not sample and memory_only is not None:
                sample = {"timestamp": point.get("collected_at"),
                          "memory": {"current_bytes": memory_only}}
            if sample:
                collected.append(sample)
            if has_identity:
                identity = entry.get("identity") if entry else None
                if index:
                    _compare_identity(service, previous, identity, point.get("collected_at"), changes)
                previous = identity
            for filename in ((entry.get("coverage") or {}).get("missing_files") or []):
                if filename not in missing_files:
                    missing_files.append(filename)
            cgroup_path = cgroup_path or (entry.get("coverage") or {}).get("cgroup_path") or sample.get("cgroup_path")
        start_sample = _first_sample(start_entry)
        end_sample = _first_sample(end_entry)
        if not start_sample:
            issues.append({"scope": "target:" + service, "item": "cgroup start boundary",
                           "reason": start_entry.get("missing_at_start") or "target sample missing at window start",
                           "impact": "cgroup cost cannot be ranked for this target"})
        if not end_sample:
            issues.append({"scope": "target:" + service, "item": "cgroup end boundary",
                           "reason": end_entry.get("missing_at_start") or "target sample missing at window end",
                           "impact": "cgroup cost cannot be ranked for this target"})
        merged[service] = {
            "requested": service,
            "service": service,
            "name": start_entry.get("name") or end_entry.get("name"),
            "identity": start_entry.get("identity") or end_entry.get("identity"),
            "identity_changes": changes,
            "samples": collected,
            "start": start_sample,
            "end": end_sample,
            "coverage": {"missing_files": missing_files, "cgroup_path": cgroup_path},
        }
        if start_entry.get("missing_at_start"):
            merged[service]["missing_at_start"] = start_entry["missing_at_start"]
    return merged, issues


def _counter_field(start, end, field, missing, resets):
    start_value = ((start.get("resource") or {}).get(field))
    end_value = ((end.get("resource") or {}).get(field))
    if telemetry.parse_int(start_value) is None or telemetry.parse_int(end_value) is None:
        missing.append({"scope": "guest", "item": field, "reason": "whole-guest counter missing at window boundary",
                        "impact": "the corresponding controller metric is unavailable"})
        return None
    return telemetry.counter_delta(start_value, end_value, "guest", "resource." + field, resets)


def _service_restarts(start, end, missing, resets):
    total = 0
    for service in TARGET_SERVICES:
        before = ((start.get("services") or {}).get(service) or {})
        after = ((end.get("services") or {}).get(service) or {})
        if before.get("missing") or after.get("missing") or not before or not after:
            missing.append({"scope": "target:" + service, "item": "service state",
                            "reason": "service inspect state missing at window boundary",
                            "impact": "restart and OOM lifecycle coverage is incomplete"})
            continue
        delta = telemetry.counter_delta(before.get("restart_count"), after.get("restart_count"),
                                        "target:" + service, "restart_count", resets)
        if delta is None:
            missing.append({"scope": "target:" + service, "item": "restart_count",
                            "reason": "restart counter missing or discontinuous",
                            "impact": "restart lifecycle coverage is incomplete"})
        else:
            total += delta
    return total


def _finite_metric(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def build_window(start, end, samples, duration_seconds, sample_error=None, expected_target=None,
                 interval_seconds=None):
    resource_missing = []
    resource_resets = []

    cpu_ticks = _counter_field(start, end, "cpu_ticks", resource_missing, resource_resets)
    disk_bytes = _counter_field(start, end, "disk_bytes", resource_missing, resource_resets)
    ticks_start = telemetry.parse_int(start.get("ticks_per_second"))
    ticks_end = telemetry.parse_int(end.get("ticks_per_second"))
    if not ticks_start or not ticks_end or ticks_start != ticks_end:
        resource_missing.append({"scope": "guest", "item": "ticks_per_second",
                                 "reason": "clock tick scale missing or changed",
                                 "impact": "whole-guest CPU seconds cannot be computed"})
        ticks = None
    else:
        ticks = ticks_end

    memory_values = []
    for label, point in [("start", start), *[(f"sample-{index}", item) for index, item in enumerate(samples)],
                         ("end", end)]:
        value = telemetry.parse_int((point.get("resource") or {}).get("memory_bytes"))
        if value is None:
            resource_missing.append({"scope": "guest", "item": "memory_bytes",
                                     "reason": "whole-guest memory gauge missing at " + label,
                                     "impact": "peak memory cannot be established"})
        else:
            memory_values.append(value)
    peak_memory = max(memory_values) if memory_values else None

    merged_targets, target_issues = _merge_targets(start, end, samples)
    raw = {
        "project": start.get("namespace") or end.get("namespace"),
        "target_scope": list(TARGET_SERVICES),
        "started_at": start.get("collected_at"),
        "ended_at": end.get("collected_at"),
        "duration_seconds": duration_seconds,
        "measured_duration_seconds": duration_seconds,
        "targets": merged_targets,
        "sql": {"enabled": True, "start": start.get("sql") or {}, "end": end.get("sql") or {}, "errors": []},
    }
    report = telemetry.build_report(raw, include_samples=True)
    coverage = report["coverage"]
    coverage["missing"].extend(resource_missing + target_issues)
    coverage["resets"].extend(resource_resets)
    coverage["assumptions"].append({
        "scope": "guest", "item": "observer_effect",
        "reason": "interval samples enter the guest over SSH and read /proc plus cgroup memory files",
        "impact": "whole-guest CPU and disk include small probe overhead; compare like-for-like arms",
    })

    if expected_target is not None:
        for label, point in [("start", start), *[(f"sample-{index}", item) for index, item in enumerate(samples)],
                             ("end", end)]:
            actual = point.get("target") or {}
            for field in TARGET_IDENTITY_FIELDS:
                if actual.get(field) != expected_target.get(field):
                    coverage["identity_changes"].append({"target": "guest", "field": field,
                                                         "before": expected_target.get(field),
                                                         "after": actual.get(field),
                                                         "at": point.get("collected_at") or label})

    if sample_error is not None:
        coverage["missing"].append({"scope": "guest", "item": "interval_sample",
                                    "reason": str(sample_error),
                                    "impact": "memory peak may be under-observed"})
    if not samples:
        coverage["missing"].append({"scope": "guest", "item": "interval_sample",
                                    "reason": "no interval sample completed inside the window",
                                    "impact": "memory peak may be under-observed"})

    restarts = _service_restarts(start, end, coverage["missing"], coverage["resets"])
    target_metrics = report.get("targets") or {}
    oom_values = []
    for service in TARGET_SERVICES:
        memory = ((target_metrics.get(service) or {}).get("memory") or {})
        value = memory.get("oom_kill_delta")
        if value is None:
            coverage["missing"].append({"scope": "target:" + service, "item": "memory.events.oom_kill",
                                        "reason": "OOM kill counter missing at window boundary",
                                        "impact": "OOM lifecycle coverage is incomplete"})
        else:
            oom_values.append(value)
    oom_kills = sum(oom_values) if len(oom_values) == len(TARGET_SERVICES) else None

    statements = ((report.get("sql") or {}).get("statements") or {}).get("top_by_time") or []
    if not any((row.get("count_delta") or 0) > 0 for row in statements):
        coverage["missing"].append({"scope": "sql", "item": "sql_statement_digest_activity",
                                    "reason": "no site-schema digest execution delta observed",
                                    "impact": "SQL time cannot be established for the arm workload"})
    sql_ms_values = [row.get("time_ms_delta") for row in statements if telemetry.finite(row.get("time_ms_delta"))]
    sql_seconds = sum(sql_ms_values) / 1000.0
    lock_waits = ((report.get("sql") or {}).get("lock_waits") or {})
    metrics = {
        "cpu_core_seconds": (cpu_ticks / ticks) if cpu_ticks is not None and ticks else None,
        "peak_memory_bytes": peak_memory,
        "sql_seconds": sql_seconds,
        "io_bytes": disk_bytes,
        "lock_wait_ms": lock_waits.get("row_lock_time_ms_delta"),
    }
    counters = {"oom_kills": oom_kills, "restarts": restarts}

    invalid_metrics = [name for name, value in metrics.items() if not _finite_metric(value)]
    invalid_counters = [name for name, value in counters.items() if not isinstance(value, int) or value < 0]
    complete = (not coverage["missing"] and not coverage["resets"] and not coverage["identity_changes"]
                and not invalid_metrics and not invalid_counters)
    evidence = {
        "schema": SCHEMA,
        "complete": complete,
        "target": end.get("target") or start.get("target"),
        "window": {"started_at": start.get("collected_at"), "ended_at": end.get("collected_at"),
                   "duration_seconds": duration_seconds, "interval_seconds": interval_seconds},
        "metrics": metrics,
        "counters": counters,
        "coverage": coverage,
        "rankings": report.get("rankings") or [],
        "hypotheses": report.get("hypotheses") or [],
        "targets": target_metrics,
        "raw": {"start": start, "end": end, "interval_samples": samples, "merged_targets": merged_targets},
    }
    problems = []
    if coverage["missing"]:
        problems.append("missing " + ",".join(item.get("item", "unknown") for item in coverage["missing"][:5]))
    if coverage["resets"]:
        problems.append("counter reset")
    if coverage["identity_changes"]:
        problems.append("identity change")
    if invalid_metrics:
        problems.append("invalid metrics " + ",".join(invalid_metrics))
    if invalid_counters:
        problems.append("invalid counters " + ",".join(invalid_counters))
    return evidence, problems


class ArmMeasurement:
    def __init__(self, target, message, folder, interval):
        self.target = target
        self.message = copy.deepcopy(message)
        self.folder = folder
        self.interval = interval
        self.result = {}
        self.samples = []
        self.sample_error = None
        self.stop = threading.Event()
        self.thread = None
        self.started = None
        self.sample_message = None

    def _remote(self, operation, timeout=60, message=None):
        payload = dict(self.message if message is None else message)
        return seasonal_driver.remote(self.target, payload, GUEST_SCRIPT,
                                      operation + "(message)", timeout=timeout)

    def _sample_loop(self):
        sequence = 0
        while not self.stop.is_set():
            try:
                sample = self._remote("telemetry_sample", timeout=45, message=self.sample_message)
                sample["sequence"] = sequence
                self.samples.append(sample)
            except Exception as exc:
                self.sample_error = exc
                return
            sequence += 1
            if self.stop.wait(self.interval):
                return

    def _require_start_coverage(self):
        missing = []
        for service in TARGET_SERVICES:
            entry = (self.start.get("targets") or {}).get(service) or {}
            state = (self.start.get("services") or {}).get(service) or {}
            if not _first_sample(entry):
                missing.append(service + " cgroup")
            if (entry.get("coverage") or {}).get("missing_files"):
                missing.append(service + " cgroup files")
            if state.get("missing") or state.get("running") is not True:
                missing.append(service + " service")
        digest = ((self.start.get("sql") or {}).get("digest") or {})
        if digest.get("supported") is not True:
            missing.append("SQL digest: " + str(digest.get("reason") or "unsupported"))
        if missing:
            raise ValueError("deployed project telemetry coverage incomplete: " + ", ".join(missing))

    def __enter__(self):
        self.started = time.monotonic()
        self.start = self._remote("telemetry_boundary", timeout=90)
        self._require_start_coverage()
        cgroup_paths = {}
        for service in TARGET_SERVICES:
            entry = (self.start.get("targets") or {}).get(service) or {}
            sample = _first_sample(entry)
            path = ((entry.get("coverage") or {}).get("cgroup_path") or sample.get("cgroup_path"))
            if path:
                cgroup_paths[service] = path
        self.sample_message = copy.deepcopy(self.message)
        self.sample_message["telemetry_cgroups"] = cgroup_paths
        self.thread = threading.Thread(target=self._sample_loop, name="seasonal-telemetry-sample", daemon=True)
        self.thread.start()
        return self.result

    def _finish(self):
        end = self._remote("telemetry_boundary", timeout=90)
        duration = time.monotonic() - self.started
        evidence, problems = build_window(self.start, end, list(self.samples), duration, self.sample_error,
                                          expected_target=self.message["target"], interval_seconds=self.interval)
        reference = seasonal_driver.artifact(self.folder, ARTIFACT_NAME, evidence)
        if problems:
            detail = "incomplete arm telemetry: " + "; ".join(problems) + "; evidence retained in " + ARTIFACT_NAME
            if self.sample_error is not None:
                if hasattr(self.sample_error, "add_note"):
                    self.sample_error.add_note(detail)
                raise self.sample_error
            raise ValueError(detail)
        self.result.update({"metrics": evidence["metrics"], "counters": evidence["counters"],
                            "artifact": reference["artifact"], "sha256": reference["sha256"]})

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        try:
            self._finish()
        except Exception as telemetry_error:
            if exc_type is not None:
                if hasattr(exc, "add_note"):
                    exc.add_note("telemetry finalization also failed: " + str(telemetry_error))
                return False
            raise
        return False


def _validate_inputs(target, message, folder, interval):
    _require(isinstance(target, dict) and target.get("host") == "khitmee-lab"
             and isinstance(target.get("config"), str) and target.get("config"),
             "pinned khitmee-lab SSH target required")
    _require(isinstance(message, dict), "arm message must be an object")
    _require(NAMESPACE_RE.fullmatch(str(message.get("namespace") or "")) is not None,
             "deployed project namespace required")
    _require(SHA256_RE.fullmatch(str(message.get("fixture_sha256") or "")) is not None,
             "pinned fixture digest required")
    identity = message.get("target")
    _require(isinstance(identity, dict), "guest boot identity required")
    for field in TARGET_IDENTITY_FIELDS:
        _require(identity.get(field) not in (None, ""), "guest boot identity missing " + field)
    _require(identity.get("hostname") == "khitmee-lab" and identity.get("virtualization") == "kvm"
             and identity.get("vcpus") == 2 and identity.get("swap_bytes") == 0,
             "pinned guest identity required")
    _require(isinstance(folder, Path) and folder.is_dir(), "artifact folder must exist")
    _require(isinstance(interval, (int, float)) and math.isfinite(interval) and 0 < interval <= 60,
             "sample interval must be between zero and sixty seconds")


def measure(target, message, folder, *, interval=5.0):
    """Measure the workload executed inside the returned context manager.

    ``target`` is the pinned SSH mapping passed to ``seasonal_driver.remote``;
    ``message`` must contain the deployed arm namespace, pinned fixture digest, and
    complete boot identity.  ``folder`` is the trusted host artifact directory.
    CPU, disk and SQL are counter boundaries; RAM is peak-observed from interval
    samples (five seconds by default) to limit observer overhead during a full season.
    """
    folder = Path(folder).resolve(strict=True)
    interval = float(interval)
    _validate_inputs(target, message, folder, interval)
    return ArmMeasurement(target, message, folder, interval)
