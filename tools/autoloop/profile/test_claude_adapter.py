from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoloop.profile.claude import read_claude_transcripts


def assistant_row(request_id: str, with_tool: bool) -> dict:
    content = []
    if with_tool:
        content.append({"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "src/a.py"}})
    return {
        "type": "assistant",
        "timestamp": "2026-11-28T01:00:02Z",
        "sessionId": "claude-session-1",
        "requestId": request_id,
        "cwd": r"E:\Khitmee-ERPNext",
        "message": {
            "id": "msg-1",
            "model": "claude-test",
            "content": content,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "output_tokens": 8,
                "output_tokens_details": {"thinking_tokens": 3},
            },
        },
    }


class ClaudeTranscriptAdapterTest(unittest.TestCase):
    def test_streamed_usage_deduped_and_tool_output_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "claude-session-1.jsonl"
            rows = [
                {"type": "user", "timestamp": "2026-11-28T01:00:00Z", "sessionId": "claude-session-1", "message": {"role": "user", "content": "synthetic user prompt"}},
                assistant_row("req-1", True),
                assistant_row("req-1", False),
                {"type": "user", "timestamp": "2026-11-28T01:00:03Z", "sessionId": "claude-session-1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "abcdef"}]}},
                {"type": "cost-state", "totalCostUSD": 1.25, "modelUsage": {}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            scan = read_claude_transcripts(root, "2026-11-28", "Khitmee-ERPNext")

        self.assertEqual(len(scan.records), 1)
        record = scan.records[0]
        self.assertEqual(record.model, "claude-test")
        self.assertEqual(record.turn, 1)
        self.assertEqual(record.uncached_input, 10)
        self.assertEqual(record.cached_input, 20)
        self.assertEqual(record.cache_write, 5)
        self.assertEqual(record.output, 8)
        self.assertEqual(record.reasoning, 3)
        self.assertEqual(record.measured_tokens(), 43)
        self.assertFalse(record.inclusion["cached_input_in_input"])
        self.assertTrue(record.inclusion["reasoning_in_output"])
        self.assertIsNone(record.cost)
        self.assertEqual(record.unknowns["cost"], "cost_state_is_session_aggregate_not_turn_attributable")
        self.assertEqual(len(scan.activities), 1)
        activity = scan.activities[0]
        self.assertEqual(activity.tool_name, "Read")
        self.assertEqual(activity.output_bytes, 6)
        self.assertEqual(len(activity.read_path_hashes), 1)
        self.assertTrue(any("duplicate streamed usage rows collapsed" in notice.message for notice in scan.notices))

    def test_old_tool_results_contribute_no_windowed_activities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "claude-old.jsonl"
            rows = [
                {"type": "user", "timestamp": "2025-01-01T01:00:00Z", "sessionId": "claude-old", "message": {"role": "user", "content": "synthetic user prompt"}},
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T01:00:01Z",
                    "sessionId": "claude-old",
                    "message": {"id": "msg-old", "content": [{"type": "tool_use", "id": "old-tool", "name": "Read", "input": {"file_path": "src/old.py"}}]},
                },
                {"type": "user", "timestamp": "2025-01-01T01:00:02Z", "sessionId": "claude-old", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old-tool", "content": "old"}]}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            scan = read_claude_transcripts(root, "2026-11-28", "Khitmee-ERPNext")

        self.assertEqual(len(scan.records), 0)
        self.assertEqual(len(scan.activities), 0)


if __name__ == "__main__":
    unittest.main()
