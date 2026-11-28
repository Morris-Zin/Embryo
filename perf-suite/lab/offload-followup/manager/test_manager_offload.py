#!/usr/bin/env python3
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manager_offload as mo


ACTORS = {
    "summary_warehouse": {"user": "warehouse@example.test", "expected_permissions": ["report"]},
    "summary_monthly": {"user": "monthly@example.test", "expected_permissions": ["report"]},
    "detail_reader": {"user": "detail@example.test", "expected_permissions": ["report"]},
    "export_requester": {"user": "export@example.test", "expected_permissions": ["report", "export"]},
}


class FakeOracleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    query_count = 0
    export_count = 0
    report_names = []
    lock = threading.Lock()

    def log_message(self, fmt, *args):
        return

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def send(self, status, body, content_type="application/json", extra_headers=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.read_body()
        path = self.path.split("?", 1)[0]
        if path == "/api/method/login":
            fields = parse_qs(body.decode())
            user = fields.get("usr", [""])[0]
            if fields.get("pwd", [""])[0] != "pw":
                self.send(401, json.dumps({"error": "bad login"}))
                return
            self.send(200, json.dumps({"message": "Logged In", "full_name": user}), extra_headers=(("Set-Cookie", f"sid=sid-{user}; Path=/"),))
            return
        if path == "/api/method/" + mo.QUERY_METHOD:
            with self.lock:
                type(self).query_count += 1
            time.sleep(0.05)
            fields = parse_qs(body.decode())
            with self.lock:
                type(self).report_names.append(fields.get("report_name", [""])[0])
            filters = json.loads(fields.get("filters", ["{}"])[0])
            start = int(fields.get("start", ["0"])[0] or 0)
            page_length = int(fields.get("page_length", ["50"])[0] or 50)
            group = filters.get("group_by")
            if group == "Invoice":
                if "start" in fields or "page_length" in fields:
                    rows = [{"name": f"INV-{start + index + 1}", "posting_date": "2026-01-01", "qty": index + 1} for index in range(min(2, page_length))]
                else:
                    rows = [{"name": f"INV-{index + 1}", "posting_date": "2026-01-01", "qty": index + 1} for index in range(120)]
                columns = [{"fieldname": "name"}, {"fieldname": "posting_date"}, {"fieldname": "qty"}]
            else:
                rows = [{"group": group, "qty": 2, "amount": "10.500"}]
                columns = [{"fieldname": "group"}, {"fieldname": "qty"}, {"fieldname": "amount"}]
            self.send(200, json.dumps({"message": {"columns": columns, "result": rows}}))
            return
        if path.endswith("enqueue_profit_csv_export"):
            self.send(200, json.dumps({"message": {"export_log_name": "EXP-1", "status": "Completed"}}))
            return
        self.send(404, json.dumps({"error": "missing"}))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.rstrip("/") == "/app":
            self.send(200, "<html><script>frappe.csrf_token = 'unit-csrf';</script></html>", "text/html")
            return
        if path.endswith("download_profit_csv_export"):
            with self.lock:
                type(self).export_count += 1
            self.send(200, "Name,Qty\nINV-1,2\n", "text/csv", extra_headers=(("Content-Disposition", "attachment; filename=profit.csv"),))
            return
        self.send(404, json.dumps({"error": "missing"}))


class ManagerOffloadTests(unittest.TestCase):
    def setUp(self):
        FakeOracleHandler.query_count = 0
        FakeOracleHandler.export_count = 0
        FakeOracleHandler.report_names = []
        self.oracle = ThreadingHTTPServer(("127.0.0.1", 0), FakeOracleHandler)
        self.oracle_thread = threading.Thread(target=self.oracle.serve_forever, daemon=True)
        self.oracle_thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.request = self.make_request(str(Path(self.tmp.name) / "artifacts"))
        self.credentials = {
            "summary_warehouse": {"user": ACTORS["summary_warehouse"]["user"], "password": "pw"},
            "summary_monthly": {"user": ACTORS["summary_monthly"]["user"], "password": "pw"},
            "detail_reader": {"user": ACTORS["detail_reader"]["user"], "password": "pw"},
            "export_requester": {"user": ACTORS["export_requester"]["user"], "password": "pw"},
        }

    def tearDown(self):
        self.oracle.shutdown()
        self.oracle.server_close()
        self.oracle_thread.join(timeout=2)

    def make_request(self, artifact_dir):
        oracle_url = f"http://127.0.0.1:{self.oracle.server_address[1]}"
        return {
            "schema_version": mo.SCHEMA_VERSION,
            "trial_id": "unit-test",
            "fixture_sha256": "a" * 64,
            "seed": 7,
            "site": "unit.localhost",
            "artifact_dir": artifact_dir,
            "company": "Unit Company",
            "report_windows": {
                "year_to_date": {"scope": "year_to_date", "from_date": "2026-01-01", "to_date": "2026-11-26"},
                "full_year": {"scope": "full_year", "from_date": "2025-11-27", "to_date": "2026-11-26"},
            },
            "source": {"base_url": oracle_url, "credential_env": "MANAGER_TEST_CREDS", "actors": ACTORS},
            "oracle": {
                "base_url": oracle_url,
                "credential_env": "MANAGER_TEST_CREDS",
                "erpnext_unmodified": True,
                "erpnext_version": "test",
                "snapshot_kind": "static-fixture",
                "snapshot_cutoff_date": "2026-11-26",
                "source_fixture_sha256": "a" * 64,
            },
            "manager_workload": {
                "summary_report": "Fast Gross Profit",
                "detail_report": "Fast Gross Profit",
                "detail_page_count": 2,
                "detail_page_length": 50,
                "export_enqueue_method": "unit.enqueue_profit_csv_export",
                "export_download_method": "unit.download_profit_csv_export",
            },
            "integration": {"mode": "split-manager-url", "listen_host": "127.0.0.1", "listen_port": 18186, "owner_accepted_proxy_overhead": False},
            "freshness": {"required_write_cutoff": "pre-trial-static", "cashier_write_cursor": None, "max_staleness_seconds": 0},
            "export_timeout_seconds": 2,
        }

    def test_canonical_hash_matches_seasonal_contract(self):
        value = {"a": 1, "b": [2, {"a": "x"}]}
        self.assertEqual(mo.canonical_json_sha256(value), "2c77e8e42231c5a2b199e37b8c1225614c241a3b2e7623a7ebd6f17815a4a738")

    def test_request_validation_rejects_dynamic_write_requirement(self):
        req = self.make_request(str(Path(self.tmp.name) / "other"))
        req["freshness"]["required_write_cutoff"] = "include-cashier-writes"
        mo.validate_trial_request(req)
        freshness = mo.assess_freshness(req)
        self.assertEqual(freshness["decision"], "parity-reject")
        self.assertIn("cashier", freshness["reason"])

    def test_capture_verify_and_exact_cache_replay(self):
        req = mo.validate_trial_request(self.request)
        with mock.patch.dict(os.environ, {"MANAGER_TEST_CREDS": json.dumps(self.credentials)}):
            capture = mo.capture_oracle(req)
        self.assertTrue(all(capture["coverage"].values()), capture["coverage"])
        self.assertEqual(len(capture["observations"]), 6)

        state = mo.OffloadServerState(req)
        server = ThreadingHTTPServer(("127.0.0.1", 0), mo.make_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        try:
            # Every preseeded oracle response replays exactly without another oracle query.
            before = FakeOracleHandler.query_count
            for actor, group in (("summary_warehouse", "Warehouse"), ("summary_monthly", "Monthly")):
                user = ACTORS[actor]["user"]
                login, sid = mo.frappe_login(base, user, "pw")
                self.assertEqual(login.status, 200)
                filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": group}
                response = mo.frappe_post_method(base, mo.QUERY_METHOD, {"report_name": "Fast Gross Profit", "filters": json.dumps(filters), "ignore_prepared_report": 1}, sid)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.header("X-Khitmee-Offload-Cache"), "hit")
            self.assertEqual(FakeOracleHandler.query_count, before)

            user = ACTORS["detail_reader"]["user"]
            _login, sid = mo.frappe_login(base, user, "pw")
            filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": "Invoice"}
            for page in (0, 1):
                response = mo.frappe_post_method(base, mo.QUERY_METHOD, {"report_name": "Fast Gross Profit", "filters": json.dumps(filters), "ignore_prepared_report": 1, "start": page * 50, "page_length": 50}, sid)
                self.assertEqual(response.status, 200)
            user = ACTORS["export_requester"]["user"]
            _login, sid = mo.frappe_login(base, user, "pw")
            filters = {"company": "Unit Company", "from_date": "2025-11-27", "to_date": "2026-11-26", "group_by": "Invoice"}
            enqueue = mo.frappe_post_method(base, "unit.enqueue_profit_csv_export", {"filters": json.dumps(filters)}, sid)
            self.assertEqual(enqueue.status, 200)
            download = mo.frappe_get_method(base, "unit.download_profit_csv_export", urlencode({"export_log_name": "EXP-1"}), sid)
            self.assertEqual(download.status, 200)
            self.assertIn(b"INV-1", download.body)

            # k6 fetches /app for its CSRF bootstrap and sends JSON for the export enqueue.
            bootstrap = mo.http_request(base, "GET", "/app", b"", {"Cookie": "sid=" + sid, "Accept": "text/html"})
            self.assertEqual(bootstrap.status, 200)
            self.assertIn(b"unit-csrf", bootstrap.body)
            json_enqueue = mo.http_request(
                base,
                "POST",
                "/api/method/unit.enqueue_profit_csv_export",
                json.dumps({"filters": filters}).encode("utf-8"),
                {"Content-Type": "application/json", "Accept": "application/json", "Cookie": "sid=" + sid, "X-Frappe-CSRF-Token": "unit-csrf"},
            )
            self.assertEqual(json_enqueue.status, 200)
            self.assertEqual(json_enqueue.header("X-Khitmee-Offload-Cache"), "hit")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        verdict = mo.verify(req)
        self.assertTrue(verdict["passed"], verdict)
        self.assertEqual(verdict["detail_pages_exact"], 2)
        self.assertTrue(verdict["all_oracle_observations_exact"])

    def test_concurrent_misses_are_measured_without_upstream_errors(self):
        req = mo.validate_trial_request(self.request)
        with mock.patch.dict(os.environ, {"MANAGER_TEST_CREDS": json.dumps(self.credentials)}):
            mo.capture_oracle(req)
        state = mo.OffloadServerState(req)
        state.cache.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), mo.make_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            sessions = {}
            for actor in ("summary_warehouse", "summary_monthly", "detail_reader"):
                user = ACTORS[actor]["user"]
                _login, sid = mo.frappe_login(base, user, "pw")
                sessions[actor] = (user, sid)
            specs = [
                (sessions["summary_warehouse"], "Warehouse", {}),
                (sessions["summary_monthly"], "Monthly", {}),
                (sessions["detail_reader"], "Invoice", {"start": 0, "page_length": 50}),
                (sessions["detail_reader"], "Invoice", {"start": 50, "page_length": 50}),
            ]
            errors = []

            def call(spec):
                try:
                    (user, sid), group, extra = spec
                    filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": group}
                    fields = {"report_name": "Fast Gross Profit", "filters": json.dumps(filters), "ignore_prepared_report": 1}
                    fields.update(extra)
                    response = mo.frappe_post_method(base, mo.QUERY_METHOD, fields, sid)
                    if response.status != 200:
                        errors.append(f"HTTP {response.status}")
                except Exception as exc:
                    errors.append(str(exc))

            workers = [threading.Thread(target=call, args=(spec,)) for spec in specs]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
            self.assertEqual(errors, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = state.metrics.snapshot()
            if snapshot["active_requests"] == 0 and snapshot["completed_requests"] == snapshot["total_requests"]:
                break
            time.sleep(0.01)
        state.metrics.write()
        metrics = mo.load_json(Path(req["artifact_dir"]) / "service-metrics.json")
        self.assertGreaterEqual(metrics["max_active_requests"], 2)
        self.assertEqual(metrics["errors"], 0)
        events = mo.load_events(Path(req["artifact_dir"]) / "external-events.jsonl")
        self.assertFalse(any(event.get("event") == "upstream_error" for event in events))

    def test_export_capability_reject_does_not_fail_timed_minute_when_excluded(self):
        req = self.make_request(str(Path(self.tmp.name) / "no-export-timed"))
        req["manager_workload"]["export_in_timed_load"] = False
        req = mo.validate_trial_request(req)
        artifact = Path(req["artifact_dir"])
        artifact.mkdir(parents=True, exist_ok=True)
        capture = {
            "schema_version": "manager-oracle-capture-v1",
            "observations": [
                {"ok": True, "cache_key": "wh", "group": "Warehouse", "sha256": "a", "observation_id": "wh"},
                {"ok": True, "cache_key": "mo", "group": "Monthly", "sha256": "b", "observation_id": "mo"},
                {"ok": True, "cache_key": "d0", "group": "manager_detail", "sha256": "c", "observation_id": "d0"},
                {"ok": True, "cache_key": "d1", "group": "manager_detail", "sha256": "d", "observation_id": "d1"},
                {"ok": False, "event": "export_reject", "group": "export_complete", "reason": "method absent"},
            ],
            "freshness": {},
        }
        mo.atomic_write_json(artifact / "oracle-capture.json", capture)
        events = [
            {"event": "external_response", "cache_key": "wh", "sha256": "a", "cache": "hit"},
            {"event": "external_response", "cache_key": "mo", "sha256": "b", "cache": "hit"},
            {"event": "external_response", "cache_key": "d0", "sha256": "c", "cache": "hit"},
            {"event": "external_response", "cache_key": "d1", "sha256": "d", "cache": "hit"},
        ]
        with (artifact / "external-events.jsonl").open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(mo.json_dumps(event) + "\n")
        verdict = mo.verify(req)
        self.assertTrue(verdict["passed"], verdict)
        self.assertTrue(verdict["measured_scope_passed"])
        self.assertFalse(verdict["export_capability_passed"])
        self.assertEqual(verdict["export_parity_rejects"][0]["reason"], "method absent")


    def test_fail_closed_cache_miss_does_not_query_upstream(self):
        req = self.make_request(str(Path(self.tmp.name) / "fail-closed"))
        req["integration"]["fail_closed_on_cache_miss"] = True
        req = mo.validate_trial_request(req)
        state = mo.OffloadServerState(req)
        server = ThreadingHTTPServer(("127.0.0.1", 0), mo.make_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            user = ACTORS["summary_warehouse"]["user"]
            _login, sid = mo.frappe_login(base, user, "pw")
            filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": "Warehouse"}
            before = FakeOracleHandler.query_count
            response = mo.frappe_post_method(base, mo.QUERY_METHOD, {"report_name": "Fast Gross Profit", "filters": json.dumps(filters), "ignore_prepared_report": 1}, sid)
            self.assertEqual(response.status, 409)
            self.assertEqual(FakeOracleHandler.query_count, before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


    def test_unverified_detail_cache_is_not_served_and_live_fallback_rewrites_to_stock(self):
        req = self.make_request(str(Path(self.tmp.name) / "detail-route-policy"))
        req["integration"]["fail_closed_on_cache_miss"] = True
        req = mo.validate_trial_request(req)
        state = mo.OffloadServerState(req)
        user = ACTORS["detail_reader"]["user"]
        filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": "Invoice"}
        key_payload = mo.report_key_payload("Fast Gross Profit", filters, 0, 50, ignore_prepared_report=1)
        key = mo.cache_key(mo.STOCK_CACHE_KIND, user, key_payload)
        state.cache[key] = mo.HttpResponse(200, "OK", [("Content-Type", "application/json")], b'{"message":{"result":[{"fast":"poison"}]}}')
        server = ThreadingHTTPServer(("127.0.0.1", 0), mo.make_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            _login, sid = mo.frappe_login(base, user, "pw")
            fields = {"report_name": "Fast Gross Profit", "filters": json.dumps(filters), "ignore_prepared_report": 1, "start": 0, "page_length": 50}
            before = FakeOracleHandler.query_count
            strict = mo.frappe_post_method(base, mo.QUERY_METHOD, fields, sid)
            self.assertEqual(strict.status, 409)
            self.assertEqual(before, FakeOracleHandler.query_count)
            state.req["integration"]["fail_closed_on_cache_miss"] = False
            response = mo.frappe_post_method(base, mo.QUERY_METHOD, fields, sid)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.header("X-Khitmee-Offload-Route"), "stock-detail-live-oracle")
            self.assertNotIn(b"poison", response.body)
            self.assertIn(b"INV-1", response.body)
            self.assertEqual(FakeOracleHandler.report_names[-1], "Gross Profit")
            self.assertEqual(state.cache[key].body, b'{"message":{"result":[{"fast":"poison"}]}}')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_hybrid_serving_requires_bound_candidate_summary_and_stock_detail_bytes(self):
        stock = "1" * 64
        candidate_bad = "2" * 64
        detail_key = "detail-key"
        summary_keys = {"Warehouse": "warehouse-key", "Monthly": "monthly-key"}
        responses = {key: {"sha256": "3" * 64} for key in summary_keys.values()}
        responses[detail_key] = {"sha256": "4" * 64}
        verification = {
            "schema_version": mo.HYBRID_SERVING_VERIFICATION_SCHEMA,
            "snapshot_id": "5" * 64,
            "candidate_commit": "d1be4a691bea7c0557b89d2a59e7fec9df89bae3",
            "summary_source_report": "Fast Gross Profit",
            "summary_qualification": {
                group: {
                    "stock_rows_sha256": stock,
                    "candidate_rows_sha256": stock,
                    "stock_double_execution_verified": True,
                    "exact_ordered_candidate_equal": True,
                }
                for group in mo.SUMMARY_GROUPS
            },
            "summary_cache_keys": {
                group: {"cache_key": key, "response_sha256": "3" * 64}
                for group, key in summary_keys.items()
            },
            "detail_source_report": "Gross Profit",
            "detail_stock_double_execution_ordered_hash_equal": True,
            "stock_detail_rows_sha256": stock,
            "candidate_detail_rows_sha256": candidate_bad,
            "candidate_detail_rejected": True,
            "detail_cache_keys": {"page-0": {"cache_key": detail_key, "response_sha256": "4" * 64}},
        }
        index = {"verification": verification, "responses": responses}
        self.assertTrue(mo.candidate_summary_cache_verified(index))
        self.assertTrue(mo.detail_static_cache_verified(index))

        missing_binding = json.loads(json.dumps(index))
        del missing_binding["verification"]["summary_cache_keys"]["Monthly"]
        self.assertFalse(mo.candidate_summary_cache_verified(missing_binding))
        corrupt_summary = json.loads(json.dumps(index))
        corrupt_summary["responses"][summary_keys["Warehouse"]]["sha256"] = "6" * 64
        self.assertFalse(mo.candidate_summary_cache_verified(corrupt_summary))
        missing_detail_binding = json.loads(json.dumps(index))
        missing_detail_binding["verification"]["detail_cache_keys"] = {}
        self.assertFalse(mo.detail_static_cache_verified(missing_detail_binding))

    def test_hybrid_summary_live_fallback_is_not_relabeled_as_candidate_cache(self):
        req = self.make_request(str(Path(self.tmp.name) / "hybrid-live-fallback"))
        req["integration"]["fail_closed_on_cache_miss"] = False
        req = mo.validate_trial_request(req)
        state = mo.OffloadServerState(req)
        state.hybrid_serving_index = True
        state.candidate_summary_cache_verified = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), mo.make_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            user = ACTORS["summary_warehouse"]["user"]
            _login, sid = mo.frappe_login(base, user, "pw")
            filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": "Warehouse"}
            fields = {"report_name": "Fast Gross Profit", "filters": json.dumps(filters), "ignore_prepared_report": 1}
            response = mo.frappe_post_method(base, mo.QUERY_METHOD, fields, sid)
            key_payload = mo.report_key_payload("Fast Gross Profit", filters, ignore_prepared_report=1)
            key = mo.cache_key(mo.STOCK_CACHE_KIND, user, key_payload)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.header("X-Khitmee-Offload-Cache"), "miss")
            self.assertEqual(response.header("X-Khitmee-Offload-Route"), "stock-summary-live-oracle")
            self.assertNotIn(key, state.cache)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_dynamic_window_is_not_served_as_current(self):
        req = self.make_request(str(Path(self.tmp.name) / "dynamic"))
        req["report_windows"]["year_to_date"]["to_date"] = "2026-11-27"
        req = mo.validate_trial_request(req)
        assessment = mo.sync_status(req)
        self.assertEqual(assessment["freshness"]["decision"], "parity-reject")
        self.assertIn("snapshot cutoff", assessment["freshness"]["reason"])
        self.assertTrue(any(row["path"] == "current/dirty-day cashier writes" for row in assessment["parity_reject_paths"]))

    def test_independent_stock_comparison_requires_exact_ordered_rows(self):
        import verify_stock_oracle_independent as independent
        with tempfile.TemporaryDirectory() as tmp:
            cached = Path(tmp) / "cached.bin"
            cached.write_text(json.dumps({"message": {"columns": [{"fieldname": "warehouse"}], "result": [["a", 1], ["b", 2]]}}), encoding="utf-8")
            equal = independent.compare_exact("case", [{"fieldname": "warehouse"}], [["a", 1], ["b", 2]], cached)
            reordered = independent.compare_exact("case", [{"fieldname": "warehouse"}], [["b", 2], ["a", 1]], cached)
            self.assertTrue(equal["ordered_exact_equal"])
            self.assertFalse(reordered["ordered_exact_equal"])
            self.assertEqual(reordered["mismatch"]["first_ordered_row_difference"], 0)
            changed_type = independent.compare_exact("case", [{"fieldname": "warehouse"}], [["a", True], ["b", 2]], cached)
            self.assertFalse(changed_type["ordered_exact_equal"])
            self.assertFalse(changed_type["type_exact_equal"])
            self.assertIn("type_exact_equal", changed_type["mismatch"])
            self.assertEqual(changed_type["mismatch"]["first_ordered_row_difference"], 0)

    def test_independent_stock_comparison_rejects_column_difference(self):
        import verify_stock_oracle_independent as independent
        with tempfile.TemporaryDirectory() as tmp:
            cached = Path(tmp) / "cached.bin"
            cached.write_text(json.dumps({"message": {"columns": [{"fieldname": "warehouse"}], "result": [["a", 1]]}}), encoding="utf-8")
            result = independent.compare_exact("case", [{"fieldname": "warehouse", "width": 100}], [["a", 1]], cached)
            self.assertFalse(result["ordered_exact_equal"])
            self.assertFalse(result["mismatch"]["columns_equal"])


    def test_streaming_canonical_hash_applies_scalar_handler_without_full_document(self):
        from decimal import Decimal
        def handler(value):
            if isinstance(value, Decimal):
                return float(value)
            return value
        self.assertEqual(
            mo.canonical_json_streaming_sha256({"amount": Decimal("1.5")}, handler),
            mo.canonical_json_sha256({"amount": 1.5}),
        )

    def test_direct_inprocess_detail_capture_projects_exact_pages_and_hashes_complete_source(self):
        import types
        req = mo.validate_trial_request(self.request)
        capture_dir = Path(self.tmp.name) / "direct-detail"
        response_dir = capture_dir / "oracle-responses"
        response_dir.mkdir(parents=True)
        columns = [{"fieldname": "name"}, {"fieldname": "qty"}]
        rows = [{"name": f"INV-{index + 1}", "qty": index + 1} for index in range(120)]
        result = {"columns": columns, "result": rows, "execution_time": 0.01}

        class FakeFrappe:
            _dict = dict
            def __init__(self):
                self.local = types.SimpleNamespace()
                self.user = None
            def set_user(self, user):
                self.user = user

        fake = FakeFrappe()
        frappe_mod = types.ModuleType("frappe")
        frappe_mod._dict = dict
        desk_mod = types.ModuleType("frappe.desk")
        desk_mod.__path__ = []
        query_mod = types.ModuleType("frappe.desk.query_report")
        query_mod.run = lambda **_kwargs: result
        modules = {"frappe": frappe_mod, "frappe.desk": desk_mod, "frappe.desk.query_report": query_mod}
        handler = lambda value: str(value)
        with mock.patch.object(mo, "inprocess_connect", return_value=fake),              mock.patch.object(mo, "frappe_json_handler", return_value=handler),              mock.patch.dict(sys.modules, modules):
            observations, cache = mo.capture_detail_inprocess(req, req["oracle"], self.credentials, response_dir, "sid-unit")

        self.assertEqual(len(observations), 2)
        self.assertTrue(all(obs["ok"] for obs in observations), observations)
        expected_source_sha = mo.canonical_json_streaming_sha256([columns, rows], handler)
        for page, obs in enumerate(observations):
            self.assertEqual(obs["row_count"], 50)
            self.assertEqual(obs["oracle_source_row_count"], 120)
            self.assertEqual(obs["oracle_source_columns_rows_sha256"], expected_source_sha)
            self.assertTrue(obs["oracle_source_complete"])
            self.assertIs(obs["oracle_source_truncated"], False)
            artifact = response_dir.parent / obs["response_artifact"]
            payload = json.loads(artifact.read_bytes().decode("utf-8"))
            self.assertEqual(len(payload["message"]["result"]), 50)
            self.assertEqual(payload["message"]["result"][0]["name"], f"INV-{page * 50 + 1}")
        filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": "Invoice"}
        key_payload = mo.report_key_payload("Fast Gross Profit", filters, 0, 50, ignore_prepared_report=1)
        stock_key = mo.cache_key(mo.STOCK_CACHE_KIND, ACTORS["detail_reader"]["user"], key_payload)
        self.assertIn(stock_key, cache)
        self.assertNotIn(mo.cache_key("query_report", ACTORS["detail_reader"]["user"], key_payload), cache)

    def test_legacy_custom_query_report_cache_cannot_be_selected(self):
        req = mo.validate_trial_request(self.request)
        artifact_dir = Path(req["artifact_dir"])
        response_dir = artifact_dir / "oracle-responses"
        response_dir.mkdir(parents=True)
        user = ACTORS["summary_warehouse"]["user"]
        filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": "Warehouse"}
        payload = mo.report_key_payload("Fast Gross Profit", filters, ignore_prepared_report=1)
        legacy_key = mo.cache_key("query_report", user, payload)
        stock_key = mo.cache_key(mo.STOCK_CACHE_KIND, user, payload)
        self.assertNotEqual(legacy_key, stock_key)
        body = json.dumps({"message": {"columns": [], "result": [["custom"]]}}, separators=(",", ":")).encode("utf-8")
        artifact_name = "cache-custom.bin"
        (response_dir / artifact_name).write_bytes(body)
        legacy_index = {legacy_key: {"artifact": "oracle-responses/" + artifact_name, "sha256": mo.sha256_bytes(body), "status": 200, "headers": []}}
        mo.atomic_write_json(artifact_dir / "oracle-cache-index.json", legacy_index)
        state = mo.OffloadServerState(req)
        self.assertEqual(state.cache, {})
        self.assertNotIn(stock_key, state.cache)
        self.assertNotIn(legacy_key, state.cache)

    def test_cache_index_requires_matching_stock_provenance(self):
        req = mo.validate_trial_request(self.request)
        user = ACTORS["summary_warehouse"]["user"]
        filters = {"company": "Unit Company", "from_date": "2026-01-01", "to_date": "2026-11-26", "group_by": "Warehouse"}
        payload = mo.report_key_payload("Fast Gross Profit", filters, ignore_prepared_report=1)
        stock_key = mo.cache_key(mo.STOCK_CACHE_KIND, user, payload)
        body = json.dumps({"message": {"columns": [], "result": [["stock"]]}}, separators=(",", ":")).encode("utf-8")

        def write_index(root, provenance):
            artifact_dir = Path(root)
            response_dir = artifact_dir / "oracle-responses"
            response_dir.mkdir(parents=True)
            (response_dir / "cache-stock.bin").write_bytes(body)
            index = {
                "schema_version": mo.CACHE_INDEX_SCHEMA_VERSION,
                "created_at": mo.utc_now(),
                "provenance": provenance,
                "responses": {stock_key: {"artifact": "oracle-responses/cache-stock.bin", "sha256": mo.sha256_bytes(body), "status": 200, "headers": []}},
            }
            mo.atomic_write_json(artifact_dir / "oracle-cache-index.json", index)
            return artifact_dir

        good_dir = write_index(Path(self.tmp.name) / "good-index", mo.cache_index_provenance(req))
        good_state = mo.OffloadServerState({**req, "artifact_dir": str(good_dir)})
        self.assertIn(stock_key, good_state.cache)
        bad_provenance = mo.cache_index_provenance(req)
        bad_provenance["oracle_report"] = "Fast Gross Profit"
        bad_dir = write_index(Path(self.tmp.name) / "bad-index", bad_provenance)
        bad_state = mo.OffloadServerState({**req, "artifact_dir": str(bad_dir)})
        self.assertNotIn(stock_key, bad_state.cache)
        corrupt_dir = write_index(Path(self.tmp.name) / "corrupt-index", mo.cache_index_provenance(req))
        (corrupt_dir / "oracle-responses" / "cache-stock.bin").write_bytes(body + b"corruption")
        corrupt_state = mo.OffloadServerState({**req, "artifact_dir": str(corrupt_dir)})
        self.assertNotIn(stock_key, corrupt_state.cache)

    def test_independent_verifier_type_shape_and_page_projection_are_explicit(self):
        import verify_stock_oracle_independent as independent
        self.assertNotEqual(independent.type_shape(1), independent.type_shape(1.0))
        self.assertNotEqual(independent.type_shape(1), independent.type_shape(True))
        rows = list(range(120))
        page, start, end = independent.project_ordered_page(rows, 1, 50)
        self.assertEqual((start, end), (50, 100))
        self.assertEqual(page[0], 50)
        self.assertEqual(len(page), 50)
        self.assertEqual(page[-1], 99)

    def test_capture_refuses_to_overwrite_existing_oracle_evidence(self):
        req = mo.validate_trial_request(self.request)
        with mock.patch.dict(os.environ, {"MANAGER_TEST_CREDS": json.dumps(self.credentials)}):
            mo.capture_oracle(req)
            with self.assertRaises(mo.RequestError):
                mo.capture_oracle(req)


if __name__ == "__main__":
    unittest.main()
