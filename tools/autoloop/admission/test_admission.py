"""Offline replay of the ADR 0005 admission acceptance cases."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from .. import leases
from . import AdmissionError, Controller, EnforcedBounds, Usage


NOW = 1_800_000_000
REPO = "github.com/example/erp"
LIMIT = Usage(100, 100, 10_000)
CAP = Usage(10, 10, 1_000)
BOUNDS = EnforcedBounds(CAP, "shadow-token-limit", "shadow-fixed-price", "shadow-deadline", NOW + 90)


def _race_for_budget(root: str, run: str) -> None:
    controller = Controller(REPO, _lock_dir=Path(root))
    with patch.object(leases.time, "time", return_value=NOW):
        print("ready", flush=True)
        if input() != "start":
            raise RuntimeError("reservation race did not start")
        result = controller.reserve(
            run=run, shard="a", role="worker", generation=1, request_id=run, bounds=BOUNDS, expires_at=NOW + 60,
        )
        if result.reservation_id:
            result = controller.begin_dispatch(result.reservation_id)
        print(json.dumps([result.status, result.reason]))


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp) / "locks"
        self.clock = self.enterContext(patch.object(leases.time, "time", return_value=NOW))
        self.controller = Controller(REPO, _lock_dir=self.root)

    def configure(self, *, dag=None, resources=None, run_limit=LIMIT, campaign_limit=LIMIT, **limits) -> None:
        self.controller.register_campaign("campaign", campaign_limit, expires_at=NOW + 100)
        self.controller.register_run(
            "run", "campaign", run_limit, expires_at=NOW + 100, dag=dag or {"a": ()}, resources=resources, **limits,
        )

    def reserve(self, shard="a", generation=1, bounds=BOUNDS, *, role="worker"):
        return self.controller.reserve(
            run="run", shard=shard, role=role, generation=generation,
            request_id=f"{shard}-{generation}" if role == "worker" else f"{shard}-{role}-{generation}",
            bounds=bounds, expires_at=NOW + 60,
        )

    def start(self, shard="a", generation=1, *, role="worker"):
        reservation = self.reserve(shard, generation, role=role)
        self.assertEqual(reservation.status, "reserved", reservation)
        rid = reservation.reservation_id
        self.assertEqual(self.controller.begin_dispatch(rid).status, "dispatching")
        self.assertEqual(self.controller.attach(rid, f"task-{shard}-{generation}").status, "attached")
        return rid

    def test_replays_dag_and_debits_retries(self) -> None:
        self.configure(dag={"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})
        self.assertEqual(self.reserve("d").reason, "dependencies")
        first = self.start()
        self.controller.settle(first, outcome="failed", actual=Usage(3, 2, 100))
        self.assertEqual(self.reserve("b").status, "waiting")
        retry = self.start(generation=2)
        self.controller.settle(retry, outcome="accepted", actual=Usage(2, 2, 100))
        b = self.start("b")
        c = self.start("c")
        self.controller.settle(c, outcome="accepted", actual=Usage(2, 2, 100))
        self.assertEqual(self.reserve("d").status, "waiting")
        self.controller.settle(b, outcome="accepted", actual=Usage(2, 2, 100))
        self.controller.settle(self.start("d"), outcome="accepted", actual=Usage(2, 2, 100))
        entries = list(self.controller.snapshot()["reservations"].values())
        self.assertEqual(len(entries), 5)
        self.assertEqual(sum(entry["strike"] for entry in entries), 1)
        self.assertEqual(sum(entry["charge"]["tokens"] for entry in entries), 11)

    def test_reviewer_generations_start_at_one_after_implementation_passes(self) -> None:
        self.configure(dag={"a": (), "b": ("a",)}, max_worker_strikes=2)
        impl = self.start(role="implementation")
        self.controller.settle(impl, outcome="accepted", actual=Usage(2, 2, 100))
        review = self.start(role="review")
        self.controller.settle(review, outcome="failed", actual=Usage(2, 2, 100))
        retry = self.start(generation=2, role="review")
        self.assertEqual(self.reserve(role="deep-review").reason, "strike_reserved")
        self.controller.settle(retry, outcome="accepted", actual=Usage(2, 2, 100))
        self.assertEqual(self.reserve(generation=3, role="review").reason, "role_passed")
        self.assertEqual(self.reserve(generation=2, role="implementation").reason, "shard_passed")
        self.assertEqual(self.reserve("b").status, "reserved")

    def test_review_and_dependent_shards_wait_for_implementation_acceptance(self) -> None:
        self.configure(dag={"a": (), "b": ("a",)})
        self.assertEqual(self.reserve(role="reviewer").reason, "dependencies")
        failed = self.start(role="implementer")
        self.controller.settle(failed, outcome="failed", actual=Usage(2, 2, 100))
        self.assertEqual(self.reserve(role="reviewer").reason, "dependencies")
        self.assertEqual(self.reserve("b").reason, "dependencies")
        impl = self.start(generation=2, role="implementer")
        self.controller.settle(impl, outcome="accepted", actual=Usage(2, 2, 100))
        self.assertEqual(self.reserve(role="reviewer").status, "reserved")
        self.assertEqual(self.reserve("b").status, "reserved")

    def test_settle_unsent_reservation_frees_budget_and_lease(self) -> None:
        self.configure(resources={"a": ("host-timing",)}, campaign_limit=CAP)
        rid = self.reserve().reservation_id
        result = self.controller.settle(rid, outcome="not_run")
        self.assertEqual(result.status, "settled")
        self.assertEqual(self.controller.settle(rid, outcome="not_run"), result)
        self.assertEqual(self.controller.begin_dispatch(rid).status, "denied")
        record = self.controller.snapshot()["reservations"][rid]
        self.assertEqual(record["dispatch"], "not_run")
        self.assertEqual(record["settlement"]["outcome"], "not_run")
        self.assertEqual(record["charge"], asdict(Usage(0, 0, 0)))
        self.assertFalse(record["strike"])
        self.assertIsNone(leases.read_lease("host-timing", lock_dir=self.root))
        next_reservation = self.reserve(generation=2)
        self.assertEqual(next_reservation.status, "reserved")
        self.assertEqual(self.controller.begin_dispatch(next_reservation.reservation_id).status, "dispatching")
        with self.assertRaisesRegex(AdmissionError, "requires evidence"):
            self.controller.settle(next_reservation.reservation_id, outcome="not_run")
        self.assertIsNotNone(leases.read_lease("host-timing", lock_dir=self.root))

    def test_only_one_of_release_and_dispatch_can_win(self) -> None:
        self.configure(campaign_limit=CAP)
        rid = self.reserve().reservation_id
        barrier = Barrier(2, timeout=5)

        def release():
            contender = Controller(REPO, _lock_dir=self.root)
            barrier.wait()
            try:
                return contender.settle(rid, outcome="not_run").status
            except AdmissionError as exc:
                self.assertIn("requires evidence", str(exc))
                return "denied"

        def dispatch():
            contender = Controller(REPO, _lock_dir=self.root)
            barrier.wait()
            return contender.begin_dispatch(rid).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            released = pool.submit(release)
            dispatched = pool.submit(dispatch)
        result = (released.result(), dispatched.result())
        self.assertIn(result, {("settled", "denied"), ("denied", "dispatching")})
        record = self.controller.snapshot()["reservations"][rid]
        if result[0] == "settled":
            self.assertEqual(record["dispatch"], "not_run")
            self.assertEqual(record["charge"], asdict(Usage(0, 0, 0)))
        else:
            self.assertEqual(record["dispatch"], "dispatching")
            self.assertIsNone(record["charge"])

    def test_two_processes_share_campaign_cap_across_runs(self) -> None:
        self.configure(campaign_limit=CAP)
        self.controller.register_run("other", "campaign", LIMIT, expires_at=NOW + 100, dag={"a": ()})
        code = "import sys; from autoloop.admission.test_admission import _race_for_budget; _race_for_budget(*sys.argv[1:])"
        workers = []
        outcomes = []
        try:
            for run in ("run", "other"):
                workers.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", code, str(self.root), run],
                    cwd=Path(__file__).resolve().parents[2],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            for worker in workers:
                self.assertEqual(worker.stdout.readline().strip(), "ready")
            for worker in workers:
                worker.stdin.write("start\n")
                worker.stdin.flush()
            for worker in workers:
                output, stderr = worker.communicate(timeout=15)
                self.assertEqual(worker.returncode, 0, stderr)
                outcomes.append(json.loads(output))
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate(timeout=5)
        self.assertCountEqual(outcomes, [["dispatching", None], ["waiting", "budget_reserved"]])
        entries = list(self.controller.snapshot()["reservations"].values())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["limits"], asdict(CAP))

    def test_wsl_and_windows_roots_share_legacy_lease(self) -> None:
        alias = self.root.parent / "interop"
        alias.mkdir()
        wsl_root = alias / ".." / "locks"
        self.configure(resources={"a": ("host-timing",)})
        with patch.object(leases, "_pid_namespace", return_value="wsl:simulated"):
            held = leases.acquire_lease("host-timing", "wsl-holder", lock_dir=wsl_root)
        windows = Controller(REPO, _lock_dir=self.root)
        self.assertEqual(Controller(REPO, _lock_dir=wsl_root).path, windows.path)
        self.assertEqual(self.reserve().reason, "resource_lease")
        self.assertEqual(windows.snapshot()["reservations"], {})
        leases.release_lease("host-timing", "wsl-holder", token=held["token"], lock_dir=wsl_root)
        rid = self.start()
        with self.assertRaises(leases.LeaseConflictError):
            leases.acquire_lease("host-timing", "other-holder", lock_dir=wsl_root)
        self.controller.settle(rid, outcome="accepted", actual=Usage(2, 2, 100))
        self.assertIsNone(leases.read_lease("host-timing", lock_dir=wsl_root))
        self.assertFalse(windows.snapshot()["reservations"][rid]["strike"])

    def test_quota_expires_before_dispatch_and_across_campaign(self) -> None:
        self.configure(dag={"a": (), "b": ()})
        quota = replace(BOUNDS, expires_at=NOW + 3)
        rid = self.reserve(bounds=quota).reservation_id
        self.clock.return_value = NOW + 2
        self.assertEqual(self.controller.begin_dispatch(rid).reason, "quota_expired")
        entry = self.controller.snapshot()["reservations"][rid]
        self.assertEqual(entry["dispatch"], "not_run")
        self.assertEqual(entry["charge"], asdict(Usage(0, 0, 0)))
        self.clock.return_value = NOW + 100
        self.assertEqual(self.reserve("b").reason, "quota_expired")
        self.assertEqual(self.controller.snapshot()["campaigns"]["campaign"]["terminal_reason"], "quota_expired")

    def test_kill_during_spawn_fences_late_attach_and_preserves_spend(self) -> None:
        self.configure(dag={"a": (), "b": (), "c": ()}, resources={"a": ("host-timing",), "b": ("perf-stack",)})
        rid = self.reserve().reservation_id
        queued = self.reserve("b").reservation_id
        self.assertEqual(self.controller.begin_dispatch(rid).status, "dispatching")
        self.controller.observe(rid, Usage(4, 3, 100))
        Controller(REPO, _lock_dir=self.root).stop("killed", run="run")
        self.assertEqual(self.reserve("c").reason, "killed")
        self.assertEqual(self.controller.begin_dispatch(queued).reason, "killed")
        self.assertIsNone(leases.read_lease("perf-stack", lock_dir=self.root))
        self.assertIsNotNone(leases.read_lease("host-timing", lock_dir=self.root))
        entry = self.controller.snapshot()["reservations"][rid]
        self.assertEqual(entry["dispatch"], "dispatching")
        self.assertEqual(entry["request_id"], "a-1")
        self.assertEqual(entry["charge"], asdict(CAP))
        self.assertEqual(entry["observed"], asdict(Usage(4, 3, 100)))
        self.assertEqual(self.controller.attach(rid, "late-task").status, "cancel")
        self.controller.settle(rid, outcome="accepted", actual=Usage(5, 4, 500))
        entry = self.controller.snapshot()["reservations"][rid]
        self.assertEqual(entry["terminal_reason"], "killed")
        self.assertFalse(entry["accepted"])
        self.assertFalse(entry["strike"])
        self.assertEqual(entry["charge"], asdict(Usage(5, 4, 500)))
        self.assertIsNone(leases.read_lease("host-timing", lock_dir=self.root))

    def test_each_cap_and_bounds_are_independently_required(self) -> None:
        self.configure(run_limit=CAP, dag={"a": (), "b": ()})
        for larger in (Usage(11, 1, 1), Usage(1, 11, 1), Usage(1, 1, 1001)):
            with self.subTest(limits=larger):
                self.assertEqual(self.reserve(bounds=replace(BOUNDS, limits=larger)).reason, "budget_limit")
        for bounds in (
            replace(BOUNDS, token_ceiling=None),
            replace(BOUNDS, token_ceiling=BOUNDS.wall_ceiling),
            replace(BOUNDS, cost_ceiling=None),
            replace(BOUNDS, wall_ceiling=None),
        ):
            self.assertEqual(self.reserve(bounds=bounds).reason, "unenforceable_bounds")
        self.assertEqual(self.controller.snapshot()["reservations"], {})
        rid = self.start()
        self.assertEqual(self.reserve("b").reason, "budget_reserved")
        self.controller.settle(rid, outcome="accepted", actual=CAP)
        self.assertEqual(self.reserve("b").reason, "exhausted")

    def test_stall_unknown_spawn_and_proven_no_dispatch(self) -> None:
        self.configure(dag={"a": (), "b": ()})
        rid = self.reserve().reservation_id
        self.controller.begin_dispatch(rid)
        with self.assertRaises(AdmissionError):
            self.controller.settle(rid, outcome="accepted")
        self.clock.return_value = NOW + 1
        self.assertEqual(self.reserve("b").reason, "stalled")
        self.assertEqual(self.controller.snapshot()["reservations"][rid]["charge"], asdict(CAP))
        self.controller.settle(rid, outcome="not_run", no_dispatch_evidence_id="trusted-t3-lookup")
        entry = self.controller.snapshot()["reservations"][rid]
        self.assertEqual(entry["terminal_reason"], "stalled")
        self.assertEqual(entry["charge"], asdict(Usage(0, 0, 0)))

    def test_failure_limits_cannot_be_bypassed_by_retries_or_replan(self) -> None:
        self.configure(dag={"a": (), "b": ()}, max_worker_strikes=1, max_total_attempts=2)
        first = self.start()
        self.controller.settle(first, outcome="failed", actual=Usage(1, 1, 10))
        self.assertEqual(self.reserve(generation=2).reason, "strike_limit")
        self.controller.replan("run")
        second = self.start(generation=2)
        self.controller.settle(second, outcome="failed", actual=Usage(1, 1, 10))
        self.assertEqual(self.reserve(generation=3).reason, "strike_limit")
        self.assertEqual(self.reserve("b").reason, "attempt_limit")
        with self.assertRaises(AdmissionError):
            self.controller.replan("run")

    def test_multi_resource_wait_releases_partial_acquisition(self) -> None:
        self.configure(resources={"a": ("host-timing", "perf-stack")}, campaign_limit=CAP)
        other = leases.acquire_lease("perf-stack", "other", lock_dir=self.root)
        self.assertEqual(self.reserve().reason, "resource_lease")
        self.assertEqual(self.controller.snapshot()["requests"], {})
        self.assertIsNone(leases.read_lease("host-timing", lock_dir=self.root))
        leases.release_lease("perf-stack", "other", token=other["token"], lock_dir=self.root)
        rid = self.start()
        self.controller.settle(rid, outcome="accepted", actual=Usage(1, 1, 10))
        entry, = self.controller.snapshot()["reservations"].values()
        self.assertEqual(entry["generation"], 1)
        self.assertFalse(entry["strike"])

    def test_overrun_fences_campaign_and_preserves_measured_usage(self) -> None:
        self.configure(dag={"a": (), "b": (), "c": ()})
        a = self.start()
        b = self.start("b")
        self.controller.observe(a, Usage(11, 4, 500))
        self.assertEqual(self.reserve("c").reason, "bound_exceeded")
        self.controller.settle(a, outcome="failed", actual=Usage(9, 5, 600))
        self.controller.settle(b, outcome="cancelled")
        records = self.controller.snapshot()["reservations"]
        self.assertEqual(records[a]["charge"], asdict(Usage(11, 5, 600)))
        self.assertTrue(records[a]["bound_exceeded"])
        self.assertIsNone(records[b]["observed"])
        self.assertEqual(records[b]["charge"], asdict(CAP))
        self.assertEqual(records[b]["terminal_reason"], "bound_exceeded")
