#!/usr/bin/env python3
"""Offline contract tests for dev-only process/kernel OOM telemetry."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import process_kernel_telemetry as pkt
import seasonal_driver
import seasonal_telemetry
from test_seasonal_telemetry import MESSAGE, SSH, TARGET


CONTAINER = "f54f7734f1726fbde591997dcac17ee3d12effe9f79240bc39b20662ac0cfcc6"
CGROUP = "/sys/fs/cgroup/system.slice/docker-" + CONTAINER + ".scope"
KERNEL_CGROUP = "/system.slice/docker-" + CONTAINER + ".scope"
GUNICORN_CMD = ["/home/frappe/frappe-bench/env/bin/gunicorn", "--chdir=/home/frappe/frappe-bench/sites",
                "--bind=0.0.0.0:8000", "--threads=2", "--workers=2", "--worker-class=gthread",
                "--worker-tmp-dir=/dev/shm", "--timeout=120", "--keep-alive=65", "--preload",
                "--max-requests=1500", "--max-requests-jitter=300", "frappe.app:application"]


def service_meta():
    return {"container_name": MESSAGE["namespace"] + "-backend-1", "container_id": CONTAINER,
            "image_id": "sha256:" + "3" * 64, "container_pid": 11180,
            "started_at": "2026-11-27T07:12:08.548999982Z", "restart_count": 0,
            "oom_killed": False, "cgroup_path": CGROUP, "command": GUNICORN_CMD,
            "command_truncated": False, "entrypoint": [], "entrypoint_truncated": False}


def process(pid, ppid, rss, threads):
    return {"pid": pid, "tgid": pid, "ppid": ppid, "uid": 1000, "comm": "gunicorn",
            "state": "S (sleeping)", "cmdline": GUNICORN_CMD, "cmdline_truncated": False,
            "container_init": pid == 11180, "start_time_ticks": 1000 + pid,
            "vm_size_bytes": 417853440, "rss_bytes": rss, "rss_anon_bytes": rss - 6_000_000,
            "rss_file_bytes": 6_000_000, "rss_shmem_bytes": 0,
            "threads": [{"tid": tid, "tgid": pid, "comm": comm, "state": "S (sleeping)"}
                        for tid, comm in threads]}


def process_sample():
    return {"collected_at": "2026-11-27T07:13:53.957Z", "target": TARGET,
            "namespace": MESSAGE["namespace"],
            "targets": {"backend": {"cgroup_path": CGROUP, "memory_current_bytes": 567521280,
                                     "memory_max_bytes": 603979776,
                                     "memory_events": {"oom": 0, "oom_kill": 0},
                                     "processes": [
                                         process(11180, 1, 86900736, [(11180, "gunicorn")]),
                                         process(11408, 11180, 325341184,
                                                 [(11408, "gunicorn"), (12646, "ThreadPoolExecu")]),
                                         process(11409, 11180, 307425280,
                                                 [(11409, "gunicorn"), (12648, "ThreadPoolExecu")]),
                                     ]}}}


def kernel_records():
    rows = [
        (1795763636495260, "ThreadPoolExecu invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=0"),
        (1795763636495405, "CPU: 0 PID: 12646 Comm: ThreadPoolExecu Not tainted 6.8.0-147-generic #147-Ubuntu"),
        (1795763636500774, "memory: usage 589840kB, limit 589824kB, failcnt 626"),
        (1795763636500788, "Memory cgroup stats for " + KERNEL_CGROUP + ":"),
        (1795763636505353, "[  11180]  1000 11180    31216    21216    19872     1344         0   303104        0             0 gunicorn"),
        (1795763636505378, "[  11408]  1000 11408   102015    79429    77925     1504         0   774144        0             0 gunicorn"),
        (1795763636505387, "[  11409]  1000 11409    93578    75055    73583     1472         0   733184        0             0 gunicorn"),
        (1795763636505394, "oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=docker-" + CONTAINER + ".scope,mems_allowed=0,oom_memcg=" + KERNEL_CGROUP + ",task_memcg=" + KERNEL_CGROUP + ",task=gunicorn,pid=11408,uid=1000"),
        (1795763636505417, "Memory cgroup out of memory: Killed process 11408 (gunicorn) total-vm:408060kB, anon-rss:311700kB, file-rss:6016kB, shmem-rss:0kB, UID:1000 pgtables:756kB oom_score_adj:0"),
    ]
    return [{"__REALTIME_TIMESTAMP": str(us), "__MONOTONIC_TIMESTAMP": str(us // 1000),
             "__CURSOR": "cursor-%d" % index, "_BOOT_ID": "boot-1", "_HOSTNAME": "khitmee-lab",
             "SYSLOG_IDENTIFIER": "kernel", "PRIORITY": "4", "MESSAGE": message}
            for index, (us, message) in enumerate(rows)]


class RemoteFake:
    def __init__(self, sample=None):
        self.calls = []
        self.sample = sample if sample is not None else process_sample()

    def __call__(self, target, message, script="", suffix="main(message)", timeout=360):
        self.calls.append((dict(message), suffix))
        services = {"backend": service_meta()}
        if "process_kernel_start" in suffix:
            return {"collected_at": "2026-11-27T07:13:00.754Z", "target": TARGET,
                    "namespace": MESSAGE["namespace"], "page_size_bytes": 4096,
                    "ticks_per_second": 100, "journal_cursor": "start-cursor", "services": services}
        if "process_kernel_sample" in suffix:
            return json.loads(json.dumps(self.sample))
        if "process_kernel_stop" in suffix:
            return {"collected_at": "2026-11-27T07:14:53.414Z", "target": TARGET,
                    "namespace": MESSAGE["namespace"], "services": services,
                    "kernel": {"start_cursor": "start-cursor", "end_cursor": "cursor-8",
                               "record_count": len(kernel_records()), "records": kernel_records()}}
        raise AssertionError(suffix)


class ProcessKernelTelemetryTest(unittest.TestCase):
    def install_remote(self, fake):
        original = seasonal_driver.remote
        seasonal_driver.remote = fake
        self.addCleanup(setattr, seasonal_driver, "remote", original)

    def wait_for_sample(self, handle):
        for _ in range(200):
            if handle.samples:
                return
            time.sleep(0.001)
        self.fail("process sample did not complete")

    def test_parse_kernel_oom_event_exact_fields(self):
        event, = pkt.parse_oom_events(kernel_records())
        self.assertTrue(event["complete"])
        self.assertEqual(event["invoked_at"], "2026-11-27T07:13:56.495260Z")
        self.assertEqual(event["allocator"], {"tid": 12646, "comm": "ThreadPoolExecu",
                                              "timestamp_us": 1795763636495405,
                                              "timestamp": "2026-11-27T07:13:56.495405Z"})
        self.assertEqual(event["memory_cgroup"], {"usage_bytes": 603996160, "limit_bytes": 603979776,
                                                  "failcnt": 626, "usage_reported_kib": 589840,
                                                  "limit_reported_kib": 589824})
        self.assertEqual(event["victim_selection"]["pid"], 11408)
        self.assertEqual(event["victim_selection"]["constraint"], "CONSTRAINT_MEMCG")
        self.assertEqual(event["victim"]["rss_bytes"], 325341184)
        self.assertEqual(event["victim"]["rss_anon_bytes"], 319180800)
        self.assertEqual(event["victim"]["rss_file_bytes"], 6160384)
        self.assertEqual(event["victim"]["total_vm_bytes"], 417853440)
        self.assertEqual(event["victim"]["pgtables_bytes"], 774144)
        self.assertEqual(len(event["tasks"]), 3)

    def test_start_stop_artifact_maps_allocator_thread_to_owning_process(self):
        fake = RemoteFake()
        self.install_remote(fake)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            handle = pkt.start(SSH, MESSAGE, folder, interval=0.01)
            self.wait_for_sample(handle)
            result = pkt.stop(handle)
            self.assertTrue(result["complete"])
            self.assertEqual(result["artifact"], pkt.ARTIFACT_NAME)
            raw = (folder / pkt.ARTIFACT_NAME).read_bytes()
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            artifact = json.loads(raw)
            event, = artifact["oom_events"]
            self.assertEqual(event["service"], "backend")
            self.assertEqual(event["container_id"], CONTAINER)
            self.assertEqual(event["allocator"]["resolution"], "direct-thread-sample")
            self.assertEqual(event["allocator"]["owning_process"]["pid"], 11408)
            self.assertEqual(event["allocator"]["owning_process"]["process_role"], "container-child")
            self.assertEqual(event["allocator"]["thread"]["comm"], "ThreadPoolExecu")
            self.assertEqual(event["victim"]["pid"], 11408)
            self.assertEqual(event["victim"]["process"]["cmdline"], GUNICORN_CMD)
            self.assertEqual(artifact["collection_scope"]["route_attribution"],
                             "not-collected-in-procfs-or-kernel-oom-records")

    def test_measure_context_manager_returns_started_handle_and_stops(self):
        self.install_remote(RemoteFake())
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with pkt.measure(SSH, MESSAGE, folder, interval=0.01) as handle:
                self.assertIsInstance(handle, pkt.ProcessKernelMeasurement)
                self.wait_for_sample(handle)
            self.assertTrue(handle.finished)
            self.assertTrue((folder / pkt.ARTIFACT_NAME).is_file())

    def test_unmapped_allocator_fails_closed_and_retains_evidence(self):
        sample = process_sample()
        for proc in sample["targets"]["backend"]["processes"]:
            proc["threads"] = [thread for thread in proc["threads"] if thread["tid"] != 12646]
        self.install_remote(RemoteFake(sample=sample))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            handle = pkt.start(SSH, MESSAGE, folder, interval=0.01)
            self.wait_for_sample(handle)
            with self.assertRaisesRegex(ValueError, "allocator_thread_owner"):
                pkt.stop(handle)
            artifact = json.loads((folder / pkt.ARTIFACT_NAME).read_bytes())
            self.assertFalse(artifact["complete"])
            event, = artifact["oom_events"]
            self.assertEqual(event["allocator"]["resolution"], "unresolved-thread-not-in-process-samples")

    def test_sensitive_cmdline_values_are_redacted_in_guest_shape(self):
        self.assertEqual(pkt.sanitize_text("x MYSQL_ROOT_PASSWORD=secret y"), "x MYSQL_ROOT_PASSWORD=<redacted> y")

    def test_guest_payload_is_valid_python_when_composed_like_remote(self):
        arm_source = (Path(__file__).with_name("arm_guest.py")).read_text()
        source = (arm_source + "\nmessage=json.loads('{}')\n" + pkt.GUEST_SCRIPT
                  + "\nprocess_kernel_start(message)\n")
        compile(source, "process-kernel-telemetry-guest", "exec")

    def test_input_validation_rejects_non_lab_target_before_remote_call(self):
        fake = RemoteFake()
        self.install_remote(fake)
        bad = dict(MESSAGE, target=dict(TARGET, hostname="not-the-lab"))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "pinned guest identity"):
                pkt.start(SSH, bad, Path(temp))
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
