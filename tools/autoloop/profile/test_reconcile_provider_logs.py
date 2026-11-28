from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoloop.profile.model import ScanResult, UsageRecord
from autoloop.profile.provider_logs import read_t3_provider_logs
from autoloop.profile.reconcile import reconcile_scan


class ReconcileTest(unittest.TestCase):
    def test_detailed_usage_wins_and_incremental_cost_survives_once(self) -> None:
        detailed = UsageRecord(
            provider="claude",
            source="claude_transcript",
            record_id="claude-request:req-1",
            granularity="request",
            native_session_id="session-1",
            uncached_input=10,
            cached_input=20,
            cache_write=5,
            output=8,
            reasoning=3,
            inclusion={"reasoning_in_output": True},
        )
        turn = UsageRecord(
            provider="claude",
            source="t3_provider_log",
            record_id="t3-claude-result:result-1:model",
            granularity="turn",
            native_session_id="session-1",
            uncached_input=100,
            cached_input=200,
            cache_write=50,
            output=80,
            reasoning=30,
            cost=0.5,
            currency="USD",
        )
        snapshot = UsageRecord(
            provider="claude",
            source="t3_state",
            record_id="t3-state:turn-1",
            granularity="turn_snapshot",
            native_session_id="session-1",
            uncached_input=1000,
            cached_input=2000,
            output=800,
        )
        scan = ScanResult(records=[detailed, turn, snapshot])
        reconcile_scan(scan)

        self.assertEqual(len(scan.records), 2)
        by_source = {record.source: record for record in scan.records}
        self.assertEqual(by_source["claude_transcript"].measured_tokens(), 43)
        self.assertIsNone(by_source["t3_provider_log"].measured_tokens())
        self.assertEqual(by_source["t3_provider_log"].cost, 0.5)
        self.assertEqual(by_source["t3_provider_log"].coverage, "cost_only")
        self.assertNotIn("t3_state", by_source)
        total = sum(record.measured_tokens() or 0 for record in scan.records)
        self.assertEqual(total, 43)


class ProviderLogAdapterTest(unittest.TestCase):
    def test_claude_result_fixture_and_acp_blind_spot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_payload = {
                "type": "result",
                "uuid": "result-1",
                "session_id": "claude-session-1",
                "model_usage_since_last_result": {
                    "claude-test": {
                        "inputTokens": 10,
                        "cacheReadInputTokens": 20,
                        "cacheCreationInputTokens": 5,
                        "outputTokens": 8,
                        "thinkingTokens": 3,
                        "costUSD": 0.25,
                    }
                },
            }
            outer = {"provider": "claudeAgent", "event": {"payload": result_payload}}
            acp_outer = {"event": {"provider": "acpRegistry", "kind": "request", "payload": {"method": "session/prompt", "status": "started"}}}
            (root / "events.thread-one.log").write_text(
                "[2026-11-28T01:00:00.000Z] NTIVE: " + json.dumps(outer) + "\n",
                encoding="utf-8",
            )
            (root / "events.thread-two.log").write_text(
                "[2026-11-28T01:00:00.000Z] NTIVE: " + json.dumps(acp_outer) + "\n",
                encoding="utf-8",
            )

            scan = read_t3_provider_logs(root, "2026-11-28")

        self.assertEqual(len(scan.records), 1)
        record = scan.records[0]
        self.assertEqual(record.provider, "claude")
        self.assertEqual(record.measured_tokens(), 43)
        self.assertEqual(record.cost, 0.25)
        self.assertEqual(record.currency, "USD")
        self.assertIn("devin", scan.providers)
        self.assertTrue(any("no usage counters" in notice.message for notice in scan.notices))


if __name__ == "__main__":
    unittest.main()
