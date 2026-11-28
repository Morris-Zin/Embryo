"""Acceptance tests for the diff-aware test/evaluator integrity guard."""

from __future__ import annotations

import unittest
from pathlib import Path

from autoloop.proofs import (
    AcceptanceCriterion,
    IntegrityVerdict,
    OwnerWaiver,
    TrustedBaseManifest,
    evaluate_integrity,
    trusted_manifest_for_files,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = "2026-11-28T12:00:00Z"


def read_tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def load_fixture_manifest(fixture: str) -> TrustedBaseManifest:
    return TrustedBaseManifest.from_json((FIXTURES / fixture / "base_manifest.json").read_text(encoding="utf-8"))


def evaluate_text_files(
    base: dict[str, str],
    candidate: dict[str, str],
    kinds: dict[str, str] | None = None,
    *,
    criteria=(),
    waivers=(),
):
    protected = kinds or {path: "test" for path in base}
    manifest = trusted_manifest_for_files(base, protected, acceptance_criteria=criteria, waivers=waivers)
    return evaluate_integrity(base, candidate, manifest, now=NOW)


def violation_rules(report) -> set[str]:
    return {violation.rule_id for violation in report.violations}


class IntegrityGuardAcceptanceTest(unittest.TestCase):
    def test_benign_test_rewrite_passes_guard(self):
        root = FIXTURES / "benign_test_rewrite"
        report = evaluate_integrity(read_tree(root / "base"), read_tree(root / "candidate"), load_fixture_manifest("benign_test_rewrite"), now=NOW)
        self.assertEqual(report.verdict, IntegrityVerdict.PASS)
        self.assertEqual(report.violations, ())

    def test_removed_assertion_fixture_is_flagged(self):
        root = FIXTURES / "removed_assertion"
        report = evaluate_integrity(read_tree(root / "base"), read_tree(root / "corrupt"), load_fixture_manifest("removed_assertion"), now=NOW)
        self.assertEqual(report.verdict, IntegrityVerdict.FAIL)
        self.assertIn("deleted-assertion", violation_rules(report))
        self.assertIn("omitted-case", violation_rules(report))
        self.assertTrue(any(v.acceptance_criterion == "total-assertion" for v in report.violations))

    def test_softened_comparator_fixture_is_flagged(self):
        root = FIXTURES / "softened_comparator"
        report = evaluate_integrity(read_tree(root / "base"), read_tree(root / "corrupt"), load_fixture_manifest("softened_comparator"), now=NOW)
        self.assertEqual(report.verdict, IntegrityVerdict.FAIL)
        self.assertIn("softened-comparator", violation_rules(report))
        self.assertTrue(any(v.acceptance_criterion == "exact-gross-profit-percent" for v in report.violations))

    def test_added_python_skip_marker_is_flagged(self):
        base = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        candidate = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    @unittest.skip('hidden')\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        report = evaluate_text_files(base, candidate)
        self.assertIn("added-skip-only-ignore", violation_rules(report))

    def test_added_js_only_marker_is_flagged(self):
        base = {"tests/example.k6.js": "import { test } from 'k6';\ntest('case', () => { assert(true); });\n"}
        candidate = {"tests/example.k6.js": "import { test } from 'k6';\ntest.only('case', () => { assert(true); });\n"}
        report = evaluate_text_files(base, candidate)
        self.assertIn("added-skip-only-ignore", violation_rules(report))

    def test_omitted_case_is_flagged(self):
        base = {
            "tests/test_cases.py": "import unittest\n\nclass Cases(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n    def test_two(self):\n        self.assertEqual(2, 2)\n"
        }
        candidate = {
            "tests/test_cases.py": "import unittest\n\nclass Cases(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n"
        }
        report = evaluate_text_files(base, candidate)
        self.assertIn("omitted-case", violation_rules(report))

    def test_weakened_numeric_threshold_is_flagged(self):
        base = {
            "tests/test_latency.py": "import unittest\n\nclass Latency(unittest.TestCase):\n    def test_tail(self):\n        elapsed = 1.0\n        self.assertTrue(elapsed < 2.0)\n"
        }
        candidate = {
            "tests/test_latency.py": "import unittest\n\nclass Latency(unittest.TestCase):\n    def test_tail(self):\n        elapsed = 1.0\n        self.assertTrue(elapsed < 3.0)\n"
        }
        report = evaluate_text_files(
            base,
            candidate,
            criteria=(AcceptanceCriterion("latency-bound", "tests/test_latency.py", "elapsed"),),
        )
        self.assertIn("weakened-threshold", violation_rules(report))
        self.assertTrue(
            any(
                violation.rule_id == "weakened-threshold" and violation.acceptance_criterion == "latency-bound"
                for violation in report.violations
            )
        )

    def test_named_threshold_and_increased_float_tolerance_are_flagged(self):
        base = {
            "tests/test_bounds.py": "import math\nimport unittest\n\nMAX_LATENCY = 2.0\n\nclass Bounds(unittest.TestCase):\n    def test_tail(self):\n        elapsed = 1.0\n        self.assertTrue(elapsed < MAX_LATENCY)\n        self.assertTrue(math.isclose(elapsed, 1.0, abs_tol=0.1))\n"
        }
        candidate = {
            "tests/test_bounds.py": "import math\nimport unittest\n\nMAX_LATENCY = 3.0\n\nclass Bounds(unittest.TestCase):\n    def test_tail(self):\n        elapsed = 1.0\n        self.assertTrue(elapsed < MAX_LATENCY)\n        self.assertTrue(math.isclose(elapsed, 1.0, abs_tol=0.5))\n"
        }
        report = evaluate_text_files(base, candidate)
        self.assertIn("weakened-threshold", violation_rules(report))
        self.assertGreaterEqual(sum(v.rule_id == "weakened-threshold" for v in report.violations), 2)

    def test_changed_oracle_and_fixture_files_are_flagged(self):
        base = {
            "tests/test_evaluator.py": "import unittest\n\nclass Evaluator(unittest.TestCase):\n    def test_case(self):\n        self.assertTrue(True)\n",
            "oracle/expected.json": '{"answer": 10}\n',
            "fixtures/input.json": '{"value": 10}\n',
        }
        candidate = dict(base)
        candidate["oracle/expected.json"] = '{"answer": 11}\n'
        candidate["fixtures/input.json"] = '{"value": 11}\n'
        report = evaluate_text_files(
            base,
            candidate,
            {
                "tests/test_evaluator.py": "test",
                "oracle/expected.json": "oracle",
                "fixtures/input.json": "fixture",
            },
        )
        self.assertIn("changed-oracle-file", violation_rules(report))
        self.assertIn("changed-fixture-file", violation_rules(report))

    def test_altered_command_or_exit_handling_is_flagged(self):
        base = {
            "evaluator/runner.py": "import subprocess\n\n\ndef run():\n    result = subprocess.run(['python', 'gate.py'], check=True)\n    return result.returncode == 0\n"
        }
        candidate = {
            "evaluator/runner.py": "import subprocess\n\n\ndef run():\n    result = subprocess.run(['python', 'other_gate.py'], check=True)\n    return result.returncode == 0\n"
        }
        report = evaluate_text_files(base, candidate, {"evaluator/runner.py": "evaluator"})
        self.assertIn("altered-command-or-exit-handling", violation_rules(report))

    def test_altered_exit_code_handling_is_flagged(self):
        base = {
            "evaluator/runner.py": "import sys\n\n\ndef finish(ok):\n    if ok:\n        sys.exit(0)\n    sys.exit(2)\n"
        }
        candidate = {
            "evaluator/runner.py": "import sys\n\n\ndef finish(ok):\n    if ok:\n        sys.exit(0)\n    sys.exit(0)\n"
        }
        report = evaluate_text_files(base, candidate, {"evaluator/runner.py": "evaluator"})
        self.assertIn("altered-command-or-exit-handling", violation_rules(report))

    def test_oracle_change_can_use_exact_expiring_owner_waiver(self):
        base = {
            "tests/test_evaluator.py": "import unittest\n\nclass Evaluator(unittest.TestCase):\n    def test_case(self):\n        self.assertTrue(True)\n",
            "oracle/expected.json": '{"answer": 10}\n',
        }
        candidate = dict(base)
        candidate["oracle/expected.json"] = '{"answer": 11}\n'
        kinds = {"tests/test_evaluator.py": "test", "oracle/expected.json": "oracle"}
        initial = evaluate_text_files(base, candidate, kinds)
        self.assertEqual(initial.verdict, IntegrityVerdict.FAIL)
        waiver = OwnerWaiver(
            initial.diff_hash,
            "changed-oracle-file",
            "owner",
            "oracle correction with independent replacement evidence",
            "evidence-sha256:def456",
            "2026-12-01T00:00:00Z",
        )
        waived = evaluate_text_files(base, candidate, kinds, waivers=(waiver,))
        self.assertEqual(waived.verdict, IntegrityVerdict.PASS)
        self.assertEqual(len(waived.waived_violations), 1)

    def test_expiring_owner_waiver_exact_diff_rule_and_evidence(self):
        base = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        candidate = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    @unittest.skip('platform incident')\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        initial = evaluate_text_files(base, candidate)
        self.assertEqual(initial.verdict, IntegrityVerdict.FAIL)
        waiver = OwnerWaiver(
            diff_hash=initial.diff_hash,
            rule_id="added-skip-only-ignore",
            owner="owner",
            rationale="temporary platform outage with live replacement evidence",
            replacement_evidence="evidence-sha256:abc123",
            expires_at="2026-12-01T00:00:00Z",
        )
        waived = evaluate_text_files(base, candidate, waivers=(waiver,))
        self.assertEqual(waived.verdict, IntegrityVerdict.PASS)
        self.assertEqual(len(waived.waived_violations), 1)
        self.assertEqual(waived.applied_waivers, (waiver,))

    def test_waiver_with_wrong_diff_hash_or_rule_does_not_apply(self):
        base = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        candidate = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    @unittest.skip('hidden')\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        initial = evaluate_text_files(base, candidate)
        wrong_hash = OwnerWaiver(
            "0" * 64,
            "added-skip-only-ignore",
            "owner",
            "wrong diff",
            "evidence",
            "2026-12-01T00:00:00Z",
        )
        wrong_rule = OwnerWaiver(
            initial.diff_hash,
            "deleted-assertion",
            "owner",
            "wrong rule",
            "evidence",
            "2026-12-01T00:00:00Z",
        )
        report = evaluate_text_files(base, candidate, waivers=(wrong_hash, wrong_rule))
        self.assertEqual(report.verdict, IntegrityVerdict.FAIL)
        self.assertIn("added-skip-only-ignore", violation_rules(report))

    def test_expired_or_incomplete_waiver_does_not_apply(self):
        base = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        candidate = {
            "tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    @unittest.skip('hidden')\n    def test_case(self):\n        self.assertTrue(True)\n"
        }
        initial = evaluate_text_files(base, candidate)
        expired = OwnerWaiver(
            initial.diff_hash,
            "added-skip-only-ignore",
            "owner",
            "expired exception",
            "evidence",
            "2026-11-01T00:00:00Z",
        )
        expired_report = evaluate_text_files(base, candidate, waivers=(expired,))
        self.assertEqual(expired_report.verdict, IntegrityVerdict.FAIL)
        self.assertIn("waiver-expired", {item.reason for item in expired_report.rejected_waivers})

        incomplete = OwnerWaiver(initial.diff_hash, "added-skip-only-ignore", "owner", "rationale", "", "2026-12-01T00:00:00Z")
        incomplete_report = evaluate_text_files(base, candidate, waivers=(incomplete,))
        self.assertEqual(incomplete_report.verdict, IntegrityVerdict.FAIL)
        self.assertIn(
            "waiver-missing-owner-rationale-or-replacement-evidence",
            {item.reason for item in incomplete_report.rejected_waivers},
        )

    def test_waiver_cannot_remove_acceptance_criterion(self):
        root = FIXTURES / "removed_assertion"
        base = read_tree(root / "base")
        corrupt = read_tree(root / "corrupt")
        manifest = load_fixture_manifest("removed_assertion")
        initial = evaluate_integrity(base, corrupt, manifest, now=NOW)
        waiver = OwnerWaiver(
            initial.diff_hash,
            "deleted-assertion",
            "owner",
            "attempted acceptance removal",
            "replacement evidence",
            "2026-12-01T00:00:00Z",
        )
        waived_manifest = trusted_manifest_for_files(
            base,
            {"tests/test_totals.py": "test"},
            acceptance_criteria=(AcceptanceCriterion("total-assertion", "tests/test_totals.py", "total"),),
            waivers=(waiver,),
        )
        report = evaluate_integrity(base, corrupt, waived_manifest, now=NOW)
        self.assertEqual(report.verdict, IntegrityVerdict.FAIL)
        self.assertIn("deleted-assertion", violation_rules(report))
        self.assertIn("acceptance-criterion-not-waivable", {item.reason for item in report.rejected_waivers})

    def test_untrusted_base_digest_fails_closed(self):
        base = {"tests/test_example.py": "import unittest\n\nclass Example(unittest.TestCase):\n    def test_case(self):\n        self.assertTrue(True)\n"}
        manifest = trusted_manifest_for_files(base, {"tests/test_example.py": "test"})
        changed_base = {"tests/test_example.py": base["tests/test_example.py"] + "\n# untrusted edit\n"}
        report = evaluate_integrity(changed_base, changed_base, manifest, now=NOW)
        self.assertEqual(report.verdict, IntegrityVerdict.FAIL)
        self.assertIn("untrusted-base", violation_rules(report))

        waiver = OwnerWaiver(
            report.diff_hash,
            "untrusted-base",
            "owner",
            "attempted trust override",
            "evidence",
            "2026-12-01T00:00:00Z",
        )
        waived_manifest = trusted_manifest_for_files(base, {"tests/test_example.py": "test"}, waivers=(waiver,))
        waived_report = evaluate_integrity(changed_base, changed_base, waived_manifest, now=NOW)
        self.assertEqual(waived_report.verdict, IntegrityVerdict.FAIL)
        self.assertIn("untrusted-base", violation_rules(waived_report))
        self.assertIn("trust-oracle-rule-not-waivable", {item.reason for item in waived_report.rejected_waivers})


if __name__ == "__main__":
    unittest.main()
