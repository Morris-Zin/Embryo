#!/usr/bin/env python3
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import candidate_stock_oracle_compare as compare
import stock_oracle_reference as ref

SNAPSHOT = ref.PINNED_FIXTURE_SHA256
COMPANY = ref.PINNED_COMPANY
SITE = ref.PINNED_SITE
FROM_DATE = "2026-01-01"
TO_DATE = "2026-01-02"
ACTORS = {
    "warehouse": "summary-warehouse@seasonal.invalid",
    "monthly": "summary-monthly@seasonal.invalid",
    "detail": "detail-reader@seasonal.invalid",
}
COLUMNS = [{"fieldname": "sales_invoice"}, {"fieldname": "amount"}]
ROWS = [
    {"sales_invoice": "A", "amount": 1, "nested": [True, None]},
    {"sales_invoice": "B", "amount": 2.5, "nested": {"x": "late"}},
    {"sales_invoice": "C", "amount": 3, "nested": []},
]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_summary_checkpoint(out, case_id, implementation, actor, filters):
    rows = [{"group": filters["group_by"], "amount": 1}, {"group": "Total", "amount": 1}]
    checkpoint = {
        "schema_version": "oracle-report-checkpoint-v1",
        "case_id": case_id,
        "implementation": implementation,
        "report": "Gross Profit",
        "actor": actor,
        "filters": filters,
        "row_count": len(rows),
        "columns": COLUMNS,
        "columns_sha256": ref.sorted_json_sha256(COLUMNS),
        "rows": rows,
        "rows_sha256": ref.sorted_json_sha256(rows),
    }
    write_json(out / f"checkpoint-{case_id}-{implementation}.json", checkpoint)


def write_detail_checkpoint(out, implementation, filters, rows=ROWS):
    rows_path = out / f"checkpoint-detail-{implementation}.rows.ndjson.gz"
    evidence = compare.write_detail_rows_bytes(rows_path, COLUMNS, rows, "Gross Profit", implementation, ACTORS["detail"], filters, SNAPSHOT)
    checkpoint = {
        "schema_version": "oracle-detail-checkpoint-v1",
        "case_id": "detail",
        "implementation": implementation,
        "report": "Gross Profit",
        "actor": ACTORS["detail"],
        "filters": filters,
        "snapshot_id": SNAPSHOT,
        "columns": COLUMNS,
        "columns_sha256": ref.sorted_json_sha256(COLUMNS),
        "row_count": len(rows),
        "rows_sha256": compare.canonical_json_streaming_sha256(rows, compare.frappe_json_handler()),
        "pages": compare.exact_page_slices(rows, 2),
        "pages_error": None,
    }
    checkpoint.update(evidence)
    write_json(out / f"checkpoint-detail-{implementation}.json", checkpoint)


def preflight():
    actors = {}
    roles = {"warehouse": "summary_warehouse", "monthly": "summary_monthly", "detail": "detail_reader"}
    for key, actor in ACTORS.items():
        actors[actor] = {"exists": True, "enabled": True, "password_verified": True, "roles": ["Khitmee Seasonal Report Reader"]}
    return {
        "schema_version": "candidate-clone-preflight-v1",
        "passed": True,
        "site": SITE,
        "actors": actors,
        "repost_triggers_installed": True,
        "missing_repost_triggers": [],
        "report_source": dict(ref.PINNED_REPORT_SOURCE),
        "precision_settings": dict(ref.PINNED_PRECISION_SETTINGS),
        "financial_baseline": {
            "company": COMPANY,
            "submitted_sales_invoices": 2,
            "sales_invoice_min_posting_date": FROM_DATE,
            "sales_invoice_max_posting_date": TO_DATE,
            "stock_ledger_entries": 3,
            "stock_ledger_min_posting_date": FROM_DATE,
            "stock_ledger_max_posting_date": TO_DATE,
        },
    }


def make_reference_root(root):
    run = root / "run"
    scope = run / "full-year"
    out = scope / "out"
    out.mkdir(parents=True)
    wh_filters = {"company": COMPANY, "from_date": FROM_DATE, "to_date": TO_DATE, "group_by": "Warehouse"}
    mo_filters = {**wh_filters, "group_by": "Monthly"}
    detail_filters = {**wh_filters, "group_by": "Invoice"}
    for group, filters in (("warehouse", wh_filters), ("monthly", mo_filters)):
        case = f"summary-{group}"
        write_summary_checkpoint(out, case, "stock", ACTORS[group], filters)
        write_summary_checkpoint(out, case, "stock-verify", ACTORS[group], filters)
        write_json(out / f"verification-summary-{group}.json", {
            "schema_version": "stock-oracle-summary-verification-v1", "passed": True,
            "stock_complete": True, "verification_complete": True, "mismatches": [],
        })
    write_detail_checkpoint(out, "stock", detail_filters)
    write_detail_checkpoint(out, "stock-verify", detail_filters)
    write_json(out / "clone-preflight.json", preflight())
    write_json(out / "candidate-stock-verdict.json", {"passed": True})
    lines = []
    for path in sorted(out.iterdir()):
        lines.append(f"{sha(path)}  /guest/out/{path.name}\n")
    (scope / "out-sha256sums.txt").write_text("".join(lines), encoding="utf-8")
    write_json(run / "sequence-request.json", {
        "schema_version": "year-oracle-sequence-request-v1", "run_id": "test-run",
        "fixture_sha256": SNAPSHOT, "seed_manifest_sha256": ref.PINNED_SEED_MANIFEST_SHA256,
        "site": SITE, "company": COMPANY,
        "scopes": [{"scope": "full-year", "from_date": FROM_DATE, "to_date": TO_DATE}],
    })
    write_json(run / "sequence-status.json", {"state": "passed-full-year-only"})
    write_json(run / "copyback-verification.json", {
        "schema_version": "year-oracle-copyback-verification-v1", "passed": True,
        "original_outputs_rewritten": False, "failures": [],
    })
    fixture = root / "fixture.json"
    write_json(fixture, {
        "fixture_tar_gz_sha256": SNAPSHOT, "seed_manifest_sha256": ref.PINNED_SEED_MANIFEST_SHA256,
        "site": SITE, "image_ids": {ref.PINNED_ERPNEXT_IMAGE: ref.PINNED_ERPNEXT_IMAGE_ID_PREFIX},
        "verified_counts": {"tabSales Invoice": 2, "tabStock Ledger Entry": 3},
        "report_window": {"from_date": FROM_DATE, "final_voucher_date": TO_DATE},
        "restore_receipt_sha256": "1" * 64,
        "actors": {
            "summary_warehouse": {"roles": ["Khitmee Seasonal Report Reader"]},
            "summary_monthly": {"roles": ["Khitmee Seasonal Report Reader"]},
            "detail_reader": {"roles": ["Khitmee Seasonal Report Reader"]},
        },
    })
    input_manifest = root / "input.json"
    write_json(input_manifest, {
        "approved_erpnext_image_id_prefix": ref.PINNED_ERPNEXT_IMAGE_ID_PREFIX,
        "fixture_sha256": SNAPSHOT, "seed_manifest_sha256": ref.PINNED_SEED_MANIFEST_SHA256,
        "candidate_commit": "test", "candidate_archive_sha256": "2" * 64,
    })
    cache = root / "cache"
    manifest = root / "reference.json"
    built = ref.build_reference_manifest(run, "full-year", fixture, input_manifest, cache, manifest, deep_row_limit=1000)
    return manifest, cache, built


def runtime_kwargs():
    return {
        "site": SITE, "company": COMPANY,
        "summary_from_date": FROM_DATE, "summary_to_date": TO_DATE,
        "detail_from_date": FROM_DATE, "detail_to_date": TO_DATE,
        "warehouse_actor": ACTORS["warehouse"], "monthly_actor": ACTORS["monthly"], "detail_actor": ACTORS["detail"],
        "snapshot_id": SNAPSHOT, "preflight": preflight(),
    }


class StockOracleReferenceTests(unittest.TestCase):
    def make_reference(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manifest, cache, built = make_reference_root(Path(tmp.name))
        return manifest, cache, built

    def test_warm_reference_build_validate_runtime_and_staging_pass(self):
        manifest, cache, built = self.make_reference()
        loaded = ref.validate_reference_manifest(manifest, cache)
        validation = ref.validate_runtime_match(loaded, **runtime_kwargs())
        self.assertTrue(validation["passed"], validation)
        with tempfile.TemporaryDirectory() as td:
            staged = compare.stage_reference_checkpoint(loaded, "summary-warehouse", "stock", Path(td))
            self.assertEqual(staged["implementation"], "stock")
            self.assertTrue((Path(td) / "checkpoint-summary-warehouse-stock.json").is_file())
        self.assertEqual(loaded.usage_metadata()["fresh_stock_oracle_executions"], 0)
        self.assertEqual(built["validation"]["detail_validation_mode"], "deep-stream-revalidation")

    def test_manifest_inside_cache_fails_closed(self):
        manifest, cache, _built = self.make_reference()
        inside = cache / "reference-copy.json"
        inside.write_bytes(manifest.read_bytes())
        with self.assertRaises(ref.ReferenceValidationError):
            ref.validate_reference_manifest(inside, cache)

    def test_late_artifact_corruption_invalidates_reference(self):
        manifest, cache, _built = self.make_reference()
        victim = cache / "out" / "checkpoint-detail-stock.rows.ndjson.gz"
        data = bytearray(victim.read_bytes())
        data[-5] ^= 1
        victim.write_bytes(data)
        with self.assertRaises(ref.ReferenceValidationError):
            ref.validate_reference_manifest(manifest, cache)

    def test_runtime_provenance_mismatch_rejects_reference(self):
        manifest, cache, _built = self.make_reference()
        loaded = ref.validate_reference_manifest(manifest, cache)
        kwargs = runtime_kwargs()
        kwargs["snapshot_id"] = "0" * 64
        validation = ref.validate_runtime_match(loaded, **kwargs)
        self.assertFalse(validation["passed"])
        self.assertIn("snapshot_mismatch", {row["kind"] for row in validation["mismatches"]})

    def test_runtime_precision_or_source_mismatch_rejects_reference(self):
        manifest, cache, _built = self.make_reference()
        loaded = ref.validate_reference_manifest(manifest, cache)
        kwargs = runtime_kwargs()
        kwargs["preflight"]["precision_settings"]["currency_precision_effective"] = 2
        validation = ref.validate_runtime_match(loaded, **kwargs)
        self.assertFalse(validation["passed"])
        self.assertIn("precision_settings_mismatch", {row["kind"] for row in validation["mismatches"]})
        kwargs = runtime_kwargs()
        kwargs["preflight"]["report_source"]["source_sha256"] = "0" * 64
        validation = ref.validate_runtime_match(loaded, **kwargs)
        self.assertFalse(validation["passed"])
        self.assertIn("report_source_mismatch", {row["kind"] for row in validation["mismatches"]})


if __name__ == "__main__":
    unittest.main()
