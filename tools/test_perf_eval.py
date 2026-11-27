import csv
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

import perf_env
import perf_eval_ledger as ledger
import perf_parity
import perf_profile

K6_DIR = TOOLS_DIR.parent / "perf-suite" / "k6"
if str(K6_DIR) not in sys.path:
    sys.path.insert(0, str(K6_DIR))
import evaluate_run

PERF_EVAL_SH = TOOLS_DIR / "perf_eval.sh"
BASH_BIN = shutil.which("bash") or "bash"


def bash_path(path):
    text = str(path)
    if os.name == "nt" and len(text) >= 3 and text[1:3] == ":\\":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def clean_perf_eval_env():
    env = os.environ.copy()
    for key in ("COMPOSE_PROJECT_NAME", "TARGET_URL", "K6_NETWORK", "PERF_PASSWORD", "ADMIN_PASSWORD"):
        env.pop(key, None)
    return env


def run_perf_eval(args):
    return subprocess.run(
        [BASH_BIN, bash_path(PERF_EVAL_SH), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=clean_perf_eval_env(),
        timeout=15,
    )


def run_bash_script(script):
    return subprocess.run(
        [BASH_BIN, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=clean_perf_eval_env(),
        timeout=15,
    )


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def summary_payload(search=120.0, submit=900.0, report=None, fail_rate=0.0, errors=0, tagged=False):
    metrics = {
        "search_duration": {"p(95)": search, "count": 7},
        "submit_duration": {"p(95)": submit, "count": 5},
        "search_server_duration": {"p(95)": 12.5},
        "submit_server_duration": {"p(95)": 18.5},
        "http_req_failed": {"value": fail_rate},
        "errors": {"count": errors},
    }
    if report is not None:
        metrics["report_duration"] = {"p(95)": report, "count": 3}
        metrics["report_server_duration"] = {"p(95)": 21.5}
    if tagged:
        metrics["report_duration{endpoint:query_report,report:Stock Balance}"] = {"p(95)": 1800.0}
        metrics["report_duration{report:Fast Gross Profit}"] = {"p(95)": 3200.0}
    return {"metrics": metrics}


def write_scorecard(path, run_label, verdict="GO", search=120.0, submit=900.0, report=None,
                    fail_rate=0.0, errors=0, per_report=False, backend=(300.0, 200.0, 30.0),
                    db=(400.0, 350.0, 35.0), frontend=None, fleet_rss=700.0, fleet_pss=550.0):
    def ms(value):
        return "no data" if value is None else f"{value:.1f} ms"
    lines = [
        "# Khitmee ERPNext 2 GB vs 4 GB Go / No-Go Scorecard", "",
        "Generated: 2026-11-26 00:00:00 UTC", f"Run: `{run_label}`", "",
        f"## Verdict: **{verdict}**", "", "## Pass/fail checks", "",
        "| Check | Observed | Threshold | Result | Notes |", "|---|---:|---:|:---:|---|",
        f"| Search end-to-end p95 | {ms(search)} | < 300 ms | **PASS** |  |",
        f"| Voucher submit end-to-end p95 | {ms(submit)} | < 1500 ms | **PASS** |  |",
        f"| Report end-to-end p95 | {ms(report)} | < 5000 ms | **{'SKIP' if report is None else 'PASS'}** |  |",
        f"| HTTP failure rate | {fail_rate:.5f} | < 0.005 | **PASS** |  |",
        f"| Logical / validation errors | {errors} | = 0 | **PASS** |  |",
        "| Container OOM kills | 0 | = 0 | **PASS** |  |",
        "| Container restarts | 0 | = 0 | **PASS** |  |",
    ]
    if per_report:
        lines += [
            "| Stock Balance end-to-end p95 | 1800.0 ms | < 5000 ms | **PASS** |  |",
            "| Fast Gross Profit end-to-end p95 | 3200.0 ms | < 5000 ms | **PASS** |  |",
            "", "## Per-report latency breakdown", "",
            "| Report | End-to-end p95 | Server p95 | 5 s gate |", "|---|---:|---:|:---:|",
            "| Stock Balance | 1800.0 ms | 1500.0 ms | **PASS** |",
            "| Fast Gross Profit | 3200.0 ms | 3000.0 ms | **PASS** |",
        ]
    lines += [
        "", "## Container memory evidence", "",
        "| Container | Peak RSS | Peak PSS | Peak cgroup memory | Memory limit | OOM kills |",
        "|---|---:|---:|---:|---:|---:|",
        f"| khitmee-perf-backend-1 | {backend[0]:.1f} MiB | {backend[1]:.1f} MiB | {backend[2]:.1f}% | 1.00 GiB | 0 |",
        f"| khitmee-perf-db-1 | {db[0]:.1f} MiB | {db[1]:.1f} MiB | {db[2]:.1f}% | 1.00 GiB | 0 |",
    ]
    if frontend is not None:
        lines.append(f"| khitmee-perf-frontend-1 | {frontend[0]:.1f} MiB | {frontend[1]:.1f} MiB | {frontend[2]:.1f}% | 96.0 MiB | 0 |")
    lines += [
        "", f"Fleet peak RSS: **{fleet_rss:.1f} MiB**", "",
        f"Fleet peak PSS: **{fleet_pss:.1f} MiB**", "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def write_stats(path, include_frontend=False):
    fields = ["timestamp", "container", "pid", "rss_kb", "pss_kb", "private_dirty_kb",
              "cgroup_memory_current_bytes", "cgroup_memory_max_bytes", "cgroup_oom_kill",
              "docker_oom_killed", "restart_count"]

    def row(ts, container, rss_mib, pss_mib, limit_mib=1024, cgroup_mib=None):
        cgroup_mib = rss_mib if cgroup_mib is None else cgroup_mib
        return {
            "timestamp": ts, "container": container, "pid": "100",
            "rss_kb": str(int(rss_mib * 1024)), "pss_kb": str(int(pss_mib * 1024)),
            "private_dirty_kb": "0",
            "cgroup_memory_current_bytes": str(int(cgroup_mib * 1024 * 1024)),
            "cgroup_memory_max_bytes": str(int(limit_mib * 1024 * 1024)),
            "cgroup_oom_kill": "0", "docker_oom_killed": "false", "restart_count": "0",
        }

    rows = [
        row("2026-01-01T00:00:00Z", "khitmee-perf-backend-1", 100, 80),
        row("2026-01-01T00:00:00Z", "khitmee-perf-db-1", 50, 40),
        row("2026-01-01T00:00:02Z", "khitmee-perf-backend-1", 120, 90),
        row("2026-01-01T00:00:02Z", "khitmee-perf-db-1", 70, 60),
    ]
    if include_frontend:
        rows.extend([
            row("2026-01-01T00:00:00Z", "khitmee-perf-frontend-1", 20, 8, limit_mib=96, cgroup_mib=20),
            row("2026-01-01T00:00:02Z", "khitmee-perf-frontend-1", 25, 10, limit_mib=96, cgroup_mib=25),
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def artifact_path(root, prefix, suffix, kind):
    return root / f"{prefix}-{suffix}-{kind}"


def write_scenario_artifacts(root, prefix, suffix, search=120.0, submit=900.0, report=None,
                             verdict="GO", per_report=False, include_frontend=False):
    base = f"{prefix}-{suffix}"
    write_scorecard(
        root / f"{base}-scorecard.md", base, verdict, search, submit, report,
        per_report=per_report, frontend=(25.0, 10.0, 26.0) if include_frontend else None,
    )
    write_json(root / f"{base}-k6-summary.json", summary_payload(search, submit, report, tagged=per_report))
    write_stats(root / f"{base}-container-stats.csv", include_frontend=include_frontend)


def write_pass_artifacts(root, prefix, search=120.0, submit=900.0, include_frontend=False):
    write_scenario_artifacts(root, prefix, "sustained", search, submit, include_frontend=include_frontend)
    write_scenario_artifacts(root, prefix, "burst-2x", search, submit, include_frontend=include_frontend)
    write_scenario_artifacts(root, prefix, "reports", search, submit, report=2500.0, per_report=True, include_frontend=include_frontend)
    write_scenario_artifacts(root, prefix, "reports-daily", search, submit, report=1500.0, per_report=True, include_frontend=include_frontend)


class ScorecardParsingTests(unittest.TestCase):
    def test_old_scorecard_without_per_report_breakdown(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "run-scorecard.md"
            write_scorecard(path, "run", verdict="NO-GO", search=113.4, submit=1618.8, report=None)
            parsed = ledger.parse_scorecard(path)
        self.assertEqual(parsed["verdict"], "NO-GO")
        self.assertAlmostEqual(parsed["metrics"]["search_p95_ms"], 113.4)
        self.assertAlmostEqual(parsed["metrics"]["submit_p95_ms"], 1618.8)
        self.assertIsNone(parsed["metrics"]["report_p95_ms"])
        self.assertIsNone(parsed["metrics"]["stock_balance_p95_ms"])
        self.assertEqual(parsed["metrics"]["oom_kills"], 0)
        self.assertEqual(parsed["metrics"]["restarts"], 0)
        self.assertAlmostEqual(parsed["metrics"]["backend_peak_rss_mib"], 300.0)
        self.assertAlmostEqual(parsed["metrics"]["db_peak_pss_mib"], 350.0)
        self.assertAlmostEqual(parsed["metrics"]["fleet_peak_pss_mib"], 550.0)

    def test_current_scorecard_memory_table_preserves_cgroup_mib_and_percent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "run-scorecard.md"
            path.write_text(
                '\n'.join([
                    "## Verdict: **GO**",
                    "| Container | Peak Cgroup (MiB) | Cgroup % | Peak RSS (MiB) | Peak PSS (MiB) | Limit (MiB) | memory.peak (MiB) | OOM Events | OOM Kills | Peak Swap (MiB) |",
                    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                    "| khitmee-perf-backend-1 | 245.8 | 24.0% | 400.0 | 300.0 | 1024.0 | N/A | 0 | 0 | 0.0 |",
                    "| khitmee-perf-db-1 | 98.3 | 9.6% | 150.0 | 120.0 | 1024.0 | N/A | 0 | 0 | 0.0 |",
                    "",
                ]),
                encoding="utf-8",
            )
            parsed = ledger.parse_scorecard(path)
        self.assertAlmostEqual(parsed["metrics"]["backend_peak_cgroup_mib"], 245.8)
        self.assertAlmostEqual(parsed["metrics"]["backend_peak_pct"], 24.0)
        self.assertAlmostEqual(parsed["metrics"]["backend_peak_rss_mib"], 400.0)
        self.assertAlmostEqual(parsed["metrics"]["db_peak_cgroup_mib"], 98.3)

    def test_wave_1b_scorecard_with_per_report_breakdown(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "run-scorecard.md"
            write_scorecard(path, "run", report=2400.0, per_report=True)
            parsed = ledger.parse_scorecard(path)
        self.assertAlmostEqual(parsed["metrics"]["stock_balance_p95_ms"], 1800.0)
        self.assertAlmostEqual(parsed["metrics"]["fast_gross_profit_p95_ms"], 3200.0)

    def test_container_stats_csv_memory_peaks_are_exact(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "stats.csv"
            write_stats(path)
            parsed = ledger.parse_container_stats(path)
        self.assertAlmostEqual(parsed["backend_peak_rss_mib"], 120.0)
        self.assertAlmostEqual(parsed["backend_peak_pss_mib"], 90.0)
        self.assertAlmostEqual(parsed["db_peak_rss_mib"], 70.0)
        self.assertAlmostEqual(parsed["db_peak_pss_mib"], 60.0)
        self.assertAlmostEqual(parsed["fleet_peak_rss_mib"], 190.0)
        self.assertAlmostEqual(parsed["fleet_peak_pss_mib"], 150.0)
        self.assertAlmostEqual(parsed["backend_peak_pct"], 120 / 1024 * 100)
        self.assertEqual(parsed["oom_kills"], 0)
        self.assertEqual(parsed["restarts"], 0)

    def test_k6_summary_supports_direct_nested_and_tagged_metric_shapes(self):
        direct = summary_payload(tagged=True)
        nested = {"metrics": {name: {"values": values} for name, values in direct["metrics"].items()}}
        for payload in (direct, nested):
            parsed = ledger.parse_k6_summary_payload(payload)
            self.assertAlmostEqual(parsed["search_p95_ms"], 120.0)
            self.assertAlmostEqual(parsed["stock_balance_p95_ms"], 1800.0)
            self.assertAlmostEqual(parsed["fast_gross_profit_p95_ms"], 3200.0)
            self.assertIsNone(parsed["report_p95_ms"])
            self.assertAlmostEqual(parsed["stock_balance_p95_ms"], 1800.0)
            self.assertAlmostEqual(parsed["search_server_p95_ms"], 12.5)
            self.assertAlmostEqual(parsed["submit_server_p95_ms"], 18.5)
            self.assertEqual(parsed["search_samples"], 7)
            self.assertEqual(parsed["submit_samples"], 5)


    def test_frontend_scorecard_and_summary_memory_are_parsed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            scorecard = Path(tempdir) / "frontend-scorecard.md"
            write_scorecard(scorecard, "frontend-test", frontend=(25.0, 10.0, 26.0))
            parsed = ledger.parse_scorecard(scorecard)
        self.assertAlmostEqual(parsed["metrics"]["frontend_peak_rss_mib"], 25.0)
        self.assertAlmostEqual(parsed["metrics"]["frontend_peak_pss_mib"], 10.0)
        self.assertAlmostEqual(parsed["metrics"]["frontend_peak_pct"], 26.0)

        payload = {
            "memory": {"containers": {"khitmee-perf-frontend-1": {"peak_cgroup_mib": 25, "peak_pct": 26}}},
            "headroom_gates": {"frontend": {"status": "PASS", "observed": "26%"}},
        }
        memory, checks, evidence = ledger.parse_summary_memory_payload(payload)
        self.assertTrue(evidence)
        self.assertEqual(memory["frontend_peak_cgroup_mib"], 25.0)
        self.assertEqual(memory["frontend_peak_pct"], 26.0)
        self.assertEqual(checks[0]["status"], "PASS")
        self.assertEqual(checks[0]["source"], "summary")


class LedgerIntegrationTests(unittest.TestCase):
    def record(self, tmp, pass_id, prefix, search=120.0, submit=900.0):
        results = Path(tmp) / "results"
        write_pass_artifacts(results, prefix, search=search, submit=submit)
        argv = [
            "record", "--ledger", str(Path(tmp) / "eval-history.json"),
            "--markdown", str(Path(tmp) / "LEDGER.md"),
            "--pass-id", pass_id, "--label", pass_id + " label", "--profile", "4gb",
            "--results-dir", str(results), "--prefix", prefix,
            "--parity-pass", "true", "--safety-pass",
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(ledger.main(argv), 0)
        return stdout.getvalue()

    def test_record_is_idempotent_computes_deltas_and_renders_markdown(self):
        with tempfile.TemporaryDirectory() as tempdir:
            self.record(tempdir, "pass-0-baseline", "p0", search=200.0, submit=1000.0)
            self.record(tempdir, "pass-0-baseline", "p0", search=200.0, submit=1000.0)
            self.record(tempdir, "pass-1-sql-indexes", "p1", search=150.0, submit=1100.0)

            data = json.loads((Path(tempdir) / "eval-history.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["passes"]), 2)
            first, second = data["passes"]
            self.assertEqual(first["pass_id"], "pass-0-baseline")
            self.assertEqual(first["score"]["scenarios_go"], 4)
            self.assertEqual(first["score"]["scenarios_total"], 4)
            self.assertIn("delta_vs_previous", second)
            self.assertGreater(second["delta_vs_previous"]["aggregate"]["latency_ms_saved"]["search_p95_mean"], 0)
            self.assertLess(second["delta_vs_previous"]["aggregate"]["latency_ms_saved"]["submit_p95_mean"], 0)
            self.assertTrue(second["delta_vs_previous"]["regressions"])

            markdown = (Path(tempdir) / "LEDGER.md").read_text(encoding="utf-8")
            for section in ("## Go/No-Go Matrix", "## Latency Table", "## Memory Table", "## Pass-over-Pass Deltas"):
                self.assertIn(section, markdown)
            self.assertIn("pass-0-baseline (4gb)", markdown)
            self.assertIn("REGRESSION", markdown)

    def test_compare_cli_highlights_improvement_and_regression(self):
        with tempfile.TemporaryDirectory() as tempdir:
            self.record(tempdir, "pass-0-baseline", "p0", search=200.0, submit=1000.0)
            self.record(tempdir, "pass-1-sql-indexes", "p1", search=150.0, submit=1100.0)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = ledger.main([
                    "compare", "--ledger", str(Path(tempdir) / "eval-history.json"),
                    "--base", "pass-0-baseline", "--head", "pass-1-sql-indexes",
                ])
            self.assertEqual(rc, 0)
            output = stdout.getvalue()
            self.assertIn("IMPROVEMENT", output)
            self.assertIn("REGRESSION", output)
            self.assertIn("search_p95_mean", output)
            self.assertIn("submit_p95_mean", output)

    def test_missing_values_stay_null_and_missing_scenario_is_not_go(self):
        with tempfile.TemporaryDirectory() as tempdir:
            results = Path(tempdir) / "results"
            write_scenario_artifacts(results, "partial", "sustained", report=None)
            argv = [
                "record", "--ledger", str(Path(tempdir) / "eval-history.json"),
                "--markdown", str(Path(tempdir) / "LEDGER.md"),
                "--pass-id", "partial-pass", "--label", "partial", "--profile", "4gb",
                "--results-dir", str(results), "--prefix", "partial",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ledger.main(argv), 0)
            entry = json.loads((Path(tempdir) / "eval-history.json").read_text(encoding="utf-8"))["passes"][0]
            self.assertIsNone(entry["scenarios"]["sustained"]["metrics"]["report_p95_ms"])
            self.assertEqual(entry["score"]["scenarios_go"], 1)
            self.assertEqual(entry["score"]["strict_verdict"], "NO-GO")
            self.assertEqual(entry["score"]["scenarios_missing"], 3)


class RealScorecardDryRunTests(unittest.TestCase):
    def test_real_year_scorecards_parse_without_mutating_results(self):
        repo = TOOLS_DIR.parent
        results = repo / "perf-suite" / "results"
        required = [
            results / "year-4gb-khitmee-ops-sustained-scorecard.md",
            results / "year-2gb-khitmee-ops-sustained-scorecard.md",
        ]
        if not all(path.exists() for path in required):
            self.skipTest("real year scorecards are not present")
        with tempfile.TemporaryDirectory() as tempdir:
            for profile in ("4gb", "2gb"):
                argv = [
                    "record", "--ledger", str(Path(tempdir) / "eval-history.json"),
                    "--markdown", str(Path(tempdir) / "LEDGER.md"),
                    "--pass-id", "real-dry-run", "--label", "real scorecard dry run",
                    "--profile", profile, "--results-dir", str(results),
                    "--prefix", f"year-{profile}-khitmee-ops",
                ]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ledger.main(argv), 0)
            entries = json.loads((Path(tempdir) / "eval-history.json").read_text(encoding="utf-8"))["passes"]
        self.assertEqual(len(entries), 2)
        entry = entries[0]
        self.assertEqual(set(entry["scenarios"]), {"sustained", "burst-2x", "reports-30d", "reports-1d"})
        self.assertEqual(entry["scenarios"]["sustained"]["verdict"], "NO-GO")
        self.assertAlmostEqual(entry["scenarios"]["sustained"]["metrics"]["search_p95_ms"], 113.4, places=1)
        self.assertEqual(entry["scenarios"]["burst-2x"]["verdict"], "GO")
        self.assertGreater(entry["scenarios"]["reports-30d"]["metrics"]["fleet_peak_pss_mib"], 1000.0)
        self.assertAlmostEqual(entry["scenarios"]["sustained"]["metrics"]["backend_peak_pct"], 24.0, places=1)
        entry2 = entries[1]
        self.assertEqual(entry2["profile"], "2gb")
        self.assertEqual(entry2["scenarios"]["sustained"]["verdict"], "NO-GO")
        self.assertAlmostEqual(entry2["scenarios"]["sustained"]["metrics"]["search_p95_ms"], 111.0, places=1)
        self.assertEqual(entry2["scenarios"]["burst-2x"]["verdict"], "GO")
        self.assertGreater(entry2["scenarios"]["reports-30d"]["metrics"]["fleet_peak_pss_mib"], 900.0)
        self.assertGreater(entry2["scenarios"]["reports-1d"]["metrics"]["fleet_peak_pss_mib"], 1000.0)

    def test_historical_pass0_pass1_pass2_ledger_records_still_render(self):
        ledger_path = TOOLS_DIR.parent / "perf-suite" / "results" / "eval-history.json"
        if not ledger_path.exists():
            self.skipTest("historical eval ledger is not present")
        original = ledger_path.read_text(encoding="utf-8-sig")
        data = json.loads(original)
        pass_ids = {entry.get("pass_id") for entry in data.get("passes", [])}
        if not {"pass-0", "pass-1", "pass-2"}.issubset(pass_ids):
            self.skipTest("historical pass-0/pass-1/pass-2 records are not present")
        markdown = ledger.render_markdown(data)
        self.assertIn("pass-0", markdown)
        self.assertIn("pass-1", markdown)
        self.assertIn("pass-2", markdown)
        self.assertIn("frontend_peak_cgroup_mib", markdown)
        self.assertIn("Strict verdict", markdown)
        self.assertEqual(ledger_path.read_text(encoding="utf-8-sig"), original)

    def test_render_markdown_marks_missing_legacy_v1_gates(self):
        def entry(pass_id, schema_version):
            return {
                "pass_id": pass_id,
                "profile": "4gb",
                "label": pass_id,
                "schema_version": schema_version,
                "scenarios": {
                    scenario: {"verdict": "GO", "metrics": {}}
                    for scenario in ledger.SCENARIOS
                },
                "score": {"scenarios_go": 4, "scenarios_total": 4, "strict_verdict": "GO"},
                "prefix": pass_id,
            }

        legacy = entry("legacy-pass", 1)
        current = entry("current-pass", 2)
        current["safety_pass"] = True
        current["k6_all_zero"] = True
        markdown = ledger.render_markdown({"passes": [legacy, current]})

        self.assertIn("Historical schema v1 passes (pre-Pass-2) predated safety_pass, k6_all_zero, and cgroup headroom gates", markdown)
        matrix_section = markdown.split("## Go/No-Go Matrix", 1)[1].split("## Strict Gate Failure Reasons", 1)[0]
        metadata_section = markdown.split("## Run Metadata", 1)[1]
        legacy_matrix = next(line for line in matrix_section.splitlines() if line.startswith("| legacy-pass |"))
        current_matrix = next(line for line in matrix_section.splitlines() if line.startswith("| current-pass |"))
        legacy_metadata = next(line for line in metadata_section.splitlines() if line.startswith("| legacy-pass (4gb) |"))
        current_metadata = next(line for line in metadata_section.splitlines() if line.startswith("| current-pass (4gb) |"))

        self.assertTrue(legacy_matrix.endswith("| GO (legacy v1) |"))
        self.assertTrue(current_matrix.endswith("| GO |"))
        self.assertIn("| N/A (legacy v1) | N/A (legacy v1) |", legacy_metadata)
        self.assertIn("| true | true |", current_metadata)
        self.assertNotIn("legacy v1", current_matrix)
        self.assertNotIn("legacy v1", current_metadata)


@unittest.skipUnless(shutil.which("bash"), "bash is required for perf_eval.sh tests")
class PerfEvalShellTests(unittest.TestCase):
    def test_help_exits_zero_without_credential_file_dependency(self):
        result = run_perf_eval(["--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: bash tools/perf_eval.sh", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_run_prefix_normalization_avoids_double_profile_suffix(self):
        script = (
            f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
            "normalize_run_prefix pass-1 4gb; "
            "normalize_run_prefix pass-1-4gb 4gb"
        )
        result = run_bash_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["pass-1-4gb", "pass-1-4gb"])

    def test_dotenv_loader_parses_plain_quoted_and_crlf_values_as_data(self):
        cases = [
            ("ADMIN_PASSWORD=plain-secret\r\n", "plain-secret"),
            ('ADMIN_PASSWORD="quoted secret"\r\n', "quoted secret"),
            ("ADMIN_PASSWORD='single secret'\r\n", "single secret"),
            ("OTHER=noise\r\nexport ADMIN_PASSWORD=export-secret\r\n", "export-secret"),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                found, value = perf_env.parse_dotenv_key(content, "ADMIN_PASSWORD")
                self.assertTrue(found)
                self.assertEqual(value, expected)

    def test_shell_password_loader_uses_dotenv_without_printing_secret(self):
        sentinel = "unit-test-password"
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "tools").mkdir()
            (root / "perf-suite/docker").mkdir(parents=True)
            shutil.copy2(TOOLS_DIR / "perf_env.py", root / "tools/perf_env.py")
            (root / "perf-suite/docker/.env").write_bytes(
                f"ADMIN_PASSWORD=\"{sentinel}\"\r\n".encode("utf-8")
            )
            script = (
                f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
                f"ROOT={shlex.quote(bash_path(root))}; "
                f"PYBIN={shlex.quote(bash_path(Path(sys.executable)))}; "
                "SKIP_BENCH=0; DRY_RUN=0; "
                "require_perf_password_for_bench; "
                f"if [[ \"$PERF_PASSWORD\" == {shlex.quote(sentinel)} ]]; then echo password-loaded; "
                "else echo password-mismatch >&2; exit 9; fi"
            )
            result = run_bash_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("password-loaded", result.stdout)
        self.assertNotIn(sentinel, result.stdout)
        self.assertNotIn(sentinel, result.stderr)

    def test_shell_password_loader_refuses_empty_password_without_printing_it(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "tools").mkdir()
            (root / "perf-suite/docker").mkdir(parents=True)
            shutil.copy2(TOOLS_DIR / "perf_env.py", root / "tools/perf_env.py")
            (root / "perf-suite/docker/.env").write_bytes(b"ADMIN_PASSWORD=\"\"\r\n")
            script = (
                f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
                f"ROOT={shlex.quote(bash_path(root))}; "
                f"PYBIN={shlex.quote(bash_path(Path(sys.executable)))}; "
                "SKIP_BENCH=0; DRY_RUN=0; "
                "require_perf_password_for_bench"
            )
            result = run_bash_script(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PERF_PASSWORD is empty", result.stderr)
        self.assertNotIn("ADMIN_PASSWORD=\"\"", result.stdout + result.stderr)

    def test_parity_help_is_offline_and_live_project_env_is_refused(self):
        script = f"cd {shlex.quote(bash_path(TOOLS_DIR.parent))}; bash tools/khitmee-dev.sh parity --help"
        result = run_bash_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run the maintained khitmee-perf live parity suite", result.stdout)

        refused = subprocess.run(
            [BASH_BIN, "-c", f"cd {shlex.quote(bash_path(TOOLS_DIR.parent))}; bash tools/khitmee-dev.sh parity --help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
            env={**clean_perf_eval_env(), "COMPOSE_PROJECT_NAME": "khitmee-erpnext"}, timeout=15,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("COMPOSE_PROJECT_NAME must be khitmee-perf", refused.stderr)

    def test_dry_run_parses_notes_and_parity_pass_forms(self):
        base = ["--pass-id", "pass-1", "--profile", "4gb", "--label", "shell parsing", "--dry-run", "--skip-bench"]
        cases = [
            (["--parity-pass", "--notes", "plain notes"], "true", "plain notes"),
            (["--parity-pass", "false", "--notes=equals notes"], "false", "equals notes"),
            (["--parity-pass=true", "--notes", "explicit true"], "true", "explicit true"),
        ]
        for extra, parity, notes in cases:
            with self.subTest(extra=extra):
                result = run_perf_eval(base + extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("run_prefix=pass-1-4gb", result.stdout)
                self.assertIn(f"parity_pass={parity}", result.stdout)
                self.assertIn(f"Dry run notes: {notes}", result.stdout)

    def test_dry_run_parses_stock_replenish_and_preflight_boolean_forms(self):
        base = ["--pass-id", "pass-1", "--profile", "4gb", "--label", "stock parsing", "--dry-run", "--skip-bench"]
        cases = [
            (["--replenish-stock"], "replenish_stock=1", "require_stock_preflight=0"),
            (["--replenish-stock=false", "--require-stock-preflight"], "replenish_stock=0", "require_stock_preflight=1"),
            (["--replenish-stock=true", "--require-stock-preflight=false"], "replenish_stock=1", "require_stock_preflight=0"),
        ]
        for extra, replenish, preflight in cases:
            with self.subTest(extra=extra):
                result = run_perf_eval(base + extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(replenish, result.stdout)
                self.assertIn(preflight, result.stdout)


class PerfProfilePureHelperTests(unittest.TestCase):
    def test_sql_template_normalization_redacts_literals_and_numbers(self):
        query = "SELECT *   FROM `tabItem`\nWHERE item_code = 'KM-00001' AND modified > 12345 AND name LIKE \"%abc%\""
        normalized = perf_profile.normalize_sql_template(query)
        self.assertEqual(normalized, "SELECT * FROM `tabItem` WHERE item_code = ? AND modified > ? AND name LIKE ?")
        self.assertNotIn("KM-00001", normalized)
        self.assertNotIn("abc", normalized)

    def test_sql_template_truncation_is_bounded(self):
        normalized = perf_profile.normalize_sql_template("SELECT " + "x, " * 100 + "1", max_length=40)
        self.assertLessEqual(len(normalized), 40)
        self.assertTrue(normalized.endswith("..."))

    def test_timing_summary_uses_nearest_rank_p95(self):
        summary = perf_profile.summarize_timings([100.0, 200.0, 300.0])
        self.assertEqual(summary["count"], 3)
        self.assertAlmostEqual(summary["avg_ms"], 200.0)
        self.assertAlmostEqual(summary["p95_ms"], 300.0)
        self.assertAlmostEqual(summary["max_ms"], 300.0)

    def test_human_summary_is_concise_and_includes_key_metrics(self):
        result = {
            "mode": "all",
            "read_only": True,
            "reports": {
                "stock_balance_30d": {
                    "wall_ms": 123.4,
                    "sql_ms": 45.6,
                    "sql_count": 7,
                    "rows": 12,
                    "payload_bytes": 3456,
                    "memory": {"vmrss_delta_mib": 1.2, "tracemalloc_peak_mib": 3.4},
                }
            },
            "search": {"avg_ms": 20.0, "p95_ms": 30.0, "sql_count": 10,
                       "memory": {"tracemalloc_peak_mib": 1.0}},
        }
        summary = perf_profile.format_human_summary(result)
        self.assertIn("read_only=True", summary)
        self.assertIn("stock_balance_30d", summary)
        self.assertIn("sql=45.6ms/7q", summary)
        self.assertIn("search:", summary)
        self.assertLess(len(summary.splitlines()), 12)


class CgroupAccountingRegressionTests(unittest.TestCase):
    def write_cgroup_mismatch_stats(self, path):
        fields = [
            "timestamp", "container", "pid", "rss_bytes", "pss_bytes",
            "cgroup_memory_current_bytes", "cgroup_memory_max_bytes",
            "cgroup_oom_kill", "docker_oom_killed", "restart_count",
        ]
        rows = []
        for timestamp in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z"):
            rows.append({
                "timestamp": timestamp,
                "container": "khitmee-perf-backend-1",
                "pid": "100",
                "rss_bytes": str(400 * 1024 * 1024),
                "pss_bytes": str(300 * 1024 * 1024),
                "cgroup_memory_current_bytes": str(200 * 1024 * 1024),
                "cgroup_memory_max_bytes": str(1024 * 1024 * 1024),
                "cgroup_oom_kill": "0",
                "docker_oom_killed": "false",
                "restart_count": "0",
            })
            rows.append({
                "timestamp": timestamp,
                "container": "khitmee-perf-db-1",
                "pid": "200",
                "rss_bytes": str(150 * 1024 * 1024),
                "pss_bytes": str(120 * 1024 * 1024),
                "cgroup_memory_current_bytes": str(100 * 1024 * 1024),
                "cgroup_memory_max_bytes": str(1024 * 1024 * 1024),
                "cgroup_oom_kill": "0",
                "docker_oom_killed": "false",
                "restart_count": "0",
            })
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_ledger_uses_cgroup_current_for_percent_and_keeps_rss_pss_separate(self):
        with tempfile.TemporaryDirectory() as tempdir:
            stats = Path(tempdir) / "stats.csv"
            self.write_cgroup_mismatch_stats(stats)
            parsed = ledger.parse_container_stats(stats)
        self.assertAlmostEqual(parsed["backend_peak_cgroup_mib"], 200.0)
        self.assertAlmostEqual(parsed["backend_peak_rss_mib"], 400.0)
        self.assertAlmostEqual(parsed["backend_peak_pss_mib"], 300.0)
        self.assertAlmostEqual(parsed["backend_peak_pct"], 200.0 / 1024.0 * 100.0)
        self.assertAlmostEqual(parsed["db_peak_cgroup_mib"], 100.0)
        self.assertAlmostEqual(parsed["db_peak_pct"], 100.0 / 1024.0 * 100.0)

    def test_evaluate_run_uses_cgroup_current_for_percent_and_keeps_rss_pss_separate(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            stats = root / "stats.csv"
            summary = root / "summary.json"
            self.write_cgroup_mismatch_stats(stats)
            write_json(summary, summary_payload())
            result = evaluate_run.evaluate_files(summary, stats, run_label="cgroup-unit")
        backend = result.memory.containers["khitmee-perf-backend-1"]
        self.assertAlmostEqual(backend.peak_rss_bytes, 400 * 1024 * 1024)
        self.assertAlmostEqual(backend.peak_pss_bytes, 300 * 1024 * 1024)
        self.assertAlmostEqual(backend.peak_cgroup_percent, 200.0 / 1024.0 * 100.0)


class StrictGateRegressionTests(unittest.TestCase):
    def build_entry(self, tmp, **overrides):
        results = Path(tmp) / "results"
        write_pass_artifacts(results, "strict")
        options = {
            "pass_id": "strict-pass",
            "label": "strict gate",
            "profile": "4gb",
            "results_dir": results,
            "prefix": "strict",
            "parity_pass": True,
            "safety_pass": True,
            "k6_all_zero": True,
        }
        options.update(overrides)
        return ledger.build_entry(**options)

    def test_all_verified_gates_can_publish_strict_go(self):
        with tempfile.TemporaryDirectory() as tempdir:
            entry = self.build_entry(tempdir)
        self.assertTrue(entry["score"]["strict_go"])
        self.assertEqual(entry["score"]["strict_verdict"], "GO")
        self.assertEqual(entry["score"]["strict_gate_failures"], [])
        self.assertTrue(entry["safety_pass"])
        self.assertTrue(entry["k6_all_zero"])

    def test_no_safety_pass_cannot_publish_strict_go(self):
        with tempfile.TemporaryDirectory() as tempdir:
            entry = self.build_entry(tempdir, safety_pass=False)
        self.assertEqual(entry["score"]["strict_verdict"], "NO-GO")
        self.assertIn("safety_pass is not true", entry["score"]["strict_gate_failures"])

    def test_nonzero_k6_cannot_publish_strict_go(self):
        with tempfile.TemporaryDirectory() as tempdir:
            entry = self.build_entry(tempdir, k6_all_zero=False)
        self.assertEqual(entry["score"]["strict_verdict"], "NO-GO")
        self.assertIn("one or more k6 commands exited nonzero", entry["score"]["strict_gate_failures"])

    def test_missing_k6_sample_evidence_cannot_publish_strict_go(self):
        with tempfile.TemporaryDirectory() as tempdir:
            results = Path(tempdir) / "results"
            write_pass_artifacts(results, "strict")
            summary = results / "strict-sustained-k6-summary.json"
            payload = json.loads(summary.read_text(encoding="utf-8"))
            payload["metrics"]["search_duration"].pop("count", None)
            write_json(summary, payload)
            entry = ledger.build_entry(
                pass_id="strict-pass", label="strict", profile="4gb", results_dir=results,
                prefix="strict", parity_pass=True, safety_pass=True, k6_all_zero=True,
            )
        self.assertEqual(entry["score"]["strict_verdict"], "NO-GO")
        self.assertTrue(any("search_samples k6 sample evidence is missing or zero" in reason for reason in entry["score"]["strict_gate_failures"]))

    def test_missing_expected_artifact_cannot_publish_strict_go(self):
        with tempfile.TemporaryDirectory() as tempdir:
            results = Path(tempdir) / "results"
            write_pass_artifacts(results, "strict")
            (results / "strict-reports-k6-summary.json").unlink()
            entry = ledger.build_entry(
                pass_id="strict-pass", label="strict", profile="4gb", results_dir=results,
                prefix="strict", parity_pass=True, safety_pass=True, k6_all_zero=True,
            )
        self.assertEqual(entry["score"]["strict_verdict"], "NO-GO")
        self.assertTrue(any("missing artifacts" in reason for reason in entry["score"]["strict_gate_failures"]))

    def test_missing_oom_or_restart_telemetry_cannot_publish_strict_go(self):
        with tempfile.TemporaryDirectory() as tempdir:
            results = Path(tempdir) / "results"
            write_pass_artifacts(results, "strict")
            stats = results / "strict-sustained-container-stats.csv"
            with stats.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                row["cgroup_oom_kill"] = "NA"
                row["restart_count"] = "NA"
            with stats.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            entry = ledger.build_entry(
                pass_id="strict-pass", label="strict", profile="4gb", results_dir=results,
                prefix="strict", parity_pass=True, safety_pass=True, k6_all_zero=True,
            )
        sustained = entry["scenarios"]["sustained"]["metrics"]
        self.assertIsNone(sustained["oom_kills"])
        self.assertIsNone(sustained["restarts"])
        self.assertEqual(entry["score"]["strict_verdict"], "NO-GO")
        self.assertTrue(any("oom_kills telemetry is missing" in reason for reason in entry["score"]["strict_gate_failures"]))
        self.assertTrue(any("restarts telemetry is missing" in reason for reason in entry["score"]["strict_gate_failures"]))


class StrictHeadroomGateTests(unittest.TestCase):
    def scenarios(self, **metric_overrides):
        metrics = {
            "search_p95_ms": 100.0,
            "submit_p95_ms": 500.0,
            "report_p95_ms": 1000.0,
            "search_samples": 10.0,
            "submit_samples": 10.0,
            "report_samples": 10.0,
            "oom_kills": 0.0,
            "restarts": 0.0,
            "db_peak_cgroup_mib": 100.0,
            "db_peak_pct": 10.0,
            "backend_peak_cgroup_mib": 100.0,
            "backend_peak_pct": 10.0,
            "frontend_peak_cgroup_mib": 20.0,
            "frontend_peak_pct": 10.0,
        }
        metrics.update(metric_overrides)
        return {
            scenario: {"verdict": "GO", "missing_artifacts": [], "metrics": dict(metrics), "headroom_checks": []}
            for scenario in ledger.SCENARIOS
        }

    def verdict(self, scenarios, require_headroom=True):
        return ledger.evaluate_strict_verdict(
            scenarios, parity_pass=True, safety_pass=True, k6_all_zero=True,
            require_headroom=require_headroom,
        )

    def test_db_backend_and_frontend_headroom_thresholds_are_strictly_below_limits(self):
        for key, threshold in (("db_peak_pct", 85.0), ("backend_peak_pct", 75.0), ("frontend_peak_pct", 85.0)):
            with self.subTest(key=key):
                result = self.verdict(self.scenarios(**{key: threshold}))
                self.assertEqual(result["strict_verdict"], "NO-GO")
                self.assertTrue(any(f"{key} is {threshold:.1f}%" in failure for failure in result["strict_gate_failures"]))

        just_below = self.scenarios(db_peak_pct=84.9, backend_peak_pct=74.9, frontend_peak_pct=84.9)
        self.assertEqual(self.verdict(just_below)["strict_verdict"], "GO")

    def test_missing_frontend_cgroup_telemetry_fails_closed_when_headroom_required(self):
        result = self.verdict(self.scenarios(frontend_peak_cgroup_mib=None))
        self.assertEqual(result["strict_verdict"], "NO-GO")
        self.assertTrue(any("frontend_peak_cgroup_mib telemetry is missing" in failure for failure in result["strict_gate_failures"]))

    def test_legacy_records_without_headroom_evidence_remain_compatible(self):
        legacy_metrics = {
            "search_p95_ms": 100.0,
            "submit_p95_ms": 500.0,
            "report_p95_ms": 1000.0,
            "search_samples": 10.0,
            "submit_samples": 10.0,
            "report_samples": 10.0,
            "oom_kills": 0.0,
            "restarts": 0.0,
        }
        scenarios = {
            scenario: {"verdict": "GO", "missing_artifacts": [], "metrics": dict(legacy_metrics), "headroom_checks": []}
            for scenario in ledger.SCENARIOS
        }
        legacy = self.verdict(scenarios, require_headroom=False)
        self.assertEqual(legacy["strict_verdict"], "GO")

        explicit = self.verdict(scenarios, require_headroom=True)
        self.assertEqual(explicit["strict_verdict"], "NO-GO")
        self.assertTrue(any("db_peak_cgroup_mib telemetry is missing" in failure for failure in explicit["strict_gate_failures"]))

    def test_summary_headroom_evidence_auto_enforces_fail_closed_gates(self):
        scenarios = self.scenarios(frontend_peak_cgroup_mib=None, frontend_peak_pct=None)
        for evidence in scenarios.values():
            evidence["headroom_telemetry_in_summary"] = True
        result = self.verdict(scenarios, require_headroom=False)
        self.assertEqual(result["strict_verdict"], "NO-GO")
        self.assertTrue(any("frontend_peak_cgroup_mib telemetry is missing" in failure for failure in result["strict_gate_failures"]))

    def test_failed_evaluate_run_headroom_check_fails_closed(self):
        scenarios = self.scenarios()
        for evidence in scenarios.values():
            evidence["headroom_checks"] = [{"name": "db cgroup headroom", "status": "FAIL"}]
        result = self.verdict(scenarios, require_headroom=False)
        self.assertEqual(result["strict_verdict"], "NO-GO")
        self.assertTrue(any("db cgroup headroom" in failure for failure in result["strict_gate_failures"]))

    def test_markdown_bold_pass_headroom_check_is_accepted(self):
        scenarios = self.scenarios()
        for evidence in scenarios.values():
            evidence["headroom_checks"] = [{"name": "db cgroup headroom", "status": "**PASS**"}]
        result = self.verdict(scenarios, require_headroom=False)
        self.assertEqual(result["strict_verdict"], "GO")

    def test_frontend_memory_is_recorded_strictly_validated_and_rendered(self):
        with tempfile.TemporaryDirectory() as tempdir:
            results = Path(tempdir) / "results"
            write_pass_artifacts(results, "strict", include_frontend=True)
            entry = ledger.build_entry(
                pass_id="strict-pass", label="strict", profile="4gb", results_dir=results,
                prefix="strict", parity_pass=True, safety_pass=True, k6_all_zero=True,
                require_headroom=True,
            )
            metrics = entry["scenarios"]["sustained"]["metrics"]
            self.assertAlmostEqual(metrics["frontend_peak_cgroup_mib"], 25.0)
            self.assertAlmostEqual(metrics["frontend_peak_pct"], 25.0 / 96.0 * 100.0)
            self.assertEqual(entry["score"]["strict_verdict"], "GO")
            markdown = ledger.render_markdown({"passes": [entry]})
            self.assertIn("frontend_peak_cgroup_mib", markdown)
            self.assertIn("frontend_peak_pct", markdown)

    def test_compare_includes_frontend_db_and_backend_cgroup_deltas(self):
        base = {"metrics": {"backend_peak_cgroup_mib": 100.0, "db_peak_cgroup_mib": 100.0, "frontend_peak_cgroup_mib": 20.0}}
        head = {"metrics": {"backend_peak_cgroup_mib": 90.0, "db_peak_cgroup_mib": 110.0, "frontend_peak_cgroup_mib": 15.0}}
        comparison = ledger.compare_entries(
            {"pass_id": "base", "profile": "4gb", "scenarios": {"sustained": base}},
            {"pass_id": "head", "profile": "4gb", "scenarios": {"sustained": head}},
        )
        aggregate = comparison["aggregate"]["memory_mib_saved"]
        self.assertIn("backend_peak_cgroup_mib", aggregate)
        self.assertIn("db_peak_cgroup_mib", aggregate)
        self.assertIn("frontend_peak_cgroup_mib", aggregate)
        self.assertGreater(aggregate["backend_peak_cgroup_mib"], 0)
        self.assertLess(aggregate["db_peak_cgroup_mib"], 0)
        self.assertGreater(aggregate["frontend_peak_cgroup_mib"], 0)


class ComposeMemoryBudgetTests(unittest.TestCase):
    def mem_limit_sum_mib(self, path):
        text = path.read_text(encoding="utf-8")
        return sum(int(value) for value in re.findall(r"^\s*mem_limit:\s*(\d+)m\s*$", text, re.M))

    def test_offline_compose_memory_budgets_leave_host_headroom(self):
        docker_dir = TOOLS_DIR.parent / "perf-suite" / "docker"
        self.assertLessEqual(self.mem_limit_sum_mib(docker_dir / "profile-2gb.yaml"), 2000)
        self.assertLessEqual(self.mem_limit_sum_mib(docker_dir / "profile-4gb.yaml"), 3968)


class PerfParityPureHelperTests(unittest.TestCase):
    def gp_rows(self, child_parent="SI-1", qty=2.0):
        return [
            {"sales_invoice": "SI-1", "indent": 0, "qty": 1.0, "selling_amount": 100.0, "buying_amount": 70.0, "gross_profit": 30.0, "gross_profit_%": 30.0, "currency": "MMK"},
            {"parent_invoice": child_parent, "item_code": "ITEM-1", "indent": 1, "qty": qty, "avg._selling_rate": 50.0, "valuation_rate": 35.0, "selling_amount": 100.0, "buying_amount": 70.0, "gross_profit": 30.0, "gross_profit_%": 30.0, "currency": "MMK"},
            {"sales_invoice": "Total", "qty": qty, "selling_amount": 100.0, "buying_amount": 70.0, "gross_profit": 30.0, "gross_profit_%": 30.0, "currency": "MMK"},
        ]

    def test_gross_profit_comparison_accepts_exact_tree_and_financials(self):
        rows = self.gp_rows()
        self.assertEqual(perf_parity.compare_gross_profit_rows([], rows, [], rows), [])

    def test_gross_profit_comparison_catches_invoice_tree_contiguity_break(self):
        fast_rows = self.gp_rows(child_parent="OTHER-INVOICE")
        upstream_rows = self.gp_rows()
        mismatches = perf_parity.compare_gross_profit_rows([], fast_rows, [], upstream_rows)
        self.assertTrue(any(mismatch["kind"] in {"invoice_tree", "identity_field"} for mismatch in mismatches))

    def test_gross_profit_comparison_catches_financial_mismatch(self):
        fast_rows = self.gp_rows(qty=2.0011)
        upstream_rows = self.gp_rows(qty=2.0)
        mismatches = perf_parity.compare_gross_profit_rows([], fast_rows, [], upstream_rows)
        self.assertTrue(any(mismatch["kind"] == "numeric_field" and mismatch["field"] == "qty" for mismatch in mismatches))

    def test_stock_balance_comparison_catches_numeric_mismatch(self):
        columns = [{"fieldname": "item_code"}, {"fieldname": "warehouse"}, {"fieldname": "bal_qty"}]
        normal = [{"item_code": "ITEM-1", "warehouse": "W1", "bal_qty": 10.0}]
        ignore = [{"item_code": "ITEM-1", "warehouse": "W1", "bal_qty": 10.002}]
        mismatches = perf_parity.compare_stock_balance_rows(columns, normal, columns, ignore)
        self.assertTrue(any(mismatch["kind"] == "numeric_field" and mismatch["field"] == "bal_qty" for mismatch in mismatches))

    def test_numbers_close_accepts_ieee754_dust_at_tolerance_boundary(self):
        close, diff = perf_parity.numbers_close(21007.52, 21007.519, 0.001)

        self.assertTrue(close)
        self.assertGreater(diff, 0.001)
        self.assertLess(diff, 0.001000001)

    def test_gross_profit_invoice_comparison_is_canonical_for_tie_order(self):
        def invoice_rows(first_invoice_first=True, reverse_children=False):
            invoices = []
            for invoice, item_order in (("SI-1", ("ITEM-1", "ITEM-2")), ("SI-2", ("ITEM-3", "ITEM-4"))):
                invoices.append({
                    "sales_invoice": invoice, "indent": 0, "customer": "Customer", "posting_date": "2026-11-26",
                    "selling_amount": 100.0, "buying_amount": 70.0, "gross_profit": 30.0, "gross_profit_%": 30.0,
                    "currency": "USD",
                })
                children = []
                for item_code in item_order:
                    children.append({
                        "sales_invoice": item_code, "parent_invoice": invoice, "item_code": item_code,
                        "item_name": item_code + " Name", "warehouse": "W1", "indent": 1, "qty": 1.0,
                        "selling_amount": 50.0, "buying_amount": 35.0, "gross_profit": 15.0,
                        "gross_profit_%": 30.0, "currency": "USD",
                    })
                if reverse_children:
                    children.reverse()
                invoices.extend(children)
            if not first_invoice_first:
                invoices = invoices[3:] + invoices[:3]
            invoices.append({
                "sales_invoice": "Total", "qty": 4.0, "selling_amount": 200.0, "buying_amount": 140.0,
                "gross_profit": 60.0, "gross_profit_%": 30.0, "currency": "USD",
            })
            return invoices

        upstream = invoice_rows()
        fast = invoice_rows(first_invoice_first=False, reverse_children=True)
        self.assertEqual(perf_parity.compare_gross_profit_rows([], fast, [], upstream), [])

    def test_gross_profit_non_invoice_comparison_is_canonical_for_group_tie_order(self):
        rows = [
            {"item_code": "ITEM-1", "item_name": "One", "qty": 1.0, "selling_amount": 100.0, "buying_amount": 70.0, "gross_profit": 30.0, "gross_profit_%": 30.0, "currency": "USD"},
            {"item_code": "ITEM-2", "item_name": "Two", "qty": 1.0, "selling_amount": 100.0, "buying_amount": 70.0, "gross_profit": 30.0, "gross_profit_%": 30.0, "currency": "USD"},
            {"item_code": "Total", "qty": 2.0, "selling_amount": 200.0, "buying_amount": 140.0, "gross_profit": 60.0, "gross_profit_%": 30.0, "currency": "USD"},
        ]
        reordered = [rows[1], rows[0], rows[2]]

        self.assertEqual(perf_parity.compare_gross_profit_rows([], reordered, [], rows), [])

    def test_gross_profit_non_invoice_comparison_supports_none_column_fieldnames(self):
        columns = [{"fieldname": "item_code"}, {"fieldname": "item_name"}, {"fieldname": None}, {"fieldname": "selling_amount"}]
        rows = [
            ["ITEM-1", "One", "Brand", 100.0],
            ["ITEM-2", "Two", "Brand", 100.0],
            ["Total", None, None, 200.0],
        ]
        reordered = [rows[1], rows[0], rows[2]]

        self.assertEqual(perf_parity.compare_gross_profit_rows(columns, reordered, columns, rows), [])

    def test_explain_helper_accepts_covering_straight_join_and_fails_closed_on_bad_plans(self):
        good = [
            {"table": "si2", "type": "range", "key": "khitmee_si_co_ds_ret_date", "Extra": "Using where; Using index"},
            {"table": "sii2", "type": "ref", "key": "khitmee_sii_parent_warehouse", "Extra": "Using where; Using index"},
            {"table": "tabSales Invoice Item", "type": "eq_ref", "key": "PRIMARY", "Extra": ""},
            {"table": "tabSales Invoice", "type": "eq_ref", "key": "PRIMARY", "Extra": ""},
        ]
        self.assertEqual(perf_parity.gp_explain_offenses(good), [])

        bad = [dict(row) for row in good]
        bad[0]["type"] = "ALL"
        bad[1]["Extra"] = "Using where; Using index; Using filesort"
        bad[2]["type"] = "ref"
        offenses = perf_parity.gp_explain_offenses(bad)
        self.assertTrue(any(offense["kind"] == "full_table_scan" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "using_filesort" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "outer_access" for offense in offenses))

        missing = [row for row in good if row["table"] not in {"si2", "tabSales Invoice"}]
        offenses = perf_parity.gp_explain_offenses(missing)
        self.assertTrue(any(offense["kind"] == "si2_missing" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "outer_table_missing" for offense in offenses))

    def test_packed_item_and_sle_voucher_explain_helpers_fail_closed(self):
        packed_good = [{
            "table": "tabPacked Item", "type": "ref",
            "key": "khitmee_pi_pt_ds_item", "Extra": "Using index",
        }]
        self.assertEqual(perf_parity.packed_item_prefilter_explain_offenses(packed_good), [])
        packed_bad = [dict(packed_good[0], type="ALL", key=None, Extra="Using filesort")]
        offenses = perf_parity.packed_item_prefilter_explain_offenses(packed_bad)
        self.assertTrue(any(offense["kind"] == "full_table_scan" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "wrong_index" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "using_filesort" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "covering_index_missing" for offense in offenses))

        sle_good = [{
            "table": "tabStock Ledger Entry", "type": "range",
            "key": "voucher_no_voucher_type_index", "Extra": "Using where",
        }]
        self.assertEqual(perf_parity.sle_voucher_explain_offenses(sle_good), [])
        sle_bad = [dict(sle_good[0], type="ALL", key="wrong_key", Extra="Using filesort")]
        offenses = perf_parity.sle_voucher_explain_offenses(sle_bad)
        self.assertTrue(any(offense["kind"] == "full_table_scan" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "wrong_index" for offense in offenses))
        self.assertTrue(any(offense["kind"] == "using_filesort" for offense in offenses))

    def test_gp_coverage_audit_requires_returns_and_covers_present_bundles(self):
        cases = [
            ("shop-1-30d", {"company": "Khit Me Distribution", "warehouse": "Shop 1", "include_returned_invoices": 1}),
            ("all-warehouses", {"company": "Khit Me Distribution", "include_returned_invoices": 1}),
        ]
        rows = [
            {"name": "SI-RETURN", "company": "Khit Me Distribution", "posting_date": "2026-11-01", "is_return": 1, "warehouses": "Shop 1", "has_bundle": 0},
            {"name": "SI-BUNDLE", "company": "Khit Me Distribution", "posting_date": "2026-11-02", "is_return": 0, "warehouses": "Shop 1", "has_bundle": 1},
        ]
        coverage = perf_parity.audit_gp_coverage_rows(rows, cases)
        self.assertEqual(coverage["failures"], [])
        self.assertEqual(coverage["return_invoices_covered"], 1)
        self.assertEqual(coverage["bundle_invoices_covered"], 1)

        no_return_case = perf_parity.audit_gp_coverage_rows(rows, [("no-returns", {"company": "Khit Me Distribution"})])
        self.assertIn("no_gp_case_includes_returns", no_return_case["failures"])
        bundle_not_covered = perf_parity.audit_gp_coverage_rows(rows, [("other", {"company": "Other", "include_returned_invoices": 1})])
        self.assertIn("packed_item_bundle_invoices_not_covered_by_gp_cases", bundle_not_covered["failures"])

    def test_search_permission_probe_requires_allowed_present_and_denied_absent(self):
        allowed = {"items": [{"item_code": "ALLOWED-ITEM"}]}
        denied_empty = {"items": []}
        self.assertEqual(
            perf_parity.evaluate_search_permission_probe(allowed, denied_empty, "ALLOWED-ITEM", "DENIED-ITEM"),
            [],
        )
        failures = perf_parity.evaluate_search_permission_probe(
            {"items": []},
            {"items": [{"item_code": "DENIED-ITEM"}]},
            "ALLOWED-ITEM",
            "DENIED-ITEM",
        )
        self.assertIn("allowed_item_not_returned", failures)
        self.assertIn("denied_item_returned", failures)

    def run_parity_with_fake_frappe(self, counts_after, only=None):
        before_counts = dict.fromkeys(perf_parity.PERSISTED_TABLES, 7)
        after_counts = dict(before_counts)
        after_counts.update(counts_after)

        class FakeDB:
            def __init__(self):
                self.rollbacks = 0

            def sql(self, query, *args, **kwargs):
                for table in perf_parity.PERSISTED_TABLES:
                    if f"`{table}`" in query:
                        return [(before_counts[table],)]
                return []

            def rollback(self):
                self.rollbacks += 1

        class FakeFrappe:
            def __init__(self):
                self.db = FakeDB()
                self.destroyed = 0

            def destroy(self):
                self.destroyed += 1

        fake = FakeFrappe()
        args = SimpleNamespace(site="perf.localhost", profile="4gb", seed=1, only=only)
        hook_sites = []

        def hook(site):
            hook_sites.append(site)
            return after_counts

        output = io.StringIO()
        with mock.patch.object(perf_parity, "connect_frappe", return_value=(fake, "perf.localhost")), \
             mock.patch.object(perf_parity, "run_gp_parity"), \
             mock.patch.object(perf_parity, "run_sb_parity"), \
             mock.patch.object(perf_parity, "run_search_parity"), \
             mock.patch.object(perf_parity, "run_voucher_parity"), \
             contextlib.redirect_stdout(output):
            rc = perf_parity.run_inside_backend(args, fresh_connection_hook=hook)
        return rc, output.getvalue(), hook_sites

    def marker_payloads(self, output, marker):
        return [json.loads(line[len(marker) + 1:]) for line in output.splitlines() if line.startswith(marker + " ")]

    def test_full_parity_emits_unambiguous_pass_and_uses_fresh_connection_hook(self):
        rc, output, hook_sites = self.run_parity_with_fake_frappe({})
        self.assertEqual(rc, 0, output)
        self.assertEqual(hook_sites, ["perf.localhost"])
        passes = self.marker_payloads(output, "PARITY_PASS")
        partials = self.marker_payloads(output, "PARITY_PARTIAL_PASS")
        self.assertEqual(len(passes), 1)
        self.assertEqual(partials, [])
        self.assertFalse(passes[0]["partial"])
        self.assertEqual(passes[0]["sections"], sorted(perf_parity.ALL_SECTIONS))
        self.assertEqual(passes[0]["persisted_writes"], 0)
        persisted = self.marker_payloads(output, "PARITY_PERSISTED_WRITE_CHECK")
        self.assertEqual(len(persisted), 1)
        self.assertTrue(persisted[0]["ok"])
        self.assertEqual(sorted(persisted[0]["before"]), sorted(perf_parity.PERSISTED_TABLES))

    def test_only_subset_never_emits_full_parity_pass(self):
        rc, output, _hook_sites = self.run_parity_with_fake_frappe({}, only=["gp", "search"])
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.marker_payloads(output, "PARITY_PASS"), [])
        partials = self.marker_payloads(output, "PARITY_PARTIAL_PASS")
        self.assertEqual(len(partials), 1)
        self.assertTrue(partials[0]["partial"])
        self.assertEqual(partials[0]["sections"], ["gp", "search"])

    def test_fresh_connection_nonzero_persisted_writes_fail_closed(self):
        rc, output, hook_sites = self.run_parity_with_fake_frappe({"tabSales Invoice": 8})
        self.assertEqual(rc, 1)
        self.assertEqual(hook_sites, ["perf.localhost"])
        self.assertEqual(self.marker_payloads(output, "PARITY_PASS"), [])
        self.assertEqual(self.marker_payloads(output, "PARITY_PARTIAL_PASS"), [])
        persisted = self.marker_payloads(output, "PARITY_PERSISTED_WRITE_CHECK")
        self.assertEqual(persisted[0]["persisted_writes"], 1)
        self.assertFalse(persisted[0]["ok"])
        failures = self.marker_payloads(output, "PARITY_FAIL")
        self.assertIn("persisted_writes=1", failures[0]["error"])


@unittest.skipUnless(shutil.which("bash"), "bash is required for perf_eval.sh tests")
class PerfEvalArtifactCleanupTests(unittest.TestCase):
    def test_no_overwrite_refuses_stale_run_artifacts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            stale = root / "pass-1-4gb-sustained-scorecard.md"
            stale.write_text("stale", encoding="utf-8")
            script = (
                f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
                f"RESULT_DIR={shlex.quote(bash_path(root))}; "
                "RUN_PREFIX=pass-1-4gb; NO_OVERWRITE=1; "
                "clean_previous_run_artifacts"
            )
            result = run_bash_script(script)
        self.assertEqual(result.returncode, 6)
        self.assertIn("Refusing to overwrite", result.stderr)
        self.assertIn("pass-1-4gb-sustained-scorecard.md", result.stderr)

    def test_cleanup_removes_only_benchmark_stock_stats_and_log_artifacts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            for name in (
                "pass-1-4gb-sustained-scorecard.md",
                "pass-1-4gb-sustained-k6-summary.json",
                "pass-1-4gb-sustained-container-stats.csv",
                "pass-1-4gb-sustained-k6.log",
                "pass-1-4gb-stock-preflight.json",
                "pass-1-4gb-preflight-status.txt",
            ):
                (root / name).write_text("x", encoding="utf-8")
            script = (
                f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
                f"RESULT_DIR={shlex.quote(bash_path(root))}; "
                "RUN_PREFIX=pass-1-4gb; NO_OVERWRITE=0; "
                "clean_previous_run_artifacts; "
                "for f in \"$RESULT_DIR\"/pass-1-4gb-*; do printf '%s\\n' \"${f##*/}\"; done"
            )
            result = run_bash_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("removed 5", result.stdout)
        self.assertIn("pass-1-4gb-preflight-status.txt", result.stdout)
        self.assertNotIn("pass-1-4gb-sustained-scorecard.md", result.stdout)
        self.assertNotIn("pass-1-4gb-stock-preflight.json", result.stdout)

    def test_stock_replenish_helper_invokes_backend_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            seeder = root / "perf-suite/seeder"
            seeder.mkdir(parents=True)
            (seeder / "replenish_stock.py").write_text("# piped into the backend python\n", encoding="utf-8", newline="\n")
            capture = root / "docker-args.txt"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > " + shlex.quote(bash_path(capture)) + "\n"
                "echo '{\"ok\":true}'\n"
                "exit \"${DOCKER_EXIT:-0}\"\n",
                encoding="utf-8",
                newline="\n",
            )
            docker.chmod(0o755)
            output = root / "stock.json"
            script = (
                f"export PATH={shlex.quote(bash_path(bin_dir))}:\"$PATH\"; "
                f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
                f"ROOT={shlex.quote(bash_path(root))}; "
                f"run_stock_replenish_script --apply {shlex.quote(bash_path(output))}"
            )
            result = run_bash_script(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"ok": True})
            docker_args = capture.read_text(encoding="utf-8")
            self.assertIn("-w\n/home/frappe/frappe-bench/sites\nkhitmee-perf-backend-1", docker_args)
            self.assertIn("--apply", docker_args)

            failing_script = (
                f"export PATH={shlex.quote(bash_path(bin_dir))}:\"$PATH\"; export DOCKER_EXIT=7; "
                f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
                f"ROOT={shlex.quote(bash_path(root))}; "
                f"run_stock_replenish_script --check-only {shlex.quote(bash_path(output))}"
            )
            failing = run_bash_script(failing_script)
            self.assertEqual(failing.returncode, 5)
            self.assertIn("replenish_stock.py --check-only failed with exit 7", failing.stderr)

    def test_expected_service_safety_fails_when_started_at_changes(self):
        before = "\t".join([
            "backend", "khitmee-perf-backend-1", "id-backend", "running", "false", "0", "0", "0", "2026-01-01T00:00:00Z",
        ])
        after = "\t".join([
            "backend", "khitmee-perf-backend-1", "id-backend", "running", "false", "0", "0", "0", "2026-01-01T00:00:01Z",
        ])
        script = (
            f"source {shlex.quote(bash_path(PERF_EVAL_SH))}; "
            "PROFILE=4gb; "
            f"pre={shlex.quote(before)}; post={shlex.quote(after)}; "
            'verify_expected_service_safety "$pre" "$post" 0'
        )
        result = run_bash_script(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backend container StartedAt changed during the run", result.stderr + result.stdout)


class PerfEvalOrchestratorMirrorTests(unittest.TestCase):
    def make_mirror(self, root):
        root = Path(root)
        (root / "tools").mkdir(parents=True)
        (root / "perf-suite/results").mkdir(parents=True)
        (root / "perf-suite/seeder").mkdir(parents=True)
        (root / "perf-suite/scripts").mkdir(parents=True)
        shutil.copy2(PERF_EVAL_SH, root / "tools/perf_eval.sh")
        shutil.copy2(TOOLS_DIR / "perf_eval_ledger.py", root / "tools/perf_eval_ledger.py")
        shutil.copy2(TOOLS_DIR / "perf_env.py", root / "tools/perf_env.py")
        (root / "tools/khitmee-dev.sh").write_text("#!/usr/bin/env bash\necho mock-fast-check\n", encoding="utf-8", newline="\n")
        (root / "perf-suite/scripts/up.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(r'''#!/usr/bin/env bash
set -u
args="$*"
if [[ "${1:-}" == "ps" ]]; then
  if [[ "$args" == *"khitmee-erpnext"* ]]; then
    exit 0
  fi
  if [[ "$args" == *"khitmee-perf"* ]]; then
    echo id-backend
  fi
  exit 0
fi
if [[ "${1:-}" == "inspect" ]]; then
  format="${3:-}"
  target="${4:-}"
  if [[ "$format" == *"com.docker.compose.project"* ]]; then
    echo khitmee-perf
    exit 0
  fi
  service=backend
  if [[ "$target" == khitmee-perf-*-1 ]]; then
    service="${target#khitmee-perf-}"
    service="${service%-1}"
  elif [[ "$target" == id-* ]]; then
    service="${target#id-}"
  fi
  case "$format" in
    '{{.Name}} {{.State.Status}} {{.State.OOMKilled}} {{.RestartCount}} {{.State.StartedAt}}')
      echo "/khitmee-perf-${service}-1 running false 0 2026-01-01T00:00:00Z" ;;
    '{{.Id}} {{.State.Status}} {{.State.OOMKilled}} {{.RestartCount}} {{.State.StartedAt}} {{.State.Pid}}')
      echo "id-${service} running false 0 2026-01-01T00:00:00Z 123" ;;
    '{{.Id}}') echo "id-${service}" ;;
    '{{.State.Status}}') echo running ;;
    '{{.State.OOMKilled}}') echo false ;;
    '{{.RestartCount}}') echo 0 ;;
    '{{.State.StartedAt}}') echo 2026-01-01T00:00:00Z ;;
    '{{.State.Pid}}') echo 123 ;;
    *) echo "id-${service} 2026-01-01T00:00:00Z 0" ;;
  esac
  exit 0
fi
if [[ "${1:-}" == "exec" ]]; then
  if [[ "$args" == *"SHOW GLOBAL VARIABLES"* ]]; then
    printf 'aria_pagecache_buffer_size\t134217728\n'
    printf 'innodb_buffer_pool_size\t268435456\n'
    printf 'key_buffer_size\t16777216\n'
  else
    echo '{}'
  fi
  exit 0
fi
exit 0
''', encoding="utf-8", newline="\n")
        python3 = bin_dir / "python3"
        python3.write_text("#!/usr/bin/env bash\nexec \"$REAL_PYTHON\" \"$@\"\n", encoding="utf-8", newline="\n")
        for executable in (docker, python3, root / "tools/khitmee-dev.sh", root / "perf-suite/scripts/up.sh"):
            executable.chmod(0o755)
        return root, bin_dir

    def run_mirror(self, root, bin_dir, args):
        env = clean_perf_eval_env()
        env["REAL_PYTHON"] = sys.executable
        env["PATH"] = bash_path(bin_dir) + ":" + env.get("PATH", "")
        return subprocess.run(
            [BASH_BIN, bash_path(Path(root) / "tools/perf_eval.sh"), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            timeout=15,
        )

    def strict_skip_args(self):
        return [
            "--pass-id", "pass-3", "--profile", "4gb", "--label", "mirror",
            "--skip-switch", "--skip-profile", "--skip-bench", "--require-parity=false",
        ]

    def test_no_overwrite_aborts_before_fast_check_or_artifact_side_effects(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root, bin_dir = self.make_mirror(tempdir)
            results = Path(root) / "perf-suite/results"
            stale = results / "pass-3-4gb-sustained-scorecard.md"
            stale.write_text("stale", encoding="utf-8")
            result = self.run_mirror(root, bin_dir, self.strict_skip_args() + ["--no-overwrite"])
            self.assertEqual(result.returncode, 6)
            self.assertIn("Refusing to overwrite", result.stderr)
            self.assertNotIn("mock-fast-check", result.stdout)
            self.assertFalse((results / "pass-3-4gb-preflight-status.txt").exists())
            self.assertTrue(stale.exists())

    def test_strict_ledger_no_go_forces_nonzero_orchestrator_exit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root, bin_dir = self.make_mirror(tempdir)
            result = self.run_mirror(root, bin_dir, self.strict_skip_args())
            self.assertEqual(result.returncode, 6)
            self.assertIn("strict ledger verdict is NO-GO, not GO", result.stderr)
            self.assertTrue((Path(root) / "perf-suite/results/eval-history.json").exists())
            preflight = (Path(root) / "perf-suite/results/pass-3-4gb-preflight-status.txt").read_text(encoding="utf-8")
            self.assertIn("aria_pagecache_buffer_size=134217728", preflight)
            self.assertIn("key_buffer_size=16777216", preflight)
            self.assertIn("innodb_buffer_pool_size=268435456", preflight)


if __name__ == "__main__":
    unittest.main()
