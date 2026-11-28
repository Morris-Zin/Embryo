import importlib.util
import json
import unittest
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
        self.assertIn('lease_renewal_failed', source)
        self.assertIn('trap cleanup EXIT INT TERM', source)
        self.assertIn('cannot use Windows lease interop', source)

    def test_foreign_load_is_recorded_and_marks_timing_invalid(self):
        source = (ROOT / "qualify_2gb.sh").read_text(encoding="utf-8")
        self.assertIn("foreign-load-start.json", source)
        self.assertIn("foreign-load-end.json", source)
        self.assertIn("foreign_load_start_count", source)
        self.assertIn("foreign_load_end_count", source)
        self.assertIn("verdict = 'INVALID'", source)
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


if __name__ == "__main__":
    unittest.main()
