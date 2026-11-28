"""Acceptance tests for the cross-checkout receipts ledger."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoloop.receipts import Broker, ReceiptError, authority_dir, checkout_repo_id
from autoloop.routing import record_attempt, routing_report

BEFORE = {"base": "base", "head": "before", "tree": "tree-before"}
AFTER = {"base": "base", "head": "after", "tree": "tree-after"}


def reservation(generation: int = 1, **changes):
    return {
        "run": "run", "shard": "shard", "role": "implementation", "generation": generation,
        "request_id": f"request-{generation}", "request_digest": "request-sha", "brief_digest": "brief-sha",
        "task_class": "implementation", "difficulty": "medium", "contract_revision": 1,
        "contract_hash": "contract-sha", "policy": "incumbent", "epoch": "epoch-1",
        "git": BEFORE, "provider": "provider", "model": "model", "settings": {"effort": "low"},
        "limits": {"tokens": 100}, "expires_at": time.time() + 120,
        **changes,
    }


def result(cost: float, **changes):
    return {
        "status": "completed", "disposition": "accepted", "duration_s": 10,
        "usage": {"source": "provider", "cost": cost}, "git_before": BEFORE, "git_after": AFTER,
        "changed_paths": ["tools/example.py"], "evidence_ids": ["gate-id"], "publish": True,
        **changes,
    }


WORKER = """
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from autoloop.receipts import Broker, ReceiptError, checkout_repo_id
from autoloop.receipts.test_broker import reservation

authority, mode, label = sys.argv[1:]
authority = Path(authority)
with (patch("autoloop.receipts._is_wsl", return_value=False),
      patch("autoloop.leases._is_wsl", return_value=False),
      patch("autoloop.leases._windows_program_data", return_value=authority),
      patch("autoloop.leases._LINUX_RESOURCE_LOCK_DIR", authority / "autoloop")):
    broker = Broker(checkout_repo_id(Path.cwd()))
if mode == "race":
    (authority / label).touch()
    deadline = time.monotonic() + 10
    while not (authority / "go").exists():
        if time.monotonic() >= deadline:
            sys.exit("race did not start")
        time.sleep(0.01)
try:
    record = broker.reserve(**reservation(request_id=label))
except ReceiptError:
    if mode == "race":
        sys.exit(17)
    raise
if mode in ("dispatching", "attached"):
    broker.begin_dispatch(record["reservation_id"])
    # Stand in for T3 persisting the request before its response reaches the coordinator.
    (authority / f"{label}.t3").write_text("t3-recovered", encoding="utf-8")
if mode == "attached":
    broker.attach(record["reservation_id"], "t3-recovered")
if mode != "race":
    os._exit(23)
"""


class BrokerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.host = self.root / "host"
        self.host.mkdir()
        with (
            patch("autoloop.receipts._is_wsl", return_value=False),
            patch("autoloop.leases._is_wsl", return_value=False),
            patch("autoloop.leases._windows_program_data", return_value=self.host),
            patch("autoloop.leases._LINUX_RESOURCE_LOCK_DIR", self.host / "autoloop"),
        ):
            self.authority = authority_dir()
        self.assertEqual(self.authority, self.host / "autoloop" / "receipts")
        source = self.root / "upstream"
        subprocess.run(["git", "init", "--bare", "-q", str(source)], check=True)
        self.repo = self.clone("first", source)
        self.repo_id = checkout_repo_id(self.repo)
        self.assertIsNotNone(self.repo_id)
        self.broker = Broker(self.repo_id, _root=self.authority)

    def clone(self, name, source):
        checkout = self.root / name
        subprocess.run(["git", "clone", "-q", str(source), str(checkout)], check=True, capture_output=True)
        return checkout

    def child(self, checkout, mode, request_id):
        tools_path = str(Path(__file__).resolve().parents[2])
        env = {**os.environ, "PYTHONPATH": tools_path}
        with subprocess.Popen(
            [sys.executable, "-B", "-c", WORKER, str(self.host), mode, request_id],
            cwd=checkout, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as child:
            try:
                return child.wait(timeout=25), child.communicate()[1]
            finally:
                if child.poll() is None:
                    child.kill()

    def attached(self, generation=1, **changes):
        record = self.broker.reserve(**reservation(generation, **changes))
        rid = record["reservation_id"]
        self.broker.begin_dispatch(rid)
        self.broker.attach(rid, f"task-{generation}")
        self.broker.attach(rid, f"task-{generation}")
        return rid

    def test_two_clones_race_for_one_generation(self):
        second = self.clone("second", self.root / "upstream")
        self.assertEqual(checkout_repo_id(second), self.repo_id)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(self.child, repo, "race", label)
                       for repo, label in ((self.repo, "a"), (second, "b"))]
            deadline = time.monotonic() + 8
            while not all((self.host / name).exists() for name in ("a", "b")):
                if time.monotonic() >= deadline:
                    self.fail("both child processes did not reach the dispatch race")
                time.sleep(0.01)
            (self.host / "go").touch()
            results = [future.result() for future in futures]
        self.assertEqual(sorted(code for code, _ in results), [0, 17], results)
        records = [self.broker.by_request(label) for label in ("a", "b")]
        self.assertEqual(sum(record is not None for record in records), 1)

    def test_github_origin_identity_matches_across_protocols(self):
        second = self.clone("second", self.root / "upstream")
        for checkout, url in (
            (self.repo, "https://GitHub.com/Owner/Project.git"),
            (second, "git@github.com:owner/project.git"),
        ):
            subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", url], check=True)
            self.assertEqual(checkout_repo_id(checkout), "github.com/owner/project")

    def test_crash_before_send_is_not_run_and_evidence_is_retained(self):
        self.assertEqual(self.child(self.repo, "reserved", "never-sent"), (23, ""))
        entry = self.broker.by_request("never-sent")
        rid = entry["record"]["reservation_id"]
        self.assertEqual(self.broker.reconcile(rid), "not_run")
        self.assertEqual(self.broker.reconcile(rid), "not_run")
        [(record, receipt)] = self.broker.settled()
        self.assertEqual(record, entry["record"])
        self.assertEqual(receipt["status"], "not_run")
        self.assertEqual(receipt["usage"]["source"], "not_observed")
        self.assertIsNone(receipt["usage"]["cost"])
        self.assertIsNone(self.broker.current_publication("run", "shard", "implementation"))

    def test_crash_with_uncertain_dispatch_blocks_replacement_until_t3_recovery(self):
        self.assertEqual(self.child(self.repo, "dispatching", "lost-reply"), (23, ""))
        rid = self.broker.by_request("lost-reply")["record"]["reservation_id"]
        self.assertEqual(self.broker.reconcile(rid), "quarantined")
        self.assertEqual(self.broker.settled(), [])
        with self.assertRaisesRegex(ReceiptError, "uncertain dispatch"):
            self.broker.reserve(**reservation(2))
        task_id = (self.host / "lost-reply.t3").read_text(encoding="utf-8")
        self.broker.attach(rid, task_id)
        self.assertEqual(self.broker.reconcile(rid), "attached")
        self.assertEqual(self.broker.settle(rid, **result(3))["task_id"], "t3-recovered")

    def test_t3_absence_evidence_releases_quarantine(self):
        record = self.broker.reserve(**reservation())
        rid = record["reservation_id"]
        self.broker.begin_dispatch(rid)
        self.assertEqual(self.broker.reconcile(rid), "quarantined")
        self.assertEqual(self.broker.reconcile(rid, no_dispatch_evidence_id="lookup-1"), "not_run")
        self.assertEqual(self.broker.settled()[0][1]["evidence_ids"], ["lookup-1"])
        self.assertEqual(self.broker.reserve(**reservation(2))["generation"], 2)
        with self.assertRaises(ReceiptError):
            self.broker.attach(rid, "unexpected")

    def test_crash_after_attach_preserves_confirmed_task(self):
        self.assertEqual(self.child(self.repo, "attached", "attached-task"), (23, ""))
        entry = self.broker.by_request("attached-task")
        rid = entry["record"]["reservation_id"]
        self.assertEqual(self.broker.reconcile(rid), "attached")
        self.assertEqual(self.broker.settle(rid, **result(2))["task_id"], "t3-recovered")

    def test_late_result_is_immutable_spend_and_never_displaces_current(self):
        old = self.attached()
        current = self.attached(2)
        current_result = result(3.5, worker={"provider": "actual-provider", "model": "actual-model"})
        published = self.broker.settle(current, **current_result)
        late = self.broker.settle(old, **result(1.5))
        self.assertEqual(late["publication"], "superseded")
        self.assertEqual(published["publication"], "published")
        self.assertEqual(self.broker.settle(current, **current_result), published)
        with self.assertRaisesRegex(ReceiptError, "different settlement"):
            self.broker.settle(current, **result(7))
        self.assertEqual(self.broker.current_publication("run", "shard", "implementation"), published)
        self.assertEqual({receipt["publication"] for _, receipt in self.broker.settled()}, {"published", "superseded"})
        self.assertEqual(sum(receipt["usage"]["cost"] for _, receipt in self.broker.settled()), 5)
        for _, receipt in self.broker.settled():
            payload = {key: value for key, value in receipt.items() if key != "receipt_id"}
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), receipt["receipt_id"])

    def test_revoked_expired_and_stale_results_keep_spend(self):
        revoked = self.attached()
        self.broker.revoke(revoked, "contract changed")
        self.assertEqual(self.broker.settle(revoked, **result(2))["publication"], "revoked")
        expires = time.time() + 120
        expired = self.attached(2, expires_at=expires)
        with patch("autoloop.receipts.time.time", return_value=expires):
            self.assertEqual(self.broker.settle(expired, **result(3))["publication"], "expired")
        stale = self.attached(3)
        self.assertEqual(
            self.broker.settle(stale, **result(4, git_before={**BEFORE, "tree": "stale"}))["publication"],
            "stale",
        )
        self.assertIsNone(self.broker.current_publication("run", "shard", "implementation"))
        self.assertEqual(sum(receipt["usage"]["cost"] for _, receipt in self.broker.settled()), 9)

    def test_publication_reads_expiry_after_successful_settlement(self):
        expires = time.time() + 120
        rid = self.attached(expires_at=expires)
        receipt = self.broker.settle(rid, **result(2))
        self.assertEqual(self.broker.current_publication("run", "shard", "implementation"), receipt)
        with patch("autoloop.receipts.time.time", return_value=expires):
            self.assertIsNone(self.broker.current_publication("run", "shard", "implementation"))
        self.assertEqual(self.broker.settled()[0][1], receipt)

    def local_execution(self, key, generation=1, **changes):
        record = self.broker.reserve(**reservation(
            generation, role="check-execution", request_id=f"{key}-{generation}", cache_key_digest=key, **changes,
        ))
        started = time.monotonic()
        check = subprocess.run([sys.executable, "-B", "-c", "print('check output'); raise SystemExit(1)"],
                               capture_output=True, check=False)
        outcome = {
            "exit_code": check.returncode, "output_digest": hashlib.sha256(check.stdout).hexdigest(),
            "evidence_id": "gate-\u00e9",
            "evidence_digest": "evidence-sha", "completed_at": int(time.time()),
        }
        rid = record["reservation_id"]
        receipt = self.broker.settle_execution(
            rid, duration_s=time.monotonic() - started, git_before=BEFORE, git_after=BEFORE, **outcome,
        )
        digest = hashlib.sha256(json.dumps(
            outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return rid, receipt, dict(cache_key_digest=key, outcome_digest=digest, **outcome)

    def test_local_execution_without_t3_attests_exact_outcome(self):
        rid, receipt, fields = self.local_execution("key-a")
        receipt_id = receipt["receipt_id"]
        self.assertIsNone(receipt["task_id"])
        self.assertEqual(receipt["outcome_digest"], fields["outcome_digest"])
        self.assertTrue(self.broker.validate_execution_receipt(receipt_id, SimpleNamespace(**fields)))
        for field, mismatch in {
            "exit_code": 0, "output_digest": "other", "cache_key_digest": "key-b", "outcome_digest": "wrong",
            "evidence_id": "other", "evidence_digest": "wrong", "completed_at": fields["completed_at"] - 1,
        }.items():
            with self.subTest(field=field):
                attestation = SimpleNamespace(**{**fields, field: mismatch})
                self.assertFalse(self.broker.validate_execution_receipt(receipt_id, attestation))
        self.assertFalse(self.broker.validate_execution_receipt("missing", SimpleNamespace(**fields)))
        self.broker.revoke(rid, "evidence withdrawn")
        self.assertFalse(self.broker.validate_execution_receipt(receipt_id, SimpleNamespace(**fields)))
        self.assertIsNone(self.broker.current_publication("run", "shard", "check-execution", cache_key_digest="key-a"))

    def test_local_check_keys_have_independent_generations_and_expiry(self):
        expires = time.time() + 120
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.local_execution, "key-a", expires_at=expires)
            second = workers.submit(self.local_execution, "key-b", expires_at=expires + 120)
            _, old, old_fields = first.result()
            _, other, other_fields = second.result()
        self.assertTrue(self.broker.validate_execution_receipt(old["receipt_id"], SimpleNamespace(**old_fields)))
        self.assertTrue(self.broker.validate_execution_receipt(other["receipt_id"], SimpleNamespace(**other_fields)))
        _, fresh, fresh_fields = self.local_execution("key-a", generation=2, expires_at=expires)
        self.assertFalse(self.broker.validate_execution_receipt(old["receipt_id"], SimpleNamespace(**old_fields)))
        self.assertTrue(self.broker.validate_execution_receipt(fresh["receipt_id"], SimpleNamespace(**fresh_fields)))
        with patch("autoloop.receipts.time.time", return_value=expires):
            self.assertFalse(self.broker.validate_execution_receipt(fresh["receipt_id"], SimpleNamespace(**fresh_fields)))
            self.assertTrue(self.broker.validate_execution_receipt(other["receipt_id"], SimpleNamespace(**other_fields)))
            self.assertIsNone(self.broker.current_publication(
                "run", "shard", "check-execution", cache_key_digest="key-a",
            ))

    def test_routing_reads_receipts_with_or_without_the_legacy_log(self):
        old = self.attached()
        current = self.attached(2, model="retry")
        self.broker.settle(old, **result(1.5))
        self.broker.settle(current, **result(3.5))
        estimate = self.attached(run="other", request_id="estimate")
        self.broker.settle(estimate, **result(0, usage={"source": "estimated", "cost": 4}))
        self.local_execution("check-key")
        log = self.root / "attempts.jsonl"
        with (patch("autoloop.routing.authority_dir", return_value=self.authority),
              patch("autoloop.routing._is_wsl", return_value=False)):
            rows = {row["model"]: row for row in routing_report(self.repo, log)["routes"]}
            self.assertEqual(rows["model"]["spend"], 1.5)
            self.assertEqual(rows["model"]["estimated_spend"], 4)
            self.assertEqual(rows["model"]["first_attempt_acceptance"], 0.5)
            self.assertEqual(rows["model"]["eventual_acceptance"], 1)
            self.assertEqual(rows["retry"]["spend"], 3.5)
            self.assertFalse(log.exists())
            record_attempt(self.repo, log, task_id="manual", attempt=1, task_class="implementation",
                           model="model", disposition="accepted", checks_passed=True, cost=2)
            line = log.read_text(encoding="utf-8")
            log.write_text(line + line, encoding="utf-8")
            rows = {row["model"]: row for row in routing_report(self.repo, log)["routes"]}
            self.assertEqual(rows["model"]["spend"], 3.5)
            self.assertEqual(rows["model"]["attempts"], 3)
            self.assertEqual(rows["model"]["first_attempt_acceptance"], 2 / 3)
            self.assertEqual(rows["model"]["eventual_acceptance"], 1)
            self.assertEqual(log.read_text(encoding="utf-8"), line + line)

    def test_authority_rejects_wsl_fallback_and_worktree_local_roots(self):
        with (patch("autoloop.receipts._is_wsl", return_value=True),
              self.assertRaisesRegex(ReceiptError, "Windows Python")):
            Broker(self.repo_id)
        with self.assertRaisesRegex(ReceiptError, "outside Git worktrees"):
            Broker(self.repo_id, _root=self.repo / "ledger")
