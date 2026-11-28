import importlib.util
import json
import unittest
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_foreign_load_module():
    spec = importlib.util.spec_from_file_location("foreign_load", ROOT / "foreign_load.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QualifyEntryPointTests(unittest.TestCase):
    def test_windows_lease_interop_and_lifecycle_are_fail_closed(self):
        source = (ROOT / "qualify_2gb.sh").read_text(encoding="utf-8")
        self.assertIn("python.exe", source)
        self.assertIn("wslpath", source)
        self.assertIn('LEASE_DOMAIN="windows-interop"', source)
        self.assertIn('lease_cli acquire --resource khitmee-perf', source)
        self.assertIn('lease_cli renew --resource khitmee-perf', source)
        self.assertIn('lease_cli renew --resource host-timing', source)
        self.assertIn('lease_cli release --resource khitmee-perf', source)
        self.assertIn('lease_cli release --resource host-timing', source)
        self.assertLess(source.index('acquire --resource khitmee-perf --owner'), source.index('acquire --resource host-timing --owner'))
        self.assertIn('lease-renew.failed', source)
        decision = (ROOT / "qualification_decision.py").read_text(encoding="utf-8")
        self.assertIn('lease_renewal_failed', decision)
        self.assertIn('trap cleanup EXIT INT TERM', source)
        self.assertIn('cannot use Windows lease interop', source)

    def test_foreign_load_is_recorded_and_marks_timing_invalid(self):
        source = (ROOT / "qualify_2gb.sh").read_text(encoding="utf-8")
        self.assertIn("foreign-load-start.json", source)
        self.assertIn("foreign-load-end.json", source)
        decision = (ROOT / "qualification_decision.py").read_text(encoding="utf-8")
        self.assertIn("foreign_load_start_count", decision)
        self.assertIn("foreign_load_end_count", decision)
        self.assertIn("'INVALID' if not timing_valid", decision)
        recorder = (ROOT / "foreign_load.py").read_text(encoding="utf-8")
        self.assertIn("other_running_containers", recorder)
        self.assertIn("foreign_containers", recorder)
        self.assertIn('BASELINE_PROJECTS = {"khitmee-erpnext"}', recorder)

    def test_foreign_duplicate_container_guard_and_temporary_crlf_fallback(self):
        source = (ROOT / "qualify_2gb.sh").read_text(encoding="utf-8")
        self.assertIn("foreign or duplicate khitmee-perf containers exist", source)
        self.assertIn("will not repair another actor's stack", source)
        self.assertIn("TEMPORARY CRLF fallback", source)
        self.assertIn("stats-collector.sh.lf-tmp", source)
        self.assertNotIn("\r", source)

    def test_foreign_load_classifies_live_baseline_separately_from_foreign_load(self):
        module = load_foreign_load_module()
        containers = [
            {"Id": "a"*64, "Name": "/khitmee-perf-backend-1", "Config": {"Image": "perf", "Labels": {"com.docker.compose.project": "khitmee-perf"}}, "State": {"Status": "running"}},
            {"Id": "b"*64, "Name": "/khitmee-erpnext-backend-1", "Config": {"Image": "live", "Labels": {"com.docker.compose.project": "khitmee-erpnext"}}, "State": {"Status": "running"}},
            {"Id": "c"*64, "Name": "/report-oracle-proof-backend-1", "Config": {"Image": "other", "Labels": {"com.docker.compose.project": "report-oracle-proof"}}, "State": {"Status": "running"}},
        ]
        responses = {"ps": "aaa" + chr(10) + "bbb" + chr(10) + "ccc" + chr(10), "inspect": json.dumps(containers)}
        original = module.docker
        def fake_docker(args):
            return responses["inspect"] if args[0] == "inspect" else responses["ps"]
        module.docker = fake_docker
        try:
            result = module.record()
        finally:
            module.docker = original
        self.assertEqual(result["other_running_count"], 2)
        self.assertEqual(result["baseline_count"], 1)
        self.assertEqual(result["foreign_count"], 1)
        self.assertEqual(result["foreign_containers"][0]["name"], "report-oracle-proof-backend-1")

    def test_runtime_profiles_encode_the_two_comparison_shapes(self):
        baseline = (ROOT / "profiles/runtime-gunicorn-1x12.yaml").read_text(encoding="utf-8")
        alternate = (ROOT / "profiles/runtime-gunicorn-2x1.yaml").read_text(encoding="utf-8")
        self.assertIn("cpus: 0.75", baseline)
        self.assertIn("--threads=1", alternate)
        self.assertIn("--workers=2", alternate)
        self.assertIn("mem_limit: 512m", alternate)
        self.assertIn("mem_limit: 544m", alternate)


    def test_candidate_tree_preflight_migration_and_attribution_are_wired(self):
        source = (ROOT / "qualify_2gb.sh").read_text(encoding="utf-8")
        decision = (ROOT / "qualification_decision.py").read_text(encoding="utf-8")
        combined = source + decision
        for token in (
            "--app-tree PATH", "--app-ref REF", "--preflight",
            "app_tree.py", "deploy_candidate.py", "candidate-migration.json",
            "candidate_role_grants", "app_commit_sha", "app_tree_hash",
            "compose-observability.yaml", "seasonal_preflight", "request_attribution_summary",
            "qualification_decision.py", "guest_attestation.py", "--oracle-command",
        ):
            self.assertIn(token, combined)
        self.assertLess(source.index("app_tree.py"), source.index("lease_cli acquire --resource khitmee-perf"))
        deploy = (ROOT / "deploy_candidate.py").read_text(encoding="utf-8")
        self.assertIn('"bench", "--site", args.site, "migrate"', deploy)
        self.assertIn('user.add_roles("Accounts User", "Accounts Manager")', deploy)
        self.assertIn("candidate-deployment-failed.json", deploy)
        self.assertIn("dirs_exist_ok=True", deploy)
        self.assertIn("--force-recreate", deploy)
        self.assertIn("candidate frontend attribution deployment", deploy)

    def test_app_tree_materializes_commit_and_fails_closed_when_dirty(self):
        spec = importlib.util.spec_from_file_location("app_tree", ROOT / "app_tree.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td) / "checkout"
            app = checkout / "apps" / "khitmee_ops"
            app.mkdir(parents=True)
            (app / "marker.txt").write_text("candidate" + chr(10))
            subprocess.run(["git", "init"], cwd=checkout, check=True, capture_output=True)
            subprocess.run(["git", "add", "apps/khitmee_ops/marker.txt"], cwd=checkout, check=True, capture_output=True)
            subprocess.run([
                "git", "-c", "user.name=offline-test", "-c", "user.email=offline@test.invalid",
                "commit", "-m", "candidate",
            ], cwd=checkout, check=True, capture_output=True)
            materialized = Path(td) / "materialized"
            clean = module.materialize(checkout, "", materialized)
            self.assertEqual((materialized / "apps" / "khitmee_ops" / "marker.txt").read_text(), "candidate" + chr(10))
            self.assertEqual(len(clean["app_commit_sha"]), 40)
            self.assertEqual(len(clean["app_tree_hash"]), 40)
            (app / "dirty.txt").write_text("dirty")
            with self.assertRaisesRegex(RuntimeError, "dirty"):
                module.materialize(checkout, "", Path(td) / "dirty-materialized")

    def test_attribution_joins_nginx_and_gunicorn_and_estimates_queue(self):
        spec = importlib.util.spec_from_file_location("attribution", ROOT / "attribution.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "nginx.log").write_text(
                '{"request_id":"r1","endpoint_class":"search","method":"GET","uri":"/api/method/search","status":"200",'
                '"request_time":"0.200","upstream_connect_time":"0.001","upstream_header_time":"0.150","upstream_response_time":"0.180"}' + chr(10)
            )
            (root / "gunicorn.log").write_text("r1|search|GET|api/method/search|q=x|200|100000|42" + chr(10))
            nginx = module.parse_nginx_log(root / "nginx.log")
            gunicorn = module.parse_gunicorn_log(root / "gunicorn.log")
            joined, missing = module.join_rows(nginx, gunicorn)
            self.assertEqual(missing, 0)
            self.assertEqual(joined[0]["gunicorn_service_ms"], 100.0)
            self.assertEqual(joined[0]["estimated_dispatch_to_entry_ms"], 80.0)
            summary = module.endpoint_summary(joined)
            self.assertEqual(summary["search"]["count"], 1)
            self.assertEqual(summary["search"]["estimated_dispatch_to_entry_ms"]["p95"], 80.0)
            stats = module.delta_commandstats(
                chr(10).join(["# Commandstats", "cmdstat_get:calls=10,usec=1000,usec_per_call=100", ""]),
                chr(10).join(["# Commandstats", "cmdstat_get:calls=14,usec=3000,usec_per_call=750", ""]),
            )
            self.assertEqual(stats[0]["calls"], 4)
            self.assertEqual(stats[0]["total_ms"], 2.0)
            locks = module.delta_locks(
                chr(10).join(["Innodb_row_lock_waits" + chr(9) + "7", "Innodb_row_lock_time" + chr(9) + "700", ""]),
                chr(10).join(["Innodb_row_lock_waits" + chr(9) + "10", "Innodb_row_lock_time" + chr(9) + "1600", ""]),
            )
            self.assertEqual(locks["row_lock_waits_delta"], 3)
            self.assertEqual(locks["row_lock_time_ms_delta"], 900)


    def test_preflight_evaluator_requires_every_step_status_and_denial(self):
        spec = importlib.util.spec_from_file_location("evaluate_preflight", ROOT / "evaluate_preflight.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "k6.log"
            results = {step: True for step in module.REQUIRED_STEPS}
            events = [
                {"event": "preflight_unauthorized_detail_denied"},
                {"event": "export_status"},
                {"event": "preflight_result", "passed": True, "results": results, "missing_steps": [], "failed_steps": []},
            ]
            path.write_text("".join("noise KHITMEE_SEASONAL_EVENT " + json.dumps(event) + chr(10) for event in events))
            evaluation = module.evaluate_log(path)
            self.assertTrue(evaluation["passed"], evaluation)
            results["sale"] = False
            path.write_text("".join("noise KHITMEE_SEASONAL_EVENT " + json.dumps(event) + chr(10) for event in events))
            evaluation = module.evaluate_log(path)
            self.assertFalse(evaluation["passed"])
            self.assertIn("required step did not succeed: sale", evaluation["failures"])


    def write_decision_artifact(self, root, *, profile="acceptance", restart_after=0, oom_after=False, oracle=True):
        artifact = Path(root)
        def proof(restart=0, oom=False):
            return {
                "measurement_passed": True,
                "within_budget": True,
                "budget_problems": [],
                "workload_memory_limit_total_mib": 1568,
                "services": [{"service": "backend", "id": "abc123", "restart_count": restart, "oom_killed": oom}],
            }
        for name, value in {
            "resource-proof-before.json": proof(),
            "resource-proof-after.json": proof(restart_after, oom_after),
            "foreign-load-start.json": {"foreign_count": 0, "foreign_containers": [], "baseline_containers": []},
            "foreign-load-end.json": {"foreign_count": 0, "foreign_containers": [], "baseline_containers": []},
            "candidate-deployment.json": {"app_commit_sha": "a" * 40, "app_tree_hash": "b" * 40},
            "candidate-migration.json": {"required": True, "exit_code": 0},
            "guest-attestation-before.json": {"passed": True, "cpuset_cpus_effective": "2-3", "cpu_max": "200000 100000", "memory_max_bytes": 2147483648, "memory_swap_max": "0"},
            "guest-attestation-after.json": {"passed": True, "cpuset_cpus_effective": "2-3", "cpu_max": "200000 100000", "memory_max_bytes": 2147483648, "memory_swap_max": "0"},
            "seasonal-verification.json": {"by_type": {}},
        }.items():
            (artifact / name).write_text(json.dumps(value))
        (artifact / "resource-proof-after.exit").write_text("0" + chr(10))
        (artifact / "oracle-command.exit").write_text("0" + chr(10))
        if oracle:
            checks = {key: True for key in ("numbering", "voucher_rows", "stock_ledger", "gl_entries", "bins", "reports", "retries", "csv", "perf_parity_require_db")}
            (artifact / "oracle-evidence.json").write_text(json.dumps({
                "schema_version": "seasonal-stock-oracle-v1", "run_id": "run-1", "passed": True, "checks": checks,
            }))
        config = {"profile": profile, "managers_enabled": True, "phases": [{"name": "baseline", "phase": "baseline", "duration_seconds": 3600}]}
        (artifact / "k6.log").write_text("noise KHITMEE_SEASONAL_EVENT " + json.dumps({"event": "config", "config": config}) + chr(10))

    def decision_module(self):
        spec = importlib.util.spec_from_file_location("qualification_decision", ROOT / "qualification_decision.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_smoke_workload_pass_can_never_qualify_as_2gb(self):
        module = self.decision_module()
        with tempfile.TemporaryDirectory() as td:
            self.write_decision_artifact(td, profile="smoke")
            result = module.decide(td, 0, "run-1", "smoke")
            self.assertTrue(result["workload_passed"])
            self.assertFalse(result["qualification_evidence"])
            self.assertEqual(result["qualified"], "NO-GO")
            self.assertEqual(result["verdict"], "NO-GO")
            self.assertIn("full-hour acceptance", " ".join(result["qualification_evidence_problems"]))

    def test_postrun_resource_identity_restart_and_oom_delta_invalidates_qualification(self):
        module = self.decision_module()
        with tempfile.TemporaryDirectory() as td:
            self.write_decision_artifact(td, restart_after=1)
            result = module.decide(td, 0, "run-1", "acceptance")
            self.assertFalse(result["resource_window_ok"])
            self.assertFalse(result["qualification_evidence"])
            self.assertEqual(result["qualified"], "NO-GO")
            self.assertIn("restart count changed", " ".join(result["resource_window_problems"]))
        comparison = module.resource_window_comparison(
            {"services": [{"service": "db", "id": "one", "restart_count": 0, "oom_killed": False}]},
            {"services": [{"service": "db", "id": "two", "restart_count": 0, "oom_killed": True}]},
        )
        self.assertFalse(comparison["passed"])
        self.assertIn("container identity changed", " ".join(comparison["problems"]))
        self.assertIn("OOM state changed", " ".join(comparison["problems"]))

    def test_full_acceptance_requires_guest_and_oracle_and_can_qualify(self):
        module = self.decision_module()
        with tempfile.TemporaryDirectory() as td:
            self.write_decision_artifact(td)
            result = module.decide(td, 0, "run-1", "acceptance")
            self.assertTrue(result["qualification_evidence"], result["qualification_evidence_problems"])
            self.assertEqual(result["qualified"], "GO")
        with tempfile.TemporaryDirectory() as td:
            self.write_decision_artifact(td, oracle=False)
            result = module.decide(td, 0, "run-1", "acceptance")
            self.assertFalse(result["oracle_evidence_ok"])
            self.assertEqual(result["qualified"], "NO-GO")


if __name__ == "__main__":
    unittest.main()
