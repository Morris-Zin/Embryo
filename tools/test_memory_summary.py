import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_summary import summarize


FIELDS = [
    "timestamp", "container", "container_id", "pid", "rss_kb", "pss_kb", "private_dirty_kb",
    "cgroup_memory_current_bytes", "cgroup_memory_max_bytes", "cgroup_oom_kill", "docker_oom_killed",
    "restart_count", "cgroup_memory_peak_bytes", "cgroup_oom_events", "cgroup_swap_current_bytes",
    "cgroup_memory_anon_bytes", "cgroup_memory_file_bytes", "cgroup_memory_kernel_bytes",
    "cgroup_memory_slab_bytes", "cgroup_cpu_usage_usec", "cgroup_cpu_nr_throttled", "cgroup_cpu_throttled_usec",
]


def row(ts, container, rss_kb, pss_kb, cpu_usec):
    return {
        "timestamp": ts,
        "container": container,
        "container_id": "abc",
        "pid": "100",
        "rss_kb": str(rss_kb),
        "pss_kb": str(pss_kb),
        "private_dirty_kb": str(pss_kb // 2),
        "cgroup_memory_current_bytes": str(rss_kb * 1024),
        "cgroup_memory_max_bytes": str(1024 * 1024 * 1024),
        "cgroup_oom_kill": "0",
        "docker_oom_killed": "false",
        "restart_count": "0",
        "cgroup_memory_peak_bytes": str(rss_kb * 1024),
        "cgroup_oom_events": "0",
        "cgroup_swap_current_bytes": "0",
        "cgroup_memory_anon_bytes": str(pss_kb * 1024),
        "cgroup_memory_file_bytes": "0",
        "cgroup_memory_kernel_bytes": "0",
        "cgroup_memory_slab_bytes": "0",
        "cgroup_cpu_usage_usec": str(cpu_usec),
        "cgroup_cpu_nr_throttled": "0",
        "cgroup_cpu_throttled_usec": "0",
    }


class MemorySummaryTests(unittest.TestCase):
    def test_summarizes_memory_and_cpu_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.csv"
            rows = [
                row("2026-01-01T00:00:00+00:00", "backend", 102400, 51200, 1_000_000),
                row("2026-01-01T00:00:02+00:00", "backend", 204800, 102400, 3_000_000),
                row("2026-01-01T00:00:00+00:00", "db", 102400, 51200, 2_000_000),
                row("2026-01-01T00:00:02+00:00", "db", 102400, 51200, 4_000_000),
            ]
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            result = summarize(path)
        backend = result["containers"]["backend"]
        self.assertEqual(backend["samples"], 2)
        self.assertEqual(backend["pss_mib_peak"], 100.0)
        self.assertEqual(backend["rss_mib_avg"], 150.0)
        self.assertEqual(backend["cpu_percent_avg"], 100.0)
        self.assertEqual(result["fleet"]["pss_mib_peak"], 150.0)
        self.assertEqual(result["fleet"]["cpu_percent_avg"], 200.0)

    def test_missing_process_memory_does_not_become_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.csv"
            missing = row("2026-01-01T00:00:00+00:00", "backend", 102400, 51200, 1)
            missing["pss_kb"] = "NA"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerow(missing)
            result = summarize(path)
        self.assertIsNone(result["fleet"]["pss_mib_avg"])
        self.assertEqual(result["fleet"]["rss_mib_avg"], 100.0)

    def test_window_filters_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.csv"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerow(row("2026-01-01T00:00:00+00:00", "backend", 102400, 51200, 1))
                writer.writerow(row("2026-01-01T00:00:04+00:00", "backend", 204800, 102400, 2))
            from memory_summary import parse_time
            result = summarize(path, start=parse_time("2026-01-01T00:00:03+00:00"))
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["containers"]["backend"]["pss_mib_peak"], 100.0)


if __name__ == "__main__":
    unittest.main()
