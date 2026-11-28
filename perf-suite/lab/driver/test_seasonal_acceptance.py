import json
from pathlib import Path
import tempfile
import unittest

import seasonal_acceptance as acceptance


class SeasonalEvidenceTests(unittest.TestCase):
    def test_full_runner_dependencies_are_installed_together(self):
        import seasonal_driver
        import seasonal_telemetry
        from posting_evidence import capture, capture_before
        self.assertTrue(callable(capture))
        self.assertTrue(callable(capture_before))
        self.assertTrue(callable(seasonal_telemetry.measure))
        self.assertTrue(callable(seasonal_driver.execute))

    def test_error_lines_cannot_supply_events(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "console.jsonl"
            event = {"event": "voucher_complete", "voucher_no": "SYNTHETIC-1"}
            log.write_text(json.dumps({"msg": "other diagnostic"}) + "\n" +
                           json.dumps({"msg": acceptance.PREFIX + json.dumps(event)}) + "\n")
            self.assertEqual(acceptance.read_events(log), [event])

    def test_missing_phases_cannot_pass_as_an_hour(self):
        settings = {"profile": "acceptance", "managers_enabled": True, "export_enabled": True,
                    "phases": [{"name": "baseline", "duration_seconds": 60, "scale": 1}]}
        with self.assertRaisesRegex(ValueError, "duration"):
            acceptance.score([{"event": "config", "config": settings}])

    def test_nearest_rank_p95_keeps_slow_tail(self):
        self.assertEqual(acceptance.p95([10] * 18 + [1000, 2000]), 1000)
        with self.assertRaises(ValueError):
            acceptance.p95([])

    def test_manager_reference_catches_one_changed_result(self):
        req = {"fixture_sha256": "a"*64, "seed": 2}
        events, cases = [], []
        for group in ("Warehouse", "Monthly", "manager_detail", "export_complete"):
            event = "manager_summary" if group in ("Warehouse", "Monthly") else group
            cases.append({"observation_id": group, "event": event, "snapshot": "fixture-cut",
                          "group": group, "sha256": "b"*64})
            events.append({"observation_id": group, "event": event, "group_by": group,
                           "result_sha256": "b"*64, "all_pages_sha256": "b"*64, "sha256": "b"*64})
        reference = {**req, "observations": cases}
        acceptance.compare_manager_outputs(reference, req, events)
        events[2]["all_pages_sha256"] = "c"*64
        with self.assertRaisesRegex(ValueError, "stock report mismatch"):
            acceptance.compare_manager_outputs(reference, req, events)
        events[2]["all_pages_sha256"] = "b"*64
        events[0]["result_sha256"] = "c"*64
        with self.assertRaisesRegex(ValueError, "stock report mismatch"):
            acceptance.compare_manager_outputs(reference, req, events)
