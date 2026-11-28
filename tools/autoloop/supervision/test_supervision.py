"""Offline acceptance tests for deterministic review supervision."""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

from autoloop.supervision import ActionKind, Disposition, import_run_state, next_action

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "backend_2gb_review_rounds.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def open_finding(fid: str, **overrides):
    finding = {
        "finding_id": fid,
        "severity": "P1",
        "source": "review",
        "status": "open",
        "summary": f"{fid} defect in tools/example.py:12",
        "evidence": f"evidence-{fid}",
    }
    finding.update(overrides)
    return finding


class HistoricalReviewReplayTest(unittest.TestCase):
    def test_replays_two_historical_review_rounds_deterministically(self):
        self.assertEqual(len(FIXTURE["rounds"]), 2)
        for round_fixture in FIXTURE["rounds"]:
            with self.subTest(round=round_fixture["name"]):
                imported = import_run_state(round_fixture["state"])
                action = next_action(imported.findings, imported.history, {}, {})
                repeated = next_action(imported.findings, imported.history, {}, {})
                expected = round_fixture["expected"]

                self.assertEqual(action, repeated)
                self.assertEqual(action.kind.value, expected["kind"])
                self.assertEqual(action.method, expected["method"])
                self.assertEqual(action.reason, expected["reason"])
                self.assertEqual(action.finding_ids, tuple(expected["finding_ids"]))
                self.assertEqual(action.kind, ActionKind.ITERATE)

    def test_importer_preserves_the_structured_finding_fields(self):
        imported = import_run_state(FIXTURE["rounds"][0]["state"])
        by_id = {finding.finding_id: finding for finding in imported.findings}
        finding = by_id["cache-child-ownership-follows-links"]

        self.assertEqual(finding.canonical_theme, "cache-child-ownership-follows-links")
        self.assertEqual(finding.severity, "P1")
        self.assertEqual(finding.evidence_nature, "observation")
        self.assertEqual(finding.implicated_path, "perf_parity.py")
        self.assertEqual(finding.implicated_line, 2853)
        self.assertEqual(finding.implicated_tree, "5d9c81e92907724e1f58f7ac57d22601e0489c8ab827358d7d7465f8a8e477eb")
        self.assertEqual(finding.evidence_id, "Astra deep review of PR #19 @5c04ede")
        self.assertIsNone(finding.protected_invariant)
        self.assertEqual(finding.disposition, Disposition.OPEN.value)
        self.assertIsNone(finding.resolution)

    def test_shuffled_findings_and_history_produce_identical_actions(self):
        round_fixture = FIXTURE["rounds"][1]
        imported = import_run_state(round_fixture["state"])
        baseline = next_action(imported.findings, imported.history, {}, {})
        for seed in range(10):
            rng = random.Random(seed)
            findings = list(imported.findings)
            history = list(imported.history)
            rng.shuffle(findings)
            rng.shuffle(history)
            self.assertEqual(next_action(findings, history, {}, {}), baseline)
            self.assertEqual(next_action(findings, history, {}, {}).to_dict(), baseline.to_dict())


class ReviewRulePrecedenceTest(unittest.TestCase):
    def test_synthetic_oscillation_escalates_when_blocker_set_repeats_without_new_evidence(self):
        finding = open_finding("restart-count-reads-wrong-field", canonical_theme="restart-count-reads-wrong-field")
        history = [
            {"at": "2026-11-28T00:00:00Z", "event": "record-finding", "finding_id": finding["finding_id"], "evidence": "same-review-evidence"},
            {"at": "2026-11-28T00:01:00Z", "event": "resolve-finding", "finding_id": finding["finding_id"]},
            {"at": "2026-11-28T00:02:00Z", "event": "record-finding", "finding_id": finding["finding_id"], "evidence": "same-review-evidence"},
        ]

        action = next_action([finding], history, {}, {})

        self.assertEqual(action.kind, ActionKind.ESCALATE)
        self.assertEqual(action.method, "oscillation")
        self.assertEqual(action.reason, "repeated-blocker-set-without-new-evidence")
        self.assertEqual(action.finding_ids, (finding["finding_id"],))

    def test_same_blocker_with_new_evidence_does_not_escalate_as_oscillation(self):
        finding = open_finding("restart-count-reads-wrong-field")
        history = [
            {"at": "2026-11-28T00:00:00Z", "event": "record-finding", "finding_id": finding["finding_id"], "evidence": "review-round-1"},
            {"at": "2026-11-28T00:01:00Z", "event": "resolve-finding", "finding_id": finding["finding_id"]},
            {"at": "2026-11-28T00:02:00Z", "event": "record-finding", "finding_id": finding["finding_id"], "evidence": "review-round-2-new-tree"},
        ]

        action = next_action([finding], history, {}, {})

        self.assertEqual(action.kind, ActionKind.ITERATE)
        self.assertEqual(action.reason, "fix-induced-blocker")

    def test_false_positive_is_preserved_as_a_disposition_and_does_not_block_pass(self):
        state = {
            "findings": {
                "false-positive-example": open_finding(
                    "false-positive-example",
                    disposition="false_positive",
                    resolution="deterministic replay disproved the review claim",
                    protected_invariant="exact financial results",
                ),
                "resolved-example": open_finding(
                    "resolved-example",
                    status="resolved",
                    resolution="fixed and reviewed on the same tree",
                ),
            },
            "history": [],
        }

        imported = import_run_state(state)
        by_id = {finding.finding_id: finding for finding in imported.findings}
        action = next_action(imported.findings, imported.history, {}, {})

        self.assertIn("false-positive-example", by_id)
        self.assertEqual(by_id["false-positive-example"].disposition, Disposition.FALSE_POSITIVE.value)
        self.assertEqual(by_id["false-positive-example"].resolution, "deterministic replay disproved the review claim")
        self.assertEqual(by_id["false-positive-example"].protected_invariant, "exact financial results")
        self.assertEqual(action.kind, ActionKind.PASS)

    def test_missing_owner_decision_clarifies_before_budget_escalation(self):
        finding = open_finding("owner-decision-needed")
        action = next_action(
            [finding],
            [],
            {"rounds_used": 4, "max_rounds": 4, "repair_tokens_remaining": 0},
            {"required_owner_decisions": ["owner-decision-needed"]},
        )

        self.assertEqual(action.kind, ActionKind.CLARIFY)
        self.assertEqual(action.method, "owner-decision")
        self.assertEqual(action.reason, "missing-owner-decision")
        self.assertEqual(action.finding_ids, ("owner-decision-needed",))

    def test_budget_and_round_caps_escalate_before_iteration(self):
        finding = open_finding("ordinary-blocker")
        cases = (
            ({"rounds_used": 3, "max_rounds": 3}, {}, "round-cap-hit"),
            ({"rounds_used": 3}, {"max_rounds": 3}, "round-cap-hit"),
            ({"repair_tokens_remaining": 0}, {}, "budget-cap-hit"),
        )
        for budget, contract, reason in cases:
            with self.subTest(reason=reason, contract=contract):
                action = next_action([finding], [], budget, contract)
                self.assertEqual(action.kind, ActionKind.ESCALATE)
                self.assertEqual(action.method, "cap")
                self.assertEqual(action.reason, reason)

    def test_fixed_pending_review_may_pass_when_checks_are_valid(self):
        finding = open_finding("fixed-example", status="fixed_pending_review", resolution="fixed commit")
        action = next_action([finding], [], {}, {})
        self.assertEqual(action.kind, ActionKind.PASS)
        self.assertEqual(action.reason, "required-findings-resolved-and-checks-valid")

    def test_invalid_checks_continue_iteration_when_no_blocker_remains(self):
        finding = open_finding("fixed-example", status="fixed_pending_review", resolution="fixed commit")
        action = next_action([finding], [], {}, {"checks_valid": False})
        self.assertEqual(action.kind, ActionKind.ITERATE)
        self.assertEqual(action.method, "restore-checks")
        self.assertEqual(action.reason, "checks-invalid")


class AmbiguityRuleTest(unittest.TestCase):
    def ambiguous_finding(self, **overrides):
        return open_finding(
            "ambiguous-defect",
            ambiguous=True,
            impact="high",
            deterministic_reproduction_available=True,
            reproduction_cost_tokens=0,
            **overrides,
        )

    def test_frozen_threshold_selects_cheap_deterministic_reproduction(self):
        action = next_action([self.ambiguous_finding()], [], {}, {})

        self.assertEqual(action.kind, ActionKind.CLARIFY)
        self.assertEqual(action.method, "reproduce")
        self.assertEqual(action.token_cap, 0)
        self.assertEqual(action.finding_ids, ("ambiguous-defect",))

    def test_still_ambiguous_result_is_one_capped_investigation(self):
        finding = self.ambiguous_finding(reproduction_attempted=True)
        action = next_action(
            [finding],
            [],
            {"investigation_tokens_remaining": 50},
            {"max_investigation_tokens": 20},
        )

        self.assertEqual(action.kind, ActionKind.CLARIFY)
        self.assertEqual(action.method, "investigate")
        self.assertEqual(action.token_cap, 20)

    def test_second_investigation_is_never_selected(self):
        finding = self.ambiguous_finding(reproduction_attempted=True)
        history = [{"at": "2026-11-28T00:00:00Z", "event": "record-investigation", "finding_id": "ambiguous-defect"}]
        action = next_action([finding], history, {"investigation_tokens_remaining": 50}, {})

        self.assertEqual(action.kind, ActionKind.CLARIFY)
        self.assertEqual(action.method, "ask_owner")
        self.assertIsNone(action.token_cap)

    def test_unfunded_still_ambiguous_result_asks_owner(self):
        finding = self.ambiguous_finding(reproduction_attempted=True)
        action = next_action([finding], [], {}, {})

        self.assertEqual(action.kind, ActionKind.CLARIFY)
        self.assertEqual(action.method, "ask_owner")

    def test_already_reproduced_defect_iterates_without_an_extra_call(self):
        finding = self.ambiguous_finding(reproduction_attempted=True, reproduced=True)
        action = next_action([finding], [], {"investigation_tokens_remaining": 50}, {})

        self.assertEqual(action.kind, ActionKind.ITERATE)
        self.assertEqual(action.method, "repair")
        self.assertEqual(action.finding_ids, ("ambiguous-defect",))


if __name__ == "__main__":
    unittest.main()
