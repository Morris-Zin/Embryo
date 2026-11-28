from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autoloop.profile.t3_state import read_t3_state


class T3StateAdapterTest(unittest.TestCase):
    def test_turn_snapshot_and_tool_activity_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "statev2.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """
                create table projection_projects (project_id text, title text, workspace_root text);
                create table orchestration_v2_projection_threads (thread_id text, project_id text, title text, payload_json text);
                create table orchestration_v2_projection_provider_threads (provider_thread_id text, thread_id text, provider text, provider_session_id text, payload_json text);
                create table orchestration_v2_projection_runs (run_id text, thread_id text, ordinal integer, provider text, payload_json text);
                create table orchestration_v2_projection_run_attempts (attempt_id text, run_id text, attempt_ordinal integer, provider text, payload_json text);
                create table orchestration_v2_projection_provider_turns (provider_turn_id text, thread_id text, provider_thread_id text, run_attempt_id text, ordinal integer, started_at text, completed_at text, payload_json text);
                create table orchestration_v2_projection_turn_items (turn_item_id text, thread_id text, run_id text, provider_thread_id text, type text, updated_at text, payload_json text);
                """
            )
            con.execute("insert into projection_projects values (?,?,?)", ("project-1", "Khitmee-ERPNext", r"E:\Khitmee-ERPNext"))
            thread_payload = {"worktreePath": r"C:\worktrees\Khitmee-ERPNext\t3-test", "modelSelection": {"model": "gpt-test"}}
            con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?)", ("thread-1", "project-1", "shard/profile synthetic", json.dumps(thread_payload)))
            provider_thread_payload = {"nativeThreadRef": {"nativeId": "codex-session-1", "strength": "strong"}}
            con.execute("insert into orchestration_v2_projection_provider_threads values (?,?,?,?,?)", ("provider-thread-1", "thread-1", "codex", "provider-session-1", json.dumps(provider_thread_payload)))
            con.execute("insert into orchestration_v2_projection_runs values (?,?,?,?,?)", ("run-1", "thread-1", 1, "codex", json.dumps({"modelSelection": {"model": "gpt-test"}})))
            con.execute("insert into orchestration_v2_projection_run_attempts values (?,?,?,?,?)", ("attempt-1", "run-1", 2, "codex", "{}"))
            usage = {"tokenUsage": {"inputTokens": 100, "cachedInputTokens": 70, "outputTokens": 30, "reasoningOutputTokens": 5, "usedTokens": 130, "updatedAt": "2026-11-28T01:00:01Z"}}
            con.execute(
                "insert into orchestration_v2_projection_provider_turns values (?,?,?,?,?,?,?,?)",
                ("turn-1", "thread-1", "provider-thread-1", "attempt-1", 4, "2026-11-28T01:00:00Z", "2026-11-28T01:00:02Z", json.dumps(usage)),
            )
            item_payload = {"input": "cat src/a.py", "output": "abcdef", "ordinal": 7}
            con.execute(
                "insert into orchestration_v2_projection_turn_items values (?,?,?,?,?,?,?)",
                ("item-1", "thread-1", "run-1", "provider-thread-1", "command_execution", "2026-11-28T01:00:03Z", json.dumps(item_payload)),
            )
            con.commit()
            con.close()

            scan = read_t3_state(db, "2026-11-28", "Khitmee-ERPNext")

        self.assertEqual(len(scan.records), 1)
        record = scan.records[0]
        self.assertEqual(record.provider, "codex")
        self.assertEqual(record.native_session_id, "codex-session-1")
        self.assertEqual(record.run, "run-1")
        self.assertEqual(record.shard, "profile")
        self.assertEqual(record.retry, 1)
        self.assertEqual(record.turn, 4)
        self.assertEqual(record.uncached_input, 30)
        self.assertEqual(record.cached_input, 70)
        self.assertEqual(record.output, 30)
        self.assertEqual(record.reasoning, 5)
        self.assertIsNone(record.cache_write)
        self.assertEqual(record.measured_tokens(), 130)
        self.assertTrue(record.project_hit)
        self.assertEqual(len(scan.activities), 1)
        self.assertEqual(scan.activities[0].output_bytes, 6)
        self.assertEqual(len(scan.activities[0].read_path_hashes), 1)


if __name__ == "__main__":
    unittest.main()
