import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_run import evaluate_files


STATS_FIELDS = [
    "timestamp",
    "container",
    "pid",
    "rss_kb",
    "pss_kb",
    "private_dirty_kb",
    "cgroup_memory_current_bytes",
    "cgroup_memory_max_bytes",
    "cgroup_oom_kill",
    "docker_oom_killed",
    "restart_count",
    "cgroup_memory_peak_bytes",
    "cgroup_oom_events",
    "cgroup_swap_current_bytes",
]


def write_summary(
    path,
    search_p95=180.0,
    submit_p95=900.0,
    report_p95=2500.0,
    failed_rate=0.0,
    errors=0,
    extra_metrics=None,
    include_report=True,
):
    payload = {
        "metrics": {
            "search_duration": {"type": "trend", "values": {"p(95)": search_p95, "count": 100}},
            "submit_duration": {"type": "trend", "values": {"p(95)": submit_p95, "count": 60}},
            "http_req_failed": {"type": "rate", "values": {"rate": failed_rate}},
            "errors": {"type": "counter", "values": {"count": errors}},
            "sale_vouchers": {"type": "counter", "values": {"count": 25}},
            "transfer_vouchers": {"type": "counter", "values": {"count": 7}},
        }
    }
    if include_report:
        payload["metrics"]["report_duration"] = {"type": "trend", "values": {"p(95)": report_p95, "count": 24}}
    if extra_metrics:
        payload["metrics"].update(extra_metrics)
    path.write_text(json.dumps(payload), encoding="utf-8")


def tagged_report_metrics(report_rows):
    metrics = {}
    for report, end_to_end_p95, server_p95 in report_rows:
        metrics[f"report_duration{{report:{report}}}"] = {"p(95)": end_to_end_p95, "count": 12}
        metrics[f"report_server_duration{{report:{report}}}"] = {"p(95)": server_p95, "count": 12}
    return metrics


def container_row(
    container,
    rss_mib,
    oom_kill="0",
    docker_oom_killed="false",
    restart_count="0",
    timestamp="2026-06-01T00:00:00Z",
    pid="1001",
    cgroup_mib=None,
):
    rss_kb = rss_mib * 1024
    cgroup_kb = cgroup_mib * 1024 if cgroup_mib is not None else rss_kb
    return {
        "timestamp": timestamp,
        "container": container,
        "pid": pid,
        "rss_kb": str(rss_kb),
        "pss_kb": str(rss_kb - 20 * 1024),
        "private_dirty_kb": str(rss_kb - 40 * 1024),
        "cgroup_memory_current_bytes": str(cgroup_kb * 1024),
        "cgroup_memory_max_bytes": str(512 * 1024 * 1024),
        "cgroup_oom_kill": oom_kill,
        "docker_oom_killed": docker_oom_killed,
        "restart_count": restart_count,
        "cgroup_memory_peak_bytes": str(cgroup_kb * 1024),
        "cgroup_oom_events": "0",
        "cgroup_swap_current_bytes": "0",
    }


def write_stats(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def healthy_rows(oom_kill="0", docker_oom_killed="false", restart_count="0"):
    return [
        container_row("khitmee-perf-backend-1", 700, oom_kill, docker_oom_killed, restart_count),
        container_row("khitmee-perf-db-1", 250, "0", "false", "0"),
    ]


def strict_headroom_rows():
    return [
        container_row("khitmee-perf-backend-1", 300, cgroup_mib=300),
        container_row("khitmee-perf-db-1", 300, cgroup_mib=300),
        container_row("khitmee-perf-frontend-1", 300, cgroup_mib=300),
    ]


def passing_report_tags():
    return tagged_report_metrics(
        [
            ("Stock Balance", 1800.0, 1500.0),
            ("Fast Gross Profit", 3200.0, 3000.0),
        ]
    )


class EvaluateRunTests(unittest.TestCase):
    def evaluate_fixture(
        self,
        rows=None,
        search_p95=180.0,
        report_p95=2500.0,
        extra_metrics=None,
        strict=False,
        require_all_metrics=True,
        include_report=True,
    ):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            summary = root / "summary.json"
            stats = root / "stats.csv"
            write_summary(
                summary,
                search_p95=search_p95,
                report_p95=report_p95,
                extra_metrics=extra_metrics,
                include_report=include_report,
            )
            write_stats(stats, healthy_rows() if rows is None else rows)
            return evaluate_files(
                summary,
                stats,
                run_label="unit-test",
                require_all_metrics=require_all_metrics,
                strict=strict,
            )

    def check_named(self, result, name):
        return next(check for check in result.checks if check.name == name)

    def test_passing_run_is_go(self):
        result = self.evaluate_fixture()
        self.assertEqual(result.verdict, "GO")
        core_names = {
            "Search end-to-end p95",
            "Voucher submit end-to-end p95",
            "Report end-to-end p95",
            "HTTP failure rate",
            "Logical / validation errors",
            "Container OOM kills",
            "Container restarts",
        }
        self.assertTrue(all(check.status == "PASS" for check in result.checks if check.name in core_names))
        headroom = [check for check in result.checks if check.name.endswith("cgroup headroom")]
        self.assertEqual(len(headroom), 3)
        self.assertTrue(all(check.status == "SKIP" for check in headroom))
        self.assertIn("## Verdict: **GO**", result.markdown)
        self.assertIn("khitmee-perf-backend-1", result.markdown)
        self.assertIn("2 GiB", result.markdown)

    def test_oom_kill_is_no_go(self):
        result = self.evaluate_fixture(rows=healthy_rows(oom_kill="1"))
        self.assertEqual(result.verdict, "NO-GO")
        oom_check = self.check_named(result, "Container OOM kills")
        self.assertEqual(oom_check.status, "FAIL")
        self.assertIn("Any OOM kill invalidates", result.markdown)

    def test_docker_oom_killed_is_no_go(self):
        result = self.evaluate_fixture(rows=healthy_rows(docker_oom_killed="true"))
        self.assertEqual(result.verdict, "NO-GO")
        oom_check = self.check_named(result, "Container OOM kills")
        self.assertEqual(oom_check.status, "FAIL")
        self.assertIn("Docker-reported OOM kills", result.markdown)

    def test_missing_oom_telemetry_is_no_go(self):
        rows = healthy_rows()
        for row in rows:
            row["cgroup_oom_kill"] = "NA"
            row["docker_oom_killed"] = ""
        result = self.evaluate_fixture(rows=rows)
        self.assertEqual(result.verdict, "NO-GO")
        oom_check = self.check_named(result, "Container OOM kills")
        self.assertEqual(oom_check.status, "FAIL")
        self.assertIn("never treated as zero", result.markdown)

    def test_container_restart_is_no_go(self):
        result = self.evaluate_fixture(rows=healthy_rows(restart_count="2"))
        self.assertEqual(result.verdict, "NO-GO")
        restart_check = self.check_named(result, "Container restarts")
        self.assertEqual(restart_check.status, "FAIL")
        self.assertEqual(restart_check.observed, "2")

    def test_missing_restart_telemetry_is_no_go(self):
        rows = healthy_rows()
        for row in rows:
            row["restart_count"] = "NA"
        result = self.evaluate_fixture(rows=rows)
        self.assertEqual(result.verdict, "NO-GO")
        restart_check = self.check_named(result, "Container restarts")
        self.assertEqual(restart_check.status, "FAIL")

    def test_mid_run_container_restart_is_no_go(self):
        rows = [
            container_row("khitmee-perf-backend-1", 700, "0", "false", "0", timestamp="2026-06-01T00:00:00Z"),
            container_row("khitmee-perf-db-1", 250, "0", "false", "0", timestamp="2026-06-01T00:00:00Z"),
            container_row("khitmee-perf-backend-1", 700, "0", "false", "1", timestamp="2026-06-01T00:00:02Z"),
            container_row("khitmee-perf-db-1", 250, "0", "false", "0", timestamp="2026-06-01T00:00:02Z"),
        ]
        result = self.evaluate_fixture(rows=rows)
        self.assertEqual(result.verdict, "NO-GO")
        restart_check = self.check_named(result, "Container restarts")
        self.assertEqual(restart_check.status, "FAIL")
        self.assertEqual(restart_check.observed, "1")

    def test_preexisting_steady_restart_count_does_not_taint_clean_run(self):
        row1_backend = container_row("khitmee-perf-backend-1", 700, "0", "false", "1", timestamp="2026-06-01T00:00:00Z")
        row1_db = container_row("khitmee-perf-db-1", 250, "0", "false", "0", timestamp="2026-06-01T00:00:00Z")
        row2_backend = container_row("khitmee-perf-backend-1", 700, "0", "false", "1", timestamp="2026-06-01T00:00:02Z")
        row2_db = container_row("khitmee-perf-db-1", 250, "0", "false", "0", timestamp="2026-06-01T00:00:02Z")
        row1_backend["pid"] = "1234"
        row2_backend["pid"] = "1234"
        row1_db["pid"] = "5678"
        row2_db["pid"] = "5678"
        result = self.evaluate_fixture(rows=[row1_backend, row1_db, row2_backend, row2_db])
        self.assertEqual(result.verdict, "GO")
        restart_check = self.check_named(result, "Container restarts")
        self.assertEqual(restart_check.status, "PASS")
        self.assertEqual(restart_check.observed, "0")

    def test_aggregate_only_summary_has_no_per_report_gates(self):
        result = self.evaluate_fixture()
        self.assertEqual(result.verdict, "GO")
        self.assertEqual(
            [check.name for check in result.checks],
            [
                "Search end-to-end p95",
                "Voucher submit end-to-end p95",
                "Report end-to-end p95",
                "HTTP failure rate",
                "Logical / validation errors",
                "Container OOM kills",
                "Container restarts",
                "db cgroup headroom",
                "backend cgroup headroom",
                "frontend cgroup headroom",
            ],
        )
        self.assertNotIn("## Per-report latency breakdown", result.markdown)
        self.assertFalse(
            any(check.name.startswith(("Stock Balance", "Fast Gross Profit")) for check in result.checks)
        )

    def test_tagged_per_report_passing_summaries_are_go(self):
        extra_metrics = tagged_report_metrics(
            [
                ("Stock Balance", 1800.0, 1500.0),
                ("Fast Gross Profit", 3200.0, 3000.0),
            ]
        )
        extra_metrics["report_duration{endpoint:query_report,report:Stock Balance}"] = extra_metrics.pop(
            "report_duration{report:Stock Balance}"
        )
        result = self.evaluate_fixture(extra_metrics=extra_metrics)
        self.assertEqual(result.verdict, "GO")
        self.assertEqual(self.check_named(result, "Stock Balance end-to-end p95").status, "PASS")
        self.assertEqual(self.check_named(result, "Fast Gross Profit end-to-end p95").status, "PASS")
        self.assertIn("## Per-report latency breakdown", result.markdown)
        self.assertIn("Stock Balance", result.markdown)
        self.assertIn("Fast Gross Profit", result.markdown)
        self.assertIn("3000.0 ms", result.markdown)

    def test_tagged_per_report_failure_is_no_go_even_when_aggregate_passes(self):
        extra_metrics = tagged_report_metrics(
            [
                ("Stock Balance", 1800.0, 1500.0),
                ("Fast Gross Profit", 5100.0, 4800.0),
            ]
        )
        result = self.evaluate_fixture(report_p95=4999.0, extra_metrics=extra_metrics)
        self.assertEqual(result.verdict, "NO-GO")
        self.assertEqual(self.check_named(result, "Report end-to-end p95").status, "PASS")
        self.assertEqual(self.check_named(result, "Stock Balance end-to-end p95").status, "PASS")
        failing_check = self.check_named(result, "Fast Gross Profit end-to-end p95")
        self.assertEqual(failing_check.status, "FAIL")
        self.assertEqual(failing_check.observed, "5100.0 ms (n=12)")
        self.assertIn("## Per-report latency breakdown", result.markdown)

    def test_cgroup_percent_uses_cgroup_current_not_multi_process_rss(self):
        rows = [
            container_row("khitmee-perf-backend-1", 700, cgroup_mib=400),
            container_row("khitmee-perf-db-1", 250, cgroup_mib=200),
        ]
        result = self.evaluate_fixture(rows=rows)
        self.assertEqual(result.verdict, "GO")
        backend = result.memory.containers["khitmee-perf-backend-1"]
        self.assertAlmostEqual(backend.peak_cgroup_bytes, 400 * 1024 * 1024)
        self.assertAlmostEqual(backend.peak_rss_bytes, 700 * 1024 * 1024)
        self.assertAlmostEqual(backend.peak_cgroup_percent, 400 / 512 * 100.0)
        self.assertIn("| khitmee-perf-backend-1 | 400.0 | 78.1% | 700.0", result.markdown)
        self.assertNotIn("136.7%", result.markdown)

    def test_strict_passing_headroom_and_reports_are_go(self):
        result = self.evaluate_fixture(
            rows=strict_headroom_rows(),
            strict=True,
            extra_metrics=passing_report_tags(),
        )
        self.assertEqual(result.verdict, "GO")
        for name in ("db cgroup headroom", "backend cgroup headroom", "frontend cgroup headroom"):
            self.assertEqual(self.check_named(result, name).status, "PASS")
        self.assertEqual(self.check_named(result, "Stock Balance end-to-end p95").status, "PASS")
        self.assertEqual(self.check_named(result, "Fast Gross Profit end-to-end p95").status, "PASS")

    def test_strict_missing_cgroup_telemetry_fails_closed(self):
        rows = strict_headroom_rows()
        rows[1]["cgroup_memory_current_bytes"] = "NA"
        result = self.evaluate_fixture(rows=rows, strict=True, extra_metrics=passing_report_tags())
        self.assertEqual(result.verdict, "NO-GO")
        check = self.check_named(result, "db cgroup headroom")
        self.assertEqual(check.status, "FAIL")
        self.assertEqual(check.observed, "no data")
        self.assertIn("cgroup_memory_current_bytes / cgroup_memory_max_bytes", check.detail)
        self.assertIsNone(result.memory.containers["khitmee-perf-db-1"].peak_cgroup_percent)

    def test_strict_missing_required_container_fails_closed(self):
        rows = [row for row in strict_headroom_rows() if "frontend" not in row["container"]]
        result = self.evaluate_fixture(rows=rows, strict=True, extra_metrics=passing_report_tags())
        self.assertEqual(result.verdict, "NO-GO")
        check = self.check_named(result, "frontend cgroup headroom")
        self.assertEqual(check.status, "FAIL")
        self.assertEqual(check.observed, "no data")

    def test_strict_headroom_caps_fail_closed(self):
        cases = (
            ("db", 450, "db cgroup headroom"),
            ("backend", 384, "backend cgroup headroom"),
            ("frontend", 450, "frontend cgroup headroom"),
        )
        for service, cgroup_mib, check_name in cases:
            with self.subTest(service=service):
                rows = strict_headroom_rows()
                for row in rows:
                    if f"-{service}-" in row["container"]:
                        row["cgroup_memory_current_bytes"] = str(cgroup_mib * 1024 * 1024)
                result = self.evaluate_fixture(rows=rows, strict=True, extra_metrics=passing_report_tags())
                self.assertEqual(result.verdict, "NO-GO")
                self.assertEqual(self.check_named(result, check_name).status, "FAIL")

    def test_strict_requires_both_tagged_reports_when_reporting_is_active(self):
        result = self.evaluate_fixture(rows=strict_headroom_rows(), strict=True)
        self.assertEqual(result.verdict, "NO-GO")
        for report in ("Stock Balance", "Fast Gross Profit"):
            check = self.check_named(result, f"{report} end-to-end p95")
            self.assertEqual(check.status, "FAIL")
            self.assertEqual(check.observed, "no data")
            self.assertIn("n >= 1", check.threshold)

    def test_strict_missing_one_required_report_fails(self):
        extra_metrics = tagged_report_metrics([("Stock Balance", 1800.0, 1500.0)])
        result = self.evaluate_fixture(rows=strict_headroom_rows(), strict=True, extra_metrics=extra_metrics)
        self.assertEqual(result.verdict, "NO-GO")
        self.assertEqual(self.check_named(result, "Stock Balance end-to-end p95").status, "PASS")
        missing = self.check_named(result, "Fast Gross Profit end-to-end p95")
        self.assertEqual(missing.status, "FAIL")
        self.assertEqual(missing.observed, "no data")

    def test_strict_report_with_zero_samples_fails_even_when_p95_passes(self):
        extra_metrics = passing_report_tags()
        extra_metrics["report_duration{report:Stock Balance}"]["count"] = 0
        result = self.evaluate_fixture(rows=strict_headroom_rows(), strict=True, extra_metrics=extra_metrics)
        self.assertEqual(result.verdict, "NO-GO")
        stock = self.check_named(result, "Stock Balance end-to-end p95")
        self.assertEqual(stock.status, "FAIL")
        self.assertEqual(stock.observed, "1800.0 ms (n=0)")
        self.assertEqual(self.check_named(result, "Fast Gross Profit end-to-end p95").status, "PASS")

    def test_strict_report_gates_are_not_active_for_non_report_scenarios(self):
        result = self.evaluate_fixture(
            rows=strict_headroom_rows(),
            strict=True,
            require_all_metrics=False,
            include_report=False,
        )
        self.assertEqual(result.verdict, "GO")
        self.assertEqual(self.check_named(result, "Report end-to-end p95").status, "SKIP")
        self.assertFalse(
            any(check.name.startswith(("Stock Balance", "Fast Gross Profit")) for check in result.checks)
        )

    def test_search_p95_latency_fail_is_no_go(self):
        result = self.evaluate_fixture(search_p95=350.0)
        self.assertEqual(result.verdict, "NO-GO")
        search_check = self.check_named(result, "Search end-to-end p95")
        self.assertEqual(search_check.status, "FAIL")
        self.assertEqual(search_check.observed, "350.0 ms (n=100)")


if __name__ == "__main__":
    unittest.main()
