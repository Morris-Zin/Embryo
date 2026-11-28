"""Acceptance tests for the red-proof runner."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autoloop.proofs import (
    ProofMode,
    ProofVerdict,
    RedProofSpec,
    proof_digest_matches,
    run_red_proof,
    write_proof_record,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TREE = "a" * 64


def evaluator_spec(fixture: str, diagnostic: str, **overrides) -> RedProofSpec:
    root = FIXTURES / fixture
    values = {
        "finding_id": f"finding-{fixture}",
        "bound_tree_digest": TREE,
        "actual_tree_digest": TREE,
        "mode": ProofMode.EVALUATOR_REPAIR,
        "expected_diagnostic": diagnostic,
        "old_gate": root / "old_gate.py",
        "new_gate": root / "new_gate.py",
        "counterexample": root / "counterexample",
        "control": root / "control",
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return RedProofSpec(**values)


class RedProofAcceptanceTest(unittest.TestCase):
    def test_adr_repair_fixtures_pass(self):
        cases = (
            ("smoke_false_go", "smoke-run-cannot-qualify"),
            ("wrong_restart_field", "unexpected-restart-count"),
            ("softened_comparator", "gross-profit-percent-mismatch"),
            ("removed_assertion", "missing-assertion:total"),
        )
        for fixture, diagnostic in cases:
            with self.subTest(fixture=fixture):
                record = run_red_proof(evaluator_spec(fixture, diagnostic))
                self.assertEqual(record.verdict, ProofVerdict.PASS)
                self.assertEqual(record.reasons, ())
                self.assertTrue(proof_digest_matches(record))
                self.assertEqual(len(record.runs), 4)
                by_label = {run.label: run for run in record.runs}
                self.assertEqual(by_label["old-counterexample"].verdict, "GO")
                self.assertEqual(by_label["new-counterexample"].verdict, "NO-GO")
                self.assertEqual(by_label["new-counterexample"].diagnostic, diagnostic)
                self.assertEqual(by_label["old-control"].verdict, "GO")
                self.assertEqual(by_label["new-control"].verdict, "GO")
                self.assertTrue(all(run.infrastructure_error is None for run in record.runs))

    def test_proof_record_is_typed_content_addressed_provenance(self):
        record = run_red_proof(evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify"))
        payload = record.to_dict()
        self.assertEqual(payload["finding_id"], "finding-smoke_false_go")
        self.assertEqual(payload["bound_tree_digest"], TREE)
        self.assertEqual(payload["actual_tree_digest"], TREE)
        self.assertIn(("bound", TREE), {(item["role"], item["sha256"]) for item in payload["trees"]})
        self.assertIn(("actual", TREE), {(item["role"], item["sha256"]) for item in payload["trees"]})
        self.assertTrue(any(item["role"] == "old-counterexample" for item in payload["trees"]))
        self.assertEqual(payload["proof_digest"], record.proof_digest)
        for run in payload["runs"]:
            self.assertIn("argv", run)
            self.assertIn("exit_code", run)
            self.assertIn("diagnostic", run)
            self.assertRegex(run["stdout_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(run["stderr_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(run["input_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(run["gate_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(run["snapshot_tree_sha256"], r"^[0-9a-f]{64}$")

        changed = replace(record, finding_id="different-finding")
        self.assertFalse(proof_digest_matches(changed))

    def test_reproduction_on_different_tree_cannot_close_finding(self):
        record = run_red_proof(
            evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify", actual_tree_digest="b" * 64)
        )
        self.assertEqual(record.verdict, ProofVerdict.INCONCLUSIVE)
        self.assertIn("stale-tree", record.reasons)

    def test_rejection_for_wrong_reason_is_fail(self):
        root = FIXTURES / "smoke_false_go"
        with tempfile.TemporaryDirectory() as temporary:
            wrong_reason_gate = Path(temporary) / "wrong_reason_gate.py"
            wrong_reason_gate.write_text(
                (root / "new_gate.py").read_text(encoding="utf-8").replace(
                    "smoke-run-cannot-qualify", "some-other-diagnostic"
                ),
                encoding="utf-8",
            )
            record = run_red_proof(
                evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify", new_gate=wrong_reason_gate)
            )
        self.assertEqual(record.verdict, ProofVerdict.FAIL)
        self.assertIn("diagnostic-mismatch", record.reasons)

    def test_good_control_failure_is_fail_not_inconclusive(self):
        root = FIXTURES / "smoke_false_go"
        record = run_red_proof(
            evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify", control=root / "counterexample")
        )
        self.assertEqual(record.verdict, ProofVerdict.FAIL)
        self.assertIn("good-control-rejected", record.reasons)

    def test_absent_defect_is_fail_not_pass(self):
        root = FIXTURES / "smoke_false_go"
        with tempfile.TemporaryDirectory() as temporary:
            not_go_input = Path(temporary) / "input"
            shutil.copytree(root / "counterexample", not_go_input)
            payload_path = not_go_input / "result.json"
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            payload["verdict"] = "NO-GO"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            record = run_red_proof(
                evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify", counterexample=not_go_input)
            )
        self.assertEqual(record.verdict, ProofVerdict.FAIL)
        self.assertIn("counterexample-not-false-go", record.reasons)

    def test_gate_crash_is_infrastructure_inconclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            crashing_gate = Path(temporary) / "crashing_gate.py"
            crashing_gate.write_text("raise SystemExit(1)\n", encoding="utf-8")
            record = run_red_proof(
                evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify", new_gate=crashing_gate)
            )
        self.assertEqual(record.verdict, ProofVerdict.INCONCLUSIVE)
        self.assertTrue(any(reason.startswith("new-counterexample:") for reason in record.reasons))

    def test_gate_timeout_is_infrastructure_inconclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            slow_gate = Path(temporary) / "slow_gate.py"
            slow_gate.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
            record = run_red_proof(
                evaluator_spec(
                    "smoke_false_go",
                    "smoke-run-cannot-qualify",
                    new_gate=slow_gate,
                    timeout_seconds=0.1,
                )
            )
        self.assertEqual(record.verdict, ProofVerdict.INCONCLUSIVE)
        self.assertTrue(any("timeout" in reason for reason in record.reasons))

    def test_missing_gate_protocol_fields_are_inconclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            incomplete_gate = Path(temporary) / "incomplete_gate.py"
            incomplete_gate.write_text("print('{}')\n", encoding="utf-8")
            record = run_red_proof(
                evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify", new_gate=incomplete_gate)
            )
        self.assertEqual(record.verdict, ProofVerdict.INCONCLUSIVE)
        self.assertTrue(any("missing-gate-field" in reason for reason in record.reasons))

    def test_product_fix_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("bad", "good"):
                (root / name).mkdir()
            (root / "bad" / "input.json").write_text(json.dumps({"value": 1}), encoding="utf-8")
            (root / "good" / "input.json").write_text(json.dumps({"value": 2}), encoding="utf-8")
            (root / "old_gate.py").write_text(
                """import json
import sys
from pathlib import Path
payload = json.loads((Path(sys.argv[1]) / 'input.json').read_text())
if payload['value'] == 1:
    print(json.dumps({'verdict': 'NO-GO', 'diagnostic': 'incumbent-defect'}))
    raise SystemExit(2)
print(json.dumps({'verdict': 'GO', 'diagnostic': ''}))
""",
                encoding="utf-8",
            )
            (root / "new_gate.py").write_text(
                """import json
import sys
from pathlib import Path
payload = json.loads((Path(sys.argv[1]) / 'input.json').read_text())
if payload['value'] in {1, 2}:
    print(json.dumps({'verdict': 'GO', 'diagnostic': ''}))
    raise SystemExit(0)
print(json.dumps({'verdict': 'NO-GO', 'diagnostic': 'oracle-mismatch'}))
raise SystemExit(2)
""",
                encoding="utf-8",
            )
            spec = RedProofSpec(
                finding_id="product-finding",
                bound_tree_digest=TREE,
                actual_tree_digest=TREE,
                mode=ProofMode.PRODUCT_FIX,
                expected_diagnostic="incumbent-defect",
                old_gate=root / "old_gate.py",
                new_gate=root / "new_gate.py",
                counterexample=root / "bad",
                control=root / "good",
            )
            record = run_red_proof(spec)
        self.assertEqual(record.verdict, ProofVerdict.PASS)

    def test_write_proof_record_uses_content_address(self):
        record = run_red_proof(evaluator_spec("smoke_false_go", "smoke-run-cannot-qualify"))
        with tempfile.TemporaryDirectory() as temporary:
            path = write_proof_record(record, Path(temporary))
            self.assertEqual(path.name, record.proof_digest + ".json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["proof_digest"], record.proof_digest)


if __name__ == "__main__":
    unittest.main()
