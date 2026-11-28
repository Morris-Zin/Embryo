"""Unit and negative-control tests for tools/fast_verify.py and tools/perf_bench_inner.py."""

from __future__ import annotations

import cProfile
import copy
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import fast_verify as fv
import perf_bench_inner as pbi


WAREHOUSE_COLUMNS = [
    {"fieldname": "warehouse", "label": "Warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 140},
    {"fieldname": "item_code", "label": "Item Code", "fieldtype": "Data", "options": "", "width": 120},
    {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "options": "", "width": 90},
    {"fieldname": "selling_amount", "label": "Selling Amount", "fieldtype": "Currency", "options": "currency", "width": 120},
    {"fieldname": "buying_amount", "label": "Buying Amount", "fieldtype": "Currency", "options": "currency", "width": 120},
    {"fieldname": "gross_profit", "label": "Gross Profit", "fieldtype": "Currency", "options": "currency", "width": 120},
    {"fieldname": "currency", "label": "Currency", "fieldtype": "Link", "options": "Currency", "width": 80},
]


def _sample_rows():
    return [
        {
            "warehouse": "Shop 1",
            "item_code": "001",
            "qty": 2.0,
            "selling_amount": 100.000,
            "buying_amount": 70.000,
            "gross_profit": 30.000,
            "currency": "USD",
        },
        {
            "warehouse": "Shop 2",
            "item_code": "002",
            "qty": 4.0,
            "selling_amount": 250.000,
            "buying_amount": 150.000,
            "gross_profit": 100.000,
            "currency": "USD",
        },
        {
            "warehouse": "Total",
            "item_code": None,
            "qty": None,
            "selling_amount": 350.000,
            "buying_amount": 220.000,
            "gross_profit": 130.000,
            "currency": "USD",
        },
    ]


def _sample_oracle_key(**overrides):
    key = {
        "db_fingerprint": "db-sha256-111111",
        "erpnext_source_sha256": "erpnext-src-222222",
        "report": "Gross Profit",
        "filters": {
            "company": "Khit Me Distribution",
            "from_date": "2026-10-27",
            "to_date": "2026-11-26",
            "group_by": "Warehouse",
        },
        "user": "Administrator",
        "precision_settings": {
            "currency_precision": 3,
            "float_precision": 2,
            "rounding_method": "Banker's Rounding (legacy)",
        },
    }
    key.update(overrides)
    return key


class TestStockOracleCache(unittest.TestCase):
    def test_put_get_and_verify_all_succeed_for_exact_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            manifest_path = Path(tmp) / "external-manifest.json"
            cache = fv.StockOracleCache(cache_dir, manifest_path=manifest_path)
            key = _sample_oracle_key()
            self.assertIsNone(cache.get(key))

            rows = _sample_rows()
            cache.put(key, WAREHOUSE_COLUMNS, rows, metadata={"source": "stock"})

            # Canonical filter ordering hits the same entry.
            key_reordered = _sample_oracle_key(
                filters={
                    "group_by": "Warehouse",
                    "to_date": "2026-11-26",
                    "from_date": "2026-10-27",
                    "company": "Khit Me Distribution",
                }
            )
            payload = cache.get(
                key_reordered,
                expected_db_fingerprint=key["db_fingerprint"],
                expected_source_sha256=key["erpnext_source_sha256"],
                expected_precision=key["precision_settings"],
            )
            self.assertIsNotNone(payload)
            self.assertEqual(payload["columns"], WAREHOUSE_COLUMNS)
            self.assertEqual(payload["rows"], rows)
            self.assertEqual(cache.verify_all()["verified_entries"], 1)

    def test_fails_closed_on_missing_key_fields(self):
        for field in fv.REQUIRED_KEY_FIELDS:
            with self.subTest(missing=field):
                bad = _sample_oracle_key()
                del bad[field]
                with self.assertRaises(fv.OracleCacheError):
                    fv.compute_oracle_key_hash(bad)

    def test_fails_closed_on_fingerprint_source_and_precision_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = fv.StockOracleCache(tmp)
            key = _sample_oracle_key()
            cache.put(key, WAREHOUSE_COLUMNS, _sample_rows())

            with self.assertRaises(fv.OracleCacheError):
                cache.get(key, expected_db_fingerprint="different-db-fp")
            with self.assertRaises(fv.OracleCacheError):
                cache.get(key, expected_source_sha256="different-source-sha")
            with self.assertRaises(fv.OracleCacheError):
                cache.get(
                    key,
                    expected_precision={
                        "currency_precision": 2,
                        "float_precision": 2,
                        "rounding_method": "Banker's Rounding (legacy)",
                    },
                )

    def test_fails_closed_on_payload_and_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = fv.StockOracleCache(tmp)
            key = _sample_oracle_key()
            entry = cache.put(key, WAREHOUSE_COLUMNS, _sample_rows())

            payload_file = Path(tmp) / entry["payload_path"]
            original_bytes = payload_file.read_bytes()

            # Tamper a single monetary digit in payload bytes.
            tampered = original_bytes.replace(b"100.0", b"100.1", 1)
            payload_file.write_bytes(tampered)
            with self.assertRaises(fv.OracleCacheError):
                cache.get(key)
            with self.assertRaises(fv.OracleCacheError):
                cache.verify_all()

            # Restore payload and tamper manifest root digest.
            payload_file.write_bytes(original_bytes)
            manifest_file = Path(tmp) / fv.MANIFEST_FILENAME
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest["entries_root_sha256"] = "f" * 64
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(fv.OracleCacheError):
                cache.get(key)

            # Tamper payload_path to attempt path traversal.
            manifest["entries"][entry["key_hash"]]["payload_path"] = "../escape.json"
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(fv.OracleCacheError):
                cache.get(key)


class TestExactFinancialComparator(unittest.TestCase):
    def test_exact_match_and_binary_float_repr_noise_at_declared_precision_pass(self):
        oracle = _sample_rows()
        candidate = copy.deepcopy(oracle)
        candidate[0]["selling_amount"] = 99.7 + 0.3  # 100.0
        candidate[0]["gross_profit"] = Decimal("30.000")
        mismatches = fv.compare_financial_reports(
            WAREHOUSE_COLUMNS,
            candidate,
            WAREHOUSE_COLUMNS,
            oracle,
            currency_precision=3,
            float_precision=2,
        )
        self.assertEqual(mismatches, [])

    def test_negative_control_one_unit_monetary_error_fails_at_every_precision(self):
        for precision, delta in ((3, 0.001), (2, 0.01), (0, 1.0)):
            with self.subTest(precision=precision, delta=delta):
                oracle = _sample_rows()
                candidate = copy.deepcopy(oracle)
                candidate[0]["selling_amount"] = oracle[0]["selling_amount"] + delta
                mismatches = fv.compare_financial_reports(
                    WAREHOUSE_COLUMNS,
                    candidate,
                    WAREHOUSE_COLUMNS,
                    oracle,
                    currency_precision=precision,
                )
                self.assertTrue(
                    any(m["kind"] == "monetary_mismatch" and m["field"] == "selling_amount" for m in mismatches),
                    mismatches,
                )

    def test_negative_control_shop_swap_with_identical_company_total_fails(self):
        oracle = _sample_rows()
        candidate = copy.deepcopy(oracle)
        # Swap Shop 1 and Shop 2 amounts while keeping Total row identical.
        candidate[0]["selling_amount"], candidate[1]["selling_amount"] = (
            candidate[1]["selling_amount"],
            candidate[0]["selling_amount"],
        )
        candidate[0]["buying_amount"], candidate[1]["buying_amount"] = (
            candidate[1]["buying_amount"],
            candidate[0]["buying_amount"],
        )
        candidate[0]["gross_profit"], candidate[1]["gross_profit"] = (
            candidate[1]["gross_profit"],
            candidate[0]["gross_profit"],
        )
        self.assertEqual(candidate[-1]["selling_amount"], oracle[-1]["selling_amount"])
        mismatches = fv.compare_financial_reports(
            WAREHOUSE_COLUMNS,
            candidate,
            WAREHOUSE_COLUMNS,
            oracle,
            currency_precision=3,
        )
        self.assertTrue(
            any(m["kind"] == "monetary_mismatch" and m["location"] == "row:0" for m in mismatches),
            mismatches,
        )

    def test_negative_control_duplicate_rows_fail(self):
        oracle = _sample_rows()
        candidate = copy.deepcopy(oracle)
        candidate.insert(0, copy.deepcopy(oracle[0]))
        mismatches = fv.compare_financial_reports(
            WAREHOUSE_COLUMNS,
            candidate,
            WAREHOUSE_COLUMNS,
            oracle,
            identity_fields=("warehouse", "item_code"),
        )
        self.assertTrue(any(m["kind"] == "row_count" for m in mismatches), mismatches)
        self.assertTrue(any(m["kind"] == "duplicate_row" for m in mismatches), mismatches)

    def test_negative_control_string_identifier_corruption_001_vs_1_fails(self):
        for corrupted in ("1", 1, 1.0):
            with self.subTest(corrupted=corrupted):
                oracle = _sample_rows()
                candidate = copy.deepcopy(oracle)
                candidate[0]["item_code"] = corrupted
                mismatches = fv.compare_financial_reports(
                    WAREHOUSE_COLUMNS,
                    candidate,
                    WAREHOUSE_COLUMNS,
                    oracle,
                )
                self.assertTrue(
                    any(
                        m["kind"] in ("identifier_mismatch", "type_mismatch") and m["field"] == "item_code"
                        for m in mismatches
                    ),
                    mismatches,
                )

    def test_negative_control_none_replaced_by_zero_fails(self):
        oracle = _sample_rows()
        candidate = copy.deepcopy(oracle)
        candidate[-1]["qty"] = 0.0
        mismatches = fv.compare_financial_reports(
            WAREHOUSE_COLUMNS,
            candidate,
            WAREHOUSE_COLUMNS,
            oracle,
        )
        self.assertTrue(any(m["kind"] == "null_mismatch" and m["field"] == "qty" for m in mismatches), mismatches)

    def test_label_only_currency_columns_preserve_distinct_fields(self):
        cols = [
            {"label": "Selling Amount", "fieldtype": "Currency"},
            {"label": "Buying Amount", "fieldtype": "Currency"},
        ]
        mismatches = fv.compare_financial_reports(
            cols,
            [[9999.0, 10.0]],
            cols,
            [[20.0, 10.0]],
            currency_precision=3,
        )
        self.assertTrue(
            any(m["kind"] == "monetary_mismatch" and m["field"] == "selling_amount" for m in mismatches),
            mismatches,
        )

    def test_duplicate_detection_uses_precision_aware_numeric_semantics(self):
        cols = [{"fieldname": "amount", "label": "Amount", "fieldtype": "Currency"}]
        mismatches = fv.compare_financial_reports(
            cols,
            [[1.0], [1.0]],
            cols,
            [[1], [1]],
            currency_precision=3,
        )
        self.assertEqual(mismatches, [])

        below_precision = fv.compare_financial_reports(
            cols,
            [[1.0001], [1.0001]],
            cols,
            [[1.0002], [1.0002]],
            currency_precision=3,
        )
        self.assertEqual(below_precision, [])

        one_unit = fv.compare_financial_reports(
            cols,
            [[1.001], [1.001]],
            cols,
            [[1.0], [1.0]],
            currency_precision=3,
        )
        self.assertTrue(any(m["kind"] == "monetary_mismatch" for m in one_unit), one_unit)

    def test_verify_all_accepts_perf_parity_cache_schema(self):
        import perf_parity

        with tempfile.TemporaryDirectory() as tmp:
            pc = perf_parity.ParityOracleCache(tmp)
            key = {
                "db_fingerprint": "fp-1",
                "erpnext_source_sha256": "src-1",
                "report": "Gross Profit",
                "filters": {"company": "Khit Me Distribution"},
                "user": "Administrator",
                "precision_settings": {"currency_precision": 3, "float_precision": 2},
            }
            columns = [{"fieldname": "gross_profit", "fieldtype": "Currency"}]
            pc.put(key, columns, [{"gross_profit": 12.345}])
            v = fv.StockOracleCache(tmp).verify_all()
            self.assertTrue(v["ok"], v)
            self.assertEqual(v["verified_entries"], 1)

            second_key = dict(key, filters={"company": "Khit Me Distribution", "group_by": "Monthly"})
            fast_writer = fv.StockOracleCache(tmp)
            fast_writer.put(second_key, columns, [{"gross_profit": 22.345}])
            manifest = json.loads((Path(tmp) / fv.MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], perf_parity.ORACLE_CACHE_SCHEMA_VERSION)
            reopened = pc.get(second_key)
            self.assertIsNotNone(reopened)
            self.assertEqual(reopened[1], [{"gross_profit": 22.345}])


class TestOfflineRunner(unittest.TestCase):
    def test_offline_runner_executes_selected_groups_with_timings(self):
        repo_root = TOOLS_DIR.parent
        summary = fv.run_offline_suite(repo_root, groups=["parity_comparator", "fast_check_guardrails"])
        self.assertTrue(summary["ok"], summary)
        self.assertGreater(summary["total_tests"], 40)
        self.assertLess(summary["total_elapsed_ms"], 10000.0)
        self.assertEqual([g["group"] for g in summary["groups"]], ["parity_comparator", "fast_check_guardrails"])
        for group in summary["groups"]:
            self.assertTrue(group["ok"])
            self.assertGreaterEqual(group["elapsed_ms"], 0.0)


class TestPerfBenchInnerHelpers(unittest.TestCase):
    def test_safety_guards_refuse_khitmee_erpnext_and_live_sites(self):
        self.assertEqual(pbi.validate_target_project("khitmee-perf"), "khitmee-perf")
        for bad_project in ("khitmee-erpnext", "", None, "other-stack"):
            with self.subTest(project=bad_project):
                with self.assertRaises(pbi.SafetyGuardError):
                    pbi.validate_target_project(bad_project)
                with self.assertRaises(pbi.SafetyGuardError):
                    pbi.build_exec_command(bad_project or "", [])

        self.assertEqual(pbi.validate_target_site("bench-loadtest.localhost"), "bench-loadtest.localhost")
        for bad_site in ("khitmee.localhost", "pos.khitmee.localhost", ""):
            with self.subTest(site=bad_site):
                with self.assertRaises(pbi.SafetyGuardError):
                    pbi.validate_target_site(bad_site)

        self.assertEqual(pbi.validate_db_host("db"), "db")
        with self.assertRaises(pbi.SafetyGuardError):
            pbi.validate_db_host("prod-db.internal")

    def test_payload_builders_produce_exact_line_counts(self):
        pool = [{"item_code": f"KM-{i:04d}", "stock_uom": "Nos"} for i in range(1, 251)]
        for count in (10, 50, 200):
            payload = pbi.build_sale_payload(pool, count, "Shop 1", "Walk-in", "Standard Selling", "Cash")
            self.assertEqual(len(payload["items"]), count)
            self.assertEqual(payload["warehouse"], "Shop 1")
            self.assertTrue(all(row["qty"] >= 1 for row in payload["items"]))
            # Distinct item_codes allow proven_empty_future_sle_fastpath to trigger cleanly.
            item_codes = [row["item_code"] for row in payload["items"]]
            self.assertEqual(len(set(item_codes)), count)

        transfer = pbi.build_transfer_payload(pool, 50, "Dist Hub 1", "Shop 2")
        self.assertEqual(len(transfer["items"]), 50)
        self.assertEqual(transfer["source"], "Dist Hub 1")
        self.assertEqual(transfer["target"], "Shop 2")
        with self.assertRaises(ValueError):
            pbi.build_transfer_payload(pool, 10, "Shop 1", "Shop 1")

    def test_gated_report_response_is_unavailable_not_successful_latency(self):
        columns, rows, reason = pbi.split_query_report_result(
            ([{"fieldname": "gross_profit"}], [], "Detail budget exceeded")
        )
        self.assertEqual(columns, [{"fieldname": "gross_profit"}])
        self.assertEqual(rows, [])
        self.assertEqual(reason, "Detail budget exceeded")

        measurement = pbi.unavailable_report_measurement(reason, 100.0, 100.5)
        self.assertTrue(measurement["unavailable"])
        self.assertIsNone(measurement["wall_ms"])
        self.assertIsNone(measurement["sql_count"])
        self.assertEqual(measurement["reason"], reason)
        self.assertEqual(measurement["rss_delta_mib"], 0.5)

        successful = pbi.split_query_report_result((columns, [{"gross_profit": 1.0}], "ignored when rows exist"))
        self.assertEqual(successful[2], None)

    def test_latency_stats_aggregation_and_empty_none(self):
        empty = pbi.compute_latency_stats([])
        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["p50_ms"])
        self.assertIsNone(empty["p95_ms"])

        stats = pbi.compute_latency_stats([10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertEqual(stats["count"], 5)
        self.assertEqual(stats["min_ms"], 10.0)
        self.assertEqual(stats["p50_ms"], 30.0)
        self.assertEqual(stats["p95_ms"], 48.0)
        self.assertEqual(stats["max_ms"], 50.0)
        self.assertEqual(stats["mean_ms"], 30.0)

    def test_cprofile_hotspots_sql_profiler_and_commit_guard(self):
        prof = cProfile.Profile()
        prof.enable()
        _ = sum(i * i for i in range(500))
        prof.disable()
        cum_hotspots = pbi.extract_cprofile_hotspots(prof, limit=5, sort_by="cumtime")
        self_hotspots = pbi.extract_cprofile_hotspots(prof, limit=5, sort_by="tottime")
        self.assertGreaterEqual(len(cum_hotspots), 1)
        self.assertGreaterEqual(len(self_hotspots), 1)
        self.assertIn("cumtime_ms", cum_hotspots[0])
        self.assertIn("tottime_ms", self_hotspots[0])
        # Verify tottime sorting is non-increasing.
        for prev, nxt in zip(self_hotspots, self_hotspots[1:]):
            self.assertGreaterEqual(prev["tottime_ms"], nxt["tottime_ms"])

        class FakeDB:
            def sql(self, q, *args, **kwargs):
                return [(1,)]

            def commit(self):
                return None

        class FakeFrappe:
            db = FakeDB()

        frappe = FakeFrappe()
        with pbi.SqlProfiler(frappe) as sp:
            frappe.db.sql("SELECT name FROM `tabItem` WHERE name IN ('A', 'B', 'C')")
            frappe.db.sql("SELECT   name  FROM `tabItem` WHERE name IN (%s, %s)")
            frappe.db.sql("SELECT COUNT(*) FROM `tabBin` WHERE actual_qty > 10")
        self.assertEqual(sp.count, 3)
        self.assertGreaterEqual(sp.total_ms, 0.0)
        top_tpls = sp.top_templates(limit=8)
        self.assertEqual(len(top_tpls), 2)
        by_tpl = {row["template"]: row for row in top_tpls}
        self.assertEqual(by_tpl["SELECT name FROM `tabItem` WHERE name IN (?...)"]["count"], 2)
        self.assertEqual(by_tpl["SELECT COUNT(*) FROM `tabBin` WHERE actual_qty > ?"]["count"], 1)

        with pbi.CommitGuard(frappe):
            with self.assertRaises(pbi.SafetyGuardError):
                frappe.db.commit()

    def test_profile_rollback_action_tolerates_inner_db_rollback_without_savepoint(self):
        class RollbackTrackingDB:
            def __init__(self):
                self.queries = []
                self.rollbacks = 0

            def sql(self, query, *args, **kwargs):
                self.queries.append(query)
                if "SAVEPOINT" in str(query).upper():
                    raise AssertionError("SAVEPOINT must not be used in _profile_rollback_action")
                return [(1,)]

            def rollback(self):
                self.rollbacks += 1

        class FakeLocal:
            def __init__(self):
                self.document_cache = {"Sales Invoice": {"SINV-1": {}}}

        class FakeFrappe:
            def __init__(self):
                self.db = RollbackTrackingDB()
                self.local = FakeLocal()

        frappe = FakeFrappe()

        def inner_action():
            # Simulates vouchers._run_idempotent calling frappe.db.rollback() before lock.
            frappe.db.rollback()
            frappe.db.sql("SELECT 1")

        metrics = pbi._profile_rollback_action(frappe, "ignored_savepoint", inner_action, top_n=4)
        self.assertEqual(frappe.db.rollbacks, 2)
        self.assertEqual(metrics["sql_count"], 1)
        self.assertEqual(frappe.local.document_cache, {})
        self.assertIn("top_sql_templates", metrics)
        self.assertIn("cprofile_self_top", metrics)

    def test_normalize_sql_template_collapses_literals_and_truncates(self):
        tpl = pbi.normalize_sql_template("SELECT  * \n FROM `tabSLE` WHERE item_code = 'KM-001' AND qty > 12.5 AND id IN (%s, %s, %s)")
        self.assertEqual(tpl, "SELECT * FROM `tabSLE` WHERE item_code = ? AND qty > ? AND id IN (?...)")
        long_tpl = pbi.normalize_sql_template("SELECT " + ("a, " * 100), max_len=40)
        self.assertEqual(len(long_tpl), 40)
        self.assertTrue(long_tpl.endswith("..."))

    def test_comparison_renderer_and_cli_compare_mode(self):
        baseline = {
            "schema_version": pbi.SCHEMA_VERSION,
            "benchmarks": {
                "sale_10_lines": {"wall_ms": 100.0, "sql_count": 40, "sql_ms": 30.0, "rss_delta_mib": 1.2},
                "search_items_warm": {"p50_ms": 12.0, "p95_ms": 20.0, "avg_sql_count": 4.0, "avg_sql_ms": 2.5, "rss_delta_mib": 0.1},
            },
        }
        candidate = {
            "schema_version": pbi.SCHEMA_VERSION,
            "benchmarks": {
                "sale_10_lines": {"wall_ms": 50.0, "sql_count": 20, "sql_ms": 15.0, "rss_delta_mib": 0.6},
                "search_items_warm": {"p50_ms": 6.0, "p95_ms": 10.0, "avg_sql_count": 2.0, "avg_sql_ms": 1.2, "rss_delta_mib": 0.0},
            },
        }
        md = pbi.render_comparison_markdown(baseline, candidate)
        self.assertIn("| Benchmark | Baseline (ms) | Candidate (ms) | Delta (ms) | Delta (%) |", md)
        self.assertIn("sale_10_lines", md)
        self.assertIn("-50.00%", md)
        self.assertIn("search_items_warm (p50)", md)

        with tempfile.TemporaryDirectory() as tmp:
            bpath = Path(tmp) / "base.json"
            cpath = Path(tmp) / "cand.json"
            bpath.write_text(json.dumps(baseline), encoding="utf-8")
            cpath.write_text(json.dumps(candidate), encoding="utf-8")
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = pbi.main(["--compare", str(bpath), str(cpath)])
            self.assertEqual(rc, 0)
            self.assertIn("sale_10_lines", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
