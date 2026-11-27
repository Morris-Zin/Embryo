"""Comprehensive V3 acceptance test suite for /autoloop (Issue #10 section 5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from autoloop import (  # noqa: E402
    EXIT_ILLEGAL_TRANSITION,
    EXIT_POLICY_OR_INTEGRITY,
    EXIT_UNSAFE_ROLLBACK,
)
from autoloop.cli import cmd_bootstrap_repo, cmd_sync_skill, main as cli_main  # noqa: E402
from autoloop.failures import normalize_failure  # noqa: E402
from autoloop.guard import (  # noqa: E402
    GuardError,
    UnsafeRollbackError,
    assert_clean_worktree_for_rollback,
    assert_safe_local_path,
    check_governed_lf_and_bom,
    check_tree_modes,
    compute_code_tree_sha256,
    detect_case_collisions,
    format_contract_comment,
    git_read_tree_blobs,
    git_rev_parse,
    glob_match,
    is_framework_metadata_path,
    verify_pr_integrity,
    verify_untampered,
)
from autoloop.leases import (  # noqa: E402
    LeaseConflictError,
    LeaseError,
    WIN_ERROR_ACCESS_DENIED,
    WIN_WAIT_TIMEOUT,
    acquire_lease,
    acquire_mutex,
    is_pid_alive,
    read_lease,
    release_lease,
    renew_lease,
    resource_key_hash,
)
from autoloop.state import (  # noqa: E402
    IllegalTransitionError,
    apply_replan,
    bind_pr,
    complete_grill,
    human_resolve,
    init_run,
    load_run,
    lock_run,
    log_dead_end,
    publish_contract,
    record_all_shards_pass,
    record_review,
    record_safeguard,
    record_shard_pass,
    refresh_base,
    request_contract_change,
    resolve_contract_change,
    resolve_finding,
    resume_run,
    retry_shard,
    start_shard,
    submit_for_review,
)
from autoloop.static_rules import evaluate_file_map, lint_repo, load_working_tree_file_map  # noqa: E402


def _git(repo: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Morris-Zin")
    env.setdefault("GIT_AUTHOR_EMAIL", "morris@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Morris-Zin")
    env.setdefault("GIT_COMMITTER_EMAIL", "morris@example.com")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout.strip()


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))


class FakeGitHubClient:
    """Offline in-memory mock matching GitHubAPIClient protocol."""

    def __init__(
        self,
        *,
        pr_events: list[dict] | None = None,
        comments_by_number: dict[int, list[dict]] | None = None,
        pr_reviews: list[dict] | None = None,
        pr_commits: list[dict] | None = None,
        commit_lookup: dict[str, dict] | None = None,
        review_threads: list[dict] | None = None,
        check_runs: dict[str, list[dict]] | None = None,
        contract_issue_numbers: list[int] | None = None,
        forbid_check_runs: bool = False,
    ) -> None:
        self._events = list(pr_events or [])
        self._comments = {k: list(v) for k, v in (comments_by_number or {}).items()}
        self._reviews = list(pr_reviews or [])
        self._commits = list(pr_commits or [])
        self._commit_lookup = dict(commit_lookup or {})
        self._review_threads = list(review_threads or [])
        self._check_runs = dict(check_runs or {})
        self._contract_issue_numbers = list(contract_issue_numbers or [])
        self._forbid_check_runs = bool(forbid_check_runs)

    def get_pr_events(self, pr_number: int) -> list[dict]:
        return list(self._events)

    def get_issue_comments_graphql(self, issue_or_pr_number: int) -> list[dict]:
        return list(self._comments.get(int(issue_or_pr_number), []))

    def get_pr_reviews(self, pr_number: int) -> list[dict]:
        return list(self._reviews)

    def get_pr_commits(self, pr_number: int) -> list[dict]:
        return list(self._commits)

    def get_commit(self, sha: str) -> dict | None:
        if sha in self._commit_lookup:
            return self._commit_lookup[sha]
        for c in self._commits:
            if c.get("sha") == sha:
                return c
        return None

    def get_pr_review_threads(self, pr_number: int) -> list[dict]:
        return list(self._review_threads)

    def find_contract_issue_numbers(self, pr_number: int) -> list[int]:
        return list(self._contract_issue_numbers)

    def get_check_runs(self, ref: str) -> list[dict]:
        if self._forbid_check_runs:
            raise GuardError("403 Resource not accessible by integration (checks:read absent)")
        return list(self._check_runs.get(ref, []))


def _create_temp_repo(tmp_dir: Path) -> Path:
    repo = tmp_dir / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Morris-Zin")
    _git(repo, "config", "user.email", "morris@example.com")
    _git(repo, "config", "core.autocrlf", "false")

    # Copy real trusted config, rules, .gitignore, and synced SKILL.md into the fixture repo.
    shutil.copy2(REPO_ROOT / ".gitignore", repo / ".gitignore")
    shutil.copy2(REPO_ROOT / ".autoloop/config.json", _ensure_parent(repo / ".autoloop/config.json"))
    shutil.copy2(REPO_ROOT / ".autoloop/rules/embryo_core.py", _ensure_parent(repo / ".autoloop/rules/embryo_core.py"))
    shutil.copy2(REPO_ROOT / "tools/autoloop/skill/SKILL.md", _ensure_parent(repo / "tools/autoloop/skill/SKILL.md"))
    shutil.copy2(REPO_ROOT / ".devin/skills/autoloop/SKILL.md", _ensure_parent(repo / ".devin/skills/autoloop/SKILL.md"))
    _write_lf(repo / "README.md", "# Demo Repo\n")
    _write_lf(repo / "docs/guide.md", "initial guide\n")
    _write_lf(repo / "src/a.py", "def a():\n    return 1\n")
    _write_lf(repo / "src/b.py", "def b():\n    return 2\n")
    _write_lf(repo / "AGENTS.md", "## Rules\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    # Create origin/main ref pointing at initial main
    main_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
    return repo


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestScenario1OrdinaryDocsAndPolicyGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _create_temp_repo(Path(self._tmp.name))
        self.main_sha = _git(self.repo, "rev-parse", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ordinary_docs_pr_fast_non_enrolled_pass(self) -> None:
        _git(self.repo, "checkout", "-b", "docs-edit")
        _write_lf(self.repo / "docs/guide.md", "updated guide\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "docs update")
        head_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        start = time.monotonic()
        res = verify_pr_integrity(
            self.repo,
            base_sha=self.main_sha,
            head_sha=head_sha,
            pr_number=101,
            pr_author="external-contributor",
            pr_labels=[],
            github_client=FakeGitHubClient(),
        )
        elapsed = time.monotonic() - start
        self.assertEqual(res["verdict"], "non-enrolled-pass")
        self.assertFalse(res["enrolled"])
        self.assertLess(elapsed, 2.0)

    def test_unauthorized_bot_policy_edit_rejected_and_owner_override_verified(self) -> None:
        _git(self.repo, "checkout", "-b", "bot-policy")
        _write_lf(self.repo / "AGENTS.md", "## Rules\nmodified by bot\n")
        _git(self.repo, "add", "AGENTS.md")
        _git(
            self.repo,
            "commit",
            "-m",
            "bot edits AGENTS.md",
            env_extra={
                "GIT_AUTHOR_NAME": "dependabot[bot]",
                "GIT_AUTHOR_EMAIL": "dependabot[bot]@users.noreply.github.com",
            },
        )
        head_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        # Even if PR author is owner, bot commit touching policy path is rejected without explicit approval!
        with self.assertRaises(GuardError) as ctx:
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=head_sha,
                pr_number=102,
                pr_author="Morris-Zin",
                pr_labels=[],
                github_client=FakeGitHubClient(),
            )
        self.assertEqual(ctx.exception.exit_code, EXIT_POLICY_OR_INTEGRITY)

        # Non-owner-edited approval comment must still be rejected!
        tampered_client = FakeGitHubClient(
            pr_events=[{"event": "labeled", "label": "allow-rule-edit", "actor": "Morris-Zin"}],
            comments_by_number={
                102: [
                    {
                        "author": "Morris-Zin",
                        "editor": "attacker",
                        "lastEditedAt": "2026-11-27T12:00:00Z",
                        "body": f"<!-- autoloop-rule-edit-approval:v1 pr=102 head_sha={head_sha} -->",
                    }
                ]
            },
        )
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=head_sha,
                pr_number=102,
                pr_author="dependabot[bot]",
                pr_labels=["allow-rule-edit"],
                github_client=tampered_client,
            )

        # Authentic owner label event + unedited owner approval comment passes!
        authentic_client = FakeGitHubClient(
            pr_events=[{"event": "labeled", "label": "allow-rule-edit", "actor": "Morris-Zin"}],
            comments_by_number={
                102: [
                    {
                        "author": "Morris-Zin",
                        "editor": None,
                        "lastEditedAt": None,
                        "body": f"<!-- autoloop-rule-edit-approval:v1 pr=102 head_sha={head_sha} -->",
                    }
                ]
            },
        )
        res = verify_pr_integrity(
            self.repo,
            base_sha=self.main_sha,
            head_sha=head_sha,
            pr_number=102,
            pr_author="dependabot[bot]",
            pr_labels=["allow-rule-edit"],
            github_client=authentic_client,
        )
        self.assertEqual(res["verdict"], "non-enrolled-pass")


class TestScenario2MultiShardCodeTreeAndStickiness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self._tmp.name) / "locks"
        self.repo = _create_temp_repo(Path(self._tmp.name))
        self.main_sha = _git(self.repo, "rev-parse", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_two_shard_concurrent_run_checkpoint_stability_invalidation_and_deletion(self) -> None:
        run_id = "run-multi-1"
        _git(self.repo, "checkout", "-b", "feat/multi")

        init_run(
            self.repo,
            run_id,
            "Multi-shard feature",
            issue_number=10,
            pr_number=55,
            base_branch="main",
            base_sha=self.main_sha,
            lock_dir=self.lock_dir,
        )
        complete_grill(
            self.repo,
            run_id,
            summary="Split A and B",
            shards={"shard_a": ["src/a.py"], "shard_b": ["src/b.py"]},
            lock_dir=self.lock_dir,
        )
        lock_run(self.repo, run_id, base_sha=self.main_sha, pr_number=55, lock_dir=self.lock_dir)
        pub = publish_contract(self.repo, run_id, lock_dir=self.lock_dir)

        # Start Shard A and Shard B concurrently (V3 multi-shard fix: run.status in ('locked', 'in_progress'))
        s1 = start_shard(self.repo, run_id, "shard_a", lock_dir=self.lock_dir)
        self.assertEqual(s1["status"], "in_progress")
        self.assertEqual(s1["shards"]["shard_a"]["status"], "in_progress")
        self.assertEqual(s1["shards"]["shard_b"]["status"], "locked")

        s2 = start_shard(self.repo, run_id, "shard_b", lock_dir=self.lock_dir)
        self.assertEqual(s2["status"], "in_progress")
        self.assertEqual(s2["shards"]["shard_b"]["status"], "in_progress")

        # Concurrent state updates across threads must never clobber each other
        errors: list[Exception] = []
        _write_lf(self.repo / "src/a.py", "def a():\n    return 10\n")
        _write_lf(self.repo / "src/b.py", "def b():\n    return 20\n")
        _git(self.repo, "add", "src/a.py", "src/b.py")
        _git(self.repo, "commit", "-m", "implement shards a and b")
        code_sha = _git(self.repo, "rev-parse", "HEAD")
        code_tree_hash = compute_code_tree_sha256(self.repo, code_sha, run_id=run_id)

        def _pass_worker(sid: str) -> None:
            try:
                record_shard_pass(
                    self.repo,
                    run_id,
                    sid,
                    code_tree_sha256=code_tree_hash,
                    head_sha=code_sha,
                    lock_dir=self.lock_dir,
                )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_pass_worker, args=("shard_a",))
        t2 = threading.Thread(target=_pass_worker, args=("shard_b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])

        state = load_run(self.repo, run_id)
        self.assertEqual(state["status"], "gate_passed")
        self.assertEqual(state["shards"]["shard_a"]["status"], "passed")
        self.assertEqual(state["shards"]["shard_b"]["status"], "passed")

        # Submit & pass cloud + deep review
        submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        record_review(
            self.repo, run_id, stage="cloud", verdict="pass", reviewer="cloud-bot", code_tree_sha256=code_tree_hash, lock_dir=self.lock_dir
        )
        final_state = record_review(
            self.repo, run_id, stage="deep", verdict="pass", reviewer="astra", code_tree_sha256=code_tree_hash, lock_dir=self.lock_dir
        )
        self.assertEqual(final_state["status"], "ready_to_merge")

        # Checkpoint + experiment log commit must PRESERVE code_tree_sha256
        _write_lf(self.repo / f".autoloop/experiments/{run_id}-bench-1.md", "# Experiment log\n")
        _git(self.repo, "add", f".autoloop/runs/{run_id}.json", f".autoloop/experiments/{run_id}-bench-1.md")
        _git(self.repo, "commit", "-m", "checkpoint update")
        checkpoint_head = _git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(
            compute_code_tree_sha256(self.repo, checkpoint_head, run_id=run_id),
            code_tree_hash,
        )

        # Verify local untampered check passes
        untampered = verify_untampered(self.repo, run_id, head_ref=checkpoint_head)
        self.assertTrue(untampered["ok"])

        # Enrolled CI integrity verification fails without independent candidate check evidence, and passes with it
        _git(self.repo, "checkout", "main")
        gh_client = FakeGitHubClient(
            pr_events=[{"event": "labeled", "label": "autoloop", "actor": "Morris-Zin"}],
            comments_by_number={
                10: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub["comment_markdown"]}],
                55: [
                    {
                        "author": "Morris-Zin",
                        "editor": None,
                        "lastEditedAt": None,
                        "body": f"<!-- autoloop-review:v1 pr=55 rev=1 base_sha={self.main_sha} code_tree_sha256={code_tree_hash} -->",
                    }
                ],
            },
            check_runs={
                checkpoint_head: [
                    {"name": "autoloop-candidate-checks", "status": "completed", "conclusion": "success"}
                ]
            },
        )
        verdict = verify_pr_integrity(
            self.repo,
            base_sha=self.main_sha,
            head_sha=checkpoint_head,
            pr_number=55,
            pr_author="devin-worker",
            pr_labels=["autoloop"],
            github_client=gh_client,
            candidate_checks_result="success",
        )
        self.assertEqual(verdict["verdict"], "enrolled-pass")
        self.assertTrue(verdict["enrolled"])

        # Candidate checks failure/skip must fail finalize even if checkpoint claims all shards passed
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=checkpoint_head,
                pr_number=55,
                pr_author="devin-worker",
                pr_labels=["autoloop"],
                github_client=gh_client,
                candidate_checks_result="failure",
            )

        # Removing owner-applied autoloop label fails closed (enrollment stickiness)
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=checkpoint_head,
                pr_number=55,
                pr_author="devin-worker",
                pr_labels=[],
                github_client=gh_client,
                candidate_checks_result="success",
            )

        # Code edit invalidates prior shard/review evidence
        _git(self.repo, "checkout", "feat/multi")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 999\n")
        _git(self.repo, "add", "src/a.py")
        _git(self.repo, "commit", "-m", "sneaky edit after approval")
        edited_head = _git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(compute_code_tree_sha256(self.repo, edited_head, run_id=run_id), code_tree_hash)
        _git(self.repo, "checkout", "main")
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=edited_head,
                pr_number=55,
                pr_author="devin-worker",
                pr_labels=["autoloop"],
                github_client=gh_client,
                candidate_checks_result="success",
            )

        # Checkpoint deletion in a later push fails closed (stickiness)
        _git(self.repo, "checkout", "feat/multi")
        _git(self.repo, "rm", f".autoloop/runs/{run_id}.json")
        _git(self.repo, "commit", "-m", "delete checkpoint to evade gate")
        deleted_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=deleted_head,
                pr_number=55,
                pr_author="devin-worker",
                pr_labels=[],
                github_client=FakeGitHubClient(),
            )

        # Base-branch movement invalidation
        _write_lf(self.repo / "README.md", "# Demo Repo v2\n")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "move main forward")
        moved_main = _git(self.repo, "rev-parse", "main")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", moved_main)
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=moved_main,
                head_sha=checkpoint_head,
                pr_number=55,
                pr_author="devin-worker",
                pr_labels=["autoloop"],
                github_client=gh_client,
            )


class TestScenario3FindingsSafeguardsAndZeroReviewer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self._tmp.name) / "locks"
        self.repo = _create_temp_repo(Path(self._tmp.name))
        self.main_sha = _git(self.repo, "rev-parse", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finding_lifecycle_safeguard_priority_prose_cap_and_illegal_transitions(self) -> None:
        run_id = "run-review-1"
        init_run(self.repo, run_id, "Fix bug", base_sha=self.main_sha, lock_dir=self.lock_dir)

        # Illegal transition: lock before complete-grill -> exit 2
        with self.assertRaises(IllegalTransitionError) as ctx:
            lock_run(self.repo, run_id, lock_dir=self.lock_dir)
        self.assertEqual(ctx.exception.exit_code, EXIT_ILLEGAL_TRANSITION)

        complete_grill(self.repo, run_id, summary="Single shard", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)
        code_hash = compute_code_tree_sha256(self.repo, "HEAD", run_id=run_id)
        record_shard_pass(self.repo, run_id, "s1", code_tree_sha256=code_hash, lock_dir=self.lock_dir)
        submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)

        # Cloud review rejects with finding F1 -> open
        st = record_review(
            self.repo,
            run_id,
            stage="cloud",
            verdict="reject",
            reviewer="reviewer-1",
            code_tree_sha256=code_hash,
            findings=[{"finding_id": "F1", "summary": "Missing null guard"}],
            lock_dir=self.lock_dir,
        )
        self.assertEqual(st["status"], "in_progress")
        self.assertEqual(st["findings"]["F1"]["status"], "open")

        # Cannot submit-for-review while finding is still open
        record_shard_pass(self.repo, run_id, "s1", code_tree_sha256=code_hash, lock_dir=self.lock_dir)
        with self.assertRaises(IllegalTransitionError):
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)

        # Worker resolves finding -> fixed_pending_review
        st = resolve_finding(self.repo, run_id, "F1", fix_summary="Added explicit null guard", lock_dir=self.lock_dir)
        self.assertEqual(st["findings"]["F1"]["status"], "fixed_pending_review")

        submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        record_review(self.repo, run_id, stage="cloud", verdict="pass", reviewer="reviewer-1", code_tree_sha256=code_hash, lock_dir=self.lock_dir)
        st = record_review(self.repo, run_id, stage="deep", verdict="pass", reviewer="reviewer-2", code_tree_sha256=code_hash, lock_dir=self.lock_dir)
        self.assertEqual(st["status"], "safeguard_pending")
        self.assertEqual(st["findings"]["F1"]["status"], "closed_needs_safeguard")

        # Layer 4 without --why-not-static rejected (exit 2)
        with self.assertRaises(IllegalTransitionError) as ctx4:
            record_safeguard(
                self.repo,
                run_id,
                "F1",
                layer="layer_4_runtime_test",
                artifact="tools/test_x.py",
                summary="Added runtime test",
                why_not_static=None,
                lock_dir=self.lock_dir,
            )
        self.assertEqual(ctx4.exception.exit_code, EXIT_ILLEGAL_TRANSITION)

        # Prose > 15 lines rejected (exit 2)
        long_prose = "\n".join(f"line {i}" for i in range(16))
        with self.assertRaises(IllegalTransitionError):
            record_safeguard(
                self.repo,
                run_id,
                "F1",
                layer="layer_3_static_lint",
                artifact=".autoloop/rules/embryo_core.py",
                summary=long_prose,
                lock_dir=self.lock_dir,
            )

        # Valid layer_4 with --why-not-static succeeds and transitions safeguard_pending -> ready_to_merge
        st = record_safeguard(
            self.repo,
            run_id,
            "F1",
            layer="layer_4_runtime_test",
            artifact="tools/test_x.py",
            summary="Added integration regression test",
            why_not_static="Dynamic SQL plan depends on live MariaDB optimizer",
            lock_dir=self.lock_dir,
        )
        self.assertEqual(st["findings"]["F1"]["status"], "safeguarded")
        self.assertEqual(st["status"], "ready_to_merge")
        self.assertEqual(resume_run(self.repo, run_id)["status"], "ready_to_merge")

    def test_zero_reviewer_direct_route_and_replan_human_resolve(self) -> None:
        run_id = "run-zero-rev"
        init_run(
            self.repo,
            run_id,
            "Zero reviewer direct route",
            base_sha=self.main_sha,
            cloud_review=False,
            deep_review=False,
            lock_dir=self.lock_dir,
        )
        complete_grill(self.repo, run_id, summary="Direct", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)

        # Deterministic failure normalization check
        sig1 = normalize_failure("unittest", '2026-11-27T10:00:00Z File "/tmp/xyz/tools/a.py", line 12, in test_x\nAssertionError at 0x7ff12345')
        sig2 = normalize_failure("unittest", '2026-11-28T11:22:33Z File "C:\\Users\\u\\Temp\\abc\\tools\\a.py", line 99, in test_x\nAssertionError at 0xdeadbeef')
        self.assertEqual(sig1, sig2)

        # 3 strikes -> needs_replan -> apply-replan -> 3 more strikes -> blocked_human -> human-resolve
        for _ in range(3):
            st = retry_shard(self.repo, run_id, "s1", check_name="unittest", error_output="AssertionError", lock_dir=self.lock_dir)
        self.assertEqual(st["status"], "needs_replan")
        log_dead_end(self.repo, run_id, "s1", hypothesis="h1", outcome="failed", lock_dir=self.lock_dir)
        st = apply_replan(self.repo, run_id, plan_summary="New approach", lock_dir=self.lock_dir)
        self.assertEqual(st["status"], "locked")
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)
        for _ in range(3):
            st = retry_shard(self.repo, run_id, "s1", check_name="unittest", error_output="AssertionError", lock_dir=self.lock_dir)
        self.assertEqual(st["status"], "blocked_human")
        st = human_resolve(self.repo, run_id, action="resume", note="Fixed env", lock_dir=self.lock_dir)
        self.assertEqual(st["status"], "locked")
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)
        code_hash = compute_code_tree_sha256(self.repo, "HEAD", run_id=run_id)
        record_shard_pass(self.repo, run_id, "s1", code_tree_sha256=code_hash, lock_dir=self.lock_dir)
        st = submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        self.assertEqual(st["status"], "ready_to_merge")


class TestScenario4StackedPRAndContractAmendment(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self._tmp.name) / "locks"
        self.repo = _create_temp_repo(Path(self._tmp.name))
        self.main_sha = _git(self.repo, "rev-parse", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stacked_pr_blocks_policy_smuggling_against_origin_main(self) -> None:
        # Base branch smuggles a policy edit into AGENTS.md relative to origin/main.
        _git(self.repo, "checkout", "-b", "stack-base")
        _write_lf(self.repo / "AGENTS.md", "## Tampered in stack base\n")
        _git(self.repo, "add", "AGENTS.md")
        _git(self.repo, "commit", "-m", "stack base touches AGENTS.md")
        stack_base_sha = _git(self.repo, "rev-parse", "HEAD")

        # Child stacked PR only touches docs/guide.md relative to stack-base.
        _git(self.repo, "checkout", "-b", "stack-child")
        _write_lf(self.repo / "docs/guide.md", "child docs change\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "child docs change")
        child_head_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        # Non-owner stacked PR must be rejected because mb_main..head_sha touches AGENTS.md!
        with self.assertRaises(GuardError) as ctx:
            verify_pr_integrity(
                self.repo,
                base_sha=stack_base_sha,
                head_sha=child_head_sha,
                pr_number=77,
                pr_author="untrusted-agent",
                pr_labels=[],
                github_client=FakeGitHubClient(),
            )
        self.assertIn("critical policy paths", str(ctx.exception))

    def test_contract_amendment_increments_revision_monotonically(self) -> None:
        run_id = "run-amend-1"
        init_run(self.repo, run_id, "Amend test", base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(self.repo, run_id, summary="Initial", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        st = lock_run(self.repo, run_id, lock_dir=self.lock_dir)
        self.assertEqual(st["contract_revision"], 1)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)

        st = request_contract_change(
            self.repo, run_id, "s1", reason="Need src/b.py", proposed_paths=["src/b.py"], lock_dir=self.lock_dir
        )
        self.assertEqual(st["status"], "needs_contract_amendment")

        st = resolve_contract_change(
            self.repo,
            run_id,
            decision="approve",
            add_paths={"s1": ["src/b.py"]},
            lock_dir=self.lock_dir,
        )
        self.assertEqual(st["contract_revision"], 2)
        self.assertEqual(st["status"], "locked")
        self.assertIn("src/b.py", st["shards"]["s1"]["allowed_paths"])

    def test_merge_conflict_fails_closed(self) -> None:
        _git(self.repo, "checkout", "-b", "conflict-branch")
        _write_lf(self.repo / "docs/guide.md", "branch version\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "branch edit")
        head_sha = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "checkout", "main")
        _write_lf(self.repo / "docs/guide.md", "main conflicting version\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "main edit")
        new_main = _git(self.repo, "rev-parse", "main")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", new_main)

        with self.assertRaises(GuardError) as ctx:
            verify_pr_integrity(
                self.repo,
                base_sha=new_main,
                head_sha=head_sha,
                pr_number=88,
                pr_author="Morris-Zin",
                pr_labels=[],
                github_client=FakeGitHubClient(),
            )
        self.assertIn("merge-tree", str(ctx.exception))


class TestScenario5WindowsSafetyLeasesAndRollback(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_case_and_directory_prefix_collisions(self) -> None:
        self.assertTrue(detect_case_collisions(["Sales.slint", "sales.slint"]))
        self.assertTrue(detect_case_collisions(["Src/a.py", "src/b.py"]))
        self.assertTrue(detect_case_collisions(["Src", "src/a.py"]))
        self.assertEqual(detect_case_collisions(["src/a.py", "src/b.py", "docs/readme.md"]), [])

    def test_case_sensitive_segment_globber(self) -> None:
        # * never crosses /
        self.assertTrue(glob_match("src/a.py", "src/*.py"))
        self.assertFalse(glob_match("src/sub/a.py", "src/*.py"))
        # ** matches 0 or more segments
        self.assertTrue(glob_match("src/a.py", "src/**/*.py"))
        self.assertTrue(glob_match("src/sub/deep/a.py", "src/**/*.py"))
        self.assertTrue(glob_match(".github/workflows/ci.yml", ".github/**"))
        # Strictly case-sensitive even on Windows!
        self.assertFalse(glob_match("Src/a.py", "src/*.py"))
        self.assertFalse(glob_match("agents.md", "AGENTS.md"))

    def test_git_mode_bans_symlink_and_gitlink_and_crlf_bom(self) -> None:
        repo = _create_temp_repo(self.tmp_path)
        main_sha = _git(repo, "rev-parse", "main")
        _git(repo, "checkout", "-b", "bad-modes")
        blob_sha = _git(repo, "hash-object", "-w", "--stdin", env_extra=None) if False else None
        proc = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=b"README.md",
            stdout=subprocess.PIPE,
            check=True,
        )
        blob_sha = proc.stdout.decode().strip()
        # Add 120000 symlink directly in git index (works on Windows without symlink privileges!)
        _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob_sha},link.txt")
        _git(repo, "commit", "-m", "add symlink")
        sym_head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "main")
        with self.assertRaises(GuardError) as ctx:
            verify_pr_integrity(
                repo,
                base_sha=main_sha,
                head_sha=sym_head,
                pr_number=91,
                pr_author="Morris-Zin",
                pr_labels=[],
                github_client=FakeGitHubClient(),
            )
        self.assertIn("120000", str(ctx.exception))

        # Check mode 160000 (gitlink)
        from autoloop.guard import TreeEntry

        self.assertTrue(check_tree_modes([TreeEntry("160000", "commit", main_sha, "submod")]))

        # Check CRLF and UTF-8 BOM rejection on governed files
        lf_errs = check_governed_lf_and_bom(
            {
                "tools/autoloop/sample.py": b"x = 1\r\n",
                "AGENTS.md": b"\xef\xbb\xbf# Agents\n",
                "docs/ordinary.md": b"crlf allowed outside governed\r\n",
            }
        )
        self.assertEqual(len(lf_errs), 2)

    def test_local_path_containment_and_reparse_point_rejection(self) -> None:
        root = self.tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        safe = assert_safe_local_path(root, Path("sub/file.txt"))
        self.assertEqual(safe, (root / "sub/file.txt").resolve())

        with self.assertRaises(GuardError):
            assert_safe_local_path(root, Path("../outside.txt"))

        with mock.patch("autoloop.guard.is_windows_reparse_point", side_effect=lambda p: p.name == "sub"):
            with self.assertRaises(GuardError):
                assert_safe_local_path(root, Path("sub/file.txt"))

    def test_windows_openprocess_liveness_and_never_os_kill(self) -> None:
        host = socket.gethostname()

        class FakeWinAPI:
            def __init__(self, handle: int, err: int, wait_res: int, start_ns: int | None) -> None:
                self.handle = handle
                self.err = err
                self.wait_res = wait_res
                self.start_ns = start_ns
                self.closed: list[int] = []

            def open_process(self, access: int, inherit: bool, pid: int) -> tuple[int, int]:
                return self.handle, self.err

            def wait_for_single_object(self, handle: int, timeout_ms: int) -> int:
                return self.wait_res

            def get_process_creation_ns(self, handle: int) -> int | None:
                return self.start_ns

            def close_handle(self, handle: int) -> None:
                self.closed.append(handle)

        with mock.patch("os.kill", side_effect=AssertionError("os.kill must NEVER be called on Windows")):
            # ERROR_ACCESS_DENIED (5) => alive
            api_denied = FakeWinAPI(0, WIN_ERROR_ACCESS_DENIED, 0, None)
            self.assertTrue(is_pid_alive(1234, host, 100, _win_api=api_denied, _platform="win32"))
            self.assertEqual(api_denied.closed, [])

            # WAIT_TIMEOUT + matching creation time => alive & handle closed
            api_alive = FakeWinAPI(42, 0, WIN_WAIT_TIMEOUT, 9999)
            self.assertTrue(is_pid_alive(1234, host, 9999, _win_api=api_alive, _platform="win32"))
            self.assertEqual(api_alive.closed, [42])

            # WAIT_TIMEOUT + mismatched creation time (PID recycled) => dead & handle closed
            api_recycled = FakeWinAPI(43, 0, WIN_WAIT_TIMEOUT, 8888)
            self.assertFalse(is_pid_alive(1234, host, 9999, _win_api=api_recycled, _platform="win32"))
            self.assertEqual(api_recycled.closed, [43])

            # WAIT_OBJECT_0 (0) => exited/dead & handle closed
            api_dead = FakeWinAPI(44, 0, 0, 9999)
            self.assertFalse(is_pid_alive(1234, host, 9999, _win_api=api_dead, _platform="win32"))
            self.assertEqual(api_dead.closed, [44])

            # Remote host => None (cannot check PID remotely)
            self.assertIsNone(is_pid_alive(1234, "other-machine-xyz", 9999, _win_api=api_alive, _platform="win32"))

            # Live non-destructive current-process check on Windows
            if sys.platform == "win32":
                self.assertTrue(is_pid_alive(os.getpid(), host))

    def test_lease_crash_recovery_and_concurrent_mutex_contention(self) -> None:
        lock_dir = self.tmp_path / "locks"
        res_key = "repo:shard/A:docker\\perf"
        digest = resource_key_hash(res_key)
        lock_dir.mkdir(parents=True)

        # Simulate crashed creator leaving 0-byte lease and aged 0-byte mutex
        lease_file = lock_dir / f"{digest}.lease.json"
        mutex_file = lock_dir / f"{digest}.mutex"
        lease_file.write_bytes(b"")
        mutex_file.write_bytes(b"")
        old_time = time.time() - 5.0
        os.utime(mutex_file, (old_time, old_time))

        lease = acquire_lease(res_key, "worker-a", ttl_s=60.0, lock_dir=lock_dir, empty_grace_s=0.1)
        self.assertTrue(lease["reclaimed_previous"])
        self.assertEqual(lease["owner"], "worker-a")

        # Competing owner gets LeaseConflictError
        with self.assertRaises(LeaseConflictError):
            acquire_lease(res_key, "worker-b", ttl_s=60.0, lock_dir=lock_dir)

        # Renew & release work cleanly
        renewed = renew_lease(res_key, "worker-a", ttl_s=120.0, lock_dir=lock_dir, token=lease["token"])
        self.assertGreaterEqual(renewed["expires_at"], lease["expires_at"])
        self.assertTrue(release_lease(res_key, "worker-a", lock_dir=lock_dir, token=lease["token"]))
        self.assertIsNone(read_lease(res_key, lock_dir=lock_dir))

        # Concurrent mutex contention across threads guarantees mutual exclusion
        counter = {"n": 0}
        mpath = lock_dir / "shared.mutex"

        def _bump() -> None:
            for _ in range(10):
                with acquire_mutex(mpath, timeout_s=5.0):
                    cur = counter["n"]
                    time.sleep(0.002)
                    counter["n"] = cur + 1

        threads = [threading.Thread(target=_bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(counter["n"], 40)

    def test_safe_rollback_refuses_dirty_human_edits_inside_allowed_paths(self) -> None:
        repo = _create_temp_repo(self.tmp_path)
        lock_dir = self.tmp_path / "locks_rb"
        run_id = "run-rb"
        main_sha = _git(repo, "rev-parse", "main")
        init_run(repo, run_id, "Rollback safety", base_sha=main_sha, lock_dir=lock_dir)
        complete_grill(repo, run_id, summary="s1", shards={"s1": ["src/a.py"]}, lock_dir=lock_dir)
        lock_run(repo, run_id, lock_dir=lock_dir)
        start_shard(repo, run_id, "s1", lock_dir=lock_dir)

        # Two strikes succeed (no rollback yet)
        retry_shard(repo, run_id, "s1", check_name="t", error_output="AssertionError", lock_dir=lock_dir)
        retry_shard(repo, run_id, "s1", check_name="t", error_output="AssertionError", lock_dir=lock_dir)

        # Dirty human edit INSIDE allowed_paths (`src/a.py`) must refuse rollback with exit 4!
        _write_lf(repo / "src/a.py", "# precious human edit inside allowed_paths\n")
        with self.assertRaises(UnsafeRollbackError) as ctx:
            retry_shard(repo, run_id, "s1", check_name="t", error_output="AssertionError", lock_dir=lock_dir)
        self.assertEqual(ctx.exception.exit_code, EXIT_UNSAFE_ROLLBACK)
        with self.assertRaises(UnsafeRollbackError):
            assert_clean_worktree_for_rollback(repo)

    def test_sync_skill_dry_run_default_and_install(self) -> None:
        fake_home = self.tmp_path / "home"
        fake_home.mkdir()
        dry = cmd_sync_skill(REPO_ROOT, install=False, home=fake_home)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertFalse((fake_home / ".claude/skills/autoloop/SKILL.md").exists())

        installed = cmd_sync_skill(REPO_ROOT, install=True, home=fake_home)
        self.assertEqual(installed["mode"], "install")
        src_bytes = (REPO_ROOT / "tools/autoloop/skill/SKILL.md").read_bytes()
        for rel in (
            ".claude/skills/autoloop/SKILL.md",
            ".codex/skills/autoloop/SKILL.md",
            ".gemini/antigravity/skills/autoloop/SKILL.md",
            ".agents/skills/autoloop/SKILL.md",
        ):
            self.assertEqual((fake_home / rel).read_bytes(), src_bytes)


class TestScenario6AllSevenEmbryoCoreRules(unittest.TestCase):
    rules_dir = REPO_ROOT / ".autoloop/rules"

    def _run_rules(self, file_map: dict[str, bytes]) -> dict[str, list]:
        # Always include valid synced skills unless specifically testing rule 7.
        base = {
            "tools/autoloop/skill/SKILL.md": (REPO_ROOT / "tools/autoloop/skill/SKILL.md").read_bytes(),
            ".devin/skills/autoloop/SKILL.md": (REPO_ROOT / ".devin/skills/autoloop/SKILL.md").read_bytes(),
        }
        base.update(file_map)
        violations = evaluate_file_map(self.rules_dir, base)
        grouped: dict[str, list] = {}
        for v in violations:
            grouped.setdefault(v.rule_id, []).append(v)
        return grouped

    def test_live_repo_has_zero_violations(self) -> None:
        self.assertEqual(lint_repo(REPO_ROOT), [])

    def test_rule1_no_frappe_init_in_preload(self) -> None:
        # Positive: *preload*.py calling frappe.init
        pos1 = self._run_rules({"apps/khitmee_ops/gunicorn_preload.py": b"import frappe\nfrappe.init('site')\n"})
        self.assertIn("no-frappe-init-in-preload", pos1)

        # Positive: repo-local WSGI module referenced in perf-suite/docker/*.yaml
        pos2 = self._run_rules(
            {
                "perf-suite/docker/custom.yaml": b"command:\n  - gunicorn\n  - khitmee_ops.wsgi_entry:app\n",
                "apps/khitmee_ops/khitmee_ops/wsgi_entry.py": b"import frappe\nfrappe.connect()\n",
            }
        )
        self.assertIn("no-frappe-init-in-preload", pos2)

        # Negative: external frappe.app:application preserved
        neg = self._run_rules(
            {"perf-suite/docker/compose.base.yaml": (REPO_ROOT / "perf-suite/docker/compose.base.yaml").read_bytes()}
        )
        self.assertNotIn("no-frappe-init-in-preload", neg)

    def test_rule2_no_self_sigterm_single_worker(self) -> None:
        # Positive: reachable from hooks.after_request without workers >= 2 guard
        pos = self._run_rules(
            {
                "apps/khitmee_ops/khitmee_ops/hooks.py": b'after_request = ["khitmee_ops.api.memory.maybe_trim_heap"]\n',
                "apps/khitmee_ops/khitmee_ops/api/memory.py": (
                    b"import os, signal\n"
                    b"def _kill():\n    os.kill(os.getpid(), signal.SIGTERM)\n"
                    b"def maybe_trim_heap(response=None, request=None):\n    request.after_response.add(_kill)\n"
                ),
            }
        )
        self.assertIn("no-self-sigterm-single-worker", pos)

        # Negative: explicitly guarded by workers >= 2
        neg = self._run_rules(
            {
                "apps/khitmee_ops/khitmee_ops/hooks.py": b'after_request = ["khitmee_ops.api.memory.maybe_trim_heap"]\n',
                "apps/khitmee_ops/khitmee_ops/api/memory.py": (
                    b"import os, signal\n"
                    b"def maybe_trim_heap(workers=1):\n"
                    b"    if workers >= 2:\n"
                    b"        os.kill(os.getpid(), signal.SIGTERM)\n"
                ),
            }
        )
        self.assertNotIn("no-self-sigterm-single-worker", neg)

    def test_rule3_no_background_email_export(self) -> None:
        # Positive: truthy export_in_background + unstripped **form_dict forwarding
        pos = self._run_rules(
            {
                "apps/khitmee_ops/khitmee_ops/report/r.py": (
                    b"def run():\n    return export_query(export_in_background=True)\n"
                ),
            }
        )
        self.assertIn("no-background-email-export", pos)

        pos_fwd = self._run_rules(
            {
                "apps/khitmee_ops/khitmee_ops/api/r.py": (
                    b"def run(frappe):\n    return export_query(**frappe.form_dict)\n"
                ),
            }
        )
        self.assertIn("no-background-email-export", pos_fwd)

        # Negative: falsy export_in_background=False
        neg = self._run_rules(
            {
                "apps/khitmee_ops/khitmee_ops/report/r.py": (
                    b"def run():\n    return export_query(export_in_background=False)\n"
                ),
            }
        )
        self.assertNotIn("no-background-email-export", neg)

    def test_rule4_no_client_override_rate(self) -> None:
        # Positive: client JS & desktop payload
        pos = self._run_rules(
            {"apps/khitmee_ops/khitmee_ops/public/js/sales_counter.js": b"const p = { override_rate: true };\n"}
        )
        self.assertIn("no-client-override-rate", pos)

        # Negative: server-side vouchers.py and tools/perf_parity.py preserved
        neg = self._run_rules(
            {
                "apps/khitmee_ops/khitmee_ops/api/vouchers.py": b"def submit_sale(override_rate=False): pass\n",
                "tools/perf_parity.py": b"def test(): submit_sale(override_rate=True)\n",
            }
        )
        self.assertNotIn("no-client-override-rate", neg)

    def test_rule5_no_webview_in_native_desktop(self) -> None:
        # Negative: desktop-frontend absent (ignores print-companion)
        neg_absent = self._run_rules(
            {"print-companion/Cargo.toml": b'[package]\nname = "pc"\n[dependencies]\ntauri = "2"\n'}
        )
        self.assertNotIn("no-webview-in-native-desktop", neg_absent)

        # Positive: aliased package = "tauri" in desktop-frontend/Cargo.toml
        pos_alias = self._run_rules(
            {
                "desktop-frontend/Cargo.toml": (
                    b'[package]\nname = "df"\n[dependencies]\nmy_shell = { package = "tauri", version = "2.0" }\n'
                )
            }
        )
        self.assertIn("no-webview-in-native-desktop", pos_alias)

        # Positive: webview2-com in Cargo.lock
        pos_lock = self._run_rules(
            {
                "desktop-frontend/Cargo.toml": b'[package]\nname = "df"\n[dependencies]\nslint = "1.9"\n',
                "desktop-frontend/Cargo.lock": b'[[package]]\nname = "webview2-com"\nversion = "0.33.0"\n',
            }
        )
        self.assertIn("no-webview-in-native-desktop", pos_lock)

        # Negative: clean native slint desktop-frontend
        neg_clean = self._run_rules(
            {
                "desktop-frontend/Cargo.toml": b'[package]\nname = "df"\n[dependencies]\nslint = "1.9"\n',
                "desktop-frontend/Cargo.lock": b'[[package]]\nname = "slint"\nversion = "1.9.0"\n',
            }
        )
        self.assertNotIn("no-webview-in-native-desktop", neg_clean)

    def test_rule6_no_silent_zero_coercion_in_ledgers(self) -> None:
        # Positive: silent zero coercion in perf_eval_ledger, perf_parity, offload_spec
        pos_ledger = self._run_rules(
            {"tools/perf_eval_ledger.py": b"def parse_float(value):\n    if value is None:\n        return 0.0\n"}
        )
        self.assertIn("no-silent-zero-coercion-in-ledgers", pos_ledger)

        pos_parity = self._run_rules(
            {"tools/perf_parity.py": b"def as_number(value):\n    try:\n        return float(value)\n    except ValueError:\n        return 0\n"}
        )
        self.assertIn("no-silent-zero-coercion-in-ledgers", pos_parity)

        pos_offload = self._run_rules(
            {"perf-suite/offload/offload_spec.py": b"def normalize_number(value):\n    return value or 0\n"}
        )
        self.assertIn("no-silent-zero-coercion-in-ledgers", pos_offload)

        # Negative: non-parser sort key zero default preserved
        neg = self._run_rules(
            {
                "tools/perf_parity.py": b"def canonical_child_sort_key(row):\n    return round(row.get('qty') or 0, 4)\n",
                "perf-suite/offload/offload_spec.py": (REPO_ROOT / "perf-suite/offload/offload_spec.py").read_bytes(),
            }
        )
        self.assertNotIn("no-silent-zero-coercion-in-ledgers", neg)

    def test_rule7_autoloop_skill_parity_and_optin(self) -> None:
        # Positive: byte mismatch
        pos_diff = evaluate_file_map(
            self.rules_dir,
            {
                "tools/autoloop/skill/SKILL.md": b"---\ndisable-model-invocation: true\n---\nA\n",
                ".devin/skills/autoloop/SKILL.md": b"---\ndisable-model-invocation: true\n---\nB\n",
            },
        )
        self.assertTrue(any(v.rule_id == "autoloop-skill-parity-and-optin" for v in pos_diff))

        # Positive: missing disable-model-invocation: true
        pos_optin = evaluate_file_map(
            self.rules_dir,
            {
                "tools/autoloop/skill/SKILL.md": b"---\nname: autoloop\n---\nbody\n",
                ".devin/skills/autoloop/SKILL.md": b"---\nname: autoloop\n---\nbody\n",
            },
        )
        self.assertTrue(any(v.rule_id == "autoloop-skill-parity-and-optin" for v in pos_optin))

        # Positive: CRLF / BOM
        pos_crlf = evaluate_file_map(
            self.rules_dir,
            {
                "tools/autoloop/skill/SKILL.md": b"\xef\xbb\xbf---\r\ndisable-model-invocation: true\r\n---\r\n",
                ".devin/skills/autoloop/SKILL.md": b"\xef\xbb\xbf---\r\ndisable-model-invocation: true\r\n---\r\n",
            },
        )
        self.assertTrue(any(v.rule_id == "autoloop-skill-parity-and-optin" for v in pos_crlf))

    def test_cli_smoke_lint_and_dry_run_sync(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "lint"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "sync-skill", "--dry-run"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "bootstrap-repo", "--dry-run"]), 0)


class TestScenario7ReviewFixRegressions(unittest.TestCase):
    """Regression suite covering all 13 Astra findings & 15 PR review threads."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.repo = _create_temp_repo(self.tmp_path)
        self.main_sha = _git(self.repo, "rev-parse", "main")
        self.lock_dir = self.tmp_path / "locks"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bootstrap_ruleset_binds_github_actions_and_thread_resolution(self) -> None:
        boot = cmd_bootstrap_repo(self.repo, repo_slug="Morris-Zin/Embryo", dry_run=True)
        rules = boot["ruleset"]["rules"]
        pr_rule = next(r for r in rules if r["type"] == "pull_request")["parameters"]
        self.assertTrue(pr_rule["required_review_thread_resolution"])
        checks = next(r for r in rules if r["type"] == "required_status_checks")["parameters"]["required_status_checks"]
        contexts = {c["context"]: c.get("integration_id") for c in checks}
        self.assertEqual(
            contexts,
            {
                "autoloop-preflight": 15368,
                "autoloop-candidate-checks": 15368,
                "autoloop-integrity": 15368,
            },
        )

    def test_issue_only_enrollment_sticky_and_contract_requires_pr_number(self) -> None:
        # Owner creates contract bound to PR #55 on Issue #10, without pr label or .autoloop/runs file on branch.
        run_id = "run-issue-sticky"
        init_run(self.repo, run_id, "Issue bound", issue_number=10, pr_number=55, base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(self.repo, run_id, summary="g", shards={"s1": ["docs/guide.md"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, base_sha=self.main_sha, pr_number=55, lock_dir=self.lock_dir)
        pub = publish_contract(self.repo, run_id, lock_dir=self.lock_dir)

        _git(self.repo, "checkout", "-b", "evade-gate")
        _write_lf(self.repo / "docs/guide.md", "edited without run checkpoint\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "work for #10")
        head_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        gh = FakeGitHubClient(
            comments_by_number={
                10: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub["comment_markdown"]}]
            }
        )
        # Must NOT return non-enrolled-pass: issue #10 binds PR #55 -> fails because run checkpoint absent!
        with self.assertRaises(GuardError) as ctx:
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=head_sha,
                pr_number=55,
                pr_author="contributor",
                pr_labels=[],
                github_client=gh,
                pr_body="Part of #10",
            )
        self.assertIn("active run checkpoint", str(ctx.exception))

        # Owner contract omitting pr_number cannot borrow candidate checkpoint pr_number!
        init_run(self.repo, "run-no-pr", "No PR in contract", issue_number=12, base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(
            self.repo,
            "run-no-pr",
            summary="g",
            shards={"s1": ["docs/guide.md"]},
            cloud_review=False,
            deep_review=False,
            lock_dir=self.lock_dir,
        )
        lock_run(self.repo, "run-no-pr", base_sha=self.main_sha, lock_dir=self.lock_dir)
        pub_nopr = publish_contract(self.repo, "run-no-pr", lock_dir=self.lock_dir)
        start_shard(self.repo, "run-no-pr", "s1", lock_dir=self.lock_dir)

        # Candidate sets pr_number in checkpoint JSON, commits & passes locally
        _git(self.repo, "checkout", "-b", "nopr-branch")
        _write_lf(self.repo / "docs/guide.md", "cand\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "docs")
        h2 = compute_code_tree_sha256(self.repo, "HEAD", run_id="run-no-pr")
        record_shard_pass(self.repo, "run-no-pr", "s1", code_tree_sha256=h2, head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        submit_for_review(self.repo, "run-no-pr", lock_dir=self.lock_dir)
        run_json = load_run(self.repo, "run-no-pr")
        run_json["pr_number"] = 99
        _write_lf(self.repo / ".autoloop/runs/run-no-pr.json", json.dumps(run_json, indent=2) + "\n")
        _git(self.repo, "add", ".autoloop/runs/run-no-pr.json")
        _git(self.repo, "commit", "-m", "checkpoint")
        nopr_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        gh_nopr = FakeGitHubClient(
            comments_by_number={
                12: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub_nopr["comment_markdown"]}]
            }
        )
        with self.assertRaises(GuardError) as ctx_nopr:
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=nopr_head,
                pr_number=99,
                pr_author="contributor",
                pr_labels=[],
                github_client=gh_nopr,
                candidate_checks_result="success",
            )
        self.assertIn("missing required pr_number", str(ctx_nopr.exception))

        # bind_pr on branch bumps contract_revision and produces valid PR-bound contract that passes CI
        _git(self.repo, "checkout", "nopr-branch")
        st_bound = bind_pr(self.repo, "run-no-pr", pr_number=99, lock_dir=self.lock_dir)
        self.assertEqual(st_bound["pr_number"], 99)
        self.assertGreaterEqual(st_bound["contract_revision"], 2)
        pub_bound = publish_contract(self.repo, "run-no-pr", lock_dir=self.lock_dir)
        _git(self.repo, "add", ".autoloop/runs/run-no-pr.json")
        _git(self.repo, "commit", "-m", "bind pr 99")
        bound_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")
        gh_bound = FakeGitHubClient(
            comments_by_number={
                12: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub_bound["comment_markdown"]}]
            }
        )
        res_bound = verify_pr_integrity(
            self.repo,
            base_sha=self.main_sha,
            head_sha=bound_head,
            pr_number=99,
            pr_author="contributor",
            pr_labels=[],
            github_client=gh_bound,
            candidate_checks_result="success",
        )
        self.assertEqual(res_bound["verdict"], "enrolled-pass")

    def test_superseded_approval_unresolved_threads_and_safeguard_artifact_verified(self) -> None:
        run_id = "run-superseded"
        init_run(
            self.repo,
            run_id,
            "Review & safeguard checks",
            issue_number=20,
            pr_number=21,
            base_sha=self.main_sha,
            lock_dir=self.lock_dir,
        )
        complete_grill(
            self.repo,
            run_id,
            summary="g",
            shards={"s1": ["src/a.py", "tools/test_f1_guard.py"]},
            shard_checks={"s1": ["python -c \"print('ok')\""]},
            lock_dir=self.lock_dir,
        )
        lock_run(self.repo, run_id, base_sha=self.main_sha, pr_number=21, lock_dir=self.lock_dir)
        pub = publish_contract(self.repo, run_id, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)

        _git(self.repo, "checkout", "-b", "feat/sg")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 42\n")
        _write_lf(self.repo / "tools/test_f1_guard.py", "import unittest\n")
        _git(self.repo, "add", "src/a.py", "tools/test_f1_guard.py")
        _git(self.repo, "commit", "-m", "fix + test")
        h = compute_code_tree_sha256(self.repo, "HEAD", run_id=run_id)
        record_shard_pass(self.repo, run_id, "s1", code_tree_sha256=h, head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        record_review(
            self.repo, run_id, stage="cloud", verdict="reject", reviewer="alice", code_tree_sha256=h,
            findings=[{"finding_id": "F1", "summary": "Add guard"}], lock_dir=self.lock_dir,
        )
        resolve_finding(self.repo, run_id, "F1", fix_summary="fixed", lock_dir=self.lock_dir)
        record_shard_pass(self.repo, run_id, "s1", code_tree_sha256=h, head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        record_review(self.repo, run_id, stage="cloud", verdict="pass", reviewer="alice", code_tree_sha256=h, lock_dir=self.lock_dir)
        record_review(self.repo, run_id, stage="deep", verdict="pass", reviewer="bob", code_tree_sha256=h, lock_dir=self.lock_dir)
        record_safeguard(
            self.repo,
            run_id,
            "F1",
            layer="layer_4_runtime_test",
            artifact="tools/test_f1_guard.py",
            summary="Added test_f1_guard.py",
            why_not_static="Runtime contract check",
            lock_dir=self.lock_dir,
        )
        _git(self.repo, "add", f".autoloop/runs/{run_id}.json")
        _git(self.repo, "commit", "-m", "checkpoint")
        pr_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        # Superseded approval (APPROVED then CHANGES_REQUESTED by same reviewer) MUST fail!
        gh_superseded = FakeGitHubClient(
            comments_by_number={
                20: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub["comment_markdown"]}]
            },
            pr_reviews=[
                {"id": 1, "user": "alice", "state": "APPROVED", "commit_id": pr_head},
                {"id": 2, "user": "alice", "state": "CHANGES_REQUESTED", "commit_id": pr_head},
            ],
        )
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=21, pr_author="devin-worker",
                pr_labels=[], github_client=gh_superseded, candidate_checks_result="success",
            )

        # Unresolved review thread is surfaced as telemetry (not a deadlocking CI error,
        # since native GitHub Ruleset enforces required_review_thread_resolution), AND
        # finalize with candidate_checks_result must NEVER call get_check_runs (no checks:read needed).
        gh_unresolved = FakeGitHubClient(
            comments_by_number={
                20: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub["comment_markdown"]}]
            },
            pr_reviews=[{"id": 3, "user": "alice", "state": "APPROVED", "commit_id": pr_head}],
            review_threads=[{"id": "PRRT_1", "isResolved": False, "comments": [{"author": "alice", "body": "Fix"}]}],
            forbid_check_runs=True,
        )
        fin_unres = verify_pr_integrity(
            self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=21, pr_author="devin-worker",
            pr_labels=[], github_client=gh_unresolved, mode="finalize", candidate_checks_result="success",
        )
        self.assertEqual(fin_unres["verdict"], "enrolled-pass")
        self.assertEqual(fin_unres["unresolved_review_threads"], 1)

        # Preflight returns frozen shard checks; Finalize passes when thread resolved & approval clean
        gh_ok = FakeGitHubClient(
            comments_by_number={
                20: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub["comment_markdown"]}]
            },
            pr_reviews=[{"id": 3, "user": "alice", "state": "APPROVED", "commit_id": pr_head}],
            review_threads=[{"id": "PRRT_1", "isResolved": True, "comments": [{"author": "alice", "body": "Fix"}]}],
            forbid_check_runs=True,
        )
        pre = verify_pr_integrity(
            self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=21, pr_author="devin-worker",
            pr_labels=[], github_client=gh_ok, mode="preflight",
        )
        self.assertEqual(pre["verdict"], "enrolled-preflight")
        self.assertEqual(pre["shard_checks"][0]["shard_id"], "s1")
        fin = verify_pr_integrity(
            self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=21, pr_author="devin-worker",
            pr_labels=[], github_client=gh_ok, mode="finalize", candidate_checks_result="success",
        )
        self.assertEqual(fin["verdict"], "enrolled-pass")
        self.assertEqual(fin["unresolved_review_threads"], 0)

    def test_unattributed_stacked_commit_rejected_on_policy_exception(self) -> None:
        _git(self.repo, "checkout", "-b", "stack-base")
        _write_lf(self.repo / "AGENTS.md", "## Edited in unattributed commit\n")
        _git(self.repo, "add", "AGENTS.md")
        _git(self.repo, "commit", "-m", "base edit")
        base_sha = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "checkout", "-b", "stack-child")
        _write_lf(self.repo / "docs/guide.md", "child edit\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "child")
        child_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        # Missing commit provenance in both pr_commits and get_commit must REJECT even for pr_author=Morris-Zin!
        gh_missing = FakeGitHubClient(
            pr_commits=[
                {
                    "sha": child_head,
                    "author_login": "Morris-Zin",
                    "committer_login": "Morris-Zin",
                    "author_type": "User",
                    "committer_type": "User",
                }
            ]
        )
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo, base_sha=base_sha, head_sha=child_head, pr_number=31,
                pr_author="Morris-Zin", pr_labels=[], github_client=gh_missing,
            )

    def test_cli_multi_shard_worktree_clean_check_all_shards_safeguard_and_refresh_base(self) -> None:
        wt_a = self.tmp_path / "wt_a"
        wt_b = self.tmp_path / "wt_b"
        _git(self.repo, "worktree", "add", "-b", "feat/shard-a", str(wt_a), self.main_sha)
        _git(self.repo, "worktree", "add", "-b", "feat/shard-b", str(wt_b), self.main_sha)
        try:
            run_id = "run-wt-multi"
            init_run(self.repo, run_id, "WT Multi", pr_number=44, base_sha=self.main_sha, lock_dir=self.lock_dir)
            complete_grill(
                self.repo,
                run_id,
                summary="Two shards",
                shards={"shard_a": ["src/a.py"], "shard_b": ["src/b.py", "tools/test_f1.py"]},
                lock_dir=self.lock_dir,
            )
            lock_run(self.repo, run_id, base_sha=self.main_sha, pr_number=44, lock_dir=self.lock_dir)
            start_shard(self.repo, run_id, "shard_a", worktree_path=str(wt_a), lock_dir=self.lock_dir)
            start_shard(self.repo, run_id, "shard_b", worktree_path=str(wt_b), lock_dir=self.lock_dir)

            # Dirty worktree must refuse `cli check`
            _write_lf(wt_a / "src/a.py", "def a():\n    return 11\n")
            self.assertNotEqual(cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "shard_a", "--skip-fast-check"]), 0)

            _git(wt_a, "add", "src/a.py")
            _git(wt_a, "commit", "-m", "shard a work")
            # Mismatched --head-ref vs HEAD must refuse `cli check`
            self.assertNotEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "shard_a", "--head-ref", self.main_sha, "--skip-fast-check"]),
                0,
            )

            # Clean `cli check --shard shard_a` uses registered `wt_a` automatically
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "shard_a", "--skip-fast-check"]),
                0,
            )
            _write_lf(wt_b / "src/b.py", "def b():\n    return 22\n")
            _git(wt_b, "add", "src/b.py")
            _git(wt_b, "commit", "-m", "shard b work")
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "shard_b", "--skip-fast-check"]),
                0,
            )

            # Integrate both shard branches into coordinator checkout and run `--all-shards`
            _git(self.repo, "checkout", "-b", "feat/integrated", self.main_sha)
            _git(self.repo, "merge", "--no-ff", "-m", "merge a", "feat/shard-a")
            _git(self.repo, "merge", "--no-ff", "-m", "merge b", "feat/shard-b")
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]),
                0,
            )
            st = load_run(self.repo, run_id)
            self.assertEqual(st["status"], "gate_passed")

            # Review -> reject F1 -> fix + pass -> safeguard_pending -> add safeguard test -> recheck via `--all-shards` -> re-review
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
            h1 = st["shards"]["shard_a"]["passed_code_tree_sha256"]
            record_review(
                self.repo, run_id, stage="cloud", verdict="reject", reviewer="alice", code_tree_sha256=h1,
                findings=[{"finding_id": "F1", "summary": "Add test"}], lock_dir=self.lock_dir,
            )
            resolve_finding(self.repo, run_id, "F1", fix_summary="fixed", lock_dir=self.lock_dir)
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]),
                0,
            )
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="cloud", verdict="pass", reviewer="alice", code_tree_sha256=h1, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="deep", verdict="pass", reviewer="bob", code_tree_sha256=h1, lock_dir=self.lock_dir)
            self.assertEqual(load_run(self.repo, run_id)["status"], "safeguard_pending")

            # Add regression test committed under shard_b scope -> re-run `--all-shards` -> transitions to gate_passed!
            _write_lf(self.repo / "tools/test_f1.py", "def test_ok():\n    pass\n")
            _git(self.repo, "add", "tools/test_f1.py")
            _git(self.repo, "commit", "-m", "add safeguard test")
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]),
                0,
            )
            st_recheck = load_run(self.repo, run_id)
            self.assertEqual(st_recheck["status"], "gate_passed")
            h2 = st_recheck["shards"]["shard_a"]["passed_code_tree_sha256"]
            self.assertNotEqual(h2, h1)
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="cloud", verdict="pass", reviewer="alice", code_tree_sha256=h2, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="deep", verdict="pass", reviewer="bob", code_tree_sha256=h2, lock_dir=self.lock_dir)
            record_safeguard(
                self.repo, run_id, "F1", layer="layer_4_runtime_test", artifact="tools/test_f1.py",
                summary="Added test_f1", why_not_static="Runtime check", lock_dir=self.lock_dir,
            )
            self.assertEqual(load_run(self.repo, run_id)["status"], "ready_to_merge")

            # Base refresh & post-ready contract amendment work cleanly
            st_req = request_contract_change(self.repo, run_id, "shard_a", reason="Expand", proposed_paths=["README.md"], lock_dir=self.lock_dir)
            self.assertEqual(st_req["status"], "needs_contract_amendment")
            resolve_contract_change(self.repo, run_id, decision="approve", add_paths={"shard_a": ["README.md"]}, lock_dir=self.lock_dir)
            st_ref = refresh_base(self.repo, run_id, new_base_sha=self.main_sha, lock_dir=self.lock_dir)
            self.assertEqual(st_ref["status"], "locked")
        finally:
            _git(self.repo, "checkout", "main")
            _git(self.repo, "worktree", "remove", "--force", str(wt_a))
            _git(self.repo, "worktree", "remove", "--force", str(wt_b))

    def test_local_allow_policy_paths_and_rollback_restore_and_oversized_blobs(self) -> None:
        # Local allow_policy_paths authorization
        run_id = "run-pol-rb"
        _git(self.repo, "checkout", "-b", "feat/pol")
        init_run(self.repo, run_id, "Policy & Rollback", base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(
            self.repo, run_id, summary="p", shards={"s1": ["src/a.py", "AGENTS.md"]},
            allow_policy_paths=["AGENTS.md"], lock_dir=self.lock_dir,
        )
        lock_run(self.repo, run_id, base_sha=self.main_sha, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)

        _write_lf(self.repo / "AGENTS.md", "## Authorized local policy edit\n")
        _git(self.repo, "add", "AGENTS.md")
        _git(self.repo, "commit", "-m", "edit AGENTS.md")
        self.assertTrue(verify_untampered(self.repo, run_id, shard_id="s1")["ok"])

        # Real rollback restores src/a.py and removes added src/extra.py back to last_green_sha (in isolated clean repo)
        rb_parent = self.tmp_path / "rb_repo"
        rb_parent.mkdir(parents=True)
        rb_repo = _create_temp_repo(rb_parent)
        rb_main = _git(rb_repo, "rev-parse", "main")
        run2 = "run-rb-real"
        _git(rb_repo, "checkout", "-b", "feat/rb-real")
        init_run(rb_repo, run2, "Real rollback", base_sha=rb_main, lock_dir=self.lock_dir)
        complete_grill(rb_repo, run2, summary="rb", shards={"s1": ["src/*.py"]}, lock_dir=self.lock_dir)
        lock_run(rb_repo, run2, base_sha=rb_main, lock_dir=self.lock_dir)
        start_shard(rb_repo, run2, "s1", lock_dir=self.lock_dir)
        orig_hash = compute_code_tree_sha256(rb_repo, rb_main, run_id=run2)

        _write_lf(rb_repo / "src/a.py", "def a():\n    return -999\n")
        _write_lf(rb_repo / "src/extra_temp.py", "x = 1\n")
        _git(rb_repo, "add", "src/a.py", "src/extra_temp.py")
        _git(rb_repo, "commit", "-m", "broken commit")
        for _ in range(3):
            st_rb = retry_shard(rb_repo, run2, "s1", check_name="t", error_output="AssertionError", lock_dir=self.lock_dir)
        self.assertEqual(st_rb["status"], "needs_replan")
        self.assertEqual((rb_repo / "src/a.py").read_text(encoding="utf-8"), "def a():\n    return 1\n")
        self.assertFalse((rb_repo / "src/extra_temp.py").exists())
        self.assertEqual(compute_code_tree_sha256(rb_repo, "HEAD", run_id=run2), orig_hash)

        # Oversized blobs fail closed instead of silent omission
        with self.assertRaises(GuardError):
            git_read_tree_blobs(rb_repo, "HEAD", max_blob_bytes=10)
        with self.assertRaises(GuardError):
            load_working_tree_file_map(rb_repo, max_blob_bytes=10)

        # Metadata hash & scope parity: invalid slug `.md` MUST NOT be excluded from code_tree_sha256
        _write_lf(rb_repo / f".autoloop/experiments/{run2}-BAD_CAPS.md", "ignored?\n")
        _git(rb_repo, "add", f".autoloop/experiments/{run2}-BAD_CAPS.md")
        _git(rb_repo, "commit", "-m", "invalid exp filename")
        self.assertFalse(is_framework_metadata_path(f".autoloop/experiments/{run2}-BAD_CAPS.md", run2))
        self.assertNotEqual(compute_code_tree_sha256(rb_repo, "HEAD", run_id=run2), orig_hash)

    def test_kernel_mutex_recovers_instantly_on_process_kill(self) -> None:
        mpath = self.lock_dir / "proc_kill.mutex"
        flag_file = self.tmp_path / "held.txt"
        holder_code = "\n".join(
            [
                "import pathlib, sys, time",
                f"sys.path.insert(0, {str(TOOLS_DIR)!r})",
                "from autoloop.leases import acquire_mutex",
                f"with acquire_mutex(pathlib.Path({str(mpath)!r}), timeout_s=5.0):",
                f"    pathlib.Path({str(flag_file)!r}).write_text('locked')",
                "    time.sleep(30)",
            ]
        )
        proc = subprocess.Popen([sys.executable, "-B", "-c", holder_code])
        try:
            deadline = time.monotonic() + 5.0
            while not flag_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(flag_file.exists())
            with self.assertRaises(Exception):
                with acquire_mutex(mpath, timeout_s=0.25):
                    pass
            proc.kill()
            proc.wait(timeout=5)
            # Kernel releases lock immediately on kill without stale unlinking
            entered = False
            with acquire_mutex(mpath, timeout_s=2.0):
                entered = True
            self.assertTrue(entered)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_cli_lease_persistence_mandatory_token_and_metadata_containment(self) -> None:
        # Cross-invocation lease without holder_pid stays locked across CLI processes until TTL/release
        r_key = "cli:persist:res"
        lease_a = acquire_lease(r_key, "worker-a", ttl_s=120.0, lock_dir=self.lock_dir)
        self.assertIsNone(lease_a["pid"])
        with self.assertRaises(LeaseConflictError):
            acquire_lease(r_key, "worker-b", ttl_s=120.0, lock_dir=self.lock_dir)
        with self.assertRaises(LeaseConflictError):
            renew_lease(r_key, "worker-a", ttl_s=120.0, lock_dir=self.lock_dir, token=None)
        with self.assertRaises(LeaseConflictError):
            release_lease(r_key, "worker-a", lock_dir=self.lock_dir, token=None)
        renewed = renew_lease(r_key, "worker-a", ttl_s=240.0, lock_dir=self.lock_dir, token=lease_a["token"])
        self.assertIsNone(renewed["pid"])
        self.assertTrue(release_lease(r_key, "worker-a", lock_dir=self.lock_dir, token=lease_a["token"]))

        # Reparse-point rejection applies to trusted-contracts dir too
        with mock.patch("autoloop.guard.is_windows_reparse_point", side_effect=lambda p: p.name == "trusted-contracts"):
            with self.assertRaises(GuardError):
                init_run(self.repo, "run-reparse", "Test", lock_dir=self.lock_dir)
                complete_grill(self.repo, "run-reparse", summary="s", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
                lock_run(self.repo, "run-reparse", lock_dir=self.lock_dir)

    def test_refined_static_rules_2_3_4_and_7(self) -> None:
        rules_dir = REPO_ROOT / ".autoloop/rules"
        base_skills = {
            "tools/autoloop/skill/SKILL.md": (REPO_ROOT / "tools/autoloop/skill/SKILL.md").read_bytes(),
            ".devin/skills/autoloop/SKILL.md": (REPO_ROOT / ".devin/skills/autoloop/SKILL.md").read_bytes(),
        }
        # Rule 2: unrelated workers >= 2 does NOT exempt unguarded os.kill; early return DOES guard it
        v2_bad = evaluate_file_map(
            rules_dir,
            {
                **base_skills,
                "apps/khitmee_ops/khitmee_ops/hooks.py": b'after_request = ["khitmee_ops.api.m.bad"]\n',
                "apps/khitmee_ops/khitmee_ops/api/m.py": (
                    b"import os, signal\n"
                    b"def bad(workers=1):\n"
                    b"    if workers >= 2:\n        pass\n"
                    b"    os.kill(os.getpid(), signal.SIGTERM)\n"
                ),
            },
        )
        self.assertTrue(any(v.rule_id == "no-self-sigterm-single-worker" for v in v2_bad))

        v2_early = evaluate_file_map(
            rules_dir,
            {
                **base_skills,
                "apps/khitmee_ops/khitmee_ops/hooks.py": b'after_request = ["khitmee_ops.api.m.ok"]\n',
                "apps/khitmee_ops/khitmee_ops/api/m.py": (
                    b"import os, signal\n"
                    b"def ok(workers=1):\n"
                    b"    if workers < 2:\n        return\n"
                    b"    os.kill(os.getpid(), signal.SIGTERM)\n"
                ),
            },
        )
        self.assertFalse(any(v.rule_id == "no-self-sigterm-single-worker" for v in v2_early))

        # Rule 3: explicitly stripped kwargs before export_query(**kwargs) is allowed
        v3_ok = evaluate_file_map(
            rules_dir,
            {
                **base_skills,
                "apps/khitmee_ops/khitmee_ops/api/rep.py": (
                    b'def run(**kwargs):\n    kwargs.pop("export_in_background", None)\n    return export_query(**kwargs)\n'
                ),
            },
        )
        self.assertFalse(any(v.rule_id == "no-background-email-export" for v in v3_ok))

        # Rule 4: README / comments ignored, code still caught
        v4 = evaluate_file_map(
            rules_dir,
            {
                **base_skills,
                "desktop-client/README.md": b"Never send override_rate from client\n",
                "apps/khitmee_ops/khitmee_ops/public/js/ok.js": b"// Do not pass override_rate here\nconst x = 1;\n",
                "apps/khitmee_ops/khitmee_ops/public/js/bad.js": b"const payload = { override_rate: true };\n",
            },
        )
        r4_hits = [v.path for v in v4 if v.rule_id == "no-client-override-rate"]
        self.assertEqual(r4_hits, ["apps/khitmee_ops/khitmee_ops/public/js/bad.js"])

        # Rule 7: neither skill present (pre-rollout main) -> 0 violations; one missing -> violation
        v7_empty = evaluate_file_map(rules_dir, {})
        self.assertFalse(any(v.rule_id == "autoloop-skill-parity-and-optin" for v in v7_empty))
        v7_one = evaluate_file_map(rules_dir, {"tools/autoloop/skill/SKILL.md": base_skills["tools/autoloop/skill/SKILL.md"]})
        self.assertTrue(any(v.rule_id == "autoloop-skill-parity-and-optin" for v in v7_one))

    def test_export_rule_definite_sanitization_and_named_dict_comp(self) -> None:
        rules_dir = REPO_ROOT / ".autoloop/rules"
        base_skills = {
            "tools/autoloop/skill/SKILL.md": (REPO_ROOT / "tools/autoloop/skill/SKILL.md").read_bytes(),
            ".devin/skills/autoloop/SKILL.md": (REPO_ROOT / ".devin/skills/autoloop/SKILL.md").read_bytes(),
        }
        report_path = "apps/khitmee_ops/khitmee_ops/api/rep.py"

        def _hits(code: bytes) -> list:
            return [
                v for v in evaluate_file_map(rules_dir, {**base_skills, report_path: code})
                if v.rule_id == "no-background-email-export"
            ]

        # Named key-removing dict comprehension is allowed
        self.assertEqual(
            _hits(
                b'def run(**kwargs):\n'
                b'    kwargs = {k: v for k, v in kwargs.items() if k != "export_in_background"}\n'
                b'    return export_query(**kwargs)\n'
            ),
            [],
        )
        # Explicit known-falsy assignment is allowed
        self.assertEqual(
            _hits(
                b'def run(**kwargs):\n'
                b'    kwargs["export_in_background"] = False\n'
                b'    return export_query(**kwargs)\n'
            ),
            [],
        )
        # Pop only on one branch must FAIL
        self.assertTrue(
            _hits(
                b'def run(fmt, **kwargs):\n'
                b'    if fmt == "csv":\n'
                b'        kwargs.pop("export_in_background", None)\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        # Pop AFTER call must FAIL
        self.assertTrue(
            _hits(
                b'def run(**kwargs):\n'
                b'    res = export_query(**kwargs)\n'
                b'    kwargs.pop("export_in_background", None)\n'
                b'    return res\n'
            )
        )
        # Unknown flag assignment must FAIL
        self.assertTrue(
            _hits(
                b'def run(flag, **kwargs):\n'
                b'    kwargs["export_in_background"] = flag\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        # Rebinding kwargs after pop must FAIL
        self.assertTrue(
            _hits(
                b'def run(**kwargs):\n'
                b'    kwargs.pop("export_in_background", None)\n'
                b'    kwargs = frappe.form_dict\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        # Pop inside nested function must NOT sanitize outer function
        self.assertTrue(
            _hits(
                b'def run(**kwargs):\n'
                b'    def helper(kwargs):\n'
                b'        kwargs.pop("export_in_background", None)\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        # Inline dict with unknown flag or unpack-after-override must FAIL; unpack-before-False PASSES
        self.assertTrue(
            _hits(
                b'def run(flag, **kwargs):\n'
                b'    return export_query(**{**kwargs, "export_in_background": flag})\n'
            )
        )
        self.assertTrue(
            _hits(
                b'def run(**kwargs):\n'
                b'    return export_query(**{"export_in_background": False, **kwargs})\n'
            )
        )
        self.assertEqual(
            _hits(
                b'def run(**kwargs):\n'
                b'    return export_query(**{**kwargs, "export_in_background": False})\n'
            ),
            [],
        )

    def test_complete_grill_rejects_unknown_shard_checks(self) -> None:
        run_id = "run-typo-check"
        init_run(self.repo, run_id, "Typo shard check", base_sha=self.main_sha, lock_dir=self.lock_dir)
        with self.assertRaises(IllegalTransitionError) as ctx:
            complete_grill(
                self.repo,
                run_id,
                summary="g",
                shards={"reports": ["src/a.py"]},
                shard_checks={"report": ["python -c \"print(1)\""]},
                lock_dir=self.lock_dir,
            )
        self.assertIn("unknown shard", str(ctx.exception))

    def test_multi_shard_rollback_after_integration_and_refresh_base_clears_individual_pass(self) -> None:
        run_id = "run-ms-rollback"
        init_run(self.repo, run_id, "Multi-shard rollback", base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(
            self.repo,
            run_id,
            summary="Two shards",
            shards={"a": ["src/a.py"], "b": ["src/b.py"]},
            lock_dir=self.lock_dir,
        )
        lock_run(self.repo, run_id, base_sha=self.main_sha, lock_dir=self.lock_dir)

        wt_a = self.tmp_path / "wt_rb_a"
        wt_b = self.tmp_path / "wt_rb_b"
        _git(self.repo, "worktree", "add", "-b", "rb/a", str(wt_a), "main")
        _git(self.repo, "worktree", "add", "-b", "rb/b", str(wt_b), "main")
        try:
            start_shard(self.repo, run_id, "a", worktree_path=str(wt_a), last_green_sha=self.main_sha, lock_dir=self.lock_dir)
            start_shard(self.repo, run_id, "b", worktree_path=str(wt_b), last_green_sha=self.main_sha, lock_dir=self.lock_dir)

            _write_lf(wt_a / "src/a.py", "def a():\n    return 10\n")
            _git(wt_a, "add", "src/a.py")
            _git(wt_a, "commit", "-m", "a green")
            sha_a = _git(wt_a, "rev-parse", "HEAD")

            _write_lf(wt_b / "src/b.py", "def b():\n    return 20\n")
            _git(wt_b, "add", "src/b.py")
            _git(wt_b, "commit", "-m", "b green")
            sha_b = _git(wt_b, "rev-parse", "HEAD")

            self.assertEqual(cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "a", "--skip-fast-check"]), 0)
            self.assertEqual(cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "b", "--skip-fast-check"]), 0)

            _git(self.repo, "checkout", "-b", "rb/int")
            _git(self.repo, "merge", "--no-ff", "rb/a", "-m", "merge a")
            _git(self.repo, "merge", "--no-ff", "rb/b", "-m", "merge b")
            int_sha = _git(self.repo, "rev-parse", "HEAD")
            self.assertEqual(cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]), 0)

            st_int = load_run(self.repo, run_id)
            self.assertEqual(st_int["last_integrated_sha"], int_sha)
            # Shard-local rollback anchors MUST stay shard-local (not overwritten with integration commit)
            self.assertEqual(st_int["shards"]["a"]["last_green_sha"], sha_a)
            self.assertEqual(st_int["shards"]["b"]["last_green_sha"], sha_b)

            # Review rejects integration -> worker A edits and hits 3 strikes -> clean rollback without UnsafeRollbackError
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
            h_int = compute_code_tree_sha256(self.repo, int_sha, run_id=run_id)
            record_review(
                self.repo, run_id, stage="cloud", verdict="reject", reviewer="alice", code_tree_sha256=h_int,
                findings=[{"finding_id": "F-INT", "summary": "Fix edge case in a"}], lock_dir=self.lock_dir,
            )

            _write_lf(wt_a / "src/a.py", "def a():\n    return -999\n")
            _git(wt_a, "add", "src/a.py")
            _git(wt_a, "commit", "-m", "bad fix in a")
            for attempt in range(3):
                st_r = retry_shard(
                    self.repo, run_id, "a", check_name="pytest", error_output=f"fail {attempt}", lock_dir=self.lock_dir,
                )
            self.assertEqual(st_r["status"], "needs_replan")
            self.assertEqual(st_r["shards"]["a"]["status"], "needs_replan")
            self.assertEqual((wt_a / "src/a.py").read_text(encoding="utf-8"), "def a():\n    return 10\n")

            # Refresh base MUST clear has_passed_individually so check --all-shards cannot bypass individual rechecks
            apply_replan(self.repo, run_id, plan_summary="Retry cleanly", shard_id="a", lock_dir=self.lock_dir)
            _git(self.repo, "checkout", "main")
            _write_lf(self.repo / "README.md", "# Updated main M2\n")
            _git(self.repo, "add", "README.md")
            _git(self.repo, "commit", "-m", "main M2")
            m2_sha = _git(self.repo, "rev-parse", "HEAD")

            st_ref = refresh_base(self.repo, run_id, new_base_sha=m2_sha, lock_dir=self.lock_dir)
            self.assertFalse(st_ref["shards"]["a"]["has_passed_individually"])
            self.assertFalse(st_ref["shards"]["b"]["has_passed_individually"])
            start_shard(self.repo, run_id, "a", worktree_path=str(wt_a), lock_dir=self.lock_dir)
            start_shard(self.repo, run_id, "b", worktree_path=str(wt_b), lock_dir=self.lock_dir)
            # Even on a clean commit descending from M2, check --all-shards MUST fail until each shard passes individually
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]),
                EXIT_ILLEGAL_TRANSITION,
            )
        finally:
            subprocess.run(["git", "checkout", "main"], cwd=self.repo, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", str(wt_a)], cwd=self.repo, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", str(wt_b)], cwd=self.repo, capture_output=True)

    def test_post_review_safeguard_failure_supports_retry_and_recovery(self) -> None:
        run_id = "run-post-review-retry"
        init_run(self.repo, run_id, "Post-review retry", base_sha=self.main_sha, cloud_review=False, deep_review=False, lock_dir=self.lock_dir)
        complete_grill(self.repo, run_id, summary="g", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, base_sha=self.main_sha, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)

        _git(self.repo, "checkout", "-b", "feat/pr-retry")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 7\n")
        _git(self.repo, "add", "src/a.py")
        _git(self.repo, "commit", "-m", "v1")
        h1 = compute_code_tree_sha256(self.repo, "HEAD", run_id=run_id)
        record_shard_pass(self.repo, run_id, "s1", code_tree_sha256=h1, head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        # Enter safeguard_pending via an open finding -> resolved -> zero-reviewer submit
        st = load_run(self.repo, run_id)
        st["findings"]["F-POST"] = {"finding_id": "F-POST", "status": "fixed_pending_review", "summary": "Add guard"}
        (self.repo / f".autoloop/runs/{run_id}.json").write_text(json.dumps(st), encoding="utf-8")
        st_sg = submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        self.assertEqual(st_sg["status"], "safeguard_pending")

        # Safeguard check fails -> retry_shard MUST reopen out of safeguard_pending
        st_ret = retry_shard(
            self.repo, run_id, "s1", check_name="fast_check", error_output="lint failed", lock_dir=self.lock_dir,
        )
        self.assertEqual(st_ret["status"], "retrying")
        self.assertEqual(st_ret["shards"]["s1"]["status"], "retrying")
        st_de = log_dead_end(
            self.repo, run_id, "s1", hypothesis="Tried bad rule", outcome="Syntax error", lock_dir=self.lock_dir,
        )
        self.assertEqual(st_de["status"], "in_progress")
        _git(self.repo, "checkout", "main")

    def test_issue_only_enrollment_cannot_be_bypassed_by_severing_pr_references(self) -> None:
        run_id = "run-issue-only"
        init_run(self.repo, run_id, "Issue only", issue_number=42, pr_number=99, base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(self.repo, run_id, summary="g", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, base_sha=self.main_sha, pr_number=99, lock_dir=self.lock_dir)
        pub = publish_contract(self.repo, run_id, lock_dir=self.lock_dir)
        self.assertIn("Bound PR: #99", pub["comment_markdown"])

        # PR branch has NO .autoloop/runs file, NO autoloop label, and NO #42 mention in body or commits
        _git(self.repo, "checkout", "-b", "feat/severed")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 123\n")
        _git(self.repo, "add", "src/a.py")
        _git(self.repo, "commit", "-m", "unrelated message with no issue ref")
        head_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        gh_severed = FakeGitHubClient(
            comments_by_number={
                42: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub["comment_markdown"]}]
            },
            contract_issue_numbers=[42],
            forbid_check_runs=True,
        )
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=head_sha,
                pr_number=99,
                pr_author="devin-worker",
                pr_labels=[],
                pr_body="Removed all issue links",
                github_client=gh_severed,
                mode="finalize",
                candidate_checks_result="success",
            )

        # Meanwhile a truly ordinary non-enrolled PR succeeds without ever calling get_check_runs
        gh_ordinary = FakeGitHubClient(forbid_check_runs=True)
        res_ord = verify_pr_integrity(
            self.repo,
            base_sha=self.main_sha,
            head_sha=head_sha,
            pr_number=100,
            pr_author="devin-worker",
            pr_labels=[],
            pr_body="Ordinary PR",
            github_client=gh_ordinary,
            mode="finalize",
        )
        self.assertEqual(res_ord["verdict"], "non-enrolled-pass")

    def test_cli_smoke_lint_and_dry_run_sync(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "lint"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "sync-skill", "--dry-run"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "bootstrap-repo", "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
