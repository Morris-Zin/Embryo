from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoloop.profile.codex import read_codex_rollouts
from autoloop.profile.report import build_sinks


class CodexRolloutAdapterTest(unittest.TestCase):
    def test_response_usage_and_repeated_read_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "sessions" / "2026" / "11" / "28"
            session_dir.mkdir(parents=True)
            session = session_dir / "rollout-2026-11-28T01-00-00-synthetic.jsonl"
            rows = [
                {"type": "session_meta", "timestamp": "2026-11-28T01:00:00Z", "payload": {"id": "codex-session-1", "cwd": r"C:\work\Khitmee-ERPNext"}},
                {"type": "turn_context", "timestamp": "2026-11-28T01:00:01Z", "payload": {"turn_id": "turn-1", "model": "gpt-test"}},
                {"type": "token_usage_record", "timestamp": "2026-11-28T01:00:02Z", "payload": {"response_id": "resp-1", "session_id": "codex-session-1", "turn_id": "turn-1", "usage": {"input_tokens": 110, "cached_input_tokens": 40, "cache_write_input_tokens": 10, "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 130}}},
                {"type": "response_item", "timestamp": "2026-11-28T01:00:03Z", "payload": {"type": "custom_tool_call", "call_id": "call-1", "name": "exec", "input": "cat src/a.py"}},
                {"type": "response_item", "timestamp": "2026-11-28T01:00:04Z", "payload": {"type": "custom_tool_call_output", "call_id": "call-1", "output": "alpha"}},
                {"type": "response_item", "timestamp": "2026-11-28T01:00:05Z", "payload": {"type": "custom_tool_call", "call_id": "call-2", "name": "exec", "input": "cat src/a.py"}},
                {"type": "response_item", "timestamp": "2026-11-28T01:00:06Z", "payload": {"type": "custom_tool_call_output", "call_id": "call-2", "output": "alpha"}},
            ]
            session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            scan = read_codex_rollouts(root, "2026-11-28", "Khitmee-ERPNext")

        self.assertEqual(len(scan.records), 1)
        record = scan.records[0]
        self.assertEqual(record.model, "gpt-test")
        self.assertEqual(record.uncached_input, 60)
        self.assertEqual(record.cached_input, 40)
        self.assertEqual(record.cache_write, 10)
        self.assertEqual(record.output, 20)
        self.assertEqual(record.reasoning, 5)
        self.assertEqual(record.measured_tokens(), 130)
        self.assertTrue(record.inclusion["cached_input_in_input"])
        self.assertTrue(record.inclusion["reasoning_in_output"])
        self.assertIsNone(record.cost)
        self.assertIn("cost", record.unknowns)
        self.assertEqual(len(scan.activities), 2)
        self.assertEqual(scan.activities[0].read_path_hashes, scan.activities[1].read_path_hashes)
        sinks = build_sinks(scan.records, scan.activities)
        repeated = [row for row in sinks if row["kind"] == "repeated_file_read"]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["reads"], 2)
        self.assertEqual(repeated[0]["extra_reads"], 1)
        self.assertNotIn("src/a.py", repr(sinks))


if __name__ == "__main__":
    unittest.main()
