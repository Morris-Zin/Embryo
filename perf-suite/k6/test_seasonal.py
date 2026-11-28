import base64
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "seasonal"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluate_seasonal
import verify_run

ROOT = Path(__file__).resolve().parent


def start_event(client_id, voucher_type="sale", line_count=10, segment="baseline", phase="baseline"):
    return {
        "event": "voucher_start",
        "client_voucher_id": client_id,
        "type": voucher_type,
        "line_count": line_count,
        "segment": segment,
        "phase": phase,
    }


def complete_event(client_id, voucher_no, voucher_type="sale", line_count=10, phase="baseline"):
    return {
        "event": "voucher_complete",
        "client_voucher_id": client_id,
        "voucher_no": voucher_no,
        "type": voucher_type,
        "line_count": line_count,
        "phase": phase,
        "duration_ms": 100,
    }


def unknown_event(client_id, voucher_type="sale", line_count=10, phase="baseline", reason="lookup missing"):
    return {
        "event": "voucher_unknown",
        "client_voucher_id": client_id,
        "type": voucher_type,
        "line_count": line_count,
        "phase": phase,
        "duration_ms": 100,
        "reason": reason,
    }


def events_bundle(events, config=None):
    return {
        "events": events,
        "started": {e["client_voucher_id"]: e for e in events if e.get("event") == "voucher_start"},
        "completed": {e["client_voucher_id"]: e for e in events if e.get("event") == "voucher_complete"},
        "rejected": {e["client_voucher_id"]: e for e in events if e.get("event") == "voucher_reject"},
        "unknown": {e["client_voucher_id"]: e for e in events if e.get("event") == "voucher_unknown"},
        "config": config or {"run_prefix": "K6S-test-", "managers_enabled": True, "export_enabled": True},
    }


def passing_sale_rows(client_id="cid-sale", name="SI-0001"):
    return {
        "idempotency": {client_id: [{"doctype": "Sales Invoice", "name": name}]},
        "purchase_receipts": {},
        "purchase_invoices": {},
        "child_counts": {("Sales Invoice", name): [name, "10"]},
        "stock_ledger": {("Sales Invoice", name): [name, "10", "-10"]},
        "gl_entries": {("Sales Invoice", name): [name, "4", "0"]},
        "purchase_invoice_links": {},
    }


def coordinated_test_allocations(scale_num, scale_den):
    order = [
        ("sale_small", 54),
        ("sale_medium", 16),
        ("sale_large", 4),
        ("transfer", 9),
        ("purchase_cycle", 5),
    ]
    previous = {mix_key: 0 for mix_key, _per_minute in order}
    rows = []
    for minute in range(scale_den):
        step = minute + 1
        row_total = (88 * scale_num * step) // scale_den
        current = {}
        candidates = []
        for mix_key, per_minute in order:
            numerator = per_minute * scale_num * step
            lower = numerator // scale_den
            upper = -(-numerator // scale_den)
            current[mix_key] = max(previous[mix_key], lower)
            if upper > current[mix_key]:
                candidates.append(((numerator % scale_den) / scale_den, mix_key))
        needed = row_total - sum(current.values())
        for _fraction, mix_key in sorted(candidates, reverse=True)[:needed]:
            current[mix_key] += 1
        rows.append({mix_key: current[mix_key] - previous[mix_key] for mix_key, _per_minute in order})
        previous = current
    return rows


def schedule_test_segment(name="baseline", duration=60, scale_num=1, scale_den=1):
    order = [
        ("sale_small", 54, "sale", 10),
        ("sale_medium", 16, "sale", 50),
        ("sale_large", 4, "sale", 200),
        ("transfer", 9, "transfer", 50),
        ("purchase_cycle", 5, "purchase_cycle", 50),
    ]
    template = []
    allocations = coordinated_test_allocations(scale_num, scale_den)
    for minute in range(scale_den):
        for mix_key, per_minute, voucher_type, line_count in order:
            count = allocations[minute][mix_key]
            for sequence in range(count):
                template.append({
                    "mix_key": mix_key,
                    "type": voucher_type,
                    "line_count": line_count,
                    "minute": minute,
                    "sequence": sequence,
                })
    contention = []
    for minute in range(scale_den):
        for sequence in range(evaluate_seasonal.allocated_count(1, minute, scale_num, scale_den)):
            contention.append({"minute": minute, "sequence": sequence})

    def bounds(base):
        nominal = (duration / 60.0) * base * scale_num / scale_den
        minimum = max(1, math.ceil(nominal - 1e-9))
        endpoint_aligned = abs(nominal - round(nominal)) < 1e-9
        return {"nominal": nominal, "minimum": minimum, "maximum": minimum + (1 if endpoint_aligned else 0), "endpoint_aligned": endpoint_aligned}

    return {
        "name": name,
        "phase": name if name in {"baseline", "margin", "recovery"} else "overload",
        "duration_seconds": duration,
        "scale": scale_num / scale_den,
        "scale_num": scale_num,
        "scale_den": scale_den,
        "template_iterations_per_block": len(template),
        "voucher_template": template,
        "contention_template": contention,
        "arrival_bounds": {
            "regular_iterations": bounds(88),
            "contention_iterations": bounds(1),
            "search_iterations": bounds(80),
        },
        "regular_rate": {"rate": 88 * scale_num, "timeUnit": f"{scale_den}m"},
        "contention_rate": {"rate": scale_num, "timeUnit": f"{scale_den}m"},
        "search_rate": {"rate": 80 * scale_num, "timeUnit": f"{scale_den}m"},
    }


def scheduled_test_starts(segment, regular_count, contention_count):
    events = []
    template = segment["voucher_template"]
    scale_den = segment["scale_den"]
    for iteration in range(regular_count):
        entry = template[iteration % len(template)]
        entry_minute, mix_key = evaluate_seasonal.template_entry_parts(entry)
        minute = entry_minute + (iteration // len(template)) * scale_den
        shapes = evaluate_seasonal.REGULAR_TEMPLATE_SHAPES[mix_key]
        for voucher_type, line_count in shapes:
            events.append({
                "event": "voucher_start",
                "client_voucher_id": f"{segment['name']}-r{iteration}-{voucher_type}",
                "type": voucher_type,
                "line_count": line_count,
                "minute": minute,
                "segment": segment["name"],
                "phase": segment["phase"],
            })
    contention_template = segment["contention_template"]
    for iteration in range(contention_count):
        entry = contention_template[iteration % len(contention_template)]
        minute = entry["minute"] + (iteration // len(contention_template)) * scale_den
        for shop in range(6):
            events.append({
                "event": "voucher_start",
                "client_voucher_id": f"{segment['name']}-c{iteration}-sale-{shop}",
                "type": "sale",
                "line_count": 10,
                "minute": minute,
                "segment": segment["name"],
                "phase": segment["phase"],
                "contention": True,
                "warehouse": f"Shop {shop}",
                "shared_contention_item": "HOT",
            })
        events.append({
            "event": "voucher_start",
            "client_voucher_id": f"{segment['name']}-c{iteration}-transfer",
            "type": "transfer",
            "line_count": 50,
            "minute": minute,
            "segment": segment["name"],
            "phase": segment["phase"],
            "contention": True,
            "shared_contention_item": "HOT",
        })
    return events


def completions_for(starts):
    return [
        complete_event(event["client_voucher_id"], "V-" + str(index), event["type"], event["line_count"], event["phase"])
        for index, event in enumerate(starts)
    ]


class SeasonalVerifierTests(unittest.TestCase):
    def test_reconciler_passes_confirmed_sale_with_balanced_ledgers(self):
        events = events_bundle([start_event("cid-sale"), complete_event("cid-sale", "SI-0001")])
        outcome = verify_run.Reconciler(events, passing_sale_rows()).reconcile()
        self.assertTrue(outcome["passed"], outcome)
        self.assertEqual(outcome["by_type"]["sale"]["confirmed"], 1)

    def test_reconciler_detects_duplicate_purchase_receipt_identity(self):
        events = events_bundle([
            start_event("cid-pr", "purchase_receipt", 50),
            complete_event("cid-pr", "PR-0001", "purchase_receipt", 50),
        ])
        rows = passing_sale_rows()
        rows["idempotency"] = {}
        rows["child_counts"] = {("Purchase Receipt", "PR-0001"): ["PR-0001", "50"]}
        rows["stock_ledger"] = {("Purchase Receipt", "PR-0001"): ["PR-0001", "50", "100"]}
        rows["gl_entries"] = {}
        rows["purchase_receipts"] = {
            "cid-pr": [
                {"name": "PR-0001", "docstatus": "1"},
                {"name": "PR-0002", "docstatus": "1"},
            ]
        }
        outcome = verify_run.Reconciler(events, rows).reconcile()
        self.assertFalse(outcome["passed"])
        self.assertIn("duplicate_document", {item["kind"] for item in outcome["defects"]})

    def test_reconciler_detects_duplicate_identity_for_all_four_voucher_types(self):
        cases = (
            ("sale", "Sales Invoice", "idempotency"),
            ("transfer", "Stock Entry", "idempotency"),
            ("purchase_receipt", "Purchase Receipt", "purchase_receipts"),
            ("purchase_invoice", "Purchase Invoice", "purchase_invoices"),
        )
        for voucher_type, doctype, source in cases:
            with self.subTest(voucher_type=voucher_type):
                client_id = "cid-" + voucher_type
                events = events_bundle([
                    start_event(client_id, voucher_type, 2),
                    complete_event(client_id, "DOC-0001", voucher_type, 2),
                ])
                refs = [
                    {"doctype": doctype, "name": "DOC-0001", "docstatus": "1"},
                    {"doctype": doctype, "name": "DOC-0002", "docstatus": "1"},
                ]
                rows = {
                    "idempotency": {},
                    "purchase_receipts": {},
                    "purchase_invoices": {},
                    "child_counts": {("Sales Invoice", "DOC-0001"): ["DOC-0001", "2"], (doctype, "DOC-0001"): ["DOC-0001", "2"]},
                    "stock_ledger": {(doctype, "DOC-0001"): ["DOC-0001", "2", "0"]},
                    "gl_entries": {(doctype, "DOC-0001"): ["DOC-0001", "2", "0"]},
                    "purchase_invoice_links": {"DOC-0001": ["DOC-0001", "0", "0"]},
                }
                rows[source][client_id] = refs
                outcome = verify_run.Reconciler(events, rows).reconcile()
                self.assertFalse(outcome["passed"])
                self.assertIn("duplicate_document", {item["kind"] for item in outcome["defects"]})
                self.assertEqual(outcome["by_type"][voucher_type]["duplicates"], 1)

    def test_reconciler_detects_missing_and_unbalanced_transfer(self):
        events = events_bundle([
            start_event("cid-transfer", "transfer", 50),
            complete_event("cid-transfer", "STE-0001", "transfer", 50),
        ])
        rows = passing_sale_rows()
        rows["idempotency"] = {"cid-transfer": [{"doctype": "Stock Entry", "name": "STE-0001"}]}
        rows["child_counts"] = {("Stock Entry", "STE-0001"): ["STE-0001", "50"]}
        rows["stock_ledger"] = {("Stock Entry", "STE-0001"): ["STE-0001", "100", "2"]}
        rows["gl_entries"] = {}
        outcome = verify_run.Reconciler(events, rows).reconcile()
        self.assertFalse(outcome["passed"])
        self.assertIn("stock_transfer_unbalanced", {item["kind"] for item in outcome["defects"]})

    def test_parse_events_extracts_json_from_k6_console_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k6.log"
            payload = {"event": "config", "config": {"run_prefix": "K6S-abc-"}}
            encoded = json.dumps(payload).replace(chr(34), chr(92) + chr(34))
            path.write_text(
                'time="x" level=info msg="KHITMEE_SEASONAL_EVENT ' + encoded + '" source=console' + chr(10),
                encoding="utf-8",
            )
            parsed = verify_run.parse_events(path)
            self.assertEqual(parsed["config"]["run_prefix"], "K6S-abc-")
            path.write_text(json.dumps({"msg": "KHITMEE_SEASONAL_EVENT " + json.dumps(payload), "level": "info"}) + chr(10), encoding="utf-8")
            parsed = verify_run.parse_events(path)
            self.assertEqual(parsed["config"]["run_prefix"], "K6S-abc-")

    def test_parse_idempotency_rows_extracts_client_id_from_user_scoped_key(self):
        rows = [["Administrator:sale:K6S-abc-baseline-sale_small-m0-s0", "Sales Invoice", "SI-0009"]]
        parsed = verify_run.parse_idempotency_rows(rows)
        self.assertEqual(parsed["K6S-abc-baseline-sale_small-m0-s0"][0]["name"], "SI-0009")

    def test_rejected_voucher_without_commit_is_not_a_correctness_defect(self):
        events = events_bundle([
            start_event("cid-sale"),
            {
                "event": "voucher_reject",
                "client_voucher_id": "cid-sale",
                "type": "sale",
                "line_count": 10,
                "phase": "baseline",
            },
        ])
        rows = passing_sale_rows()
        rows["idempotency"] = {}
        rows["child_counts"] = {}
        rows["stock_ledger"] = {}
        rows["gl_entries"] = {}
        outcome = verify_run.Reconciler(events, rows).reconcile()
        self.assertTrue(outcome["passed"], outcome)

    def test_unknown_voucher_status_is_preserved_and_missing_document_is_safe(self):
        events = events_bundle([
            start_event("cid-pr", "purchase_receipt", 50),
            unknown_event("cid-pr", "purchase_receipt", 50),
        ])
        rows = passing_sale_rows()
        rows["idempotency"] = {}
        rows["child_counts"] = {}
        rows["stock_ledger"] = {}
        rows["gl_entries"] = {}
        outcome = verify_run.Reconciler(events, rows).reconcile()
        self.assertTrue(outcome["passed"], outcome)
        self.assertEqual(outcome["by_type"]["purchase_receipt"]["unknown"], 1)

    def test_unknown_voucher_with_committed_document_is_unconfirmed(self):
        events = events_bundle([
            start_event("cid-pr", "purchase_receipt", 50),
            unknown_event("cid-pr", "purchase_receipt", 50),
        ])
        rows = passing_sale_rows()
        rows["idempotency"] = {}
        rows["child_counts"] = {("Purchase Receipt", "PR-0001"): ["PR-0001", "50"]}
        rows["stock_ledger"] = {("Purchase Receipt", "PR-0001"): ["PR-0001", "50", "100"]}
        rows["gl_entries"] = {}
        rows["purchase_receipts"] = {"cid-pr": [{"name": "PR-0001", "docstatus": "1"}]}
        outcome = verify_run.Reconciler(events, rows).reconcile()
        self.assertFalse(outcome["passed"])
        self.assertIn("unconfirmed_document", {item["kind"] for item in outcome["defects"]})

    def test_sql_like_literal_escapes_wildcards(self):
        rendered = verify_run.sql_like_contains("K6S-a%b_c")
        self.assertIn("%", rendered)
        self.assertIn("\\%", rendered)
        self.assertIn("\\_", rendered)
        self.assertTrue(rendered.endswith("ESCAPE '\\\\'"))


class SeasonalEvaluatorTests(unittest.TestCase):
    def test_login_lifecycle_reports_attempts_and_cost(self):
        bundle = events_bundle([
            {"event": "auth_login_begin", "actor_user": "cashier@test.invalid"},
            {"event": "auth_login_end", "actor_user": "cashier@test.invalid", "login_http_ms": 100, "bootstrap_ms": 300},
            {"event": "auth_login_begin", "actor_user": "cashier@test.invalid"},
        ])
        report = evaluate_seasonal.login_lifecycle(bundle)
        self.assertEqual(report["begin_count"], 2)
        self.assertEqual(report["end_count"], 1)
        self.assertEqual(report["failed_or_incomplete_count"], 1)
        self.assertEqual(report["login_http_ms"]["total"], 100)
        self.assertEqual(report["bootstrap_ms"]["total"], 300)
        self.assertEqual(report["combined_ms"]["p95"], 400)
        self.assertIn("per active k6 VU actor", report["session_lifecycle"])

    def test_detail_failure_is_independent_from_cashier_latency(self):
        events = []
        for phase in ("baseline", "margin", "recovery"):
            for voucher_type in ("sale", "transfer", "purchase_receipt", "purchase_invoice"):
                line_count = 200 if voucher_type == "sale" else 10
                events.append(complete_event(f"{phase}-{voucher_type}", "V-1", voucher_type, line_count, phase))
            events.append({
                "event": "manager_detail_reject",
                "phase": phase,
                "reason": "pagination not honored: detail endpoint ignored page_length=50",
                "pagination_honored": False,
            })
        bundle = events_bundle(events)
        cashier = evaluate_seasonal.voucher_latency_gate(bundle)
        detail = evaluate_seasonal.detail_gate(bundle, True)
        self.assertEqual(cashier.status, "PASS", cashier)
        self.assertEqual(detail.status, "FAIL", detail)
        self.assertIn("pagination", detail.detail)

    def test_offered_counts_detect_missing_completion_accounting(self):
        segment = schedule_test_segment()
        starts = scheduled_test_starts(segment, 88, 1)
        config = {"run_prefix": "K6S-test-", "phases": [segment]}
        events = events_bundle(starts + completions_for(starts), config)
        gate, _rows = evaluate_seasonal.offered_counts_gate(events, {"metrics": {}})
        self.assertEqual(gate.status, "PASS", gate)
        events["started"]["orphan"] = start_event("orphan")
        gate, _rows = evaluate_seasonal.offered_counts_gate(events, {"metrics": {}})
        self.assertEqual(gate.status, "FAIL")
        self.assertIn("unaccounted", gate.detail + gate.observed)

    def test_endpoint_boundary_iteration_counts_match_integer_schedule_candidates(self):
        segment = schedule_test_segment()
        config = {"run_prefix": "K6S-test-", "phases": [segment]}
        for regular_count, contention_count in ((88, 1), (89, 1), (88, 2), (89, 2)):
            starts = scheduled_test_starts(segment, regular_count, contention_count)
            events = events_bundle(starts + completions_for(starts), config)
            gate, _rows = evaluate_seasonal.offered_counts_gate(events, {"metrics": {}})
            self.assertEqual(gate.status, "PASS", (regular_count, contention_count, gate))
            result = evaluate_seasonal.evaluate_events([{"event": "config", "config": config}] + events["events"], {"metrics": {}})
            row = result["arrival_schedule"][0]
            self.assertTrue(row["matched"])
            self.assertEqual(row["regular_iterations"], regular_count)
            self.assertEqual(row["contention_iterations"], contention_count)
            json.dumps(row)
        starts = scheduled_test_starts(segment, 90, 1)
        events = events_bundle(starts + completions_for(starts), config)
        gate, _rows = evaluate_seasonal.offered_counts_gate(events, {"metrics": {}})
        self.assertEqual(gate.status, "FAIL")
        self.assertIn("integer schedule", gate.detail)

    def test_unknown_terminal_events_count_as_accounted_but_not_workflow_completion(self):
        segment = schedule_test_segment()
        starts = scheduled_test_starts(segment, 88, 1)
        outcomes = completions_for(starts)
        first = starts[0]
        outcomes[0] = unknown_event(
            first["client_voucher_id"], first["type"], first["line_count"], first["phase"], "permission-aware lookup missing"
        )
        events = events_bundle(starts + outcomes, {"run_prefix": "K6S-test-", "phases": [segment]})
        gate, _rows = evaluate_seasonal.offered_counts_gate(events, {"metrics": {}})
        self.assertEqual(gate.status, "PASS", gate)
        self.assertIn("unknown 1", gate.observed)
        completion = evaluate_seasonal.workflow_completion_gate(events)
        self.assertEqual(completion.status, "FAIL")
        self.assertIn("unknown", completion.detail)

    def retry_witness_events(self, voucher_type, mix_key, *, purchase_recovery):
        client_id = f"K6S-test-baseline-{mix_key}-m0-s0"
        start = dict(start_event(client_id, voucher_type, 50), minute=0, sequence=0, mix_key=mix_key)
        voucher_no = "DOC-0001"
        complete = dict(
            complete_event(client_id, voucher_no, voucher_type, 50),
            minute=0,
            sequence=0,
            mix_key=mix_key,
            lost_ack=True,
            retried=True,
            retry_confirmed=True,
            first_committed_voucher=voucher_no,
            attempts=1 if purchase_recovery else 2,
            recovery_attempted=purchase_recovery,
            recovery_reads=2 if purchase_recovery else 0,
            reconciled=purchase_recovery,
            resubmitted=not purchase_recovery,
            retry_overlap=True,
            retry_overlap_mode="submit_plus_readonly_recovery_batch" if purchase_recovery else "duplicate_idempotent_submit_batch",
            concurrent_attempts=2,
        )
        overlap = dict(
            start,
            event="voucher_retry_overlap",
            mode=complete["retry_overlap_mode"],
            concurrent_attempts=2,
            first_inflight=True,
        )
        return [start, overlap, complete]

    def test_retry_identity_accepts_purchase_read_only_recovery(self):
        events = []
        events += self.retry_witness_events("sale", "sale_small", purchase_recovery=False)
        events += self.retry_witness_events("transfer", "transfer", purchase_recovery=False)
        events += self.retry_witness_events("purchase_receipt", "purchase_receipt", purchase_recovery=True)
        events += self.retry_witness_events("purchase_invoice", "purchase_invoice", purchase_recovery=True)
        gate = evaluate_seasonal.retry_identity_gate(events_bundle(events))
        self.assertEqual(gate.status, "PASS", gate)

    def test_retry_identity_rejects_post_commit_only_retry_evidence(self):
        events = []
        for voucher_type, mix_key, purchase in (
            ("sale", "sale_small", False),
            ("transfer", "transfer", False),
            ("purchase_receipt", "purchase_receipt", True),
            ("purchase_invoice", "purchase_invoice", True),
        ):
            events += self.retry_witness_events(voucher_type, mix_key, purchase_recovery=purchase)
        for event in events:
            if event.get("event") == "voucher_complete":
                event["retry_overlap"] = False
        gate = evaluate_seasonal.retry_identity_gate(events_bundle(events))
        self.assertEqual(gate.status, "FAIL")
        self.assertIn("in flight", gate.threshold)

    def test_retry_identity_rejects_purchase_resubmission_evidence(self):
        events = []
        events += self.retry_witness_events("sale", "sale_small", purchase_recovery=False)
        events += self.retry_witness_events("transfer", "transfer", purchase_recovery=False)
        events += self.retry_witness_events("purchase_receipt", "purchase_receipt", purchase_recovery=False)
        events += self.retry_witness_events("purchase_invoice", "purchase_invoice", purchase_recovery=True)
        gate = evaluate_seasonal.retry_identity_gate(events_bundle(events))
        self.assertNotEqual(gate.status, "PASS", gate)
        self.assertIn("purchase_receipt", gate.detail + gate.observed)

    def actor_config(self):
        return {
            "cashier": {"actor": "cashier", "user": "cashier@test.invalid", "authorization": "submit"},
            "summary_warehouse": {"actor": "summary_warehouse", "user": "warehouse@test.invalid", "authorization": "read"},
            "summary_monthly": {"actor": "summary_monthly", "user": "monthly@test.invalid", "authorization": "read"},
            "detail_reader": {"actor": "detail_reader", "user": "detail@test.invalid", "authorization": "read"},
            "export_requester": {"actor": "export_requester", "user": "export@test.invalid", "authorization": "export"},
        }

    def report_windows(self):
        return {
            "year_to_date": {"scope": "year_to_date", "from_date": "2026-01-01", "to_date": "2026-11-26"},
            "full_year": {"scope": "full_year", "from_date": "2025-11-27", "to_date": "2026-11-26"},
        }

    def test_evaluate_events_exposes_reusable_json_result_and_hash_only_outputs(self):
        config = {
            "run_prefix": "K6S-test-",
            "managers_enabled": True,
            "export_enabled": True,
            "actors": self.actor_config(),
            "report_windows": self.report_windows(),
            "phases": [],
        }
        digest = "a" * 64
        events = [
            {"event": "config", "config": config},
            {
                "event": "manager_summary",
                "observation_id": "summary/0",
                "phase": "baseline",
                "segment": "baseline",
                "group_by": "Warehouse",
                "actor": "summary_warehouse",
                "actor_user": "warehouse@test.invalid",
                "window_scope": "year_to_date",
                "window_from_date": "2026-01-01",
                "window_to_date": "2026-11-26",
                "result_sha256": digest,
            },
            {
                "event": "manager_detail",
                "observation_id": "detail/0",
                "phase": "baseline",
                "segment": "baseline",
                "actor": "detail_reader",
                "actor_user": "detail@test.invalid",
                "window_scope": "year_to_date",
                "window_from_date": "2026-01-01",
                "window_to_date": "2026-11-26",
                "first_page_sha256": "b" * 64,
            },
            {
                "event": "export_complete",
                "observation_id": "export/0",
                "phase": "baseline",
                "segment": "baseline",
                "actor": "export_requester",
                "actor_user": "export@test.invalid",
                "window_scope": "full_year",
                "window_from_date": "2025-11-27",
                "window_to_date": "2026-11-26",
                "sha256": "c" * 64,
                "row_count": 2,
                "body_bytes": 128,
            },
        ]
        result = evaluate_seasonal.evaluate_events(events, {"metrics": {}})
        self.assertIn("passed", result)
        self.assertEqual(result["scope"], "standalone seasonal workload")
        self.assertEqual(result["report_windows"], self.report_windows())
        self.assertEqual([output["output_type"] for output in result["manager_outputs"]], [
            "ordered_financial_summary",
            "invoice_detail_first_page",
            "full_year_export_body",
        ])
        serialized = json.dumps(result)
        self.assertIn("canonical_json", serialized)
        forbidden_output_keys = {"customer", "customer_name", "items", "rows", "result", "body"}
        for output in result["manager_outputs"]:
            self.assertTrue(forbidden_output_keys.isdisjoint(output.keys()), output)
        self.assertTrue(all(len(output["sha256"]) == 64 for output in result["manager_outputs"]))

    def test_actor_and_report_window_gates_fail_closed(self):
        empty = events_bundle([], {"managers_enabled": True, "phases": []})
        self.assertEqual(evaluate_seasonal.actor_separation_gate(empty).status, "FAIL")
        self.assertEqual(evaluate_seasonal.report_window_gate(empty).status, "FAIL")
        config = {
            "managers_enabled": True,
            "actors": self.actor_config(),
            "report_windows": self.report_windows(),
        }
        config["actors"]["export_requester"]["user"] = "warehouse@test.invalid"
        duplicate = events_bundle([], config)
        self.assertEqual(evaluate_seasonal.actor_separation_gate(duplicate).status, "FAIL")

    def test_manager_output_interpretation_requires_group_by(self):
        digest = "a" * 64
        base = {
            "event": "manager_summary",
            "observation_id": "summary/0",
            "actor": "summary_warehouse",
            "actor_user": "warehouse@test.invalid",
            "window_scope": "year_to_date",
            "window_from_date": "2026-01-01",
            "window_to_date": "2026-11-26",
            "result_sha256": digest,
        }
        missing = events_bundle([dict(base)])
        gate = evaluate_seasonal.comparable_manager_output_gate(missing, True)
        self.assertEqual(gate.status, "FAIL")
        self.assertIn("ordered_financial_summary/Warehouse", gate.detail)
        grouped = events_bundle([dict(base, group_by="Warehouse")])
        outputs = evaluate_seasonal.collect_manager_outputs(grouped)
        self.assertEqual(outputs[0]["group_by"], "Warehouse")

    def test_margin_uses_nonintegral_stream_bounds_and_integer_shape_vectors(self):
        segment = schedule_test_segment("margin", 900, 5, 4)
        regular = segment["arrival_bounds"]["regular_iterations"]
        contention = segment["arrival_bounds"]["contention_iterations"]
        self.assertEqual((regular["nominal"], regular["minimum"], regular["maximum"]), (1650.0, 1650, 1651))
        self.assertEqual((contention["nominal"], contention["minimum"], contention["maximum"]), (18.75, 19, 19))
        vectors = []
        for regular_count in (regular["minimum"], regular["maximum"]):
            for contention_count in sorted({contention["minimum"], contention["maximum"]}):
                matrix = evaluate_seasonal.scheduled_arrival_matrix(segment, regular_count, contention_count)
                totals = evaluate_seasonal.matrix_shape_totals(matrix)
                self.assertTrue(all(isinstance(value, int) for value in totals.values()))
                vectors.append((sum(totals.values()), tuple(sorted(totals.items()))))
        self.assertEqual(len(set(vectors[0][1])), len(vectors[0][1]))
        shape_vectors = [dict(vector[1]) for vector in vectors]
        for totals in shape_vectors:
            self.assertEqual(totals[("sale", 50)], 300)
            self.assertEqual(totals[("sale", 200)], 75)
            self.assertIn(totals[("purchase_receipt", 50)], (93, 94))
            self.assertEqual(totals[("purchase_receipt", 50)], totals[("purchase_invoice", 50)])
        self.assertEqual(evaluate_seasonal.validate_template_structure(segment), [])

        compressed = dict(segment)
        compressed["voucher_template"] = [evaluate_seasonal.template_entry_parts(entry) for entry in segment["voucher_template"]]
        regular_count = regular["minimum"]
        contention_count = contention["minimum"]
        starts = scheduled_test_starts(compressed, regular_count, contention_count)
        events = events_bundle(starts + completions_for(starts), {"run_prefix": "K6S-test-", "phases": [compressed]})
        gate, _rows = evaluate_seasonal.offered_counts_gate(events, {"metrics": {}})
        self.assertEqual(gate.status, "PASS", gate)


    def test_canonical_json_replay_vector(self):
        value = {"b": [2, {"a": "x"}], "a": 1}
        self.assertEqual(evaluate_seasonal.canonical_json_text(value), '{"a":1,"b":[2,{"a":"x"}]}')
        self.assertEqual(
            evaluate_seasonal.canonical_json_sha256(value),
            "2c77e8e42231c5a2b199e37b8c1225614c241a3b2e7623a7ebd6f17815a4a738",
        )
        self.assertEqual(evaluate_seasonal.canonical_json_text({"n": 1.0}), '{"n":1}')


class SeasonalPlanNodeTests(unittest.TestCase):
    def context(self):
        distribution = ["Dist Hub 1", "Dist Hub 2"]
        retail = ["Shop 1", "Shop 2", "Shop 3", "Shop 4"]
        warehouses = [{"name": name, "role": "distribution"} for name in distribution] + [
            {"name": name, "role": "retail"} for name in retail
        ]
        items = [{"item_code": f"ITEM-{i:04d}", "item_name": f"Item {i}", "uom": "Nos", "rate": 20 + i} for i in range(1, 221)]
        in_stock = {}
        for name in distribution:
            in_stock[name.lower()] = {"warehouse": name, "items": [{"item_code": item["item_code"], "qty": 10000, "uom": "Nos"} for item in items[:200]]}
        for name in retail:
            in_stock[name.lower()] = {"warehouse": name, "items": [{"item_code": item["item_code"], "qty": 10000, "uom": "Nos"} for item in items[:50]]}
        return {
            "raw": {"seed_id": "offline-test"},
            "items": items,
            "warehouses": warehouses,
            "inStock": in_stock,
            "suppliers": ["Supplier 1"],
            "company": "Khitmee",
            "reportWindow": {"from_date": "2025-01-01", "to_date": "2025-12-31"},
            "defaultUom": "Nos",
            "defaultCustomer": "Walk-in Customer",
            "defaultPriceList": "Standard Selling",
            "defaultCurrency": "USD",
            "paymentMode": "Cash",
        }

    def test_plan_mix_and_exact_200_line_baskets(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("node is unavailable")
        module = (ROOT / "lib/seasonal_plan.js").read_bytes()
        plan_uri = "data:text/javascript;base64," + base64.b64encode(module).decode("ascii")
        script = f"""
globalThis.__ENV = {{SEASONAL_PROFILE:'smoke', RUN_ID:'offline-test', PERF_USER:'cashier@test.invalid',
  PERF_PASSWORD:'fixture', REPORT_TO_DATE:'2026-11-26', EXPORT_FROM_DATE:'2025-11-27'}};
for (const role of ['WAREHOUSE', 'MONTHLY', 'DETAIL', 'EXPORT']) {{
  __ENV['SEASONAL_MANAGER_' + role + '_USER'] = role.toLowerCase() + '@test.invalid';
  __ENV['SEASONAL_MANAGER_' + role + '_PASSWORD'] = 'fixture';
}}
const planModule = await import('{plan_uri}');
const context = {json.dumps(self.context())};
const plan = planModule.buildSeasonalPlan(__ENV, context);
const baseline = plan.segments[0];
const perMinute = {{}};
for (const entry of baseline.template) perMinute[entry.mixKey] = (perMinute[entry.mixKey] || 0) + 1;
const large = baseline.template.find((entry) => entry.mixKey === 'sale_large');
const lines = planModule.deterministicLines(context, plan.warehouses.distribution[0], 200, 'test-basket', {{distribution: true}});
console.log(JSON.stringify({{
  templateLength: baseline.template.length,
  perMinute,
  lines: lines.length,
  distinct: new Set(lines.map((line) => line.item_code)).size,
  rate88: baseline.regularRate.rate,
  publicConfig: planModule.publicPlanConfig(plan),
  retryClasses: ['sale_small', 'sale_large', 'transfer', 'purchase_receipt', 'purchase_invoice'].filter(
    mixKey => planModule.retryWitness({{segment: 'baseline', minute: 0, sequence: 0, mixKey}})),
  secondRetry: planModule.retryWitness({{segment: 'baseline', minute: 0, sequence: 1, mixKey: 'sale_small'}}),
}}));
"""
        result = subprocess.run([node, "--input-type=module", "-"], input=script,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["templateLength"], 88)
        self.assertEqual(payload["retryClasses"], ["sale_small", "transfer", "purchase_receipt", "purchase_invoice"])
        self.assertFalse(payload["secondRetry"])
        self.assertEqual(payload["rate88"], 88)
        self.assertEqual(payload["perMinute"]["sale_small"], 54)
        self.assertEqual(payload["perMinute"]["sale_medium"], 16)
        self.assertEqual(payload["perMinute"]["sale_large"], 4)
        self.assertEqual(payload["perMinute"]["transfer"], 9)
        self.assertEqual(payload["perMinute"]["purchase_cycle"], 5)
        self.assertEqual(payload["lines"], 200)
        self.assertEqual(payload["distinct"], 200)
        public_config = payload["publicConfig"]
        baseline_public = public_config["phases"][0]
        margin_public = public_config["phases"][1]
        self.assertEqual(evaluate_seasonal.validate_template_structure(margin_public), [])
        self.assertEqual(len(margin_public["voucher_template"]), 440)
        bounds = baseline_public["arrival_bounds"]
        self.assertEqual((bounds["regular_iterations"]["minimum"], bounds["regular_iterations"]["maximum"]), (176, 177))
        self.assertEqual((bounds["contention_iterations"]["minimum"], bounds["contention_iterations"]["maximum"]), (2, 3))
        matrix = evaluate_seasonal.scheduled_arrival_matrix(baseline_public, 176, 2)
        totals = evaluate_seasonal.matrix_shape_totals(matrix)
        self.assertEqual(totals[("sale", 10)], 120)
        self.assertEqual(totals[("sale", 50)], 32)
        self.assertEqual(totals[("sale", 200)], 8)
        self.assertEqual(totals[("transfer", 50)], 20)
        self.assertEqual(totals[("purchase_receipt", 50)], 10)
        self.assertEqual(totals[("purchase_invoice", 50)], 10)
        document_lines = sum(line_count * count for (_voucher_type, line_count), count in totals.items())
        self.assertEqual(document_lines, 6400)
        self.assertEqual(public_config["report_windows"]["year_to_date"]["from_date"], "2026-01-01")
        self.assertEqual(public_config["report_windows"]["full_year"]["from_date"], "2025-11-27")
        self.assertEqual(len({actor["user"] for actor in public_config["actors"].values()}), 5)
        self.assertNotIn("expected_vouchers", json.dumps(public_config))
        self.assertIsInstance(baseline_public["voucher_template"][0], list)
        self.assertNotIn("fixture", json.dumps(public_config))


    def test_contention_transfer_client_ids_are_unique_across_fractional_bursts(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("node is unavailable")
        module = (ROOT / "lib/seasonal_plan.js").read_bytes()
        plan_uri = "data:text/javascript;base64," + base64.b64encode(module).decode("ascii")
        script = f"""
const planModule = await import('{plan_uri}');
const settings = {{runId: 'offline-test', seed: 'seed'}};
const ids = [];
for (const sequence of [0, 1]) {{
  const work = {{
    segment: 'overload_step_1', mixKey: 'contention_transfer', minute: 2,
    sequence: planModule.contentionTransferSequence(sequence),
  }};
  ids.push(planModule.makeClientVoucherId(settings, work));
}}
const saleIds = [];
for (const sequence of [0, 1]) {{
  for (let shop = 0; shop < 6; shop += 1) {{
    saleIds.push(planModule.makeClientVoucherId(settings, {{
      segment: 'overload_step_1', mixKey: 'contention_sale', minute: 2,
      sequence: planModule.contentionSaleSequence(sequence, shop),
    }}));
  }}
}}
console.log(JSON.stringify({{ids, unique: new Set(ids).size, saleUnique: new Set(saleIds).size}}));
"""
        result = subprocess.run([node, "--input-type=module", "-"], input=script,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["unique"], 2, payload)
        self.assertEqual(payload["saleUnique"], 12, payload)
        self.assertIn("-s0", payload["ids"][0])
        self.assertIn("-s1", payload["ids"][1])



class SeasonalPurchaseRecoveryNodeTests(unittest.TestCase):
    def test_lookup_uses_client_identity_and_validation_requires_identical_linked_document(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("node is unavailable")
        module = (ROOT / "lib/purchase_recovery.js").read_bytes()
        module_uri = "data:text/javascript;base64," + base64.b64encode(module).decode("ascii")
        script = """
const recovery = await import(%s);
function built(type) {
  const clientVoucherId = 'K6S-seed-baseline-' + type + '-m0-s0';
  const childDoctype = type === 'purchase_receipt' ? 'Purchase Receipt Item' : 'Purchase Invoice Item';
  const doctype = type === 'purchase_receipt' ? 'Purchase Receipt' : 'Purchase Invoice';
  const row = {
    doctype: childDoctype,
    name: 'draft-item-1',
    parent: 'draft-parent',
    parenttype: doctype,
    parentfield: 'items',
    idx: 1,
    item_code: 'ITEM-0001',
    qty: 2,
    uom: 'Nos',
    warehouse: 'Dist Hub 1',
    rate: 14.25,
  };
  if (type === 'purchase_invoice') {
    row.purchase_receipt = 'PR-0001';
    row.pr_detail = 'PR-0001-item-1';
  }
  const doc = {
    doctype,
    name: 'draft-parent',
    docstatus: 0,
    supplier: 'Supplier 1',
    company: 'Khitmee',
    posting_date: '2026-11-27',
    set_warehouse: 'Dist Hub 1',
    currency: 'USD',
    conversion_rate: 1,
    remarks: 'Khitmee seasonal client_voucher_id=' + clientVoucherId,
    items: [row],
  };
  if (type === 'purchase_invoice') doc.bill_no = clientVoucherId;
  return {clientVoucherId, work: {type}, document: doc};
}
const receipt = built('purchase_receipt');
const invoice = built('purchase_invoice');
const observerOnlyName = 'PR-OBSERVER-ONLY';
const receiptSpec = recovery.purchaseLookupSpec(receipt);
const invoiceSpec = recovery.purchaseLookupSpec(invoice);
const receiptDoc = Object.assign({}, receipt.document, {name: 'PR-0001', docstatus: 1, __islocal: 0});
const invoiceDoc = Object.assign({}, invoice.document, {name: 'PI-0001', docstatus: 1, __islocal: 0});
const changedRate = JSON.parse(JSON.stringify(receiptDoc));
changedRate.items[0].rate = 14.26;
const badLink = JSON.parse(JSON.stringify(invoiceDoc));
badLink.items[0].pr_detail = 'different-row';
console.log(JSON.stringify({
  receiptSpec,
  invoiceSpec,
  specHasObserverName: JSON.stringify(receiptSpec).includes(observerOnlyName),
  validReceipt: recovery.validateRecoveredPurchaseDocument(receiptDoc, receipt).ok,
  validInvoice: recovery.validateRecoveredPurchaseDocument(invoiceDoc, invoice).ok,
  missingZeroRejected: !recovery.validateRecoveredPurchaseDocument(
    {...invoiceDoc, update_stock: null}, {...invoice, document: {...invoice.document, update_stock: 0}}).ok,
  changedRateRejected: !recovery.validateRecoveredPurchaseDocument(changedRate, receipt).ok,
  badLinkRejected: !recovery.validateRecoveredPurchaseDocument(badLink, invoice).ok,
  purchaseTimeoutResubmits: recovery.mayResubmitAfterSubmitResponse('purchase_receipt', null),
  purchase408Resubmits: recovery.mayResubmitAfterSubmitResponse('purchase_receipt', {status: 408, body: ''}),
  sale408Resubmits: recovery.mayResubmitAfterSubmitResponse('sale', {status: 408, body: ''}),
  transferServerErrorResubmits: recovery.mayResubmitAfterSubmitResponse('transfer', {status: 500, body: ''}),
}));
""" % (json.dumps(module_uri),)
        result = subprocess.run([node, "--input-type=module", "-"], input=script,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["specHasObserverName"])
        self.assertEqual(payload["receiptSpec"]["filters"], [[
            "Purchase Receipt", "remarks", "=", "Khitmee seasonal client_voucher_id=K6S-seed-baseline-purchase_receipt-m0-s0"
        ]])
        self.assertEqual(payload["invoiceSpec"]["filters"], [[
            "Purchase Invoice", "bill_no", "=", "K6S-seed-baseline-purchase_invoice-m0-s0"
        ]])
        self.assertEqual(payload["receiptSpec"]["limitPageLength"], 2)
        self.assertTrue(payload["validReceipt"], payload)
        self.assertTrue(payload["validInvoice"], payload)
        self.assertTrue(payload["missingZeroRejected"], payload)
        self.assertTrue(payload["changedRateRejected"], payload)
        self.assertTrue(payload["badLinkRejected"], payload)
        self.assertFalse(payload["purchaseTimeoutResubmits"], payload)
        self.assertFalse(payload["purchase408Resubmits"], payload)
        self.assertTrue(payload["sale408Resubmits"], payload)
        self.assertTrue(payload["transferServerErrorResubmits"], payload)


class SeasonalStaticContractTests(unittest.TestCase):
    def test_exact_open_arrival_schedule_uses_iteration_in_test(self):
        source = (ROOT / "scenarios/seasonal.js").read_text(encoding="utf-8")
        self.assertIn("iterationInTest", source)
        self.assertNotIn("scenario.progress", source)

    def test_purchase_timeout_recovery_is_read_only_and_never_posts_again(self):
        runtime = (ROOT / "lib/seasonal_runtime.js").read_text(encoding="utf-8")
        self.assertIn("recoverPurchaseDocument(session, settings, built, firstRecoveryListResponse)", runtime)
        self.assertIn("voucher_unknown", runtime)
        self.assertIn("resubmitted", runtime)
        self.assertIn("mayResubmitAfterSubmitResponse", runtime)
        recovery_start = runtime.index("function recoverPurchaseDocument")
        recovery_end = runtime.index("function resolvePurchaseVoucher", recovery_start)
        recovery_source = runtime[recovery_start:recovery_end]
        self.assertIn("getResource", recovery_source)
        self.assertNotIn("postMethod", recovery_source)
        self.assertNotIn("sendVoucherAttempt", recovery_source)
        self.assertNotIn("firstCommittedVoucher", recovery_source)
        purchase_start = runtime.index("function resolvePurchaseVoucher(session")
        purchase_end = runtime.index("function resolvePurchaseVoucherInFlight", purchase_start)
        purchase_source = runtime[purchase_start:purchase_end]
        self.assertEqual(purchase_source.count("sendVoucherAttempt"), 1)
        self.assertNotIn("while (", purchase_source)

    def test_retry_witnesses_are_started_while_first_request_is_in_flight(self):
        runtime = (ROOT / "lib/seasonal_runtime.js").read_text(encoding="utf-8")
        self.assertIn("function resolveDirectVoucherInFlight", runtime)
        self.assertIn("function resolvePurchaseVoucherInFlight", runtime)
        self.assertIn("duplicate_idempotent_submit_batch", runtime)
        self.assertIn("submit_plus_readonly_recovery_batch", runtime)
        self.assertIn("http.batch([batchEntry(firstRequest), batchEntry(retryRequest)])", runtime)
        self.assertIn("http.batch([batchEntry(submitRequest), batchEntry(listRequest)])", runtime)
        self.assertIn("event: 'voucher_retry_overlap'", runtime)
        self.assertIn("first_inflight: true", runtime)

    def test_no_invented_capabilities_or_server_reconciliation_contract(self):
        combined = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in ("scenarios/seasonal.js", "lib/seasonal_runtime.js", "lib/seasonal_plan.js", "lib/purchase_recovery.js")
        )
        for forbidden in (
            "seasonal.capabilities",
            "seasonal.reconcile_run",
            "submit_purchase_receipt",
            "submit_linked_purchase_invoice",
            "get_export_status",
            "runSeasonalReconciliation",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn("frappe.desk.form.save.savedocs", combined)
        self.assertIn("actor_user", combined)
        self.assertIn("window_scope", combined)
        self.assertIn("first_page_sha256", combined)
        self.assertIn("hash_canonicalization", combined)
        self.assertIn("khitmee_ops.profit_summary.export_service.enqueue_profit_csv_export", combined)
        self.assertIn("khitmee_ops.profit_summary.export_service.download_profit_csv_export", combined)

    def test_contention_transfer_preserves_burst_sequence_in_runtime(self):
        runtime = (ROOT / "lib/seasonal_runtime.js").read_text(encoding="utf-8")
        self.assertIn("sequence: contentionTransferSequence(work.sequence)", runtime)
        self.assertNotIn("sequence: 0,\n    contention: true,\n  });\n  const builtTransfer", runtime)

    def test_detail_pagination_uses_candidate_invoice_page_filter(self):
        runtime = (ROOT / "lib/seasonal_runtime.js").read_text(encoding="utf-8")
        start = runtime.index("export function performDetailReport")
        end = runtime.index("function extractExportLogName", start)
        detail = runtime[start:end]
        self.assertIn("invoice_page: page + 1", detail)
        self.assertNotIn("start: page * 50", detail)
        self.assertNotIn("page_length: 50", detail)

    def test_request_observability_headers_and_endpoint_classes_are_emitted(self):
        auth = (ROOT / "lib/auth.js").read_text(encoding="utf-8")
        runtime = (ROOT / "lib/seasonal_runtime.js").read_text(encoding="utf-8")
        self.assertIn("X-K6-Request-Id", auth)
        self.assertIn("X-K6-Endpoint-Class", auth)
        self.assertIn("observabilityHeaders(options)", auth)
        self.assertIn("endpointClass: 'manager_detail'", runtime)
        self.assertIn("endpointClass = 'manager_summary'", runtime)
        self.assertIn("verifyUnauthorizedDetailDenied", runtime)

    def test_preflight_covers_every_idle_request_type_and_negative_authorization(self):
        source = (ROOT / "scenarios/seasonal_preflight.js").read_text(encoding="utf-8")
        for token in (
            "sale", "transfer", "purchase_receipt", "purchase_invoice", "search",
            "warm_summary_warehouse", "warm_summary_monthly", "detail_page_1_and_deep_page",
            "export_enqueue_status_download", "unauthorized_detail_denied",
        ):
            self.assertIn(token, source)
        self.assertIn("detailPageCount: Math.max(10", source)
        self.assertIn("errors: ['count==0']", source)
        self.assertIn("preflight_result", source)

    def test_runner_wires_independent_verifier_and_feature_scorecard(self):
        source = (ROOT / "run_k6.sh").read_text(encoding="utf-8")
        self.assertIn("perf_root/seasonal/verify_run.py".replace("perf_root", "$PERF_ROOT"), source)
        self.assertIn("evaluate_seasonal.py", source)
        self.assertIn("evaluate_preflight.py", source)
        self.assertIn("attribution.py", source)
        self.assertIn("--attribution", source)
        self.assertIn("--k6-log \"$K6_LOG\"", source)
        self.assertIn("SEASONAL_ENABLE_MANAGERS", source)
        verifier_start = source.index('"$PYTHON_BIN" "$PERF_ROOT/seasonal/verify_run.py"')
        verifier_end = source.index('VERIFY_STATUS=$?', verifier_start)
        invocation_lines = [line.rstrip() for line in source[verifier_start:verifier_end].splitlines() if line.strip()]
        for line in invocation_lines[:-1]:
            self.assertTrue(line.endswith(chr(92)), f'missing shell line continuation: {line}')


if __name__ == "__main__":
    unittest.main()
