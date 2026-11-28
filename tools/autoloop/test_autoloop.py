"""Comprehensive V3 acceptance test suite for /autoloop (Issue #10 section 5)."""

from __future__ import annotations

import atexit
from contextlib import redirect_stderr, redirect_stdout
import io
import itertools
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

from autoloop import (
    EXIT_ILLEGAL_TRANSITION,
    EXIT_POLICY_OR_INTEGRITY,
    EXIT_UNSAFE_ROLLBACK,
)
from autoloop.cli import cmd_bootstrap_repo, cmd_sync_skill, main as cli_main
from autoloop.check_tools import doctor
from autoloop.failures import normalize_failure
from autoloop.guard import (
    _GIT_BIN,
    GitHubAPIClient,
    GraphQLNotFoundError,
    GuardError,
    UnsafeRollbackError,
    _verify_safeguard_artifact,
    assert_clean_worktree_for_check,
    assert_clean_worktree_for_rollback,
    assert_safe_local_path,
    check_governed_lf_and_bom,
    check_tree_modes,
    compute_code_tree_sha256,
    contract_sha256,
    detect_case_collisions,
    format_contract_comment,
    git_read_tree_blobs,
    git_rev_parse,
    glob_match,
    is_framework_metadata_path,
    parse_latest_owner_contract,
    verify_pr_integrity,
    verify_untampered,
)
from autoloop.leases import (
    LeaseConflictError,
    LeaseError,
    WIN_ERROR_ACCESS_DENIED,
    WIN_WAIT_TIMEOUT,
    acquire_lease,
    acquire_mutex,
    default_lock_dir,
    is_pid_alive,
    read_lease,
    release_lease,
    renew_lease,
    resource_key_hash,
    resource_lock_dir,
)
from autoloop.lessons import globs_overlap
from autoloop.routing import attempt_log_path
from autoloop.state import (
    IllegalTransitionError,
    _build_frozen_contract,
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
    record_check_timings,
    record_finding,
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
from autoloop.static_rules import evaluate_file_map, lint_repo, load_rules_from_dir, load_working_tree_file_map


def _git(repo: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Morris-Zin")
    env.setdefault("GIT_AUTHOR_EMAIL", "morris@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Morris-Zin")
    env.setdefault("GIT_COMMITTER_EMAIL", "morris@example.com")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [_GIT_BIN, *args],
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


_TEMPLATE_REPO_ROOT: Path | None = None
_TEMPLATE_MAIN_SHA: str | None = None


def _get_template_repo() -> Path:
    global _TEMPLATE_REPO_ROOT, _TEMPLATE_MAIN_SHA
    if _TEMPLATE_REPO_ROOT is not None and _TEMPLATE_REPO_ROOT.is_dir():
        return _TEMPLATE_REPO_ROOT
    base_tmp = Path(tempfile.mkdtemp(prefix="autoloop-tpl-"))
    atexit.register(lambda p=base_tmp: shutil.rmtree(p, ignore_errors=True))
    repo = base_tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Morris-Zin")
    _git(repo, "config", "user.email", "morris@example.com")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "gc.auto", "0")

    # Copy real trusted config, rules, .gitignore, and synced SKILL.md into the fixture repo.
    shutil.copy2(REPO_ROOT / ".gitignore", repo / ".gitignore")
    shutil.copy2(REPO_ROOT / ".autoloop/config.json", _ensure_parent(repo / ".autoloop/config.json"))
    shutil.copy2(REPO_ROOT / ".autoloop/rules/embryo_core.py", _ensure_parent(repo / ".autoloop/rules/embryo_core.py"))
    shutil.copy2(REPO_ROOT / "tools/autoloop/skill/SKILL.md", _ensure_parent(repo / "tools/autoloop/skill/SKILL.md"))
    for name in ("__init__.py", "guard.py", "static_rules.py"):
        shutil.copy2(REPO_ROOT / "tools/autoloop" / name, repo / "tools/autoloop" / name)
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
    _TEMPLATE_REPO_ROOT = repo
    _TEMPLATE_MAIN_SHA = main_sha
    return repo


def _create_temp_repo(tmp_dir: Path) -> Path:
    repo = tmp_dir / "repo"
    shutil.copytree(_get_template_repo(), repo)
    return repo


def _template_main_sha() -> str:
    _get_template_repo()
    assert _TEMPLATE_MAIN_SHA is not None
    return _TEMPLATE_MAIN_SHA


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestScenario1OrdinaryDocsAndPolicyGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _create_temp_repo(Path(self._tmp.name))
        self.main_sha = _template_main_sha()

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
        self.main_sha = _template_main_sha()

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
                    {
                        "name": "autoloop-candidate-checks",
                        "status": "completed",
                        "conclusion": "success",
                        "app_id": 15368,
                    }
                ]
            },
        )
        # Missing per-shard trusted_shard_results when candidate_checks_result is supplied MUST fail closed
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=checkpoint_head,
                pr_number=55,
                pr_author="devin-worker",
                pr_labels=["autoloop"],
                github_client=gh_client,
                candidate_checks_result="success",
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
            trusted_shard_results={"shard_a": code_tree_hash, "shard_b": code_tree_hash},
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
        self.main_sha = _template_main_sha()

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

    def test_external_findings_use_review_lifecycle_and_reopen_readiness(self) -> None:
        run_id = "external-findings"
        init_run(self.repo, run_id, "Fix incidents", base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(self.repo, run_id, summary="Single shard", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)
        record_shard_pass(self.repo, run_id, "s1", head_sha=self.main_sha, lock_dir=self.lock_dir)
        state = record_finding(
            self.repo, run_id, "incident", severity="P0", source="incident",
            summary="Operator reported a bug", evidence="incident-1", lock_dir=self.lock_dir,
        )
        self.assertEqual(state["findings"]["incident"]["status"], "open")
        with self.assertRaisesRegex(IllegalTransitionError, "open findings"):
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        resolve_finding(self.repo, run_id, "incident", fix_summary="Fix", lock_dir=self.lock_dir)

        _write_lf(self.repo / "src/a.py", "def a():\n    return 42\n")
        _git(self.repo, "commit", "-am", "Fix inherited finding")
        fixed_by = _git(self.repo, "rev-parse", "HEAD")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main([
                "--repo", str(self.repo), "record-finding", "--run-id", run_id, "--finding-id", "inherited",
                "--severity", "P1", "--source", "inherited_review", "--summary", "Prior review issue",
                "--evidence", "prior-review", "--fixed-by", fixed_by[:12],
            ]), 0)
        finding = load_run(self.repo, run_id)["findings"]["inherited"]
        self.assertEqual((finding["status"], finding["fixed_by"], finding["evidence"]),
                         ("fixed_pending_review", fixed_by, "prior-review"))
        with self.assertRaisesRegex(IllegalTransitionError, "already exists"):
            record_finding(
                self.repo, run_id, "inherited", severity="P1", source="review",
                summary="Duplicate", evidence="prior-review", lock_dir=self.lock_dir,
            )
        with self.assertRaisesRegex(IllegalTransitionError, "missing from the gated commit"):
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        record_shard_pass(self.repo, run_id, "s1", head_sha=fixed_by, lock_dir=self.lock_dir)
        state = submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        code_hash = state["shards"]["s1"]["passed_code_tree_sha256"]
        for stage in ("cloud", "deep"):
            state = record_review(
                self.repo, run_id, stage=stage, verdict="pass", reviewer=f"{stage}-reviewer",
                code_tree_sha256=code_hash, lock_dir=self.lock_dir,
            )
        self.assertEqual(state["status"], "safeguard_pending")
        for fid, expected in (("incident", "safeguard_pending"), ("inherited", "ready_to_merge")):
            self.assertEqual(state["findings"][fid]["status"], "closed_needs_safeguard")
            state = record_safeguard(
                self.repo, run_id, fid, layer="layer_1_architecture", artifact="src/a.py",
                summary="Use the shared boundary", lock_dir=self.lock_dir,
            )
            self.assertEqual(state["status"], expected)
        state = record_finding(
            self.repo, run_id, "failure", severity="P2", source="gate_failure",
            summary="Another finding", evidence="gate log", fixed_by=fixed_by, lock_dir=self.lock_dir,
        )
        self.assertEqual(state["status"], "gate_passed")
        self.assertEqual(submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)["status"], "awaiting_cloud_review")

    def test_static_safeguard_requires_registered_rule_and_committed_bad_example(self) -> None:
        run_id = "static-proof"
        fixture = ".autoloop/rules/fixtures/no-bad/src/a.py"
        artifact = ".autoloop/rules/fixture_demo.py"
        _write_lf(self.repo / artifact, (
            "def check(files):\n"
            "    return [{'path': 'src/a.py', 'line': 1, 'message': 'bad'}]"
            " if b'BAD' in files['src/a.py'] else []\n"
            "RULES = [('no-bad', check)]\n"
        ))
        _git(self.repo, "add", artifact)
        _git(self.repo, "commit", "-m", "Add rule")
        init_run(
            self.repo, run_id, "Verify fixture", base_sha=self.main_sha,
            cloud_review=False, deep_review=False, lock_dir=self.lock_dir,
        )
        complete_grill(self.repo, run_id, summary="Single shard", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)
        record_shard_pass(self.repo, run_id, "s1", head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        record_finding(
            self.repo, run_id, "F1", severity="P1", source="incident", summary="Bad input",
            evidence="log", fixed_by=self.main_sha, lock_dir=self.lock_dir,
        )
        submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        options = {
            "layer": "layer_3_static_lint", "artifact": artifact, "summary": "Reject bad input",
            "rule_id": "no-bad", "lock_dir": self.lock_dir,
        }
        _write_lf(self.repo / fixture, "BAD\n")
        with self.assertRaisesRegex(GuardError, "missing bad-example fixture"):
            record_safeguard(self.repo, run_id, "F1", **options)
        _write_lf(self.repo / fixture, "GOOD\n")
        _git(self.repo, "add", fixture)
        _git(self.repo, "commit", "-m", "Add ineffective fixture")
        record_shard_pass(self.repo, run_id, "s1", head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        with self.assertRaisesRegex(GuardError, "does not flag"):
            record_safeguard(self.repo, run_id, "F1", **options)
        rule_source = (self.repo / artifact).read_text(encoding="utf-8")
        _write_lf(self.repo / artifact, "RULES = []\n")
        with self.assertRaisesRegex(GuardError, "check out the gated rule"):
            record_safeguard(self.repo, run_id, "F1", **options)
        _git(self.repo, "commit", "-am", "Remove the named rule")
        record_shard_pass(self.repo, run_id, "s1", head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        with self.assertRaisesRegex(GuardError, "expected exactly one registered rule"):
            record_safeguard(self.repo, run_id, "F1", **options)
        _write_lf(self.repo / artifact, rule_source)
        _write_lf(self.repo / fixture, "BAD\n")
        _git(self.repo, "commit", "-am", "Make fixture exercise the rule")
        head = _git(self.repo, "rev-parse", "HEAD")
        record_shard_pass(self.repo, run_id, "s1", head_sha=head, lock_dir=self.lock_dir)
        state = record_safeguard(self.repo, run_id, "F1", **options)
        self.assertEqual(state["status"], "ready_to_merge")
        proof = state["findings"]["F1"]["safeguard"]
        self.assertEqual(proof["rule_id"], "no-bad")
        self.assertEqual(set(proof["fixture_sha256"]), {fixture})
        candidate = git_read_tree_blobs(self.repo, head)
        finding = state["findings"]["F1"]
        _verify_safeguard_artifact(self.repo, head, candidate, run_id, "F1", finding, 15)
        candidate[fixture] = b"GOOD\n"
        with self.assertRaisesRegex(GuardError, "changed since safeguard recording"):
            _verify_safeguard_artifact(self.repo, head, candidate, run_id, "F1", finding, 15)

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
        self.main_sha = _template_main_sha()

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
            retry_shard(repo, run_id, "s1", check_name="t", error_output="AssertionError", rollback=True, lock_dir=lock_dir)
        self.assertEqual(ctx.exception.exit_code, EXIT_UNSAFE_ROLLBACK)
        with self.assertRaises(UnsafeRollbackError):
            assert_clean_worktree_for_rollback(repo)

    def test_sync_skill_dry_run_default_and_install(self) -> None:
        fake_home = self.tmp_path / "home"
        fake_home.mkdir()
        repo = _create_temp_repo(self.tmp_path)
        dry = cmd_sync_skill(repo, install=False, home=fake_home)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertFalse((fake_home / ".claude/skills/autoloop/SKILL.md").exists())

        installed = cmd_sync_skill(repo, install=True, home=fake_home)
        self.assertEqual(installed["mode"], "install")
        src_bytes = (REPO_ROOT / "tools/autoloop/skill/SKILL.md").read_bytes()
        for rel in (
            ".claude/skills/autoloop/SKILL.md",
            ".codex/skills/autoloop/SKILL.md",
            ".gemini/antigravity/skills/autoloop/SKILL.md",
            ".agents/skills/autoloop/SKILL.md",
        ):
            self.assertEqual((fake_home / rel).read_bytes(), src_bytes)
        for rel in (".devin", ".claude", ".agents"):
            self.assertEqual((repo / rel / "skills/autoloop/SKILL.md").read_bytes(), src_bytes)
            self.assertEqual((REPO_ROOT / rel / "skills/autoloop/SKILL.md").read_bytes(), src_bytes)


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

    def test_live_repo_has_zero_embryo_core_violations(self) -> None:
        self.assertEqual(lint_repo(REPO_ROOT, rules_dir=_get_template_repo() / ".autoloop/rules"), [])

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
            self.assertEqual(cli_main(["--repo", str(_get_template_repo()), "lint"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "sync-skill", "--dry-run"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "bootstrap-repo", "--dry-run"]), 0)


class TestBackendSafeguardRules(unittest.TestCase):
    def setUp(self) -> None:
        self.rules_dir = REPO_ROOT / ".autoloop/rules"
        self.rules = dict(load_rules_from_dir(self.rules_dir))

    def test_each_backend_rule_flags_its_bad_example(self) -> None:
        fixture_root = self.rules_dir / "fixtures"
        backend_rules = {path.name for path in fixture_root.iterdir() if path.is_dir()}
        self.assertTrue({
            "fastpath-must-keep-upstream-validation", "stock-row-locks-only-in-stock-locks",
            "no-chmod-on-operator-paths", "report-columns-have-fieldname-or-key",
            "bench-results-typed-gated", "wsl-scripts-lf", "test-stubs-restored",
        } <= backend_rules)
        for directory in sorted(fixture_root.iterdir()):
            if not directory.is_dir():
                continue
            with self.subTest(rule=directory.name):
                examples = {
                    path.relative_to(directory).as_posix(): path.read_bytes()
                    for path in directory.rglob("*") if path.is_file()
                }
                hits = self.rules[directory.name](examples)
                self.assertTrue(any(hit["path"] in examples for hit in hits))

    def test_stock_locks_accept_the_adr0003_planner_in_plan_order(self) -> None:
        path = "apps/khitmee_ops/khitmee_ops/api/stock_locks.py"
        raw = (TOOLS_DIR / "autoloop/fixtures/stock_locks_adr0003.py").read_bytes()
        rule = self.rules["stock-row-locks-only-in-stock-locks"]
        self.assertEqual(rule({path: raw}), [])
        reversed_items = raw.replace(b"for item in plan.items:", b"for item in reversed(plan.items):")
        self.assertTrue(any("plan.items order" in v["message"] for v in rule({path: reversed_items})))
        orphaned_lock = raw + (
            b"\n\ndef out_of_order():\n"
            b"    frappe.db.sql('SELECT name FROM tabBin WHERE name = %s FOR UPDATE', ('BIN',))\n"
        )
        self.assertTrue(any("ordered stock_locks helpers" in v["message"] for v in rule({path: orphaned_lock})))
        outside = {"apps/khitmee_ops/khitmee_ops/api/other.py": (
            b"def lock():\n    frappe.db.sql('SELECT name FROM tabItem LOCK IN SHARE MODE')\n"
        )}
        self.assertEqual([v["path"] for v in rule(outside)], list(outside))

    def test_stubs_allow_patches_and_matching_cleanups(self) -> None:
        path = "apps/khitmee_ops/khitmee_ops/tests/test_new.py"
        source = b"""
import frappe as f
import unittest
from unittest.mock import patch

class TestReport(unittest.TestCase):
    def setUp(self):
        previous = f.throw
        self.addCleanup(setattr, f, "throw", previous)
        f.throw = lambda message: None
        self.install()

    def install(self):
        self.old_db = f.db
        f.db = object()
        self.addCleanup(self.restore_db)

    def restore_db(self):
        f.db = self.old_db

    def test_report(self):
        with patch.object(f, "get_doc"):
            pass
"""
        rule = self.rules["test-stubs-restored"]
        self.assertEqual(rule({path: source}), [])
        hits = rule({path: source.replace(b' "throw", previous', b' "get_doc", previous')})
        self.assertEqual(len(hits), 1)
        self.assertIn("frappe.throw", hits[0]["message"])

    def test_wsl_attributes_reject_nested_overrides(self) -> None:
        attrs = (REPO_ROOT / ".gitattributes").read_bytes()
        files = {".gitattributes": attrs, "tools/script.sh": b"#!/bin/sh\n"}
        rule = self.rules["wsl-scripts-lf"]
        self.assertEqual(rule(files), [])
        self.assertEqual(
            [hit["path"] for hit in rule({**files, "tools/.gitattributes": b"*.sh text eol=crlf\n"})],
            ["tools/script.sh"],
        )

    def test_legacy_stub_baseline_matches_lf_and_crlf_fixtures(self) -> None:
        path = "apps/khitmee_ops/khitmee_ops/tests/test_memory_hooks_unit.py"
        rule = self.rules["test-stubs-restored"]
        fixture = TOOLS_DIR / "autoloop/fixtures/memory_hooks_legacy.py"
        lf = fixture.read_bytes().replace(b"\r\n", b"\n")
        assignment = b"    frappe.form_dict = {}"
        self.assertIn(assignment, lf)
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                source = lf.replace(b"\n", newline)
                self.assertEqual(rule({path: source}), [])
                changed = source.replace(assignment, b"    frappe.form_dict = {'new': True}")
                duplicate = source.replace(assignment, assignment + newline + assignment)
                for edited in (changed, duplicate):
                    hits = rule({path: edited})
                    self.assertEqual(len(hits), 1)
                    self.assertIn("frappe.form_dict", hits[0]["message"])

    def test_wsl_missing_root_directive_counts_once_even_when_a_shard_adds_scripts(self) -> None:
        rule = self.rules["wsl-scripts-lf"]
        attrs = (REPO_ROOT / ".gitattributes").read_bytes().replace(b"*.py text eol=lf", b"*.py text eol=crlf")
        files = {".gitattributes": attrs, "perf-suite/old.py": b"pass\n"}
        inherited = rule(files)
        self.assertEqual([hit["path"] for hit in inherited], [".gitattributes"])
        files["perf-suite/new.py"] = b"pass\n"
        self.assertEqual(rule(files), inherited)
        files["perf-suite/.gitattributes"] = b"new.py text eol=crlf\n"
        self.assertEqual([hit["path"] for hit in rule(files)], [".gitattributes", "perf-suite/new.py"])


class TestScenario7ReviewFixRegressions(unittest.TestCase):
    """Regression suite covering all 13 Astra findings & 15 PR review threads."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.repo = _create_temp_repo(self.tmp_path)
        self.main_sha = _template_main_sha()
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
        h_bound = compute_code_tree_sha256(self.repo, bound_head, run_id="run-no-pr")
        res_bound = verify_pr_integrity(
            self.repo,
            base_sha=self.main_sha,
            head_sha=bound_head,
            pr_number=99,
            pr_author="contributor",
            pr_labels=[],
            github_client=gh_bound,
            candidate_checks_result="success",
            trusted_shard_results={"s1": h_bound},
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
            trusted_shard_results={"s1": h},
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
            trusted_shard_results={"s1": h},
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
            sha_a = _git(wt_a, "rev-parse", "HEAD")
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
            sha_b = _git(wt_b, "rev-parse", "HEAD")
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "shard_b", "--skip-fast-check"]),
                0,
            )

            # Integrate both shard branches into coordinator checkout and run `--all-shards`
            _git(self.repo, "checkout", "-b", "feat/integrated", self.main_sha)
            _git(self.repo, "merge", "--no-ff", "-m", "merge a", "feat/shard-a")
            _git(self.repo, "merge", "--no-ff", "-m", "merge b", "feat/shard-b")
            int_sha = _git(self.repo, "rev-parse", "HEAD")
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]),
                0,
            )
            st = load_run(self.repo, run_id)
            self.assertEqual(st["status"], "gate_passed")
            self.assertEqual(st["last_integrated_sha"], int_sha)
            # Shard-local rollback anchors MUST stay shard-local (not overwritten with integration commit)
            self.assertEqual(st["shards"]["shard_a"]["last_green_sha"], sha_a)
            self.assertEqual(st["shards"]["shard_b"]["last_green_sha"], sha_b)

            # Review -> reject F1 -> shard_a strikes out -> clean shard-local rollback on wt_a without UnsafeRollbackError!
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
            h1 = st["shards"]["shard_a"]["passed_code_tree_sha256"]
            record_review(
                self.repo, run_id, stage="cloud", verdict="reject", reviewer="alice", code_tree_sha256=h1,
                findings=[{"finding_id": "F1", "summary": "Add test"}], lock_dir=self.lock_dir,
            )
            _write_lf(wt_a / "src/a.py", "def a():\n    return -999\n")
            _git(wt_a, "add", "src/a.py")
            _git(wt_a, "commit", "-m", "bad fix in a")
            for attempt in range(3):
                st_r = retry_shard(
                    self.repo, run_id, "shard_a", check_name="pytest", error_output=f"fail {attempt}",
                    rollback=True, lock_dir=self.lock_dir,
                )
            self.assertEqual(st_r["status"], "needs_replan")
            self.assertEqual((wt_a / "src/a.py").read_text(encoding="utf-8"), "def a():\n    return 11\n")
            apply_replan(self.repo, run_id, plan_summary="Re-run cleanly", shard_id="shard_a", lock_dir=self.lock_dir)
            start_shard(self.repo, run_id, "shard_a", worktree_path=str(wt_a), lock_dir=self.lock_dir)
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--shard", "shard_a", "--skip-fast-check"]),
                0,
            )
            # Refresh integration branch to include shard_a's rollback commit, then verify --all-shards & safeguard flow
            _git(self.repo, "merge", "--no-ff", "-m", "merge a rollback", "feat/shard-a")
            resolve_finding(self.repo, run_id, "F1", fix_summary="fixed", lock_dir=self.lock_dir)
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]),
                0,
            )
            h1_post = load_run(self.repo, run_id)["shards"]["shard_a"]["passed_code_tree_sha256"]
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="cloud", verdict="pass", reviewer="alice", code_tree_sha256=h1_post, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="deep", verdict="pass", reviewer="bob", code_tree_sha256=h1_post, lock_dir=self.lock_dir)
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
            self.assertNotEqual(h2, h1_post)
            submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="cloud", verdict="pass", reviewer="alice", code_tree_sha256=h2, lock_dir=self.lock_dir)
            record_review(self.repo, run_id, stage="deep", verdict="pass", reviewer="bob", code_tree_sha256=h2, lock_dir=self.lock_dir)
            record_safeguard(
                self.repo, run_id, "F1", layer="layer_4_runtime_test", artifact="tools/test_f1.py",
                summary="Added test_f1", why_not_static="Runtime check", lock_dir=self.lock_dir,
            )
            self.assertEqual(load_run(self.repo, run_id)["status"], "ready_to_merge")

            # Base refresh & post-ready contract amendment work cleanly AND clear has_passed_individually!
            st_req = request_contract_change(self.repo, run_id, "shard_a", reason="Expand", proposed_paths=["README.md"], lock_dir=self.lock_dir)
            self.assertEqual(st_req["status"], "needs_contract_amendment")
            resolve_contract_change(self.repo, run_id, decision="approve", add_paths={"shard_a": ["README.md"]}, lock_dir=self.lock_dir)
            _git(self.repo, "checkout", "main")
            _write_lf(self.repo / "README.md", "# Updated main M2\n")
            _git(self.repo, "add", "README.md")
            _git(self.repo, "commit", "-m", "main M2")
            m2_sha = _git(self.repo, "rev-parse", "HEAD")
            st_ref = refresh_base(self.repo, run_id, new_base_sha=m2_sha, lock_dir=self.lock_dir)
            self.assertEqual(st_ref["status"], "locked")
            self.assertFalse(st_ref["shards"]["shard_a"]["has_passed_individually"])
            self.assertFalse(st_ref["shards"]["shard_b"]["has_passed_individually"])
            start_shard(self.repo, run_id, "shard_a", worktree_path=str(wt_a), lock_dir=self.lock_dir)
            start_shard(self.repo, run_id, "shard_b", worktree_path=str(wt_b), lock_dir=self.lock_dir)
            self.assertEqual(
                cli_main(["--repo", str(self.repo), "check", "--run-id", run_id, "--all-shards", "--skip-fast-check"]),
                EXIT_ILLEGAL_TRANSITION,
            )
        finally:
            subprocess.run(["git", "checkout", "main"], cwd=self.repo, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", str(wt_a)], cwd=self.repo, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", str(wt_b)], cwd=self.repo, capture_output=True)

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
            st_rb = retry_shard(rb_repo, run2, "s1", check_name="t", error_output="AssertionError", rollback=True, lock_dir=self.lock_dir)
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
        # Lessons recorded by record-lesson for this run are framework metadata; others are not.
        self.assertTrue(is_framework_metadata_path(f".autoloop/lessons/{run2}-{'a1' * 16}.json", run2))
        self.assertFalse(is_framework_metadata_path(f".autoloop/lessons/{run2}-notahexid.json", run2))
        self.assertFalse(is_framework_metadata_path(f".autoloop/lessons/other-run-{'a1' * 16}.json", run2))
        self.assertFalse(is_framework_metadata_path(f".autoloop/lessons/{run2}-{'a1' * 16}.py", run2))
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
        # Run IDs invalid in CI (`RUN_FILE_RE`) MUST be rejected at init_run
        for bad_id in ("quarter close", "-leading-dash", "bad/slash"):
            with self.assertRaises(IllegalTransitionError):
                init_run(self.repo, bad_id, "Bad ID", base_sha=self.main_sha, lock_dir=self.lock_dir)

        # lock_run(base_sha=new_base) MUST update every shard's initial last_green_sha to the locked base
        _git(self.repo, "checkout", "-b", "feat/new-base")
        _write_lf(self.repo / "src/b.py", "def b():\n    return 99\n")
        _git(self.repo, "add", "src/b.py")
        _git(self.repo, "commit", "-m", "upstream b change")
        new_base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")
        run_lk = "run-lock-base"
        init_run(self.repo, run_lk, "Lock base", base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(self.repo, run_lk, summary="g", shards={"a": ["src/a.py"]}, lock_dir=self.lock_dir)
        st_lk = lock_run(self.repo, run_lk, base_sha=new_base, lock_dir=self.lock_dir)
        self.assertEqual(st_lk["shards"]["a"]["last_green_sha"], new_base)

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

    def test_round2_security_hardening_contracts_provenance_app_id_and_stacked_threads(self) -> None:
        # 1) Unsigned owner-attributed policy commit MUST be rejected without explicit approval
        _git(self.repo, "checkout", "-b", "feat/unsigned-policy")
        _write_lf(self.repo / "AGENTS.md", "## Unsigned policy edit\n")
        _git(self.repo, "add", "AGENTS.md")
        _git(self.repo, "commit", "-m", "unsigned owner commit")
        pol_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        gh_unsigned = FakeGitHubClient(
            pr_commits=[
                {
                    "sha": pol_head,
                    "author_login": "Morris-Zin",
                    "committer_login": "Morris-Zin",
                    "author_type": "User",
                    "committer_type": "User",
                    "verified": False,
                }
            ]
        )
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo,
                base_sha=self.main_sha,
                head_sha=pol_head,
                pr_number=201,
                pr_author="Morris-Zin",
                pr_labels=[],
                github_client=gh_unsigned,
            )

        gh_verified = FakeGitHubClient(
            pr_commits=[
                {
                    "sha": pol_head,
                    "author_login": "Morris-Zin",
                    "committer_login": "Morris-Zin",
                    "author_type": "User",
                    "committer_type": "User",
                    "verified": True,
                }
            ]
        )
        res_ver = verify_pr_integrity(
            self.repo,
            base_sha=self.main_sha,
            head_sha=pol_head,
            pr_number=201,
            pr_author="Morris-Zin",
            pr_labels=[],
            github_client=gh_verified,
        )
        self.assertEqual(res_ver["verdict"], "non-enrolled-pass")

        # 2) Contract selection across ALL discovered issues + conflict rejection + check-run app_id + stacked threads
        run_id = "run-r2-contract"
        init_run(
            self.repo, run_id, "R2 contract", issue_number=30, pr_number=202,
            base_sha=self.main_sha, cloud_review=False, deep_review=False, lock_dir=self.lock_dir,
        )
        complete_grill(self.repo, run_id, summary="g", shards={"s1": ["src/a.py"]}, lock_dir=self.lock_dir)
        lock_run(self.repo, run_id, base_sha=self.main_sha, pr_number=202, lock_dir=self.lock_dir)
        pub_rev1 = publish_contract(self.repo, run_id, lock_dir=self.lock_dir)
        start_shard(self.repo, run_id, "s1", lock_dir=self.lock_dir)

        _git(self.repo, "checkout", "-b", "feat/r2-contract")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 55\n")
        _git(self.repo, "add", "src/a.py")
        _git(self.repo, "commit", "-m", "edit a")
        h_a = compute_code_tree_sha256(self.repo, "HEAD", run_id=run_id)
        record_shard_pass(self.repo, run_id, "s1", code_tree_sha256=h_a, head_sha=_git(self.repo, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
        submit_for_review(self.repo, run_id, lock_dir=self.lock_dir)
        _git(self.repo, "add", f".autoloop/runs/{run_id}.json")
        _git(self.repo, "commit", "-m", "checkpoint rev1")
        pr_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        # Newer rev=2 posted on ANOTHER discovered issue (#31) MUST supersede rev=1 on #30 (failing rev mismatch)
        c_rev2 = json.loads((self.repo / f".autoloop/trusted-contracts/{run_id}.rev1.json").read_text(encoding="utf-8"))
        c_rev2["contract_revision"] = 2
        gh_newer_other_issue = FakeGitHubClient(
            comments_by_number={
                30: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub_rev1["comment_markdown"]}],
                31: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": format_contract_comment(c_rev2)}],
            },
            contract_issue_numbers=[30, 31],
        )
        with self.assertRaises(GuardError) as ctx_rev:
            verify_pr_integrity(
                self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=202,
                pr_author="worker", pr_labels=[], github_client=gh_newer_other_issue,
                candidate_checks_result="success", trusted_shard_results={"s1": h_a},
            )
        self.assertIn("contract_revision mismatch", str(ctx_rev.exception))

        # Conflicting contracts at same revision MUST raise GuardError
        c_conflict = json.loads((self.repo / f".autoloop/trusted-contracts/{run_id}.rev1.json").read_text(encoding="utf-8"))
        c_conflict["goal"] = "Tampered goal at same rev"
        gh_conflict = FakeGitHubClient(
            comments_by_number={
                30: [
                    {"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub_rev1["comment_markdown"]},
                    {"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": format_contract_comment(c_conflict)},
                ],
            },
        )
        with self.assertRaises(GuardError) as ctx_conf:
            verify_pr_integrity(
                self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=202,
                pr_author="worker", pr_labels=[], github_client=gh_conflict,
                candidate_checks_result="success", trusted_shard_results={"s1": h_a},
            )
        self.assertIn("conflicting owner contracts", str(ctx_conf.exception))

        # Order-independent contract conflict check: all 6 permutations of (rev1_a, rev1_b, rev2) select rev2,
        # while conflicting highest rev ALWAYS raises GuardError.
        c_rev1_a = {"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub_rev1["comment_markdown"]}
        c_rev1_b = {"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": format_contract_comment(c_conflict)}
        c_rev2_c = {"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": format_contract_comment(c_rev2)}
        for perm in itertools.permutations([c_rev1_a, c_rev1_b, c_rev2_c]):
            selected = parse_latest_owner_contract(list(perm), {"Morris-Zin"}, run_id=run_id)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["contract_revision"], 2)

        # Standalone finalize WITHOUT per-shard trusted_shard_results MUST fail closed (even with check_runs)
        gh_no_att = FakeGitHubClient(
            comments_by_number={
                30: [{"author": "Morris-Zin", "editor": None, "lastEditedAt": None, "body": pub_rev1["comment_markdown"]}],
            },
            check_runs={
                pr_head: [{"name": "autoloop-candidate-checks", "status": "completed", "conclusion": "success", "app_id": 15368}]
            },
            review_threads=[{"id": "PRRT_STACK", "isResolved": False, "comments": []}],
        )
        with self.assertRaises(GuardError):
            verify_pr_integrity(
                self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=202,
                pr_author="worker", pr_labels=[], github_client=gh_no_att,
            )

        # Stacked target branch MUST fail on unresolved threads; default target branch passes with attestation
        with self.assertRaises(GuardError) as ctx_stack:
            verify_pr_integrity(
                self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=202,
                pr_author="worker", pr_labels=[], base_branch="feat/parent", github_client=gh_no_att,
                candidate_checks_result="success", trusted_shard_results={"s1": h_a},
            )
        self.assertIn("unresolved review thread", str(ctx_stack.exception))
        ok_main = verify_pr_integrity(
            self.repo, base_sha=self.main_sha, head_sha=pr_head, pr_number=202,
            pr_author="worker", pr_labels=[], base_branch="main", github_client=gh_no_att,
            candidate_checks_result="success", trusted_shard_results={"s1": h_a},
        )
        self.assertEqual(ok_main["verdict"], "enrolled-pass")

    def test_round2_timeline_filters_cross_repo_and_incidental_not_found(self) -> None:
        client = GitHubAPIClient("Morris-Zin/Embryo", token="fake")
        responses = [
            {
                "repository": {
                    "pullRequest": {
                        "timelineItems": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "__typename": "CrossReferencedEvent",
                                    "isCrossRepository": True,
                                    "source": {"__typename": "Issue", "number": 480, "repository": {"nameWithOwner": "other/repo"}},
                                },
                                {
                                    "__typename": "CrossReferencedEvent",
                                    "isCrossRepository": False,
                                    "source": {"__typename": "Issue", "number": 10, "repository": {"nameWithOwner": "Morris-Zin/Embryo"}},
                                },
                                {
                                    "__typename": "ConnectedEvent",
                                    "isCrossRepository": False,
                                    "subject": {"__typename": "Issue", "number": 15, "repository": {"nameWithOwner": "other/fork"}},
                                },
                                {
                                    "__typename": "ConnectedEvent",
                                    "isCrossRepository": False,
                                    "subject": {"__typename": "Issue", "number": 12, "repository": {"nameWithOwner": "morris-zin/embryo"}},
                                },
                            ],
                        }
                    }
                }
            },
            {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {"__typename": "Issue", "number": 14, "repository": {"nameWithOwner": "Morris-Zin/Embryo"}},
                        {"__typename": "Issue", "number": 999, "repository": {"nameWithOwner": "someone/else"}},
                    ],
                }
            },
        ]
        with mock.patch.object(client, "_graphql", side_effect=responses):
            nums = client.find_contract_issue_numbers(11)
        self.assertEqual(nums, [10, 12, 14])

        # Incidental `#9999` in PR body returning GraphQLNotFoundError must NOT break ordinary PRs
        _git(self.repo, "checkout", "-b", "feat/incidental-ref")
        _write_lf(self.repo / "docs/guide.md", "Mentions upstream #9999\n")
        _git(self.repo, "add", "docs/guide.md")
        _git(self.repo, "commit", "-m", "docs mention #9999")
        inc_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        class _NotFoundGH(FakeGitHubClient):
            def get_issue_comments_graphql(self, issue_or_pr_number: int) -> list[dict]:
                if int(issue_or_pr_number) == 9999:
                    raise GraphQLNotFoundError("Could not resolve to an issue or pull request with the number of 9999")
                return super().get_issue_comments_graphql(issue_or_pr_number)

        res = verify_pr_integrity(
            self.repo, base_sha=self.main_sha, head_sha=inc_head, pr_number=203,
            pr_author="contributor", pr_labels=[], pr_body="Relates to #9999",
            github_client=_NotFoundGH(forbid_check_runs=True),
        )
        self.assertEqual(res["verdict"], "non-enrolled-pass")

    def test_round2_safeguard_rejects_readme_and_untouched_tests(self) -> None:
        _git(self.repo, "checkout", "-b", "feat/sg-readme")
        _write_lf(self.repo / "perf-suite/offload/README.md", "# Readme\n")
        _write_lf(self.repo / "perf-suite/k6/scenarios/report_stress.js", "export const options = {};\n")
        _write_lf(self.repo / "perf-suite/k6/scenarios/README.md", "# K6 Readme\n")
        _write_lf(self.repo / "tools/test_real.py", "import unittest\n")
        _git(
            self.repo,
            "add",
            "perf-suite/offload/README.md",
            "perf-suite/k6/scenarios/report_stress.js",
            "perf-suite/k6/scenarios/README.md",
            "tools/test_real.py",
        )
        _git(self.repo, "commit", "-m", "add readme, k6 scenario, and test")
        head = _git(self.repo, "rev-parse", "HEAD")
        fmap = git_read_tree_blobs(self.repo, head)
        _git(self.repo, "checkout", "main")

        # READMEs under perf-suite/ MUST be rejected for layer_4_runtime_test
        for bad_doc in ("perf-suite/offload/README.md", "perf-suite/k6/scenarios/README.md"):
            with self.assertRaises(GuardError):
                _verify_safeguard_artifact(
                    self.repo, head, fmap, "run-sg", "F1",
                    {
                        "safeguard": {
                            "layer": "layer_4_runtime_test",
                            "artifact": bad_doc,
                            "summary": "Docs",
                            "why_not_static": "Runtime check",
                        }
                    },
                    15,
                    changed_paths={bad_doc, "tools/test_real.py"},
                )
        # Changed k6 scenario JS PASSES; untouched k6 scenario FAILS
        _verify_safeguard_artifact(
            self.repo, head, fmap, "run-sg", "F1",
            {
                "safeguard": {
                    "layer": "layer_4_runtime_test",
                    "artifact": "perf-suite/k6/scenarios/report_stress.js",
                    "summary": "Added k6 threshold",
                    "why_not_static": "Runtime latency threshold",
                }
            },
            15,
            changed_paths={"perf-suite/k6/scenarios/report_stress.js"},
        )
        with self.assertRaises(GuardError):
            _verify_safeguard_artifact(
                self.repo, head, fmap, "run-sg", "F1",
                {
                    "safeguard": {
                        "layer": "layer_4_runtime_test",
                        "artifact": "perf-suite/k6/scenarios/report_stress.js",
                        "summary": "Untouched k6",
                        "why_not_static": "Runtime latency threshold",
                    }
                },
                15,
                changed_paths=set(),
            )
        # Untouched test file (not in changed_paths) MUST be rejected
        with self.assertRaises(GuardError):
            _verify_safeguard_artifact(
                self.repo, head, fmap, "run-sg", "F1",
                {
                    "safeguard": {
                        "layer": "layer_4_runtime_test",
                        "artifact": "tools/test_real.py",
                        "summary": "Pre-existing test",
                        "why_not_static": "Runtime check",
                    }
                },
                15,
                changed_paths=set(),
            )
        # Real modified test file PASSES
        _verify_safeguard_artifact(
            self.repo, head, fmap, "run-sg", "F1",
            {
                "safeguard": {
                    "layer": "layer_4_runtime_test",
                    "artifact": "tools/test_real.py",
                    "summary": "Added regression test",
                    "why_not_static": "Runtime check",
                }
            },
            15,
            changed_paths={"tools/test_real.py"},
        )

    def test_round2_local_check_frozen_commands_lease_token_amended_checks_and_integrated_rollback(self) -> None:
        # 1) Lease reacquisition without existing token MUST fail with LeaseConflictError
        l1 = acquire_lease("res:r2-tok", "worker-1", ttl_s=60.0, holder_pid=os.getpid(), lock_dir=self.lock_dir)
        with self.assertRaises(LeaseConflictError):
            acquire_lease("res:r2-tok", "worker-1", ttl_s=60.0, holder_pid=os.getpid(), lock_dir=self.lock_dir)
        l2 = acquire_lease(
            "res:r2-tok", "worker-1", ttl_s=120.0, holder_pid=os.getpid(), token=l1["token"], lock_dir=self.lock_dir
        )
        self.assertEqual(l2["token"], l1["token"])
        release_lease("res:r2-tok", "worker-1", token=l1["token"], lock_dir=self.lock_dir)

        # 2) Staged rename from src/b.py -> .autoloop/runs/demo.json MUST be refused by BOTH rollback and check cleanliness guards
        _write_lf(self.repo / ".autoloop/runs/demo.json", "{}\n")
        _git(self.repo, "add", ".autoloop/runs/demo.json")
        _git(self.repo, "mv", "-f", "src/b.py", ".autoloop/runs/demo.json")
        with self.assertRaises(UnsafeRollbackError):
            assert_clean_worktree_for_rollback(self.repo, allow_paths={".autoloop/runs/demo.json"})
        with self.assertRaises(GuardError):
            assert_clean_worktree_for_check(self.repo, run_id="demo")
        _git(self.repo, "reset", "--hard", "HEAD")
        (self.repo / ".autoloop/runs/demo.json").unlink(missing_ok=True)

        # 3) Local `check` runs FrozenContractV1 checks even under --skip-fast-check & rejects mutable tampering
        run_chk = "run-r2-frozen-chk"
        init_run(self.repo, run_chk, "Frozen check", base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(
            self.repo, run_chk, summary="g", shards={"s1": ["src/a.py"]},
            shard_checks={"s1": [f'"{sys.executable}" -c "import sys; sys.exit(1)"']},
            lock_dir=self.lock_dir,
        )
        lock_run(self.repo, run_chk, base_sha=self.main_sha, lock_dir=self.lock_dir)
        start_shard(self.repo, run_chk, "s1", lock_dir=self.lock_dir)
        # --skip-fast-check MUST still execute frozen shard check and fail!
        self.assertEqual(
            cli_main(["--repo", str(self.repo), "check", "--run-id", run_chk, "--shard", "s1", "--skip-fast-check"]),
            EXIT_POLICY_OR_INTEGRITY,
        )
        # Clearing mutable checkpoint checks MUST be detected by verify_untampered
        run_file = self.repo / f".autoloop/runs/{run_chk}.json"
        tampered = json.loads(run_file.read_text(encoding="utf-8"))
        tampered["shards"]["s1"]["checks"] = []
        run_file.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(GuardError):
            verify_untampered(self.repo, run_chk, shard_id="s1")

        # 4) Amending shard B's checks MUST reopen shard B to locked and block check --all-shards
        run_am = "run-r2-amend-chk"
        init_run(self.repo, run_am, "Amend checks", base_sha=self.main_sha, lock_dir=self.lock_dir)
        complete_grill(
            self.repo, run_am, summary="g", shards={"a": ["src/a.py"], "b": ["src/b.py"]}, lock_dir=self.lock_dir,
        )
        lock_run(self.repo, run_am, base_sha=self.main_sha, lock_dir=self.lock_dir)
        start_shard(self.repo, run_am, "a", lock_dir=self.lock_dir)
        start_shard(self.repo, run_am, "b", lock_dir=self.lock_dir)
        h0 = compute_code_tree_sha256(self.repo, "HEAD", run_id=run_am)
        record_shard_pass(self.repo, run_am, "a", code_tree_sha256=h0, head_sha=self.main_sha, lock_dir=self.lock_dir)
        record_shard_pass(self.repo, run_am, "b", code_tree_sha256=h0, head_sha=self.main_sha, lock_dir=self.lock_dir)
        request_contract_change(self.repo, run_am, "a", reason="Add check on b", lock_dir=self.lock_dir)
        st_am = resolve_contract_change(
            self.repo, run_am, decision="approve",
            shard_checks={"b": [f'"{sys.executable}" -c "print(1)"']}, lock_dir=self.lock_dir,
        )
        self.assertEqual(st_am["shards"]["b"]["status"], "locked")
        self.assertFalse(st_am["shards"]["b"]["has_passed_individually"])

        # 5) Integrated post-review safeguard failure in a SEPARATE linked integration worktree
        # rolls back that integration worktree (leaving coordinator on main untouched!), AND
        # refuses explicit integrated retry before integration or when branch switched!
        int_parent = self.tmp_path / "int_rb_repo"
        int_parent.mkdir(parents=True, exist_ok=True)
        int_repo = _create_temp_repo(int_parent)
        int_main = _git(int_repo, "rev-parse", "HEAD")
        run_int = "run-r2-int-rb"
        init_run(
            int_repo, run_int, "Int rollback", base_sha=int_main,
            cloud_review=False, deep_review=False, lock_dir=self.lock_dir,
        )
        complete_grill(int_repo, run_int, summary="g", shards={"a": ["src/a.py"], "b": ["src/b.py"]}, lock_dir=self.lock_dir)
        lock_run(int_repo, run_int, base_sha=int_main, lock_dir=self.lock_dir)
        wt_a = self.tmp_path / "int_wt_a"
        wt_b = self.tmp_path / "int_wt_b"
        wt_int = self.tmp_path / "int_wt_combined"
        _git(int_repo, "worktree", "add", "-b", "irb/a", str(wt_a), "main")
        _git(int_repo, "worktree", "add", "-b", "irb/b", str(wt_b), "main")
        _git(int_repo, "worktree", "add", "-b", "irb/int", str(wt_int), "main")
        try:
            start_shard(int_repo, run_int, "a", worktree_path=str(wt_a), last_green_sha=int_main, lock_dir=self.lock_dir)
            start_shard(int_repo, run_int, "b", worktree_path=str(wt_b), last_green_sha=int_main, lock_dir=self.lock_dir)
            # Explicit --integrated before any integrated pass MUST fail closed
            with self.assertRaises(IllegalTransitionError):
                retry_shard(
                    int_repo, run_int, "a", check_name="c", error_output="e", integrated=True, rollback=True, lock_dir=self.lock_dir
                )

            _write_lf(wt_a / "src/a.py", "def a():\n    return 11\n")
            _git(wt_a, "add", "src/a.py")
            _git(wt_a, "commit", "-m", "a")
            _write_lf(wt_b / "src/b.py", "def b():\n    return 22\n")
            _git(wt_b, "add", "src/b.py")
            _git(wt_b, "commit", "-m", "b")
            self.assertEqual(cli_main(["--repo", str(int_repo), "check", "--run-id", run_int, "--shard", "a", "--skip-fast-check"]), 0)
            self.assertEqual(cli_main(["--repo", str(int_repo), "check", "--run-id", run_int, "--shard", "b", "--skip-fast-check"]), 0)

            # Merge in linked integration worktree while coordinator stays on main
            _git(wt_int, "merge", "--no-ff", "irb/a", "-m", "merge a")
            _git(wt_int, "merge", "--no-ff", "irb/b", "-m", "merge b")
            int_sha = _git(wt_int, "rev-parse", "HEAD")
            self.assertEqual(
                cli_main(["--repo", str(int_repo), "check", "--run-id", run_int, "--all-shards", "--worktree", str(wt_int), "--skip-fast-check"]),
                0,
            )
            st = load_run(int_repo, run_int)
            self.assertEqual(st["last_integrated_worktree"], str(wt_int.resolve()))
            self.assertEqual(st["last_integrated_branch"], "irb/int")

            st["findings"]["F-SG"] = {"finding_id": "F-SG", "status": "fixed_pending_review", "summary": "Add guard"}
            (int_repo / f".autoloop/runs/{run_int}.json").write_text(json.dumps(st), encoding="utf-8")
            self.assertEqual(submit_for_review(int_repo, run_int, lock_dir=self.lock_dir)["status"], "safeguard_pending")

            # Commit failing safeguard in wt_int; coordinator remains on main
            _write_lf(wt_int / "src/a.py", "def a():\n    return -1\n")
            _git(wt_int, "add", "src/a.py")
            _git(wt_int, "commit", "-m", "broken safeguard commit")
            for idx in range(3):
                st_rb = retry_shard(
                    int_repo, run_int, "a", check_name="fast_check", error_output=f"err {idx}",
                    rollback=True, lock_dir=self.lock_dir,
                )
            self.assertEqual(st_rb["status"], "needs_replan")
            # Linked integration worktree rolled back cleanly; coordinator `main` untouched!
            self.assertEqual((wt_int / "src/a.py").read_text(encoding="utf-8"), "def a():\n    return 11\n")
            self.assertEqual((wt_int / "src/b.py").read_text(encoding="utf-8"), "def b():\n    return 22\n")
            self.assertEqual(_git(int_repo, "rev-parse", "HEAD"), int_main)
            self.assertEqual((int_repo / "src/a.py").read_text(encoding="utf-8"), "def a():\n    return 1\n")
            self.assertEqual(st_rb["history"][-1].get("rolled_back_to"), int_sha)

            # Detached integration pass MUST record last_integrated_detached=True and refuse rollback
            # even if later switched to a named branch literally called "__DETACHED__"
            apply_replan(int_repo, run_int, plan_summary="Detached check", shard_id="a", lock_dir=self.lock_dir)
            start_shard(int_repo, run_int, "a", worktree_path=str(wt_a), lock_dir=self.lock_dir)
            record_shard_pass(int_repo, run_int, "a", code_tree_sha256=compute_code_tree_sha256(wt_a, "HEAD", run_id=run_int), head_sha=_git(wt_a, "rev-parse", "HEAD"), lock_dir=self.lock_dir)
            _git(wt_int, "checkout", "--detach", int_sha)
            st_det = record_all_shards_pass(
                int_repo,
                run_int,
                code_tree_sha256=compute_code_tree_sha256(wt_int, "HEAD", run_id=run_int),
                head_sha=int_sha,
                worktree_path=str(wt_int),
                lock_dir=self.lock_dir,
            )
            self.assertTrue(st_det["last_integrated_detached"])
            self.assertIsNone(st_det["last_integrated_branch"])
            # Switch wt_int onto a branch literally named "__DETACHED__" descending from int_sha -> MUST refuse rollback!
            _git(wt_int, "checkout", "-b", "__DETACHED__")
            _write_lf(wt_int / "src/a.py", "def a():\n    return 777\n")
            _git(wt_int, "add", "src/a.py")
            _git(wt_int, "commit", "-m", "other branch work")
            retry_shard(int_repo, run_int, "a", check_name="c", error_output="e1", integrated=True, lock_dir=self.lock_dir)
            retry_shard(int_repo, run_int, "a", check_name="c", error_output="e2", integrated=True, lock_dir=self.lock_dir)
            with self.assertRaises(UnsafeRollbackError):
                retry_shard(int_repo, run_int, "a", check_name="c", error_output="e3", integrated=True, rollback=True, lock_dir=self.lock_dir)
            self.assertEqual((wt_int / "src/a.py").read_text(encoding="utf-8"), "def a():\n    return 777\n")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt_a)], cwd=int_repo, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", str(wt_b)], cwd=int_repo, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", str(wt_int)], cwd=int_repo, capture_output=True)

    def test_round2_static_rules_worker_zero_and_export_edge_cases(self) -> None:
        rules_dir = REPO_ROOT / ".autoloop/rules"
        base_skills = {
            "tools/autoloop/skill/SKILL.md": (REPO_ROOT / "tools/autoloop/skill/SKILL.md").read_bytes(),
            ".devin/skills/autoloop/SKILL.md": (REPO_ROOT / ".devin/skills/autoloop/SKILL.md").read_bytes(),
        }

        # Rule 2: `workers == 0` MUST NOT protect a single-worker (workers == 1) process!
        v_zero = evaluate_file_map(
            rules_dir,
            {
                **base_skills,
                "apps/khitmee_ops/khitmee_ops/hooks.py": b'after_request = ["khitmee_ops.api.m.bad0"]\n',
                "apps/khitmee_ops/khitmee_ops/api/m.py": (
                    b"import os, signal\n"
                    b"def bad0(workers=1):\n"
                    b"    if workers == 0:\n        return\n"
                    b"    os.kill(os.getpid(), signal.SIGTERM)\n"
                ),
            },
        )
        self.assertTrue(any(v.rule_id == "no-self-sigterm-single-worker" for v in v_zero))

        report_path = "apps/khitmee_ops/khitmee_ops/api/rep.py"

        def _hits(code: bytes) -> list:
            return [
                v for v in evaluate_file_map(rules_dir, {**base_skills, report_path: code})
                if v.rule_id == "no-background-email-export"
            ]

        # DictComp filtering value (not emitted key) MUST FAIL
        self.assertTrue(
            _hits(
                b'def run(**kwargs):\n'
                b'    kwargs = {k: v for k, v in kwargs.items() if v != "export_in_background"}\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        # Closed flag-free dictionary literal MUST PASS
        self.assertEqual(
            _hits(
                b'def run(**kwargs):\n'
                b'    kwargs = {"format": kwargs.get("format")}\n'
                b'    return export_query(**kwargs)\n'
            ),
            [],
        )
        # Computed key after False override MUST FAIL (named & inline)
        self.assertTrue(
            _hits(
                b'def run(opt, val, **kwargs):\n'
                b'    kwargs = {**kwargs, "export_in_background": False, opt: val}\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        self.assertTrue(
            _hits(
                b'def run(opt, val, **kwargs):\n'
                b'    return export_query(**{**kwargs, "export_in_background": False, opt: val})\n'
            )
        )
        # Unconditional pop in `if` condition MUST PASS; short-circuited RHS MUST FAIL
        self.assertEqual(
            _hits(
                b'def run(**kwargs):\n'
                b'    if kwargs.pop("export_in_background", False):\n'
                b'        return None\n'
                b'    return export_query(**kwargs)\n'
            ),
            [],
        )
        self.assertTrue(
            _hits(
                b'def run(flag, **kwargs):\n'
                b'    if flag and kwargs.pop("export_in_background", False):\n'
                b'        return None\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        # Lambda default with truthy export_in_background MUST FAIL
        self.assertTrue(
            _hits(
                b'def run():\n'
                b'    return lambda r=export_query(export_in_background=True): r\n'
            )
        )
        # Try/except where kwargs is rebound before exception MUST FAIL in except
        self.assertTrue(
            _hits(
                b'def run(**kwargs):\n'
                b'    kwargs = {"export_in_background": False}\n'
                b'    try:\n'
                b'        kwargs = frappe.form_dict\n'
                b'        prepare()\n'
                b'    except ValueError:\n'
                b'        return export_query(**kwargs)\n'
            )
        )
        # Try/finally where kwargs is popped before return MUST PASS in finally;
        # if prepare() runs BEFORE pop, an exception can enter finally unsanitized -> MUST FAIL
        self.assertEqual(
            _hits(
                b'def run(**kwargs):\n'
                b'    try:\n'
                b'        kwargs.pop("export_in_background", None)\n'
                b'        return prepare()\n'
                b'    finally:\n'
                b'        export_query(**kwargs)\n'
            ),
            [],
        )
        self.assertTrue(
            _hits(
                b'def run(**kwargs):\n'
                b'    try:\n'
                b'        prepare()\n'
                b'        kwargs.pop("export_in_background", None)\n'
                b'    finally:\n'
                b'        export_query(**kwargs)\n'
            )
        )
        # Early return in try before sanitization MUST NOT poison export AFTER finally!
        self.assertEqual(
            _hits(
                b'def run(skip, **kwargs):\n'
                b'    try:\n'
                b'        if skip:\n'
                b'            return None\n'
                b'        kwargs.pop("export_in_background", None)\n'
                b'    finally:\n'
                b'        cleanup()\n'
                b'    return export_query(**kwargs)\n'
            ),
            [],
        )
        # kwargs = {}; kwargs.update(frappe.form_dict) and `|=` MUST invalidate safe mark; safe update PASSES
        self.assertTrue(
            _hits(
                b'def run():\n'
                b'    kwargs = {}\n'
                b'    kwargs.update(frappe.form_dict)\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        self.assertTrue(
            _hits(
                b'def run():\n'
                b'    kwargs = {}\n'
                b'    kwargs |= frappe.form_dict\n'
                b'    return export_query(**kwargs)\n'
            )
        )
        self.assertEqual(
            _hits(
                b'def run():\n'
                b'    kwargs = {}\n'
                b'    kwargs.update({"format": "csv"})\n'
                b'    return export_query(**kwargs)\n'
            ),
            [],
        )
        # 1-arg setdefault("export_in_background") defaults to None -> safe; truthy default -> unsafe
        self.assertEqual(
            _hits(
                b'def run():\n'
                b'    kwargs = {}\n'
                b'    kwargs.setdefault("export_in_background")\n'
                b'    return export_query(**kwargs)\n'
            ),
            [],
        )
        self.assertTrue(
            _hits(
                b'def run(flag):\n'
                b'    kwargs = {}\n'
                b'    kwargs.setdefault("export_in_background", flag)\n'
                b'    return export_query(**kwargs)\n'
            )
        )

    def test_cli_smoke_lint_and_dry_run_sync(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli_main(["--repo", str(_get_template_repo()), "lint"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "sync-skill", "--dry-run"]), 0)
            self.assertEqual(cli_main(["--repo", str(REPO_ROOT), "bootstrap-repo", "--dry-run"]), 0)


class TestWorkflowMetadata(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = _create_temp_repo(self.root)
        self.base = _template_main_sha()

    def cli(self, *args: str, expected: int = 0) -> tuple[dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(["--repo", str(self.repo), *args])
        self.assertEqual(code, expected, f"{stdout.getvalue()}\n{stderr.getvalue()}")
        return json.loads(stdout.getvalue() or "{}"), stderr.getvalue()

    def create_run(
        self, run_id: str, *extra: str, shard: str = "a=src/a.py", check: str | None = None,
    ) -> None:
        self.cli("init", "--run-id", run_id, "--goal", "Example", *extra)
        checks = ("--shard-check", f"a={check}") if check else ()
        self.cli("complete-grill", "--run-id", run_id, "--summary", "Agreed", "--shard", shard, *checks)
        self.cli("lock", "--run-id", run_id)

    def test_lessons_overlap_and_interview_drafts(self) -> None:
        for left, right in [
            ("src/**/test_*.py", "src/[a-z]*/test_?.py"),
            ("src/**/**", "src"),
            ("src/[!a-c].py", "src/[b-d].py"),
        ]:
            self.assertTrue(globs_overlap(left, right), (left, right))
            self.assertTrue(globs_overlap(right, left))
        for left, right in [
            ("src/*.py", "src/sub/*.py"),
            ("src/*a.py", "src/*b.py"),
            ("Src/**", "src/**"),
            ("src/[a-c].py", "src/[d-f].py"),
        ]:
            self.assertFalse(globs_overlap(left, right), (left, right))
        self.create_run("earlier", "--no-cloud-review", "--no-deep-review")
        self.cli("start-shard", "--run-id", "earlier", "--shard", "a")
        self.cli("log-dead-end", "--run-id", "earlier", "--shard", "a", "--signature", "check:error:src/a.py",
                 "--hypothesis", "Used stale data", "--outcome", "Wrong result")
        record_shard_pass(self.repo, "earlier", "a", head_sha=self.base)
        state = load_run(self.repo, "earlier")
        state["findings"]["rule"] = {"status": "fixed_pending_review", "summary": "Document the guard"}
        _write_lf(self.repo / ".autoloop/runs/earlier.json", json.dumps(state) + "\n")
        self.cli("submit-for-review", "--run-id", "earlier")
        self.cli("record-safeguard", "--run-id", "earlier", "--finding-id", "rule", "--layer", "layer_1_architecture",
                 "--artifact", "src/a.py", "--summary", "Use the shared implementation")
        self.assertEqual(_git(self.repo, "status", "--porcelain", "--untracked-files=all"), "?? .autoloop/runs/earlier.json")
        lesson, _ = self.cli("record-lesson", "--run-id", "earlier", "--kind", "process", "--summary", "Study the guide",
                             "--evidence", "docs/guide.md", "--applies-to", "docs/[a-z]*.md")
        self.assertEqual((self.repo / lesson["file"]).parent, self.repo / ".autoloop/lessons")
        init, _ = self.cli("init", "--run-id", "later", "--goal", "Learn", "--paths", "docs/**")
        self.assertEqual([item["kind"] for item in init["lessons"]], ["process"])
        grill, _ = self.cli("complete-grill", "--run-id", "later", "--summary", "Agreed", "--shard", "a=src/*.py")
        self.assertEqual({item["kind"] for item in grill["lessons"]}, {"dead_end", "safeguard"})
        self.assertTrue(all(item["draft"] for item in grill["lessons"]))
        empty, _ = self.cli("lessons", "--paths", "elsewhere/**")
        self.assertEqual(empty["lessons"], [])

    def test_attempt_log_and_route_statistics(self) -> None:
        log = self.root / "attempts.jsonl"

        def attempt(task_id, n, model, *extra):
            return self.cli(
                "record-attempt", "--log", str(log), "--task-id", task_id, "--attempt", str(n),
                "--model", model, "--task-class", "implementation", "--provider", "codex", *extra,
            )[0]["record"]

        accepted = ("--disposition", "accepted", "--checks-passed", "true")
        first = attempt("a", 1, "Sol", *accepted, "--elapsed-to-acceptance-minutes", "6", "--cost", "1.25")
        self.assertIsNone(first["review_minutes"])
        self.assertTrue(first["checks_passed"])
        self.assertEqual(attempt("a", 1, "Sol", *accepted, "--elapsed-to-acceptance-minutes", "6", "--cost", "1.25"), first)
        attempt("b", 1, "Sol", "--disposition", "rejected", "--cost", "0.75")
        attempt("b", 2, "Astra", *accepted, "--elapsed-to-acceptance-minutes", "10", "--cost", "3")
        attempt("c", 1, "Sol", "--disposition", "rejected")
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 4)
        report, _ = self.cli("routing-report", "--log", str(log))
        routes = {route["model"]: route for route in report["routes"]}
        self.assertAlmostEqual(routes["Sol"]["first_attempt_acceptance"], 1 / 3)
        self.assertAlmostEqual(routes["Sol"]["eventual_acceptance"], 2 / 3)
        self.assertEqual(routes["Sol"]["median_elapsed_to_acceptance_minutes"], 8)
        self.assertEqual(routes["Sol"]["spend"], 2)
        self.assertTrue(routes["Sol"]["too_few_samples"])
        self.assertIsNone(routes["Astra"]["first_attempt_acceptance"])
        self.assertEqual(routes["Astra"]["spend"], 3)
        self.cli("record-attempt", "--log", str(self.repo / "apps/log.jsonl"), "--task-id", "a", "--attempt", "1",
                 expected=EXIT_POLICY_OR_INTEGRITY)
        other = self.root / "other-worktree"
        _write_lf(other / ".git", "gitdir: fake\n")
        with self.assertRaises(GuardError):
            attempt_log_path(self.repo, other / "attempts.jsonl")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "XDG_STATE_HOME": str(self.root)}):
            subdir = "KhitmeePerf" if os.name == "nt" else "khitmee-perf"
            self.assertEqual(attempt_log_path(self.repo, None), self.root / subdir / "agent-attempts.jsonl")

    def test_approve_new_shard_with_frozen_checks(self) -> None:
        run_id = "amendment"
        self.create_run(run_id, shard="a=src/[a-m].py")
        self.cli("request-contract-change", "--run-id", run_id, "--shard", "new", "--reason", "Add evidence",
                 "--proposed-path", "docs/**")
        resolve = ("resolve-contract-change", "--run-id", run_id)
        self.cli(*resolve, "--decision", "approve", "--add-shard-paths", "new=src/a*.py",
                 expected=EXIT_ILLEGAL_TRANSITION)
        self.cli(*resolve, "--decision", "approve", "--add-shard-paths", "unrequested=docs/**",
                 expected=EXIT_ILLEGAL_TRANSITION)
        self.assertEqual(load_run(self.repo, run_id)["contract_revision"], 1)
        rejected, _ = self.cli(*resolve, "--decision", "reject")
        self.assertNotIn("new", rejected["shards"])
        self.cli("request-contract-change", "--run-id", run_id, "--shard", "new", "--reason", "Add evidence")
        approved, _ = self.cli(*resolve, "--decision", "approve", "--add-shard-paths", "new=docs/**",
                               "--shard-check", "new=git status --porcelain")
        self.assertEqual(approved["contract_revision"], 2)
        self.assertEqual(approved["shards"]["new"]["status"], "locked")
        frozen = verify_untampered(self.repo, run_id, shard_id="new")["contract_shards"]["new"]
        self.assertEqual(frozen, {"allowed_paths": ["docs/**"], "checks": ["git status --porcelain"]})
        approved["shards"]["new"]["checks"] = []
        _write_lf(self.repo / f".autoloop/runs/{run_id}.json", json.dumps(approved) + "\n")
        with self.assertRaisesRegex(GuardError, "checks tampered"):
            verify_untampered(self.repo, run_id, shard_id="new")

    def test_drop_unused_shard_revises_contract_and_releases_scope(self) -> None:
        run_id = "drop-unused"
        run = ("--run-id", run_id)
        self.cli("init", *run, "--goal", "Keep only justified shards", "--no-cloud-review", "--no-deep-review")
        self.cli("complete-grill", *run, "--summary", "Two proposed shards",
                 "--shard", "keep=src/a.py", "--shard", "unused=src/b.py", "--allowed-path", "src/**")
        self.cli("lock", *run, "--pr", "55")
        self.cli("start-shard", *run, "--shard", "keep")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 42\n")
        _git(self.repo, "commit", "-am", "Implement active shard")
        self.cli("check", *run, "--shard", "keep", "--skip-fast-check")
        reason = "ADRs found no justified work"
        request = ("request-contract-change", *run, "--shard", "unused", "--drop", "--reason", reason)
        pending, _ = self.cli(*request)
        self.assertEqual(pending["contract_revision"], 1)
        self.assertIn("unused", pending["shards"])
        resolve = ("resolve-contract-change", *run, "--decision")
        rejected, _ = self.cli(*resolve, "reject")
        self.assertEqual(rejected["shards"]["unused"]["status"], "locked")
        self.assertEqual(rejected["contract_revision"], 1)
        self.cli(*request)
        approved, _ = self.cli(*resolve, "approve")
        self.assertEqual(approved["contract_revision"], 2)
        self.assertEqual(set(approved["shards"]), {"keep"})
        self.assertTrue(approved["shards"]["keep"]["has_passed_individually"])
        drop = {"shard": "unused", "allowed_paths": ["src/b.py"], "reason": reason, "contract_revision": 2}
        self.assertEqual(approved["history"][-1]["dropped_shards"], [drop])
        self.assertEqual(_build_frozen_contract(approved)["dropped_shards"], [drop])
        self.assertEqual(set(verify_untampered(self.repo, run_id)["contract_shards"]), {"keep"})
        with self.assertRaisesRegex(GuardError, "unknown shard"):
            verify_untampered(self.repo, run_id, shard_id="unused")
        integrated, _ = self.cli("check", *run, "--all-shards", "--skip-fast-check")
        ready, _ = self.cli("submit-for-review", *run)
        self.assertEqual(ready["status"], "ready_to_merge")
        self.cli("publish-contract", *run)
        _git(self.repo, "add", f".autoloop/runs/{run_id}.json")
        _git(self.repo, "commit", "-m", "Checkpoint approved scope drop")

        def ci(mode="preflight"):
            return verify_pr_integrity(
                self.repo, base_sha=self.base, head_sha=_git(self.repo, "rev-parse", "HEAD"),
                pr_number=55, pr_author="worker", pr_labels=["autoloop"], mode=mode,
                trusted_contracts_dir=self.repo / ".autoloop/trusted-contracts",
                candidate_checks_result="success", trusted_shard_results={"keep": integrated["code_tree_sha256"]},
            )

        self.assertEqual([row["shard_id"] for row in ci()["shard_checks"]], ["keep"])
        self.assertEqual(ci("finalize")["verdict"], "enrolled-pass")
        _write_lf(self.repo / "src/b.py", "def b():\n    return 9\n")
        _git(self.repo, "commit", "-am", "Change former scope")
        with self.assertRaisesRegex(GuardError, "dropped shard scope.*src/b.py"):
            verify_untampered(self.repo, run_id, base_ref="HEAD")
        with self.assertRaisesRegex(GuardError, "dropped shard scope.*src/b.py"):
            ci()
        self.cli("request-contract-change", *run, "--shard", "replacement", "--reason", "New justified scope")
        self.cli(*resolve, "approve", "--add-shard-paths", "replacement=src/b.py")
        self.assertTrue(verify_untampered(self.repo, run_id)["ok"])

    def test_drop_started_shard_rechecks_commit_evidence_on_approval(self) -> None:
        run = ("--run-id", "drop-started")
        self.create_run("drop-started", shard="keep=src/a.py")
        self.cli("request-contract-change", *run, "--shard", "idle", "--reason", "Provisional scope")
        resolve = ("resolve-contract-change", *run, "--decision", "approve")
        self.cli(*resolve, "--add-shard-paths", "idle=src/b.py")
        _git(self.repo, "checkout", "-b", "idle-local")
        self.cli("start-shard", *run, "--shard", "idle")
        req = ("request-contract-change", *run, "--shard", "idle", "--drop", "--reason", "No work needed")
        self.cli(*req)
        _write_lf(self.repo / "src/b.py", "def b():\n    return 8\n")
        _git(self.repo, "commit", "-am", "Work lands after request")
        _git(self.repo, "checkout", "main")
        _, error = self.cli(*resolve, expected=EXIT_ILLEGAL_TRANSITION)
        self.assertIn("commits in its allowed paths", error)
        self.assertIn("idle", load_run(self.repo, "drop-started")["shards"])
        _git(self.repo, "branch", "-D", "idle-local")
        state, _ = self.cli(*resolve)
        self.assertEqual(set(state["shards"]), {"keep"})

    def test_dropped_paths_require_explicit_reassignment_even_with_overlapping_scope(self) -> None:
        run_id = "drop-overlap"
        run = ("--run-id", run_id)
        self.cli("init", *run, "--goal", "Release an unused scope")
        self.cli("complete-grill", *run, "--summary", "Independent and provisional work",
                 "--shard", "keep=src/**", "--shard", "unused=src/b.py")
        self.cli("lock", *run)
        resolve = ("resolve-contract-change", *run, "--decision", "approve")
        self.cli("request-contract-change", *run, "--shard", "unused", "--drop", "--reason", "Unused")
        self.cli(*resolve)
        _write_lf(self.repo / "src/b.py", "def b():\n    return 7\n")
        _git(self.repo, "commit", "-am", "Change an unassigned path")
        with self.assertRaisesRegex(GuardError, "dropped shard scope.*src/b.py"):
            verify_untampered(self.repo, run_id, shard_id="keep")
        self.cli("request-contract-change", *run, "--shard", "keep", "--reason", "Explicitly reclaim the path")
        approved, _ = self.cli(*resolve, "--add-shard-paths", "keep=src/b.py")
        self.assertEqual(approved["dropped_shards"][0]["readded"], {"keep": ["src/b.py"]})
        self.assertTrue(verify_untampered(self.repo, run_id, shard_id="keep")["ok"])

    def test_drop_rejects_worktrees_findings_and_the_only_shard(self) -> None:
        run = ("--run-id", "drop-protected")
        self.cli("init", *run, "--goal", "Respect active work")
        self.cli("complete-grill", *run, "--summary", "Separate scopes",
                 "--shard", "keep=src/a.py", "--shard", "worked=src/b.py", "--shard", "reported=docs/**")
        self.cli("lock", *run)
        self.cli("start-shard", *run, "--shard", "worked", "--worktree", str(self.repo))
        self.cli("record-finding", *run, "--shard", "reported", "--finding-id", "F1", "--severity", "P1",
                 "--source", "review", "--summary", "Existing finding", "--evidence", "review")
        for shard, diagnostic in (("worked", "registered worktree"), ("reported", "has findings")):
            _, error = self.cli("request-contract-change", *run, "--shard", shard, "--drop", "--reason", "Unused",
                                expected=EXIT_ILLEGAL_TRANSITION)
            self.assertIn(diagnostic, error)
        self.create_run("drop-only")
        _, error = self.cli("request-contract-change", "--run-id", "drop-only", "--shard", "a", "--drop",
                            "--reason", "Unused", expected=EXIT_ILLEGAL_TRANSITION)
        self.assertIn("last active shard", error)

    def test_preintegrated_base_hint_and_retarget(self) -> None:
        _git(self.repo, "checkout", "-b", "parent")
        _write_lf(self.repo / "docs/guide.md", "unmerged dependency\n")
        _git(self.repo, "commit", "-am", "unmerged dependency")
        stacked_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-b", "child", "main")
        _git(self.repo, "merge", "--no-ff", "parent", "-m", "integrate dependency")
        integration_sha = _git(self.repo, "rev-parse", "HEAD")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 7\n")
        _git(self.repo, "commit", "-am", "shard change")
        run_id = "stacked"
        self.create_run(run_id, "--base-branch", "main")
        self.cli("start-shard", "--run-id", run_id, "--shard", "a")
        check = ("check", "--run-id", run_id, "--shard", "a", "--skip-fast-check")
        _, error = self.cli(*check, expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("--base-ref <integration-commit>", error)
        self.cli(*check, "--base-sha", integration_sha)
        change, _ = self.cli("refresh-base", "--run-id", run_id, "--new-base-sha", stacked_sha,
                             "--new-base-branch", "parent")
        self.assertEqual((change["base_branch"], change["base_sha"], change["contract_revision"]), ("parent", stacked_sha, 2))
        self.assertFalse(change["shards"]["a"]["has_passed_individually"])
        published, _ = self.cli("publish-contract", "--run-id", run_id)
        contract = json.loads(Path(published["trusted_contract_path"]).read_text(encoding="utf-8"))
        self.assertEqual(contract["base_branch"], "parent")
        self.assertTrue(contract["base_branch_bound"])
        self.cli("request-contract-change", "--run-id", run_id, "--shard", "a", "--reason", "Retarget to main")
        change, _ = self.cli("resolve-contract-change", "--run-id", run_id, "--decision", "approve",
                             "--new-base-sha", self.base, "--new-base-branch", "main")
        self.assertEqual((change["base_branch"], change["base_sha"], change["contract_revision"]), ("main", self.base, 3))

    def _install_lint_ratchet_fixture(self) -> str:
        rule = self.repo / ".autoloop/rules/test_ratchet.py"
        _write_lf(rule,
                  "def check(files):\n"
                  "    count = int(b'# inherited' in files['src/b.py'])\n"
                  "    count += int(b'# grow lint' in files['src/a.py'])\n"
                  "    return [{'path': 'src/b.py', 'line': n + 1, 'message': 'fixture violation'}"
                  " for n in range(count)]\n"
                  "RULES = [('fixture-ratchet', check)]\n")
        gate = self.repo / "tools/fast_check.py"
        _write_lf(gate,
                  "from autoloop.static_rules import lint_repo\n"
                  "def check(root):\n"
                  "    assert not lint_repo(root), 'fast_check autoloop_lint failed'\n")
        _write_lf(self.repo / "src/b.py", "def b():\n    return 2  # inherited\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "trusted lint with an inherited violation")
        return _git(self.repo, "rev-parse", "HEAD")

    def test_individual_check_warns_about_inherited_static_lint_and_integration_rejects_it(self) -> None:
        base = self._install_lint_ratchet_fixture()
        self.create_run("lint-inherited")
        run = ("--run-id", "lint-inherited")
        self.cli("start-shard", *run, "--shard", "a")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 3\n")
        _git(self.repo, "commit", "-am", "independent shard change")
        result, warnings = self.cli("check", *run, "--shard", "a")
        self.assertIn("WARNING: inherited static lint: fixture-ratchet:src/b.py:1:", warnings)
        self.assertTrue(result["tooling"]["fast_check_ran"])
        stats = {
            "base_sha": base, "base_counts": {"fixture-ratchet": 1},
            "head_counts": {"fixture-ratchet": 1}, "warning_count": 1,
        }
        state = load_run(self.repo, "lint-inherited")
        self.assertEqual(result["tooling"]["static_lint"], stats)
        self.assertEqual(state["shards"]["a"]["passed_tooling"]["static_lint"], stats)
        self.assertEqual(state["history"][-1]["static_lint"], stats)
        _, error = self.cli("check", *run, "--all-shards", expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("fixture-ratchet:src/b.py:1:", error)
        self.assertNotIn("WARNING: inherited", error)

    def test_individual_check_rejects_static_lint_growth_in_unchanged_files(self) -> None:
        base = self._install_lint_ratchet_fixture()
        self.create_run("lint-growth")
        run = ("--run-id", "lint-growth")
        self.cli("start-shard", *run, "--shard", "a")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 3  # grow lint\n")
        _git(self.repo, "commit", "-am", "trigger a new violation through an unchanged file")
        self.assertEqual(_git(self.repo, "diff", "--name-only", base, "HEAD"), "src/a.py")
        # An explicit scope-comparison base cannot reset the frozen lint baseline.
        command = ("check", *run, "--shard", "a", "--base-ref", "HEAD", "--skip-fast-check")
        _, error = self.cli(*command, expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("rule counts increased (fixture-ratchet 1 -> 2)", error)
        self.assertIn("fixture-ratchet:src/b.py:2:", error)

    def test_individual_check_rejects_inherited_static_lint_in_touched_files(self) -> None:
        self._install_lint_ratchet_fixture()
        self.create_run("lint-touched", shard="a=src/b.py")
        run = ("--run-id", "lint-touched")
        self.cli("start-shard", *run, "--shard", "a")
        _write_lf(self.repo / "src/b.py", "def b():\n    return 7  # inherited\n")
        _git(self.repo, "commit", "-am", "touch the inherited violation")
        _, error = self.cli("check", *run, "--shard", "a", "--skip-fast-check", expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("shard static lint: fixture-ratchet:src/b.py:1:", error)
        self.assertNotIn("rule counts increased", error)

    def test_check_timings_capture_jobs_and_failed_attempt(self) -> None:
        _write_lf(_ensure_parent(self.repo / "tools/fast_check.py"),
                  "from concurrent.futures import ThreadPoolExecutor\nimport os\n"
                  "def probe():\n    return True\n"
                  "def check(root):\n"
                  "    with ThreadPoolExecutor() as pool:\n        assert pool.submit(probe).result()\n"
                  "    if os.environ.get('AUTOLOOP_TEST_FAIL_GATE'):\n        raise SystemExit(7)\n")
        _git(self.repo, "add", "tools/fast_check.py")
        _git(self.repo, "commit", "-m", "fixture offline gate")
        self.create_run("timers", check="git status --porcelain")
        self.cli("start-shard", "--run-id", "timers", "--shard", "a")
        check = ("check", "--run-id", "timers", "--shard", "a")
        with mock.patch("autoloop.check_tools.monotonic", side_effect=[0, 12, 20, 21]):
            self.cli(*check)
        with mock.patch.dict(os.environ, {"AUTOLOOP_TEST_FAIL_GATE": "1"}):
            with mock.patch("autoloop.check_tools.monotonic", side_effect=[0, 67]):
                _, error = self.cli(*check, expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("regressed from 12.0s to 67.0s", error)
        state = load_run(self.repo, "timers")
        passed, failed = state["check_timings"]["shard:a"]
        self.assertIn("a #1: git status --porcelain", passed["steps"])
        self.assertIn("fast_check.py/probe", failed["steps"])
        self.assertEqual([passed["passed"], failed["passed"]], [True, False])
        self.assertEqual(state["status"], "in_progress")
        self.assertEqual(state["shards"]["a"]["status"], "in_progress")
        self.assertEqual(state["total_attempts"], 0)
        self.assertIsNone(state["shards"]["a"]["passed_code_tree_sha256"])
        self.assertNotIn("passed_tooling", state["shards"]["a"])
        self.assertEqual(state["shards"]["a"]["pending_dead_end_sig"],
                         normalize_failure("fast_check.py", "fast_check.py failed with exit 7"))
        self.assertEqual(state["history"][-1]["event"], "check-failed")
        for seconds in range(6):
            record_check_timings(self.repo, "timers", scope="shard:a", head_sha=self.base,
                                 steps={"probe": seconds}, passed=True)
        self.assertEqual([item["steps"]["probe"] for item in load_run(self.repo, "timers")["check_timings"]["shard:a"]],
                         [1, 2, 3, 4, 5])

    def test_record_failures_without_rollback_at_strike_limit(self) -> None:
        self.create_run("failures")
        self.cli("start-shard", "--run-id", "failures", "--shard", "a")
        _write_lf(self.repo / "src/a.py", "def a():\n    return 13\n")
        _git(self.repo, "commit", "-am", "worker change")
        head = _git(self.repo, "rev-parse", "HEAD")
        for _ in range(3):
            state, _ = self.cli("retry-shard", "--run-id", "failures", "--shard", "a", "--check-name", "pytest",
                                "--error-output", "AssertionError src/a.py:17")
        self.assertEqual(state["status"], "needs_replan")
        self.assertEqual(state["shards"]["a"]["pending_dead_end_sig"],
                         normalize_failure("pytest", "AssertionError src/a.py:17"))
        self.assertNotIn("rolled_back_to", state["history"][-1])
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head)

    def test_oracle_register_requires_review_and_authentication(self) -> None:
        run_id = "oracle"
        self.create_run(run_id, "--no-cloud-review", "--no-deep-review", "--pr", "123", shard="a=src/**")
        self.cli("start-shard", "--run-id", run_id, "--shard", "a")
        args = ("record-oracle-mismatch", "--run-id", run_id, "--shard", "a", "--summary", "Stock rounds early",
                "--why-ours-is-correct", "Totals must reconcile", "--upstream-ref")
        self.cli(*args, "bad-ref", "--test", "src/test_parity.py", expected=EXIT_POLICY_OR_INTEGRITY)
        registered, _ = self.cli(*args, "erpnext/report.py:18@v16", "--test", "src/test_parity.py")
        mismatch_id = registered["oracle_mismatch_id"]
        second, _ = self.cli(*args, "erpnext/report.py:18@v16", "--test", "src/test_parity.py")
        self.assertEqual(second["contract_revision"], registered["contract_revision"])
        check = ("check", "--run-id", run_id, "--shard", "a", "--skip-fast-check")
        submit = ("submit-for-review", "--run-id", run_id)
        self.cli(*check)
        self.cli(*submit, expected=EXIT_ILLEGAL_TRANSITION)
        _write_lf(self.repo / "src/test_parity.py", "def test_reconciliation():\n    assert 1 + 2 == 3\n")
        _git(self.repo, "add", "src/test_parity.py")
        _git(self.repo, "commit", "-m", "regression test")
        gated, _ = self.cli(*check)
        submitted, _ = self.cli(*submit)
        self.assertEqual(submitted["status"], "awaiting_deep_review")
        self.assertEqual(list(submitted["open_oracle_mismatches"]), [mismatch_id])
        review = ("record-review", "--run-id", run_id, "--stage", "deep", "--verdict", "pass", "--reviewer", "reviewer")
        self.cli(*review, expected=EXIT_ILLEGAL_TRANSITION)
        state, _ = self.cli(*review, "--oracle-mismatch", mismatch_id)
        self.assertEqual(state["status"], "ready_to_merge")
        pub, _ = self.cli("publish-contract", "--run-id", run_id)
        checkpoint = f".autoloop/runs/{run_id}.json"
        _git(self.repo, "add", checkpoint)
        _git(self.repo, "commit", "-m", "reviewed oracle register")
        head = _git(self.repo, "rev-parse", "HEAD")
        client = FakeGitHubClient(
            comments_by_number={123: [{"author": "Morris-Zin", "body": pub["comment_markdown"]}]},
            pr_reviews=[{"user": "reviewer", "state": "APPROVED", "commit_id": head}],
        )
        code_hash = gated["code_tree_sha256"]
        def ci(head_sha: str):
            return verify_pr_integrity(
                self.repo, base_sha=self.base, head_sha=head_sha, pr_number=123, pr_author="worker",
                pr_labels=["autoloop"], github_client=client, candidate_checks_result="success",
                trusted_shard_results={"a": code_hash},
            )
        self.assertEqual(ci(head)["verdict"], "enrolled-pass")
        state["reviews"][-1]["oracle_mismatch_ids"] = []
        _write_lf(self.repo / checkpoint, json.dumps(state) + "\n")
        _git(self.repo, "commit", "-am", "stale mismatch review")
        with self.assertRaisesRegex(GuardError, "unreviewed oracle mismatches"):
            ci(_git(self.repo, "rev-parse", "HEAD"))
        del state["oracle_mismatches"]
        _write_lf(self.repo / checkpoint, json.dumps(state) + "\n")
        with self.assertRaisesRegex(GuardError, "oracle mismatches do not match FrozenContractV1"):
            verify_untampered(self.repo, run_id)

    def test_doctor_runs_windows_docker_separately(self) -> None:
        wsl_docker = ["wsl.exe", "-d", "KhitmeeERPNext", "--", "docker", "info"]
        with mock.patch("autoloop.check_tools._reachable", side_effect=lambda cmd: cmd != wsl_docker) as probe:
            with mock.patch("fast_check.missing_dev_tools", return_value=["ruff"]):
                result = doctor(windows=True)
        probe.assert_any_call(wsl_docker)
        self.assertFalse(result["ok"])
        self.assertTrue(result["checks"]["KhitmeeERPNext"]["ok"])
        self.assertFalse(result["checks"]["docker_in_KhitmeeERPNext"]["ok"])
        self.assertIn("--group apps/khitmee_ops/pyproject.toml:dev", result["checks"]["dev_group"]["fix"])

    def test_active_and_older_checkpoints_verify_with_original_contract_digest(self) -> None:
        for run_id in ("backend-2gb-proof-20261128", "backend-2gb-2vcpu-20261128", "devprod-verification-20261127"):
            source = REPO_ROOT / ".autoloop/runs" / f"{run_id}.json"
            with self.subTest(run_id=run_id):
                target = _ensure_parent(self.repo / ".autoloop/runs" / source.name)
                shutil.copyfile(source, target)
                state = load_run(self.repo, run_id)
                frozen = _build_frozen_contract(state)
                self.assertEqual(contract_sha256(frozen), state["contract_sha256"])
                trusted = _ensure_parent(self.repo / ".autoloop/trusted-contracts" / f"{run_id}.json")
                _write_lf(trusted, json.dumps(frozen) + "\n")
                result = verify_untampered(self.repo, run_id, base_ref=self.base, head_ref=self.base)
                self.assertTrue(result["ok"])
                self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_shards_run_trusted_tools_and_only_promote_individual_passes(self) -> None:
        gate = "tools/fast_check.py"
        rule = ".autoloop/rules/embryo_core.py"
        _write_lf(
            self.repo / gate,
            "def check(root):\n"
            "    if 'always pass' in (root / 'tools/fast_check.py').read_text():\n        raise SystemExit(7)\n"
            "    if 'reject policy' in (root / 'tools/fast_check.py').read_text():\n"
            "        print('AssertionError tools/fast_check.py:42')\n        raise SystemExit(9)\n"
            "    if 'needs_tooling_patch' in (root / 'src/a.py').read_text():\n        raise SystemExit(7)\n",
        )
        _git(self.repo, "add", gate)
        _git(self.repo, "commit", "-m", "trusted gate")
        base = _git(self.repo, "rev-parse", "HEAD")
        policy, worker = self.root / "policy", self.root / "worker"
        _git(self.repo, "worktree", "add", "-b", "policy", str(policy), base)
        _git(self.repo, "worktree", "add", "-b", "worker", str(worker), base)
        run = ("--run-id", "trusted-tooling")
        self.cli("init", *run, "--goal", "Gate workers")
        self.cli("complete-grill", *run, "--summary", "Agreed",
                 "--shard", f"policy={gate},{rule}", "--shard", "worker=src/a.py",
                 "--allow-policy-path", gate, "--allow-policy-path", rule)
        self.cli("lock", *run)
        self.cli("start-shard", *run, "--shard", "policy", "--worktree", str(policy))
        self.cli("start-shard", *run, "--shard", "worker", "--worktree", str(worker))
        _write_lf(policy / gate, "def check(root):\n    pass  # always pass\n")
        _write_lf(policy / rule, "raise RuntimeError('candidate rule code must never execute')\n")
        _git(policy, "commit", "-am", "attempt to bypass the gate")
        _, error = self.cli("check", *run, "--shard", "policy", expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("fast_check.py failed with exit 7", error)
        self.cli("check", *run, "--shard", "policy", "--skip-fast-check")
        state = load_run(self.repo, "trusted-tooling")
        self.assertNotIn("trusted_policy_pass_sha", state["shards"]["policy"])
        _write_lf(worker / "src/a.py", "def a():\n    return 1  # needs_tooling_patch\n")
        _git(worker, "commit", "-am", "worker source")
        _, error = self.cli("check", *run, "--shard", "worker", expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("fast_check.py failed with exit 7", error)
        self.assertEqual(_git(worker, "show", f"HEAD:{gate}"), _git(self.repo, "show", f"{base}:{gate}"))

        _git(policy, "checkout", base, "--", rule)
        _write_lf(policy / gate, "def check(root):\n    assert (root / 'src/a.py').is_file()\n")
        _git(policy, "commit", "-am", "fix the gate")
        policy_head = _git(policy, "rev-parse", "HEAD")
        policy_pass, _ = self.cli("check", *run, "--shard", "policy")
        self.assertEqual(policy_pass["tooling"]["policy_shas"], {})
        worker_pass, _ = self.cli("check", *run, "--shard", "worker")
        evidence = load_run(self.repo, "trusted-tooling")["shards"]["worker"]["passed_tooling"]
        self.assertEqual(evidence, worker_pass["tooling"])
        self.assertEqual(evidence["policy_shas"], {"policy": policy_head})
        self.assertNotEqual(evidence["sha256"], policy_pass["tooling"]["sha256"])

        gate_source = (policy / gate).read_text(encoding="utf-8")
        _write_lf(policy / gate, gate_source + "# reject policy\n")
        _git(policy, "commit", "-am", "break the previously passing policy shard")
        failed_head = _git(policy, "rev-parse", "HEAD")
        self.cli("check", *run, "--shard", "policy", expected=EXIT_POLICY_OR_INTEGRITY)
        failed = load_run(self.repo, "trusted-tooling")["shards"]["policy"]
        self.assertEqual((failed["status"], failed["last_green_sha"]), ("in_progress", policy_head))
        self.assertEqual(_git(policy, "rev-parse", "HEAD"), failed_head)
        self.assertFalse(failed["has_passed_individually"])
        self.assertIsNone(failed["passed_code_tree_sha256"])
        self.assertNotIn("passed_tooling", failed)
        self.assertNotIn("trusted_policy_pass_sha", failed)
        self.assertEqual(failed["pending_dead_end_sig"], normalize_failure(
            "fast_check.py", "AssertionError tools/fast_check.py:42\nfast_check.py failed with exit 9",
        ))
        with self.assertRaisesRegex(IllegalTransitionError, "trusted tooling from shard 'policy' changed"):
            record_shard_pass(self.repo, "trusted-tooling", "worker", tooling=evidence)
        _, error = self.cli("check", *run, "--shard", "worker", expected=EXIT_POLICY_OR_INTEGRITY)
        self.assertIn("fast_check.py failed with exit 7", error)

        _write_lf(policy / gate, gate_source)
        _git(policy, "commit", "-am", "repair policy shard")
        recovered = _git(policy, "rev-parse", "HEAD")
        self.cli("check", *run, "--shard", "policy")
        worker_pass, _ = self.cli("check", *run, "--shard", "worker")
        self.assertEqual(worker_pass["tooling"]["policy_shas"], {"policy": recovered})

    def test_lease_common_dir_override_and_status(self) -> None:
        linked = self.root / "lease-worktree"
        _git(self.repo, "worktree", "add", "-b", "lease-shard", str(linked), self.base)
        with mock.patch.dict(os.environ):
            os.environ.pop("AUTOLOOP_LOCK_DIR", None)
            os.environ.pop("AUTOLOOP_SHARED_LOCK_DIR", None)
            self.assertEqual(default_lock_dir(self.repo), self.repo / ".git/autoloop-locks")
            self.assertEqual(default_lock_dir(self.repo), default_lock_dir(linked))
            override = self.root / "shared-locks"
            os.environ["AUTOLOOP_LOCK_DIR"] = str(override)
            self.assertEqual(default_lock_dir(linked), override)
            def lease(action, *extra, expected=0):
                return self.cli("lease", action, "--resource", "khitmee-perf", *extra, expected=expected)[0]

            first = lease("acquire", "--owner", "worker")
            status = lease("status")
            self.assertTrue(status["active"])
            self.assertEqual(status["holder"]["owner"], "worker")
            self.assertNotIn("token", status["holder"])
            self.assertTrue(lease("release", "--owner", "worker", "--token", first["token"])["released"])
            self.assertEqual(lease("status"), {"resource": "khitmee-perf", "active": False, "holder": None})

    def test_windows_backed_wsl_fails_closed_before_creating_lock(self) -> None:
        override = self.root / "must-not-create"
        with mock.patch.dict(os.environ, {"AUTOLOOP_LOCK_DIR": str(override)}):
            with mock.patch("autoloop.leases._is_wsl", return_value=True):
                with mock.patch("autoloop.leases._on_windows_drive", return_value=True):
                    _, error = self.cli("lease", "acquire", "--resource", "khitmee-perf", "--owner", "wsl", expected=1)
                    self.assertIn("python.exe tools/autoloop/cli.py lease", error)
                    self.assertFalse(override.exists())
                with mock.patch("autoloop.leases._on_windows_drive", return_value=False):
                    self.assertEqual(resource_lock_dir(self.repo), override)

    def test_foreign_pid_lease_survives_until_ttl(self) -> None:
        lock_dir = self.root / "foreign-locks"
        resource = "khitmee-perf"
        first = acquire_lease(resource, "foreign", ttl_s=30, lock_dir=lock_dir)
        path = lock_dir / f"{resource_key_hash(resource)}.lease.json"
        first.update(pid=123, pid_namespace="foreign")
        _write_lf(path, json.dumps(first) + "\n")
        with mock.patch("autoloop.leases.is_pid_alive", return_value=False) as liveness:
            with self.assertRaises(LeaseConflictError):
                acquire_lease(resource, "local", lock_dir=lock_dir)
            liveness.assert_not_called()
        first["expires_at"] = 0
        _write_lf(path, json.dumps(first) + "\n")
        self.assertTrue(acquire_lease(resource, "local", lock_dir=lock_dir)["reclaimed_previous"])

    def test_shell_scripts_use_lf_in_windows_worktrees(self) -> None:
        attrs = REPO_ROOT / ".gitattributes"
        shutil.copyfile(attrs, self.repo / attrs.name)
        paths = ("perf-suite/probe.sh", "perf-suite/probe.bash", "tools/cli.sh")
        output = _git(self.repo, "check-attr", "eol", "--", *paths)
        self.assertEqual(output.splitlines(), [f"{path}: eol: lf" for path in paths])


if __name__ == "__main__":
    unittest.main()
