#!/usr/bin/env python3
import hashlib
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import candidate_stock_oracle_compare as c


SNAPSHOT = "8cc3e00c5bf5c41d7565bced818d718e21fc18d32b8ea651ec227e4b614a6d19"
ACTOR = "detail-reader@seasonal.invalid"
FILTERS = {"company": "Khit Me Distribution", "from_date": "2026-01-01", "to_date": "2026-01-02", "group_by": "Invoice"}
COLUMNS = [{"fieldname": "sales_invoice"}, {"fieldname": "amount"}]


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_run(root, stock_rows, verify_rows, page_length=2):
    out = root / "out"
    out.mkdir(parents=True)
    checkpoints = {}
    for implementation, rows in (("stock", stock_rows), ("stock-verify", verify_rows)):
        rows_path = out / f"checkpoint-detail-{implementation}.rows.ndjson.gz"
        evidence = c.write_detail_rows_bytes(
            rows_path, COLUMNS, rows, c.STOCK_REPORT, implementation, ACTOR, FILTERS, SNAPSHOT,
        )
        checkpoint = {
            "schema_version": "oracle-detail-checkpoint-v1",
            "implementation": implementation,
            "report": c.STOCK_REPORT,
            "actor": ACTOR,
            "filters": dict(FILTERS),
            "snapshot_id": SNAPSHOT,
            "columns": list(COLUMNS),
            "row_count": len(rows),
            "rows_sha256": c.canonical_json_streaming_sha256(rows, c.frappe_json_handler()),
            "pages": c.exact_page_slices(rows, page_length),
            "pages_error": None,
            "worker_exitcode": 0,
        }
        checkpoint.update(evidence)
        path = out / f"checkpoint-detail-{implementation}.json"
        path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
        checkpoints[implementation] = checkpoint
    return checkpoints


def make_candidate_checkpoint(out, rows, page_length=2):
    rows_path = out / "checkpoint-detail-candidate.rows.ndjson.gz"
    evidence = c.write_detail_rows_bytes(
        rows_path, COLUMNS, rows, c.CANDIDATE_REPORT, "candidate", ACTOR, FILTERS, SNAPSHOT,
    )
    checkpoint = {
        "schema_version": "oracle-detail-checkpoint-v1",
        "implementation": "candidate",
        "report": c.CANDIDATE_REPORT,
        "actor": ACTOR,
        "filters": dict(FILTERS),
        "snapshot_id": SNAPSHOT,
        "columns": list(COLUMNS),
        "row_count": len(rows),
        "rows_sha256": c.canonical_json_streaming_sha256(rows, c.frappe_json_handler()),
        "pages": c.exact_page_slices(rows, page_length),
        "pages_error": None,
    }
    checkpoint.update(evidence)
    (out / "checkpoint-detail-candidate.json").write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
    return checkpoint


class WorkerIsolationTests(unittest.TestCase):
    def test_stale_success_blocks_new_worker(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(c.subprocess, "run") as run:
            out = Path(td)
            (out / "checkpoint-detail-candidate.json").write_text("{}")
            with self.assertRaises(FileExistsError):
                c.run_report_worker("Fast Gross Profit", {}, ACTOR, 50, "candidate", out, "detail", "detail")
            run.assert_not_called()

    def test_nonzero_exit_cannot_publish_success(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            def failed_worker(*args, **kwargs):
                (out / "checkpoint-detail-candidate.json").write_text('{"schema_version":"oracle-detail-checkpoint-v1"}')
                return c.subprocess.CompletedProcess([], 1)
            with mock.patch.object(c.subprocess, "run", side_effect=failed_worker):
                result = c.run_report_worker("Fast Gross Profit", {}, ACTOR, 50, "candidate", out, "detail", "detail")
            self.assertEqual(result["worker_exitcode"], 1)
            self.assertNotEqual(result["schema_version"], "oracle-detail-checkpoint-v1")


class DetailStreamingTests(unittest.TestCase):
    def run_case(self, stock_rows, verify_rows, mutate=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        checkpoints = make_run(root, stock_rows, verify_rows)
        if mutate:
            mutate(root, checkpoints)
        return root, c.stream_compare_stock_detail_artifacts(
            checkpoints["stock"], checkpoints["stock-verify"], FILTERS, ACTOR, root / "out", 2,
        )

    def test_exact_ordered_stream_passes(self):
        rows = [
            {"sales_invoice": "A", "amount": 1, "nested": [True, None, "x"]},
            {"sales_invoice": "B", "amount": 2.5, "nested": []},
            {"sales_invoice": "C", "amount": 3, "nested": [{"k": "v"}]},
        ]
        _root, result = self.run_case(rows, rows)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["row_count_streamed"], 3)
        self.assertEqual(result["stock_streamed_rows_sha256"], result["verify_streamed_rows_sha256"])

    def test_ordered_row_permutation_fails(self):
        stock = [{"sales_invoice": "A", "amount": 1}, {"sales_invoice": "B", "amount": 2}]
        verify = [stock[1], stock[0]]
        _root, result = self.run_case(stock, verify)
        self.assertFalse(result["passed"])
        self.assertIn("ordered_row_exact_type_or_value_mismatch", {row["kind"] for row in result["mismatches"]})

    def test_int_float_type_difference_fails(self):
        stock = [{"sales_invoice": "A", "amount": 1}]
        verify = [{"sales_invoice": "A", "amount": 1.0}]
        _root, result = self.run_case(stock, verify)
        self.assertFalse(result["passed"])
        row = result["mismatches"][0]
        self.assertEqual(row["kind"], "ordered_row_exact_type_or_value_mismatch")
        self.assertEqual(row["difference"]["left_type"], "int")
        self.assertEqual(row["difference"]["right_type"], "float")

    def test_checkpoint_page_claim_is_validated(self):
        rows = [{"sales_invoice": "A", "amount": 1}, {"sales_invoice": "B", "amount": 2}]
        def mutate(root, checkpoints):
            checkpoints["stock"]["pages"]["page-0"]["rows"][0]["amount"] = 999
        _root, result = self.run_case(rows, rows, mutate)
        self.assertFalse(result["passed"])
        self.assertIn("checkpoint_first_two_pages_mismatch", {row["kind"] for row in result["mismatches"]})

    def test_artifact_digest_is_validated(self):
        rows = [{"sales_invoice": "A", "amount": 1}]
        def mutate(root, checkpoints):
            checkpoints["stock"]["rows_artifact_sha256"] = "0" * 64
        _root, result = self.run_case(rows, rows, mutate)
        self.assertFalse(result["passed"])
        self.assertEqual(result["mismatches"][0]["kind"], "artifact_validation")

    def run_candidate_case(self, stock_rows, candidate_rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        checkpoints = make_run(root, stock_rows, stock_rows)
        candidate = make_candidate_checkpoint(root / "out", candidate_rows)
        return c.stream_compare_candidate_detail_artifacts(
            checkpoints["stock"], candidate, FILTERS, ACTOR, root / "out", 2,
        )

    def test_candidate_exact_ordered_stream_passes(self):
        rows = [{"sales_invoice": "A", "amount": 1}, {"sales_invoice": "B", "amount": 2.5}]
        result = self.run_candidate_case(rows, rows)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["stock_streamed_rows_sha256"], result["candidate_streamed_rows_sha256"])

    def test_candidate_ordered_permutation_fails(self):
        stock = [{"sales_invoice": "A", "amount": 1}, {"sales_invoice": "B", "amount": 2}]
        candidate = [stock[1], stock[0]]
        result = self.run_candidate_case(stock, candidate)
        self.assertFalse(result["passed"])
        self.assertIn("ordered_row_exact_type_or_value_mismatch", {row["kind"] for row in result["mismatches"]})

    def test_candidate_int_float_type_difference_fails(self):
        stock = [{"sales_invoice": "A", "amount": 1}]
        candidate = [{"sales_invoice": "A", "amount": 1.0}]
        result = self.run_candidate_case(stock, candidate)
        self.assertFalse(result["passed"])
        self.assertEqual(result["mismatches"][0]["difference"]["left_type"], "int")
        self.assertEqual(result["mismatches"][0]["difference"]["right_type"], "float")

    def test_offline_recovery_receipt_is_separate_and_passes(self):
        rows = [{"sales_invoice": "A", "amount": 1}, {"sales_invoice": "B", "amount": 2}]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            checkpoints = make_run(root, rows, rows)
            names = [checkpoints["stock"]["rows_artifact"], checkpoints["stock-verify"]["rows_artifact"]]
            (root / "out-sha256sums.txt").write_text("".join(
                f"{file_sha(root / 'out' / name)}  /original/{name}\n" for name in names
            ))
            before = {str(p.relative_to(root)): file_sha(p) for p in (root / "out").iterdir() if p.is_file()}
            receipt = root / "receipt" / "offline.json"
            rc = c.offline_validate_detail_run(root, receipt, 2)
            after = {str(p.relative_to(root)): file_sha(p) for p in (root / "out").iterdir() if p.is_file()}
            self.assertEqual(rc, 0)
            self.assertTrue(receipt.is_file())
            self.assertEqual(before, after)
            body = json.loads(receipt.read_text())
            self.assertEqual(body["schema_version"], "offline-native-stock-detail-recovery-receipt-v1")
            self.assertTrue(body["passed"])
            self.assertFalse(body["original_run_rewritten"])
            self.assertTrue(body["fast_detail_rejection_preserved"])


if __name__ == "__main__":
    unittest.main()
