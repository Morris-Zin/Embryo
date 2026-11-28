from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from autoloop import EXIT_POLICY_OR_INTEGRITY, verification as v
from autoloop.admission import Controller, Usage
from autoloop.cli import main
from autoloop.guard import (
    canonical_json_bytes,
    compute_code_tree_sha256,
    contract_sha256,
    format_contract_comment,
    verification_check_id,
)
from autoloop.receipts import Broker
from autoloop.leases import atomic_write_json
from autoloop.state import complete_grill, init_run, load_run, lock_run, start_shard
from autoloop.test_autoloop import REPO_ROOT, FakeGitHubClient, _create_temp_repo, _git, _template_main_sha, _write_lf
from autoloop.trusted_tooling import materialize_tooling

RUN = "phase2-fixture"
PR = 11
IDENTITY = "github.com/morris-zin/phase2-fixture"
ORACLE = "37313d9"
COMPILE = """python -I -S -B -c "compile(open('src/a.py','rb').read(),'src/a.py','exec')" """.rstrip()


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=False)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="autoloop-integration-")))

    def legacy_cli(self):
        root = self.root / "oracle"
        if not root.is_dir():
            root.mkdir()
            archive = subprocess.check_output(["git", "archive", ORACLE, "tools/autoloop"], cwd=REPO_ROOT)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
                tree.extractall(root, filter="data")
        return root

    def fixture(self, name, *, checks=(), two=False, disabled=False, auto=False, key=None, started=True, extra_paths=()):
        parent = self.root / name
        parent.mkdir()
        repo = _create_temp_repo(parent)
        _git(repo, "remote", "add", "origin", "https://github.com/Morris-Zin/phase2-fixture.git")
        _git(repo, "switch", "-c", "candidate")
        base = _template_main_sha()
        init_run(repo, RUN, "offline integration", pr_number=PR, base_sha=base,
                 cloud_review=not disabled, deep_review=not disabled, merge_mode="auto" if auto else "human")
        allowed = {"a": ["src/a.py", *extra_paths], **({"b": ["src/b.py"]} if two else {})}
        complete_grill(repo, RUN, summary="fixture", shards=allowed, shard_checks={"a": list(checks)} if checks else None)
        lock_run(repo, RUN, verification_key=key)
        if started:
            for shard in allowed:
                start_shard(repo, RUN, shard)
        _write_lf(repo / "src/a.py", "def a():\n    return 3\n")
        _git(repo, "add", "src/a.py")
        _git(repo, "commit", "-m", "change a")
        return repo, base

    def command(self, repo, *args, oracle=False):
        source = self.legacy_cli() if oracle else REPO_ROOT
        return _run([sys.executable, "-B", str(source / "tools/autoloop/cli.py"), "--repo", str(repo), *args], repo)

    def in_process(self, repo, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = main(["--repo", str(repo), *args])
        return status, output.getvalue(), error.getvalue()

    def check_args(self, *scope):
        return ("check", "--run-id", RUN, "--skip-fast-check", *scope)

    def checkpoint(self, repo):
        return repo / ".autoloop/runs" / f"{RUN}.json"

    def legacy_projection(self, repo):
        run = load_run(repo, RUN)
        return canonical_json_bytes({
            "status": run["status"],
            "shards": {sid: {
                name: shard.get(name)
                for name in ("status", "passed_code_tree_sha256", "has_passed_individually", "last_green_sha",
                             "worker_strikes", "attempts", "retry_scope")
            } for sid, shard in run["shards"].items()},
            "history": [{key: value for key, value in item.items() if key != "at"} for item in run["history"]],
        })

    def compare_oracle(self, repo, args, *, expect):
        checkpoint = self.checkpoint(repo)
        initial = checkpoint.read_bytes()
        before = self.command(repo, *args, oracle=True)
        expected = self.legacy_projection(repo)
        checkpoint.write_bytes(initial)
        after = self.command(repo, *args)
        self.assertEqual((before.returncode, after.returncode), (expect, expect), (before.stderr, after.stderr))
        self.assertEqual(expected, self.legacy_projection(repo))
        return after

    def compare_in_process(self, repo, args, *, extra=()):
        checkpoint = self.checkpoint(repo)
        initial = checkpoint.read_bytes()
        before = self.command(repo, *args, oracle=True)
        expected = self.legacy_projection(repo)
        checkpoint.write_bytes(initial)
        code, output, error = self.in_process(repo, *args, *extra)
        self.assertEqual(code, before.returncode, (before.stderr, error))
        self.assertEqual(self.legacy_projection(repo), expected)
        return code, output, error

    def test_legacy_passing_shard_records_advisory_divergence(self):
        repo, _ = self.fixture("pass")
        result = self.compare_oracle(repo, self.check_args("--shard", "a"), expect=0)
        self.assertIn("SHADOW-DIVERGENCE shard", result.stderr)
        recorded = load_run(repo, RUN)["verification"]["shard:a"]
        self.assertFalse(recorded["ok"])
        self.assertEqual(len(recorded["digest"]), 64)

    def test_check_reports_added_skip_against_frozen_base(self):
        path = "src/test_added.py"
        repo, _ = self.fixture("integrity", extra_paths=(path,))
        _write_lf(repo / path, """import unittest

class AddedTest(unittest.TestCase):
    @unittest.skip("fixture")
    def test_total(self):
        self.assertEqual(1, 1)
""")
        _git(repo, "add", path)
        _git(repo, "commit", "-m", "add skipped test")
        checked = self.compare_oracle(repo, self.check_args("--shard", "a"), expect=0)
        self.assertIn(f"SHADOW-INTEGRITY added-skip-only-ignore {path}", checked.stderr)

    def test_legacy_failed_shard_preserves_exit_and_has_no_divergence(self):
        repo, _ = self.fixture("fail", checks=('python -c "raise SystemExit(9)"',))
        result = self.compare_oracle(repo, self.check_args("--shard", "a"), expect=EXIT_POLICY_OR_INTEGRITY)
        self.assertNotIn("SHADOW-DIVERGENCE", result.stderr)
        self.assertFalse(load_run(repo, RUN)["verification"]["shard:a"]["ok"])

    def test_legacy_all_shards_preserves_status(self):
        repo, _ = self.fixture("all", two=True)
        _git(repo, "switch", "-c", "shard-b", "origin/main")
        _write_lf(repo / "src/b.py", "def b():\n    return 4\n")
        _git(repo, "add", "src/b.py")
        _git(repo, "commit", "-m", "change b")
        passed = self.command(repo, *self.check_args("--shard", "b"), oracle=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        _git(repo, "switch", "candidate")
        passed = self.command(repo, *self.check_args("--shard", "a"), oracle=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        _git(repo, "merge", "--no-ff", "--no-edit", "shard-b")
        result = self.compare_oracle(repo, self.check_args("--all-shards"), expect=0)
        self.assertIn("SHADOW-DIVERGENCE integrated", result.stderr)
        self.assertEqual(set(load_run(repo, RUN)["verification"]["integrated"]["decisions"]), {"a", "b"})

    def test_disabled_review_and_legacy_checkpoint_preserve_ready_transition(self):
        for legacy in (False, True):
            with self.subTest(legacy_checkpoint=legacy):
                repo, _ = self.fixture(f"review-{legacy}", disabled=True)
                passed = self.command(repo, *self.check_args("--all-shards"), oracle=True)
                self.assertEqual(passed.returncode, 0, passed.stderr)
                if legacy:
                    run = load_run(repo, RUN)
                    for key in tuple(run):
                        if key.startswith("last_integrated_") or key == "check_timings":
                            run.pop(key)
                    self.checkpoint(repo).write_bytes(canonical_json_bytes(run))
                    trusted = repo / ".autoloop/trusted-contracts"
                    (trusted / f"{RUN}.rev1.json").unlink()
                result = self.compare_oracle(repo, ("submit-for-review", "--run-id", RUN), expect=0)
                self.assertEqual(load_run(repo, RUN)["status"], "ready_to_merge")
                self.assertIn("SHADOW-DIVERGENCE review_submission", result.stderr)
                self.assertIn("SHADOW-DIVERGENCE merge_eligibility", result.stderr)

    def ci_args(self, repo, base, *, mode="preflight", labels=("autoloop",), **kwargs):
        event = repo.parent / "event.json"
        event.write_bytes(canonical_json_bytes({
            "repository": {"full_name": "Morris-Zin/phase2-fixture"},
            "pull_request": {
                "number": PR, "user": {"login": "Morris-Zin"},
                "base": {"sha": base, "ref": "main"},
                "head": {"sha": _git(repo, "rev-parse", "HEAD")},
                "labels": [{"name": label} for label in labels],
            },
        }))
        return ("verify-pr-integrity", "--event-path", str(event), "--offline", "--mode", mode,
                *(item for key, value in kwargs.items() for item in ("--" + key.replace("_", "-"), value)))

    def test_legacy_revoked_auto_merge_approval_preserves_rejection(self):
        repo, base = self.fixture("revoked", disabled=True, auto=True)
        passed = self.command(repo, *self.check_args("--all-shards"), oracle=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        submitted = self.command(repo, "submit-for-review", "--run-id", RUN, oracle=True)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        _git(repo, "add", str(self.checkpoint(repo).relative_to(repo)))
        _git(repo, "commit", "-m", "record ready checkpoint")
        attestation = base64.b64encode(canonical_json_bytes({"a": compute_code_tree_sha256(repo, "HEAD", run_id=RUN)}))
        args = self.ci_args(repo, base, mode="finalize", trusted_shard_results_b64=attestation.decode())
        result = self.compare_oracle(repo, args, expect=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("requires owner-applied 'allow-auto-merge'", result.stderr)

    def signed_fixture(self, repo, base):
        run = load_run(repo, RUN)
        head = _git(repo, "rev-parse", "HEAD")
        tree = compute_code_tree_sha256(repo, head, run_id=RUN)
        contract = json.loads((repo / ".autoloop/trusted-contracts" / f"{RUN}.json").read_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            tooling = Path(temporary)
            materialize_tooling(repo, tooling, run, contract, checking_shard=None)
            sources = tuple(
                v.ToolingSource(path.relative_to(tooling).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                for path in sorted(tooling.rglob("*")) if path.is_file()
            )
        digest = v.tooling_sources_digest(sources)
        binding = v.Binding(IDENTITY, RUN, None, PR, run["contract_revision"], run["contract_sha256"], base, head)
        now = int(time.time())
        meta = v.EvidenceMeta("fixture-evidence", binding, 0, now, expires_at=now + 600)
        checks = tuple(
            v.CheckEvidence(
                meta, verification_check_id("a", index, command), gate, "deterministic", tree, digest,
                True, 0, "observed-output", "execution-receipt",
            )
            for gate in (v.Obligation.SHARD, v.Obligation.INTEGRATED)
            for index, command in enumerate(["fast_check.py", *run["shards"]["a"]["checks"]])
        )
        snapshot = v.EvidenceSnapshot(
            now, binding, v.Generations(), integration_commit=head, integration_tree_digest=tree,
            shard_tree_digest=tree, tooling_digest=digest, tooling=(v.ToolingEvidence(meta, digest, sources),),
            shard_results=(v.ShardEvidence(meta, "a", tree, ""),),
            integrations=(v.IntegrationEvidence(meta, head, tree, "integrated-receipt"),), checks=checks,
        )
        return snapshot, contract

    def sign(self, signer, destination, snapshot):
        snapshots = {"a": asdict(snapshot)}
        raw = destination.with_suffix(".payload")
        raw.write_bytes(canonical_json_bytes(snapshots))
        proc = _run(["ssh-keygen", "-Y", "sign", "-f", str(signer), "-n", "autoloop-verification-v1", str(raw)], self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = {"snapshots": snapshots, "signature": raw.with_suffix(".payload.sig").read_text(encoding="ascii")}
        destination.write_bytes(canonical_json_bytes(document))
        return document

    def signer(self):
        key = self.root / "test-key"
        result = _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        public = key.with_suffix(".pub").read_text(encoding="ascii").split()
        return key, " ".join(public[:2])

    def test_enforcement_requests_refuse_before_running_or_writing(self):
        repo, _ = self.fixture("enforce")
        before = self.checkpoint(repo).read_bytes()
        args = (
            (*self.check_args("--shard", "a"), "--verification-evidence", "missing.json"),
            ("submit-for-review", "--run-id", RUN),
            ("lock", "--run-id", RUN, "--verification-key", "missing.pub"),
        )
        with patch("autoloop.cli.refuse_local_enforcement") as lookup:
            for command in args:
                with self.subTest(command=command[0]):
                    code, _, error = self.in_process(repo, *command, "--verification-mode", "enforce")
                    self.assertNotEqual(code, 0)
                    self.assertIn("issue #22", error)
            lookup.assert_not_called()
        self.assertEqual(self.checkpoint(repo).read_bytes(), before)

        state = load_run(repo, RUN)
        trusted = repo / ".autoloop/trusted-contracts" / f"{RUN}.rev1.json"
        contract = json.loads(trusted.read_bytes())
        contract["verification_mode"] = "enforce"
        trusted.write_bytes(canonical_json_bytes(contract))
        state["contract_sha256"] = contract_sha256(contract)
        self.checkpoint(repo).write_bytes(canonical_json_bytes(state))
        before = self.checkpoint(repo).read_bytes()
        with patch("autoloop.cli.verify_with_trusted_tooling") as check:
            for command in (self.check_args("--shard", "a"), ("submit-for-review", "--run-id", RUN)):
                code, _, error = self.in_process(repo, *command)
                self.assertNotEqual(code, 0)
                self.assertIn("issue #22", error)
            check.assert_not_called()
        self.assertEqual(self.checkpoint(repo).read_bytes(), before)

    def test_signed_local_check_matches_ci_shadow(self):
        signer, public = self.signer()
        repo, base = self.fixture("parity", disabled=True, key=public)
        _git(repo, "add", str(self.checkpoint(repo).relative_to(repo)))
        _git(repo, "commit", "-m", "record running checkpoint")
        snapshot, contract = self.signed_fixture(repo, base)
        evidence_path = self.root / "evidence.json"
        document = self.sign(signer, evidence_path, snapshot)
        local = self.command(repo, *self.check_args("--all-shards"), "--verification-evidence", str(evidence_path))
        self.assertEqual(local.returncode, 0, local.stderr)
        self.assertNotIn("SHADOW-DIVERGENCE", local.stderr)
        decision = load_run(repo, RUN)["verification"]["integrated"]
        owner = "Morris-Zin"
        encoded = base64.b64encode(canonical_json_bytes(document)).decode()
        client = FakeGitHubClient(comments_by_number={PR: [
            {"author": owner, "body": format_contract_comment(contract), "lastEditedAt": None},
            {"author": owner, "body": f"<!-- autoloop-verification:v1 {encoded} -->", "lastEditedAt": None},
        ]})
        ci_args = [arg for arg in self.ci_args(repo, base) if arg != "--offline"]
        self.assertIn("--mode preflight --shadow-verification",
                      (REPO_ROOT / ".github/workflows/autoloop-gate.yml").read_text(encoding="utf-8"))
        with patch("autoloop.cli.GitHubAPIClient", return_value=client):
            code, output, error = self.in_process(repo, *ci_args, "--shadow-verification")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["shadow_integrated"], decision)
        submit = self.command(repo, "submit-for-review", "--run-id", RUN,
                              "--verification-evidence", str(evidence_path))
        self.assertEqual(submit.returncode, 0, submit.stderr)
        self.assertEqual(load_run(repo, RUN)["status"], "ready_to_merge")

    def test_ci_shadow_rejects_a_signed_approval_when_owner_revokes_the_label(self):
        signer, public = self.signer()
        repo, base = self.fixture("signed-revoked", disabled=True, auto=True, key=public)
        _git(repo, "add", str(self.checkpoint(repo).relative_to(repo)))
        _git(repo, "commit", "-m", "record checkpoint")
        snapshot, contract = self.signed_fixture(repo, base)
        approval = v.OwnerApprovalEvidence(
            snapshot.tooling[0].meta, v.Obligation.MERGE_ELIGIBILITY, "auto-merge", snapshot.integration_tree_digest,
        )
        document = self.sign(signer, self.root / "approved.json", replace(snapshot, approvals=(approval,)))
        encoded = base64.b64encode(canonical_json_bytes(document)).decode()
        client = FakeGitHubClient(
            pr_events=[{"event": event, "label": "allow-auto-merge", "actor": "Morris-Zin"}
                       for event in ("labeled", "unlabeled")],
            comments_by_number={PR: [
                {"author": "Morris-Zin", "body": format_contract_comment(contract), "lastEditedAt": None},
                {"author": "Morris-Zin", "body": f"<!-- autoloop-verification:v1 {encoded} -->", "lastEditedAt": None},
            ]},
        )
        ci_args = [arg for arg in self.ci_args(repo, base) if arg != "--offline"]
        with patch("autoloop.cli.GitHubAPIClient", return_value=client):
            code, output, error = self.in_process(repo, *ci_args, "--shadow-verification")
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertTrue(result["shadow_integrated"]["ok"])
        self.assertFalse(result["shadow_merge_eligibility"]["ok"])
        failures = result["shadow_merge_eligibility"]["decisions"]["a"]["failures"]
        self.assertIn("inactive-evidence", {failure["reason"] for failure in failures})

    def test_admission_and_delegation_hooks_leave_legacy_start_authoritative(self):
        repo, _ = self.fixture("hooks", started=False)
        broker = Broker(IDENTITY, _root=self.root / "receipts")
        expiry = time.time() + 180

        def payload(name, value):
            path = self.root / f"{name}.json"
            path.write_bytes(canonical_json_bytes(value))
            return str(path)

        def execute(*args):
            code, output, error = self.in_process(repo, *args)
            self.assertEqual(code, 0, error)
            return json.loads(output), error

        limits = {"tokens": 500, "cost_micros": 300, "wall_ms": 10000}
        with patch("autoloop.receipts.Broker", return_value=broker):
            result, error = execute(
                "start-shard", "--run-id", RUN, "--shard", "a",
                "--delegation-reservation", payload("delegation", {
                    "role": "implementation", "generation": 1, "request_id": "fixture-delegation",
                    "request_digest": "request", "brief_digest": "brief", "task_class": "implementation",
                    "difficulty": "routine", "policy": "fixture", "epoch": "1", "provider": "fixture", "model": "test",
                    "settings": {}, "limits": limits, "expires_at": expiry,
                }),
                "--admission-reservation", payload("admission-reservation", {
                    "role": "implementation", "generation": 1, "request_id": "fixture-budget",
                    "bounds": {"limits": limits, "token_ceiling": None, "cost_ceiling": None,
                               "wall_ceiling": "fixture", "expires_at": expiry},
                    "expires_at": expiry,
                }),
            )
            self.assertEqual(result["status"], "in_progress")
            self.assertIn("issue #22", result["shadow_admission"]["shadow_error"])
            self.assertIn("shadow_error: admission", error)
            rid = result["shadow_delegation"]["reservation_id"]
            execute("delegation", "begin-dispatch", rid)
            self.assertEqual(execute("delegation", "quarantine", rid)[0]["dispatch"], "quarantined")
            execute("delegation", "attach", rid, "fixture-task")
            git = result["shadow_delegation"]["git"]
            settlement, _ = execute("delegation", "settle", rid, "--payload", payload("settlement", {
                "status": "completed", "disposition": "accepted", "duration_s": 0.1,
                "usage": {"source": "not_observed"}, "git_before": git, "git_after": git,
                "evidence_ids": ["fixture-proof"], "changed_paths": [],
            }))
            self.assertEqual(settlement["task_id"], "fixture-task")

    def test_advisory_review_and_profile_entry_points(self):
        repo, _ = self.fixture("advisory")
        before = self.checkpoint(repo).read_bytes()
        result = self.command(repo, "review-next", "--run-id", RUN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "checks-invalid")
        result = self.command(repo, "profile", "--format", "json", *(
            item for flag in ("t3-db", "codex-home", "claude-project-dir", "t3-provider-log-dir", "devin-log-dir")
            for item in (f"--{flag}", str(self.root / flag))
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertEqual(self.checkpoint(repo).read_bytes(), before)

    def test_cache_and_red_proof_refuse_before_any_work(self):
        repo, _ = self.fixture("deferred", checks=(COMPILE,))
        before = self.checkpoint(repo).read_bytes()
        with (patch("autoloop.cli._run_check") as check,
              patch("autoloop.proofs.run_red_proof") as proof,
              patch("autoloop.cli.load_run") as load):
            for args in ((*self.check_args("--shard", "a"), "--check-cache"),
                         ("red-proof",), ("red-proof", "--run-id", RUN, "--output-dir", str(self.root / "proofs"))):
                with self.subTest(args=args):
                    code, _, error = self.in_process(repo, *args)
                    self.assertNotEqual(code, 0)
                    self.assertIn("issue #22", error)
            check.assert_not_called()
            proof.assert_not_called()
            load.assert_not_called()
        self.assertEqual(self.checkpoint(repo).read_bytes(), before)
        self.assertFalse((self.root / "proofs").exists())
        self.compare_oracle(repo, self.check_args("--shard", "a"), expect=0)

    def test_mutable_checkpoint_enforcement_is_ignored(self):
        repo, _ = self.fixture("mutable-mode")
        state = load_run(repo, RUN)
        state["verification_mode"] = "enforce"
        self.checkpoint(repo).write_bytes(canonical_json_bytes(state))
        self.compare_oracle(repo, self.check_args("--shard", "a"), expect=0)
        self.assertEqual(load_run(repo, RUN)["shards"]["a"]["status"], "passed")

    def test_null_shadow_record_does_not_block_passing_transition(self):
        repo, _ = self.fixture("null-shadow")
        state = load_run(repo, RUN)
        state["verification"] = None
        self.checkpoint(repo).write_bytes(canonical_json_bytes(state))
        self.compare_oracle(repo, self.check_args("--shard", "a"), expect=0)
        self.assertIn("verification checkpoint", load_run(repo, RUN)["verification"]["shard:a"]["shadow_error"])

    def test_integrity_integer_overflow_does_not_abort_the_frozen_check(self):
        path = "src/test_threshold.py"
        repo, _ = self.fixture("integrity-overflow", extra_paths=(path,), checks=('python -c "print(123456789)"',))
        _write_lf(repo / path, f"LIMIT = {'9' * 400}\n")
        _git(repo, "add", path)
        _git(repo, "commit", "-m", "add large integer threshold")
        checked = self.compare_oracle(repo, self.check_args("--shard", "a"), expect=0)
        self.assertIn("shadow_error: integrity: OverflowError", checked.stderr)
        self.assertIn("123456789", checked.stderr)

    def test_admission_never_creates_host_lease_files_in_shadow(self):
        repo, _ = self.fixture("lease", started=False)
        locks = self.root / "locks"
        locks.mkdir()
        controller = Controller(IDENTITY, _lock_dir=locks)
        expires = time.time() + 180
        limits = Usage(tokens=500, cost_micros=300, wall_ms=10000)
        controller.register_campaign("test", limits, expires_at=expires)
        controller.register_run(RUN, "test", limits, expires_at=expires, dag={"a": ()},
                                resources={"a": ("fixture-host",)})
        payload = self.root / "budget.json"
        payload.write_bytes(canonical_json_bytes({
            "role": "implementation", "generation": 1, "request_id": "active-budget",
            "expires_at": expires,
            "bounds": {"limits": asdict(limits), "token_ceiling": "provider-token",
                       "cost_ceiling": "provider-cost", "wall_ceiling": "provider-wall", "expires_at": expires},
        }))
        files_before = {path for path in locks.rglob("*") if path.is_file()}
        self.assertEqual(list(locks.glob("*.lease.json")), [])
        with patch("autoloop.admission.Controller", return_value=controller) as factory:
            _, output, _ = self.compare_in_process(
                repo, ("start-shard", "--run-id", RUN, "--shard", "a"),
                extra=("--admission-reservation", str(payload)),
            )
            self.assertEqual(list(locks.glob("*.lease.json")), [])
            self.assertIn("issue #22", json.loads(output)["shadow_admission"]["shadow_error"])
            code, _, error = self.in_process(repo, "admission", "reserve", "--run-id", RUN, "--shard", "a",
                                             "--payload", str(payload))
            self.assertNotEqual(code, 0)
            self.assertIn("issue #22", error)
            factory.assert_not_called()
        self.assertEqual({path for path in locks.rglob("*") if path.is_file()}, files_before)
        self.assertEqual(list(locks.glob("*.lease.json")), [])

    def test_malformed_decision_and_shadow_write_failure_preserve_legacy_save(self):
        repo, _ = self.fixture("shadow-write")
        args = self.check_args("--shard", "a")
        before = self.checkpoint(repo).read_bytes()
        with patch("autoloop.state.verify_phase2", return_value={"ok": True, "value": object()}):
            self.compare_in_process(repo, args)
        self.assertIn("shadow_error", load_run(repo, RUN)["verification"]["shard:a"])
        self.checkpoint(repo).write_bytes(before)

        def reject_shadow_write(path, state):
            if "verification" in state:
                raise OverflowError("fixture advisory write failed")
            atomic_write_json(path, state)

        with patch("autoloop.state.atomic_write_json", side_effect=reject_shadow_write):
            _, _, error = self.compare_in_process(repo, args)
        self.assertIn("shadow_error: verification write: OverflowError", error)
        self.assertEqual(load_run(repo, RUN)["shards"]["a"]["status"], "passed")

    def test_shadow_failures_preserve_the_oracle_exit_and_persisted_status(self):
        for stage, hook in (
            ("verify", "autoloop.state.verify_phase2"),
            ("integrity", "autoloop.cli.advise_test_integrity"),
            ("profile", "autoloop.profile.__main__.main"),
            ("receipts", "autoloop.receipts.Broker"),
            ("admission", "autoloop.cli._advise_admission"),
        ):
            with self.subTest(stage=stage), patch(hook, side_effect=OverflowError(f"{stage} fixture")) as injected:
                is_start = stage in ("receipts", "admission")
                repo, _ = self.fixture(f"invariant-{stage}", started=not is_start)
                args = ("start-shard", "--run-id", RUN, "--shard", "a") if is_start else self.check_args("--shard", "a")
                extra = (f"--{'delegation' if stage == 'receipts' else 'admission'}-reservation", "missing.json") if is_start else ()
                if stage == "profile":
                    checkpoint = self.checkpoint(repo).read_bytes()
                    code, output, error = self.in_process(repo, "profile")
                    self.assertEqual(code, 0)
                    self.assertIn("profile fixture", json.loads(output)["shadow_error"])
                    self.assertEqual(self.checkpoint(repo).read_bytes(), checkpoint)
                    with patch("autoloop.profile.__main__.build_parser", side_effect=OverflowError("profile CLI setup")):
                        self.compare_in_process(repo, args)
                else:
                    _, _, error = self.compare_in_process(repo, args, extra=extra)
                    self.assertIn("shadow_error:", error)
                injected.assert_called()

    def test_malformed_ci_evidence_cannot_change_preflight(self):
        repo, base = self.fixture("ci-bad-evidence")
        _git(repo, "add", str(self.checkpoint(repo).relative_to(repo)))
        _git(repo, "commit", "-m", "record checkpoint")
        args = self.ci_args(repo, base)
        expected = self.command(repo, *args, oracle=True)
        self.assertEqual(expected.returncode, 0, expected.stderr)
        envelope = self.root / "malformed.json"
        envelope.write_text("{", encoding="utf-8")
        code, output, error = self.in_process(
            repo, *args, "--shadow-verification", "--verification-evidence", str(envelope),
        )
        self.assertEqual(code, expected.returncode, error)
        result = json.loads(output)
        for target in ("integrated", "merge_eligibility"):
            self.assertIn("JSONDecodeError", result.pop(f"shadow_{target}")["shadow_error"])
        self.assertEqual(result, json.loads(expected.stdout))
