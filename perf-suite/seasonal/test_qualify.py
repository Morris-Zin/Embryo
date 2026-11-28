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


def load_fixture_restore_module():
    spec = importlib.util.spec_from_file_location("fixture_restore", ROOT / "fixture_restore.py")
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

    def test_supervisor_contract_uses_adr_guest_topology(self):
        source = (ROOT / "qualify_2gb.sh").read_text(encoding="utf-8")
        self.assertIn('run_bash_script "$PERF_ROOT/scripts/up.sh" --profile 2gb-guest --with-khitmee-ops', source)
        self.assertIn('CGROUP_PARENT="/sys/fs/cgroup/khitmeeperf.slice"', source)
        self.assertNotIn("docker update --cpus", source)
        self.assertNotIn('runtime-${RUNTIME_PROFILE}', source)
        self.assertIn("legacy topology overrides", source)
        block = source[source.index("SEASONAL_COMPOSE_FILES=("):source.index("DEPLOY_COMPOSE_ARGS=()")]
        for token in (
            '"$DOCKER_DIR/compose.base.yaml"',
            '"$DOCKER_DIR/profile-2gb-guest.yaml"',
            '"$DOCKER_DIR/khitmee-ops-2gb.yaml"',
            '"$DOCKER_DIR/khitmee-ops-guest.yaml"',
            '"$SCRIPT_DIR/compose-observability.yaml"',
        ):
            self.assertIn(token, block)
        self.assertNotIn('"$DOCKER_DIR/profile-2gb.yaml"', block)
        self.assertLess(block.index("compose.base.yaml"), block.index("profile-2gb-guest.yaml"))
        self.assertLess(block.index("profile-2gb-guest.yaml"), block.index("khitmee-ops-2gb.yaml"))
        self.assertLess(block.index("khitmee-ops-2gb.yaml"), block.index("khitmee-ops-guest.yaml"))

        deploy = (ROOT / "deploy_candidate.py").read_text(encoding="utf-8")
        self.assertIn('SERVICES = ["backend", "cashier", "queue", "scheduler", "websocket"]', deploy)
        self.assertIn("khitmee-migration-transient.yaml", deploy)
        observability = (ROOT / "compose-observability.yaml").read_text(encoding="utf-8")
        self.assertIn("cashier:", observability)
        self.assertIn("--access-logformat=%({x-k6-request-id}i)s", observability)

        spec = importlib.util.spec_from_file_location("perf_profiles", ROOT.parent / "docker" / "test_profiles.py")
        profiles = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(profiles)
        services = profiles.load_profile("2gb-guest")
        self.assertEqual(set(services), set(profiles.GUEST_SERVICES))
        for service in profiles.GUEST_SERVICES:
            row = services[service]
            self.assertEqual(row.get("cgroup_parent"), "khitmeeperf.slice", service)
            self.assertNotIn("cpus", row, service)
            self.assertEqual(int(row.get("cpu_shares") or 0), profiles.GUEST_CPU_SHARES[service], service)
            self.assertEqual(int(profiles.memory_mib(row.get("mem_limit"))), profiles.GUEST_MEMORY_MIB[service], service)
        self.assertEqual(sum(profiles.GUEST_MEMORY_MIB[name] for name in profiles.GUEST_SERVICES if name != "configurator"), 1728)
        up = (ROOT.parent / "scripts" / "up.sh").read_text(encoding="utf-8")
        self.assertIn('bash "$SCRIPT_DIR/guest_cgroup_setup.sh"', up)
        self.assertLess(up.index("guest_cgroup_setup.sh"), up.index("compose up -d --remove-orphans"))



    def test_fixture_repin_manifest_and_final_result_evidence(self):
        source = (ROOT / "qualify_2gb.sh").read_text(encoding="utf-8")
        restore_source = (ROOT / "fixture_restore.py").read_text(encoding="utf-8")
        new_pin = "0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd"
        self.assertIn('PINNED_FIXTURE_SHA="' + new_pin + '"', source)
        self.assertIn('PINNED_FIXTURE_SHA256 = "' + new_pin + '"', restore_source)
        self.assertIn("--fixture-manifest", source)
        self.assertIn("validate_fixture_manifest", restore_source)
        self.assertLess(restore_source.index("fixture digest mismatch"), restore_source.index("validate_fixture_manifest(manifest_arg, digest)"))
        self.assertLess(restore_source.index("validate_fixture_manifest(manifest_arg, digest)"), restore_source.index('docker("ps"'))
        self.assertLess(restore_source.index("validate_fixture_manifest(manifest_arg, digest)"), restore_source.index("tarfile.open"))
        self.assertNotIn("8cc3e00c5bf5c41d7565bced818d718e21fc18d32b8ea651ec227e4b614a6d19", source + restore_source)
        active_repin_files = [
            ROOT.parent / "lab" / "seasonal-smoke" / "mixed_minute.py",
            ROOT.parent / "lab" / "offload-followup" / "manager" / "candidate_stock_oracle_compare.py",
            ROOT.parent / "lab" / "offload-followup" / "manager" / "stock_oracle_reference.py",
            ROOT.parent / "lab" / "offload-followup" / "manager" / "test_candidate_detail_streaming.py",
            ROOT.parent / "lab" / "offload-followup" / "manager" / "trial-request.example.json",
        ]
        for active in active_repin_files:
            text = active.read_text(encoding="utf-8")
            self.assertIn(new_pin, text, active)
            self.assertNotIn("8cc3e00c5bf5c41d7565bced818d718e21fc18d32b8ea651ec227e4b614a6d19", text, active)

        module = load_fixture_restore_module()
        digest = new_pin
        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / (digest + ".manifest.json")
            manifest.write_text(json.dumps({
                "schema_version": "fixture-build-v1",
                "archive": {"sha256": digest, "format": "cold-named-volume-tar/v1"},
            }))
            validated = module.validate_fixture_manifest(manifest, digest)
            self.assertEqual(validated["archive_format"], "cold-named-volume-tar/v1")
            self.assertEqual(len(validated["sha256"]), 64)
            self.assertEqual(module.companion_manifest_path(Path(td) / (digest + ".tar.gz")), manifest)
            manifest.write_text(json.dumps({"artifacts": {"cold_volume": {"sha256": digest, "format": "cold-named-volume-tar/v1"}}}))
            module.validate_fixture_manifest(manifest, digest)

            bad_digest = json.loads(json.dumps({"archive": {"sha256": digest, "format": "cold-named-volume-tar/v1"}}))
            bad_digest["archive"]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(bad_digest))
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                module.validate_fixture_manifest(manifest, digest)

            bad_format = {"archive": {"sha256": digest, "format": "db-only-tar/v1"}}
            manifest.write_text(json.dumps(bad_format))
            with self.assertRaisesRegex(RuntimeError, "format"):
                module.validate_fixture_manifest(manifest, digest)
            manifest.unlink()
            with self.assertRaisesRegex(RuntimeError, "missing"):
                module.validate_fixture_manifest(manifest, digest)

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
            existing = Path(td) / "existing-destination"
            existing.mkdir()
            sentinel = existing / "sentinel.txt"
            sentinel.write_text("must survive")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                module.materialize(checkout, "", existing)
            self.assertEqual(sentinel.read_text(), "must survive")
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
            (root / "gunicorn.log").write_text("r1|search|GET|api/method/search|q=x|200|100000|<42>" + chr(10))
            nginx = module.parse_nginx_log(root / "nginx.log")
            gunicorn = module.parse_gunicorn_log(root / "gunicorn.log")
            joined, missing = module.join_rows(nginx, gunicorn)
            self.assertEqual(missing, 0)
            self.assertEqual(gunicorn["r1"]["pid"], 42)
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
            (root / "gunicorn.log").write_text("r1|search|GET|api/method/search|q=x|200|100000|not-a-pid" + chr(10))
            self.assertEqual(module.parse_gunicorn_log(root / "gunicorn.log"), {})
            status = module.collection_status(1, 0)
            self.assertFalse(status["collection_passed"])
            self.assertIn("zero Gunicorn application records", status["collection_problems"][0])
            self.assertIn("return 0 if collection_passed else 1", (ROOT / "attribution.py").read_text(encoding="utf-8"))


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
                "workload_memory_limit_total_mib": 1728,
                "services": [{"service": "backend", "id": "abc123", "restart_count": restart, "oom_killed": oom}],
            }
        for name, value in {
            "resource-proof-before.json": proof(),
            "resource-proof-after.json": proof(restart_after, oom_after),
            "foreign-load-start.json": {"foreign_count": 0, "foreign_containers": [], "baseline_containers": []},
            "foreign-load-end.json": {"foreign_count": 0, "foreign_containers": [], "baseline_containers": []},
            "candidate-deployment.json": {"app_commit_sha": "a" * 40, "app_tree_hash": "b" * 40},
            "fixture-restore.json": {
                "passed": True,
                "archive": "/srv/khitmee-lab/operator/fixtures/0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd.tar.gz",
                "fixture_sha256": "0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd",
                "archive_format": "cold-named-volume-tar/v1",
                "fixture_manifest_path": "/srv/khitmee-lab/operator/fixtures/0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd.manifest.json",
                "fixture_manifest_sha256": "c" * 64,
            },
            "candidate-migration.json": {"required": True, "exit_code": 0},
            "guest-attestation-before.json": {"passed": True, "cpuset_cpus_effective": "2-3", "cpu_max": "200000 100000", "memory_max_bytes": 2147483648, "memory_swap_max": "0", "online_cpu_count": 2, "nproc_count": 2, "guest_mem_total_bytes": 2000 * 1024**2, "guest_swap_total_bytes": 0},
            "guest-attestation-after.json": {"passed": True, "cpuset_cpus_effective": "2-3", "cpu_max": "200000 100000", "memory_max_bytes": 2147483648, "memory_swap_max": "0", "online_cpu_count": 2, "nproc_count": 2, "guest_mem_total_bytes": 2000 * 1024**2, "guest_swap_total_bytes": 0},
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
        incomplete = module.resource_window_comparison(
            {"services": [{"service": "db", "id": "one", "oom_killed": False}]},
            {"services": [{"service": "db", "id": "one", "restart_count": 0, "oom_killed": False}]},
        )
        self.assertFalse(incomplete["passed"])
        self.assertIn("restart counter evidence missing", " ".join(incomplete["problems"]))

    def test_full_acceptance_requires_guest_and_oracle_and_can_qualify(self):
        module = self.decision_module()
        with tempfile.TemporaryDirectory() as td:
            self.write_decision_artifact(td)
            result = module.decide(td, 0, "run-1", "acceptance")
            self.assertTrue(result["qualification_evidence"], result["qualification_evidence_problems"])
            self.assertEqual(result["qualified"], "GO")
            self.assertEqual(result["fixture_sha256"], "0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd")
            self.assertEqual(result["fixture_manifest_sha256"], "c" * 64)
            self.assertTrue(result["fixture_restore_passed"])
        with tempfile.TemporaryDirectory() as td:
            self.write_decision_artifact(td, oracle=False)
            result = module.decide(td, 0, "run-1", "acceptance")
            self.assertFalse(result["oracle_evidence_ok"])
            self.assertEqual(result["qualified"], "NO-GO")

    def resource_module(self):
        spec = importlib.util.spec_from_file_location("resource_proof", ROOT / "resource_proof.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fake_guest_inspect(self, module, service, restart_count=0, include_restart=True):
        memory = module.EXPECTED_MEMORY_MIB[service] * 1024**2
        info = {
            "Id": (service.replace("-", "0") + "0" * 64)[:64],
            "Name": "/khitmee-perf-" + service + "-1",
            "Created": "2026-01-01T00:00:00Z",
            "Config": {"Image": "perf", "Labels": {"com.docker.compose.service": service}},
            "HostConfig": {
                "Memory": memory,
                "NanoCpus": 0,
                "CpuShares": module.EXPECTED_CPU_SHARES[service],
                "CgroupParent": module.EXPECTED_CGROUP_PARENT,
            },
            "State": {"Running": service != "configurator", "Pid": 100 if service != "configurator" else 0, "OOMKilled": False},
        }
        if include_restart:
            info["RestartCount"] = restart_count
        return info

    def collect_fake_resource_proof(self, module, restart_count=0, omit_restart=False):
        containers = [
            self.fake_guest_inspect(module, service, restart_count, not (omit_restart and service == "backend"))
            for service in module.SERVICES
        ]
        original_inspect = module.inspect_containers
        original_cgroup = module.cgroup_limits

        def fake_cgroup(info):
            running = bool((info.get("State") or {}).get("Running"))
            return {
                "cgroup_memory_max_bytes": (info.get("HostConfig") or {}).get("Memory") if running else None,
                "cgroup_cpu_limit": None,
                "cgroup_cpu_max_text": "max" if running else "",
                "cgroup_path": "/sys/fs/cgroup/khitmeeperf.slice/docker-test.scope" if running else "",
            }

        module.inspect_containers = lambda: containers
        module.cgroup_limits = fake_cgroup
        try:
            return module.proof()
        finally:
            module.inspect_containers = original_inspect
            module.cgroup_limits = original_cgroup

    def test_restart_counter_is_collected_from_top_level_and_drives_no_go(self):
        resource = self.resource_module()
        before = self.collect_fake_resource_proof(resource, 0)
        after = self.collect_fake_resource_proof(resource, 1)
        self.assertTrue(before["measurement_passed"], before["problems"])
        backend = next(row for row in after["services"] if row["service"] == "backend")
        self.assertEqual(backend["restart_count"], 1)
        missing = self.collect_fake_resource_proof(resource, 0, omit_restart=True)
        self.assertFalse(missing["measurement_passed"])
        self.assertIn("missing top-level Docker RestartCount evidence", " ".join(missing["measurement_problems"]))

        original_inspect = resource.inspect_containers
        original_cgroup = resource.cgroup_limits
        quota_containers = [self.fake_guest_inspect(resource, service, 0) for service in resource.SERVICES]
        backend_info = next(row for row in quota_containers if row["Config"]["Labels"]["com.docker.compose.service"] == "backend")
        backend_info["HostConfig"]["NanoCpus"] = 500000000
        resource.inspect_containers = lambda: quota_containers
        resource.cgroup_limits = lambda info: {"cgroup_memory_max_bytes": (info.get("HostConfig") or {}).get("Memory"), "cgroup_cpu_limit": None, "cgroup_cpu_max_text": "max 100000", "cgroup_path": "scope"}
        try:
            quota = resource.proof()
        finally:
            resource.inspect_containers = original_inspect
            resource.cgroup_limits = original_cgroup
        self.assertFalse(quota["measurement_passed"])
        self.assertIn("backend has a child CPU quota", " ".join(quota["measurement_problems"]))

        module = self.decision_module()
        with tempfile.TemporaryDirectory() as td:
            self.write_decision_artifact(td)
            (Path(td) / "resource-proof-before.json").write_text(json.dumps(before))
            (Path(td) / "resource-proof-after.json").write_text(json.dumps(after))
            result = module.decide(td, 0, "run-1", "acceptance")
            self.assertFalse(result["resource_window_ok"])
            self.assertEqual(result["qualified"], "NO-GO")
            self.assertIn("backend: restart count changed 0 -> 1", result["resource_window_problems"])

    def test_oversized_guest_memory_is_diagnostic_under_one_sided_window(self):
        spec = importlib.util.spec_from_file_location("guest_attestation", ROOT / "guest_attestation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Reproduce the old two-sided acceptance shape: 2200 MiB was inside
        # 2048 MiB +/- 10%, so the former check would have qualified it.
        self.assertLessEqual(2200.0, 2048.0 * 1.10)
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "khitmeeperf.slice"
            parent.mkdir()
            (parent / "cpuset.cpus.effective").write_text("2-3")
            (parent / "cpu.max").write_text("200000 100000")
            (parent / "memory.max").write_text(str(2 * 1024**3))
            (parent / "memory.swap.max").write_text("0")
            members = ([{"service": service, "member": True} for service in module.SERVICES], [])

            def facts(memory_mib):
                return {
                    "online_cpu_count": 2,
                    "nproc_count": 2,
                    "guest_mem_total_bytes": int(memory_mib * 1024**2),
                    "guest_swap_total_bytes": 0,
                }

            def attest(memory_mib):
                return module.attest(
                    str(parent),
                    fact_reader=lambda: facts(memory_mib),
                    membership_reader=lambda project, parent_path: members,
                )

            oversized = attest(2200)
            self.assertFalse(oversized["dedicated_guest_passed"])
            self.assertFalse(oversized["passed"])
            self.assertEqual(oversized["attestation"], "diagnostic")
            self.assertIn("no more than 2048 MiB", " ".join(oversized["guest_problems"]))

            reserved = attest(1980)
            self.assertTrue(reserved["dedicated_guest_passed"], reserved["guest_problems"])
            self.assertTrue(reserved["passed"], reserved["problems"])
            self.assertEqual(reserved["attestation"], "qualifying")

    def test_larger_host_slice_is_diagnostic_and_never_qualified(self):
        spec = importlib.util.spec_from_file_location("guest_attestation", ROOT / "guest_attestation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "khitmeeperf.slice"
            parent.mkdir()
            (parent / "cpuset.cpus.effective").write_text("2-3")
            (parent / "cpu.max").write_text("200000 100000")
            (parent / "memory.max").write_text(str(2 * 1024**3))
            (parent / "memory.swap.max").write_text("0")
            members = ([{"service": service, "member": True} for service in module.SERVICES], [])

            def facts(online, nproc_count, memory_mib, swap_mib):
                return {
                    "online_cpu_count": online,
                    "nproc_count": nproc_count,
                    "guest_mem_total_bytes": int(memory_mib * 1024**2),
                    "guest_swap_total_bytes": int(swap_mib * 1024**2),
                }

            membership_reader = lambda project, parent_path: members
            large = module.attest(
                str(parent),
                fact_reader=lambda: facts(8, 8, 16 * 1024, 4 * 1024),
                membership_reader=membership_reader,
            )
            self.assertTrue(large["parent_slice_passed"])
            self.assertFalse(large["dedicated_guest_passed"])
            self.assertFalse(large["passed"])
            self.assertEqual(large["attestation"], "diagnostic")
            joined = " ".join(large["guest_problems"])
            self.assertIn("8 online CPUs", joined)
            self.assertIn("16384.0 MiB", joined)
            self.assertIn("swap is enabled", joined)

            qualifying = module.attest(
                str(parent),
                fact_reader=lambda: facts(2, 2, 2000, 0),
                membership_reader=membership_reader,
            )
            self.assertTrue(qualifying["passed"], qualifying["problems"])
            self.assertEqual(qualifying["attestation"], "qualifying")

            decision = self.decision_module()
            artifact = Path(td) / "decision-artifact"
            artifact.mkdir()
            self.write_decision_artifact(artifact)
            (artifact / "guest-attestation-before.json").write_text(json.dumps(large))
            (artifact / "guest-attestation-after.json").write_text(json.dumps(large))
            result = decision.decide(artifact, 0, "run-1", "acceptance")
            self.assertFalse(result["guest_attestation_ok"])
            self.assertFalse(result["qualification_evidence"])
            self.assertEqual(result["qualified"], "NO-GO")


if __name__ == "__main__":
    unittest.main()
