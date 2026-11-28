#!/usr/bin/env python3
"""Offline contract tests for the reusable seasonal arm telemetry window."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seasonal_driver
import seasonal_telemetry


TARGET = {
    "hostname": "khitmee-lab",
    "virtualization": "kvm",
    "boot_id": "boot-1",
    "machine_id_sha256": "m" * 64,
    "vcpus": 2,
    "memory_bytes": 2 * 1024**3,
    "swap_bytes": 0,
}
SSH = {"config": "pinned-ssh-config", "host": "khitmee-lab"}
MESSAGE = {
    "namespace": "khitmee-arm-0123456789abcdef",
    "fixture_sha256": "f" * 64,
    "target": TARGET,
}


def cgroup_sample(second, usage, memory, io_bytes):
    return {
        "timestamp": f"2026-01-01T00:00:{second:02d}Z",
        "cpu": {"usage_usec": usage, "user_usec": usage // 2, "system_usec": usage // 2,
                "nr_periods": 0, "nr_throttled": 0, "throttled_usec": 0},
        "memory": {"current_bytes": memory, "max_bytes": 1024**3, "max_unlimited": False,
                   "swap_current_bytes": 0, "events": {"oom": 0, "oom_kill": 0}},
        "io": {"devices": {"253:0": {"rbytes": io_bytes, "wbytes": io_bytes,
                                      "rios": 1, "wios": 1, "dbytes": 0, "dios": 0}}},
        "missing_files": [],
        "cgroup_path": "/sys/fs/cgroup/test",
    }


def target_entry(service, second, usage=1_000_000, memory=100, io_bytes=10, started="start"):
    return {
        "requested": service,
        "service": service,
        "name": "khitmee-arm-0123456789abcdef-" + service + "-1",
        "identity": {"container_id": service + "-id", "image_id": "sha256:image",
                     "started_at": started, "pid": 1000},
        "identity_changes": [],
        "samples": [cgroup_sample(second, usage, memory, io_bytes)],
        "coverage": {"missing_files": [], "cgroup_path": "/sys/fs/cgroup/test"},
    }


def sql_snapshot(uptime, questions, lock_ms, count, timer_ps):
    return {
        "server": {"SERVER_UUID": "server-1", "PERFORMANCE_SCHEMA": "ON", "VERSION": "11.8"},
        "status": {"UPTIME": str(uptime), "UPTIME_SINCE_FLUSH_STATUS": str(uptime),
                   "QUESTIONS": str(questions), "INNODB_ROW_LOCK_WAITS": "0",
                   "INNODB_ROW_LOCK_TIME": str(lock_ms), "INNODB_DEADLOCKS": "0"},
        "digest": {"supported": True, "reason": None, "rows": [{
            "SCHEMA_NAME": "site", "DIGEST": "digest-1", "DIGEST_TEXT": "SELECT ...",
            "COUNT_STAR": str(count), "SUM_TIMER_WAIT": str(timer_ps),
            "SUM_ROWS_EXAMINED": "1", "SUM_ROWS_SENT": "1", "SUM_ROWS_AFFECTED": "0",
            "SUM_NO_INDEX_USED": "0", "SUM_CREATED_TMP_DISK_TABLES": "0",
            "FIRST_SEEN": "2026-01-01 00:00:00", "LAST_SEEN": "2026-01-01 00:00:01",
        }]},
        "errors": [],
        "collected_at": "2026-01-01T00:00:00Z",
    }


def boundary(second, cpu_ticks, memory, disk, sql_end=False, mutate=None):
    services = {}
    targets = {}
    for service in seasonal_telemetry.TARGET_SERVICES:
        usage = 1_000_000 + second * 1000
        services[service] = {"id": service + "-id", "started_at": "start", "restart_count": 0,
                             "oom_killed": False, "running": True}
        targets[service] = target_entry(service, second, usage, memory, 10 + second)
    data = {
        "collected_at": f"2026-01-01T00:00:{second:02d}Z",
        "target": TARGET,
        "resource": {"cpu_ticks": cpu_ticks, "memory_bytes": memory, "disk_bytes": disk},
        "ticks_per_second": 100,
        "services": services,
        "targets": targets,
        "sql": sql_snapshot(100 + second, 10 + second, 25 if sql_end else 0,
                            8 if sql_end else 5, 3_000_000_000_000 if sql_end else 1_000_000_000_000),
    }
    if mutate:
        mutate(data)
    return data


class RemoteFake:
    def __init__(self, mutate_end=None, fail_end=False, fail_sample=False, mutate_start=None):
        self.calls = []
        self.mutate_end = mutate_end
        self.fail_end = fail_end
        self.fail_sample = fail_sample
        self.mutate_start = mutate_start

    def __call__(self, target, message, script="", suffix="main(message)", timeout=360):
        self.calls.append((dict(message), suffix))
        boundary_count = sum(1 for _message, call in self.calls if "telemetry_boundary" in call)
        if "telemetry_sample" in suffix:
            if self.fail_sample:
                raise KeyError("sampler boom")
            return {
                "collected_at": "2026-01-01T00:00:01Z",
                "target": TARGET,
                "namespace": MESSAGE["namespace"],
                "resource": {"cpu_ticks": 1100, "memory_bytes": 150, "disk_bytes": 1200},
                "cgroup_memory_bytes": {service: 150 for service in seasonal_telemetry.TARGET_SERVICES},
            }
        if boundary_count == 1:
            return boundary(0, 1000, 100, 1000, mutate=self.mutate_start)
        if self.fail_end:
            raise ValueError("boundary boom")
        return boundary(2, 1200, 110, 1300, sql_end=True, mutate=self.mutate_end)


class SeasonalTelemetryTest(unittest.TestCase):
    def install_remote(self, fake):
        original = seasonal_driver.remote
        seasonal_driver.remote = fake
        self.addCleanup(setattr, seasonal_driver, "remote", original)

    def test_result_is_populated_after_exit_and_artifact_digest_covers_raw_and_rankings(self):
        fake = RemoteFake()
        self.install_remote(fake)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with seasonal_telemetry.measure(SSH, MESSAGE, folder, interval=0.01) as result:
                self.assertEqual(result, {})
                self.assertFalse((folder / "arm-telemetry.json").exists())
                time.sleep(0.025)
            self.assertEqual(result["metrics"], {
                "cpu_core_seconds": 2.0,
                "peak_memory_bytes": 150,
                "sql_seconds": 2.0,
                "io_bytes": 300,
                "lock_wait_ms": 25,
            })
            self.assertEqual(result["counters"], {"oom_kills": 0, "restarts": 0})
            self.assertEqual(result["artifact"], "arm-telemetry.json")
            raw = (folder / result["artifact"]).read_bytes()
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            artifact = json.loads(raw)
            self.assertIn("raw", artifact)
            self.assertTrue(any(row["metric"] == "sql_digest_time_delta_ms" for row in artifact["rankings"]))
            self.assertTrue(artifact["complete"])
            sample_messages = [message for message, suffix in fake.calls if "telemetry_sample" in suffix]
            self.assertTrue(sample_messages)
            self.assertEqual(set(sample_messages[0]["telemetry_cgroups"]), set(seasonal_telemetry.TARGET_SERVICES))

    def test_container_identity_change_fails_closed_and_retains_evidence(self):
        def replace_backend(data):
            data["targets"]["backend"]["identity"]["started_at"] = "replacement"
            data["services"]["backend"]["restart_count"] = 1
        self.install_remote(RemoteFake(mutate_end=replace_backend))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "identity"):
                with seasonal_telemetry.measure(SSH, MESSAGE, Path(temp), interval=0.01):
                    pass
            artifact = json.loads((Path(temp) / "arm-telemetry.json").read_bytes())
            self.assertFalse(artifact["complete"])
            self.assertEqual(artifact["counters"], {"oom_kills": 0, "restarts": 1})

    def test_missing_sql_digest_is_not_substituted(self):
        def unsupported(data):
            data["sql"]["digest"] = {"supported": False, "reason": "performance_schema is OFF", "rows": []}
        self.install_remote(RemoteFake(mutate_end=unsupported))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "digest"):
                with seasonal_telemetry.measure(SSH, MESSAGE, Path(temp), interval=0.01):
                    pass

    def test_sql_counter_reset_fails_closed(self):
        def decrease_digest(data):
            data["sql"]["digest"]["rows"][0]["SUM_TIMER_WAIT"] = "1"
        self.install_remote(RemoteFake(mutate_end=decrease_digest))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "counter reset"):
                with seasonal_telemetry.measure(SSH, MESSAGE, Path(temp), interval=0.01):
                    pass

    def test_workload_error_is_preserved_when_finalization_also_fails(self):
        self.install_remote(RemoteFake(fail_end=True))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError) as caught:
                with seasonal_telemetry.measure(SSH, MESSAGE, Path(temp), interval=0.01):
                    raise RuntimeError("workload boom")
            self.assertEqual(str(caught.exception), "workload boom")
            self.assertIn("boundary boom", "\n".join(getattr(caught.exception, "__notes__", [])))

    def test_sampler_error_type_and_message_are_preserved(self):
        self.install_remote(RemoteFake(fail_sample=True))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(KeyError) as caught:
                with seasonal_telemetry.measure(SSH, MESSAGE, Path(temp), interval=0.01):
                    pass
            self.assertEqual(str(caught.exception), "'sampler boom'")
            self.assertTrue((Path(temp) / "arm-telemetry.json").is_file())

    def test_incomplete_deployed_project_fails_before_workload(self):
        def missing_backend(data):
            data["services"]["backend"] = {"missing": True}
        fake = RemoteFake(mutate_start=missing_backend)
        self.install_remote(fake)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "deployed project telemetry coverage"):
                with seasonal_telemetry.measure(SSH, MESSAGE, Path(temp), interval=0.01):
                    self.fail("workload must not start without a deployed measured service")
        self.assertFalse(any("telemetry_sample" in suffix for _message, suffix in fake.calls))

    def test_boot_identity_is_required_before_any_remote_call(self):
        fake = RemoteFake()
        self.install_remote(fake)
        message = dict(MESSAGE, target={key: value for key, value in TARGET.items() if key != "boot_id"})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "boot_id"):
                with seasonal_telemetry.measure(SSH, message, Path(temp)):
                    pass
        self.assertEqual(fake.calls, [])

    def test_guest_payload_is_valid_python_when_composed_like_remote(self):
        arm_source = (Path(__file__).with_name("arm_guest.py")).read_text()
        source = (arm_source + "\nmessage=json.loads('{}')\n" + seasonal_telemetry.GUEST_SCRIPT
                  + "\ntelemetry_boundary(message)\n")
        compile(source, "seasonal-telemetry-guest", "exec")

    def test_whole_guest_counter_reset_fails_closed(self):
        def reset_cpu(data):
            data["resource"]["cpu_ticks"] = 10
        self.install_remote(RemoteFake(mutate_end=reset_cpu))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "counter reset"):
                with seasonal_telemetry.measure(SSH, MESSAGE, Path(temp), interval=0.01):
                    pass


if __name__ == "__main__":
    unittest.main()
