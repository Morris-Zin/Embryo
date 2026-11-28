"""Complete 14-state + per-shard serialized state machine for /autoloop (V3)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from . import EXIT_ILLEGAL_TRANSITION, SCHEMA_VERSION
from .failures import normalize_failure
from .guard import (
    RUN_ID_RE,
    _run_git,
    UnsafeRollbackError,
    assert_clean_worktree_for_rollback,
    assert_safe_local_path,
    compute_code_tree_sha256,
    contract_sha256,
    format_contract_comment,
    git_current_branch,
    git_is_ancestor,
    git_read_blob,
    git_restore_to_commit,
    git_rev_parse,
    load_config,
    matches_any_glob,
    open_oracle_mismatches,
    oracle_mismatch_id,
    validate_oracle_mismatch,
)
from .leases import acquire_mutex, atomic_write_json, default_lock_dir, resource_key_hash
from .lessons import globs_overlap, record_lesson
from .static_rules import verify_rule_fixture

RUN_STATES = (
    "draft",
    "grilled",
    "locked",
    "in_progress",
    "gate_passed",
    "retrying",
    "needs_replan",
    "needs_contract_amendment",
    "blocked_human",
    "awaiting_cloud_review",
    "awaiting_deep_review",
    "safeguard_pending",
    "ready_to_merge",
    "terminated",
)

SHARD_STATES = (
    "locked",
    "in_progress",
    "passed",
    "retrying",
    "needs_replan",
    "needs_contract_amendment",
    "blocked_human",
)

FINDING_STATES = (
    "open",
    "fixed_pending_review",
    "closed_needs_safeguard",
    "safeguarded",
)

FINDING_SEVERITIES = ("P0", "P1", "P2")
FINDING_SOURCES = ("inherited_review", "incident", "gate_failure", "review")

SAFEGUARD_LAYERS = (
    "layer_1_architecture",
    "layer_2_type",
    "layer_3_static_lint",
    "layer_4_runtime_test",
)

DEFAULT_MAX_WORKER_STRIKES = 3
DEFAULT_MAX_REPLAN_COUNT = 1
DEFAULT_MAX_TOTAL_ATTEMPTS = 8
DEFAULT_MAX_PROSE_LINES = 15


class IllegalTransitionError(RuntimeError):
    exit_code = EXIT_ILLEGAL_TRANSITION


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_file(repo: Path, run_id: str) -> Path:
    if not run_id or ".." in run_id or not RUN_ID_RE.fullmatch(run_id):
        raise IllegalTransitionError(f"invalid run_id: {run_id!r}")
    target = repo / ".autoloop/runs" / f"{run_id}.json"
    assert_safe_local_path(repo, target)
    return target


def _trusted_contract_dir(repo: Path) -> Path:
    return repo / ".autoloop/trusted-contracts"


def _state_mutex_path(repo: Path, run_id: str, lock_dir: Path | None = None) -> Path:
    base = lock_dir or default_lock_dir(repo)
    digest = resource_key_hash(f"state:{run_id}")
    return base / f"{digest}.state.lock"


def load_run(repo: Path, run_id: str) -> dict[str, Any]:
    path = _run_file(repo, run_id)
    if not path.is_file():
        raise IllegalTransitionError(f"run {run_id!r} does not exist at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_run_unlocked(repo: Path, run_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_write_json(_run_file(repo, run_id), state)


def _limits_from_repo(repo: Path) -> dict[str, int]:
    defaults = {
        "max_worker_strikes": DEFAULT_MAX_WORKER_STRIKES,
        "max_replan_count": DEFAULT_MAX_REPLAN_COUNT,
        "max_total_attempts": DEFAULT_MAX_TOTAL_ATTEMPTS,
        "max_safeguard_prose_lines": DEFAULT_MAX_PROSE_LINES,
    }
    try:
        cfg = load_config(repo)
        limits = cfg.get("limits") or {}
        for key in defaults:
            if key in limits:
                defaults[key] = int(limits[key])
    except Exception:
        pass
    return defaults


def _write_trusted_contract(repo: Path, contract: dict[str, Any]) -> Path:
    cdir = _trusted_contract_dir(repo)
    assert_safe_local_path(repo, cdir, allow_missing_leaf=True)
    run_id = str(contract["run_id"])
    rev = int(contract["contract_revision"])
    rev_path = cdir / f"{run_id}.rev{rev}.json"
    latest_path = cdir / f"{run_id}.json"
    assert_safe_local_path(repo, rev_path, allow_missing_leaf=True)
    assert_safe_local_path(repo, latest_path, allow_missing_leaf=True)
    cdir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(rev_path, contract)
    atomic_write_json(latest_path, contract)
    return rev_path


def _build_frozen_contract(state: dict[str, Any]) -> dict[str, Any]:
    shards_spec = {
        sid: {
            "allowed_paths": list(sdata.get("allowed_paths") or []),
            "checks": list(sdata.get("checks") or []),
            "goal": sdata.get("goal") or "",
        }
        for sid, sdata in sorted((state.get("shards") or {}).items())
    }
    contract = {
        "schema_version": SCHEMA_VERSION,
        "run_id": state["run_id"],
        "issue_number": state.get("issue_number"),
        "pr_number": state.get("pr_number"),
        "contract_revision": int(state["contract_revision"]),
        "goal": state["goal"],
        "base_branch": state.get("base_branch") or "main",
        "merge_mode": state.get("merge_mode") or "human",
        "allowed_paths": list(state.get("allowed_paths") or []),
        "allow_policy_paths": list(state.get("allow_policy_paths") or []),
        "shards": shards_spec,
        "reviewers": dict(state.get("reviewers") or {"cloud": True, "deep": True}),
        "models": dict(state.get("models") or {}),
    }
    if state.get("oracle_mismatches"):
        contract["oracle_mismatches"] = dict(state["oracle_mismatches"])
    if state.get("dropped_shards"):
        contract["dropped_shards"] = list(state["dropped_shards"])
    if state.get("base_branch_bound"):
        contract["base_branch_bound"] = True
    if state.get("require_delegation_receipts"):
        contract["require_delegation_receipts"] = True
    return contract


def _revise_contract(repo: Path, state: dict[str, Any]) -> None:
    state["contract_revision"] = int(state.get("contract_revision") or 0) + 1
    contract = _build_frozen_contract(state)
    state["contract_sha256"] = contract_sha256(contract)
    _write_trusted_contract(repo, contract)


def _unsafeguarded_findings(state: dict[str, Any]) -> list[str]:
    findings = state.get("findings") or {}
    return sorted(
        fid for fid, fdata in findings.items() if (fdata or {}).get("status") != "safeguarded"
    )


def _open_or_pending_review_findings(state: dict[str, Any]) -> list[str]:
    findings = state.get("findings") or {}
    return sorted(
        fid
        for fid, fdata in findings.items()
        if (fdata or {}).get("status") in ("open", "fixed_pending_review")
    )


def derive_run_status(state: dict[str, Any]) -> str:
    """Derive execution-phase run status cleanly from per-shard statuses."""
    current = state["status"]
    if current in (
        "draft",
        "grilled",
        "awaiting_cloud_review",
        "awaiting_deep_review",
        "safeguard_pending",
        "ready_to_merge",
        "terminated",
    ):
        return current

    shards = state.get("shards") or {}
    if not shards:
        return current

    statuses = [s["status"] for s in shards.values()]
    if any(s == "blocked_human" for s in statuses):
        return "blocked_human"
    if any(s == "needs_contract_amendment" for s in statuses) or state.get("pending_contract_amendments"):
        return "needs_contract_amendment"
    if any(s == "needs_replan" for s in statuses):
        return "needs_replan"
    if any(s == "retrying" for s in statuses):
        return "retrying"
    if all(s == "passed" for s in statuses):
        hashes = {s.get("passed_code_tree_sha256") for s in shards.values()}
        if len(hashes) == 1 and None not in hashes and not state.get("pending_contract_amendments"):
            return "gate_passed"
        return "in_progress"
    if all(s == "locked" for s in statuses):
        return "locked"
    return "in_progress"


def _require_status(state: dict[str, Any], allowed: tuple[str, ...], action: str) -> None:
    if state["status"] not in allowed:
        raise IllegalTransitionError(
            f"cannot {action} when run status is {state['status']!r} (allowed: {', '.join(allowed)})"
        )


def _require_shard(state: dict[str, Any], shard_id: str) -> dict[str, Any]:
    shards = state.get("shards") or {}
    if shard_id not in shards:
        raise IllegalTransitionError(f"unknown shard {shard_id!r} in run {state['run_id']!r}")
    return shards[shard_id]


def _new_shard(paths: list[str], checks: list[str], base_sha: str | None) -> dict[str, Any]:
    return {
        "status": "locked",
        "allowed_paths": list(paths),
        "checks": list(checks),
        "goal": "",
        "attempts": 0,
        "worker_strikes": 0,
        "last_green_sha": base_sha,
        "passed_code_tree_sha256": None,
        "has_passed_individually": False,
        "worktree_path": None,
        "pending_dead_end_sig": None,
    }


def init_run(
    repo: Path,
    run_id: str,
    goal: str,
    *,
    issue_number: int | None = None,
    pr_number: int | None = None,
    base_branch: str = "main",
    base_sha: str | None = None,
    merge_mode: str = "human",
    cloud_review: bool = True,
    deep_review: bool = True,
    models: dict[str, Any] | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    if merge_mode not in ("human", "auto"):
        raise IllegalTransitionError(f"invalid merge_mode {merge_mode!r}")
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        path = _run_file(repo, run_id)
        if path.exists():
            raise IllegalTransitionError(f"run {run_id!r} already exists")
        limits = _limits_from_repo(repo)
        resolved_base_sha = git_rev_parse(repo, base_sha) if base_sha is not None else None
        if resolved_base_sha is None:
            try:
                resolved_base_sha = git_rev_parse(repo, base_branch)
            except Exception:
                try:
                    resolved_base_sha = git_rev_parse(repo, "HEAD")
                except Exception:
                    resolved_base_sha = None
        now = utc_now()
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "goal": goal,
            "status": "draft",
            "issue_number": issue_number,
            "pr_number": pr_number,
            "base_branch": base_branch,
            "base_sha": resolved_base_sha,
            "last_integrated_sha": None,
            "last_integrated_worktree": None,
            "last_integrated_branch": None,
            "last_integrated_detached": None,
            "merge_mode": merge_mode,
            "contract_revision": 0,
            "contract_sha256": None,
            "allowed_paths": [],
            "allow_policy_paths": [],
            "reviewers": {"cloud": bool(cloud_review), "deep": bool(deep_review)},
            "models": dict(models or {}),
            "limits": limits,
            "grill_summary": None,
            "replan_count": 0,
            "total_attempts": 0,
            "shards": {},
            "dead_ends": [],
            "pending_contract_amendments": [],
            "findings": {},
            "reviews": [],
            "safeguards": [],
            "history": [{"at": now, "event": "init", "status": "draft"}],
            "created_at": now,
            "updated_at": now,
        }
        _save_run_unlocked(repo, run_id, state)
        return state


def complete_grill(
    repo: Path,
    run_id: str,
    *,
    summary: str,
    shards: dict[str, list[str]],
    allowed_paths: list[str] | None = None,
    allow_policy_paths: list[str] | None = None,
    shard_checks: dict[str, list[str]] | None = None,
    models: dict[str, Any] | None = None,
    cloud_review: bool | None = None,
    deep_review: bool | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    if not shards:
        raise IllegalTransitionError("complete-grill requires at least one shard with allowed_paths")
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("draft",), "complete-grill")
        state["grill_summary"] = summary
        if models is not None:
            state["models"] = dict(models)
        if cloud_review is not None:
            state["reviewers"]["cloud"] = bool(cloud_review)
        if deep_review is not None:
            state["reviewers"]["deep"] = bool(deep_review)
        state["allowed_paths"] = list(allowed_paths or [])
        state["allow_policy_paths"] = list(allow_policy_paths or [])
        unknown_check_shards = sorted(set((shard_checks or {}).keys()) - set(shards.keys()))
        if unknown_check_shards:
            raise IllegalTransitionError(
                f"shard_checks references unknown shard(s): {', '.join(unknown_check_shards)}"
            )
        shard_map: dict[str, Any] = {}
        for sid, paths in shards.items():
            if not paths:
                raise IllegalTransitionError(f"shard {sid!r} must have non-empty allowed_paths")
            shard_map[sid] = _new_shard(paths, (shard_checks or {}).get(sid, []), state.get("base_sha"))
        state["shards"] = shard_map
        state["status"] = "grilled"
        state["history"].append({"at": utc_now(), "event": "complete-grill", "status": "grilled"})
        _save_run_unlocked(repo, run_id, state)
        return state


def lock_run(
    repo: Path,
    run_id: str,
    *,
    base_sha: str | None = None,
    pr_number: int | None = None,
    require_delegation_receipts: bool = False,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("grilled",), "lock")
        if base_sha is not None:
            state["base_sha"] = git_rev_parse(repo, base_sha)
        if pr_number is not None:
            state["pr_number"] = int(pr_number)
        if require_delegation_receipts:
            state["require_delegation_receipts"] = True
        state["contract_revision"] = max(1, int(state.get("contract_revision") or 0) + 1)
        contract = _build_frozen_contract(state)
        state["contract_sha256"] = contract_sha256(contract)
        _write_trusted_contract(repo, contract)
        for sdata in state["shards"].values():
            sdata["status"] = "locked"
            sdata["has_passed_individually"] = False
            sdata["last_green_sha"] = state.get("base_sha")
        state["status"] = "locked"
        state["history"].append(
            {
                "at": utc_now(),
                "event": "lock",
                "contract_revision": state["contract_revision"],
                "contract_sha256": state["contract_sha256"],
                "status": "locked",
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def publish_contract(repo: Path, run_id: str, *, lock_dir: Path | None = None) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        if int(state.get("contract_revision") or 0) < 1:
            raise IllegalTransitionError("cannot publish-contract before lock")
        contract = _build_frozen_contract(state)
        digest = contract_sha256(contract)
        if state.get("contract_sha256") != digest:
            raise IllegalTransitionError("run contract_sha256 does not match current FrozenContractV1")
        path = _write_trusted_contract(repo, contract)
        return {
            "run_id": run_id,
            "contract_revision": contract["contract_revision"],
            "contract_sha256": digest,
            "trusted_contract_path": str(path),
            "comment_markdown": format_contract_comment(contract),
        }


def start_shard(
    repo: Path,
    run_id: str,
    shard_id: str,
    *,
    worktree_path: str | None = None,
    last_green_sha: str | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """Multi-shard start: allowed whenever run.status in ('locked', 'in_progress') and shard.status == 'locked'."""
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("locked", "in_progress"), "start-shard")
        shard = _require_shard(state, shard_id)
        if shard["status"] != "locked":
            raise IllegalTransitionError(
                f"cannot start shard {shard_id!r} in status {shard['status']!r} (expected 'locked')"
            )
        shard["status"] = "in_progress"
        if worktree_path is not None:
            shard["worktree_path"] = worktree_path
        if last_green_sha is not None:
            shard["last_green_sha"] = last_green_sha
        state["status"] = derive_run_status(state)
        state["history"].append(
            {"at": utc_now(), "event": "start-shard", "shard": shard_id, "status": state["status"],
             "branch": git_current_branch(repo) if worktree_path is None else None}
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def _has_passing_reviews_for_hash(state: dict[str, Any], code_hash: str) -> bool:
    if open_oracle_mismatches(state, code_hash):
        return False
    reviewers = state.get("reviewers") or {}
    cloud_enabled = bool(reviewers.get("cloud", True))
    deep_enabled = bool(reviewers.get("deep", True))
    if not cloud_enabled and not deep_enabled:
        return True
    passed_stages = {
        r.get("stage")
        for r in (state.get("reviews") or [])
        if r.get("verdict") == "pass" and r.get("code_tree_sha256") == code_hash
    }
    if cloud_enabled and "cloud" not in passed_stages:
        return False
    if deep_enabled and "deep" not in passed_stages:
        return False
    return True


def _apply_post_pass_status(state: dict[str, Any], prev_status: str, eff_code_hash: str) -> None:
    hashes = {s.get("passed_code_tree_sha256") for s in state["shards"].values()}
    all_passed_same = (
        all(s["status"] == "passed" for s in state["shards"].values())
        and len(hashes) == 1
        and None not in hashes
        and not state.get("pending_contract_amendments")
    )
    if not all_passed_same:
        state["status"] = "in_progress"
        state["status"] = derive_run_status(state)
        return
    if prev_status in ("safeguard_pending", "ready_to_merge"):
        if _has_passing_reviews_for_hash(state, eff_code_hash):
            state["status"] = "safeguard_pending" if _unsafeguarded_findings(state) else "ready_to_merge"
        else:
            state["status"] = "gate_passed"
        return
    state["status"] = "in_progress"
    state["status"] = derive_run_status(state)


def bind_pr(
    repo: Path,
    run_id: str,
    *,
    pr_number: int,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    if int(pr_number) <= 0:
        raise IllegalTransitionError(f"pr_number must be positive, got {pr_number!r}")
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(
            state,
            (
                "grilled",
                "locked",
                "in_progress",
                "retrying",
                "gate_passed",
                "awaiting_cloud_review",
                "awaiting_deep_review",
                "safeguard_pending",
                "ready_to_merge",
            ),
            "bind-pr",
        )
        state["pr_number"] = int(pr_number)
        if int(state.get("contract_revision") or 0) >= 1:
            state["contract_revision"] = int(state["contract_revision"]) + 1
            contract = _build_frozen_contract(state)
            state["contract_sha256"] = contract_sha256(contract)
            _write_trusted_contract(repo, contract)
        state["history"].append(
            {
                "at": utc_now(),
                "event": "bind-pr",
                "pr_number": int(pr_number),
                "contract_revision": state.get("contract_revision"),
                "status": state["status"],
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def _clear_shard_pass(shard: dict[str, Any]) -> None:
    shard.update(passed_code_tree_sha256=None, has_passed_individually=False)
    shard.pop("passed_tooling", None)
    shard.pop("trusted_policy_pass_sha", None)


def _reset_shard_gate(shard: dict[str, Any]) -> None:
    _clear_shard_pass(shard)
    shard.update(status="locked", worker_strikes=0, retry_scope=None)


def _set_base(
    repo: Path, state: dict[str, Any], base_ref: str, branch: str | None,
) -> None:
    if branch is not None:
        if not branch or _run_git(repo, ["check-ref-format", f"refs/heads/{branch}"], check=False).returncode:
            raise IllegalTransitionError(f"invalid new base branch {branch!r}")
        state["base_branch_bound"] = True
        state["base_branch"] = branch
    resolved_base = git_rev_parse(repo, base_ref)
    state["base_sha"] = resolved_base
    for key in ("sha", "worktree", "branch", "detached"):
        state[f"last_integrated_{key}"] = None
    for shard in state["shards"].values():
        _reset_shard_gate(shard)
        shard["last_green_sha"] = resolved_base


def refresh_base(
    repo: Path,
    run_id: str,
    *,
    new_base_sha: str,
    new_base_branch: str | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(
            state,
            (
                "locked",
                "in_progress",
                "retrying",
                "gate_passed",
                "awaiting_cloud_review",
                "awaiting_deep_review",
                "safeguard_pending",
                "ready_to_merge",
            ),
            "refresh-base",
        )
        if state.get("pending_contract_amendments"):
            raise IllegalTransitionError("cannot refresh-base while contract amendments are pending")
        old_branch = state.get("base_branch", "main")
        was_bound = state.get("base_branch_bound")
        _set_base(repo, state, new_base_sha, new_base_branch)
        if new_base_branch is not None and (old_branch != new_base_branch or not was_bound):
            _revise_contract(repo, state)
        state["status"] = "in_progress"
        state["status"] = derive_run_status(state)
        state["history"].append(
            {
                "at": utc_now(),
                "event": "refresh-base",
                "base_sha": state["base_sha"],
                "base_branch": state.get("base_branch", "main"),
                "status": state["status"],
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def record_check_timings(
    repo: Path, run_id: str, *, scope: str, head_sha: str,
    steps: dict[str, float], passed: bool, lock_dir: Path | None = None,
) -> dict[str, float]:
    """Keep the five latest gate attempts per shard or integration scope."""
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        history = state.setdefault("check_timings", {}).setdefault(scope, [])
        previous = {
            name: seconds for attempt in history for name, seconds in attempt["steps"].items()
        }
        history.append({"at": utc_now(), "head_sha": head_sha, "passed": passed, "steps": steps})
        del history[:-5]
        _save_run_unlocked(repo, run_id, state)
        return previous


def _require_checked_contract(
    state: dict[str, Any], checked_contract: tuple[str, str | None] | None,
) -> None:
    if checked_contract is not None and checked_contract != (state["contract_sha256"], state["base_sha"]):
        raise IllegalTransitionError("contract or base changed during check; run check again")


def _require_checked_tooling(state: dict[str, Any], tooling: dict[str, Any] | None) -> None:
    for source, sha in (tooling or {}).get("policy_shas", {}).items():
        shard = state["shards"].get(source, {})
        if (shard.get("status"), shard.get("has_passed_individually"), shard.get("trusted_policy_pass_sha")) != (
            "passed", True, sha,
        ):
            raise IllegalTransitionError(f"trusted tooling from shard {source!r} changed during check; run check again")


def record_check_failure(
    repo: Path, run_id: str, *, shard_id: str | None, head_sha: str, check_name: str, output: str,
    checked_contract: tuple[str, str | None], lock_dir: Path | None = None,
) -> None:
    """Invalidate current gate evidence and retain the historical rollback target."""
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_checked_contract(state, checked_contract)
        _require_status(
            state,
            ("in_progress", "retrying", "gate_passed", "awaiting_cloud_review", "awaiting_deep_review",
             "safeguard_pending", "ready_to_merge"),
            "record-check-failure",
        )
        signature = normalize_failure(check_name, output)
        if shard_id is None and len(state["shards"]) == 1:
            shard_id = next(iter(state["shards"]))
        if shard_id is None:
            # A shared integration failure leaves individual shard qualifications intact.
            for shard in state["shards"].values():
                shard["passed_code_tree_sha256"] = None
                shard.pop("passed_tooling", None)
        else:
            shard = _require_shard(state, shard_id)
            _clear_shard_pass(shard)
            shard.update(status="in_progress", pending_dead_end_sig=signature)
        state["status"] = "in_progress"
        state["status"] = derive_run_status(state)
        state["history"].append({
            "at": utc_now(), "event": "check-failed", "shard": shard_id, "head_sha": head_sha,
            "check_name": check_name, "signature": signature, "status": state["status"],
        })
        _save_run_unlocked(repo, run_id, state)


def record_shard_pass(
    repo: Path,
    run_id: str,
    shard_id: str,
    *,
    code_tree_sha256: str | None = None,
    head_sha: str | None = None,
    checked_contract: tuple[str, str | None] | None = None,
    tooling: dict[str, Any] | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_checked_contract(state, checked_contract)
        _require_checked_tooling(state, tooling)
        prev_status = state["status"]
        _require_status(
            state,
            ("in_progress", "retrying", "gate_passed", "safeguard_pending", "ready_to_merge"),
            "pass-shard",
        )
        shard = _require_shard(state, shard_id)
        if shard["status"] not in ("in_progress", "retrying", "passed"):
            raise IllegalTransitionError(
                f"cannot pass shard {shard_id!r} from status {shard['status']!r}"
            )
        eff_code_hash = code_tree_sha256 or compute_code_tree_sha256(repo, head_sha or "HEAD", run_id=run_id)
        shard["status"] = "passed"
        shard["worker_strikes"] = 0
        shard["pending_dead_end_sig"] = None
        shard["passed_code_tree_sha256"] = eff_code_hash
        shard["has_passed_individually"] = True
        shard["retry_scope"] = None
        if head_sha:
            shard["last_green_sha"] = head_sha
        shard.pop("trusted_policy_pass_sha", None)
        if tooling is not None:
            shard["passed_tooling"] = tooling
            if tooling["fast_check_ran"] and head_sha:
                shard["trusted_policy_pass_sha"] = head_sha
        else:
            shard.pop("passed_tooling", None)
        _apply_post_pass_status(state, prev_status, eff_code_hash)
        state["history"].append(
            {
                "at": utc_now(),
                "event": "shard-passed",
                "shard": shard_id,
                "code_tree_sha256": eff_code_hash,
                "status": state["status"],
                **({"static_lint": tooling["static_lint"]} if tooling and "static_lint" in tooling else {}),
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def record_all_shards_pass(
    repo: Path,
    run_id: str,
    *,
    code_tree_sha256: str | None = None,
    head_sha: str | None = None,
    worktree_path: str | None = None,
    branch_name: str | None = None,
    checked_contract: tuple[str, str | None] | None = None,
    tooling: dict[str, Any] | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically record the shared integration `code_tree_sha256` across all shards."""
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_checked_contract(state, checked_contract)
        _require_checked_tooling(state, tooling)
        prev_status = state["status"]
        _require_status(
            state,
            ("in_progress", "retrying", "gate_passed", "safeguard_pending", "ready_to_merge"),
            "pass-all-shards",
        )
        for sid, sdata in state["shards"].items():
            if sdata["status"] not in ("in_progress", "retrying", "passed"):
                raise IllegalTransitionError(
                    f"cannot integrate shard {sid!r} from status {sdata['status']!r}"
                )
        eff_code_hash = code_tree_sha256 or compute_code_tree_sha256(repo, head_sha or "HEAD", run_id=run_id)
        if head_sha:
            state["last_integrated_sha"] = head_sha
            int_wt = Path(worktree_path).resolve() if worktree_path else repo.resolve()
            state["last_integrated_worktree"] = str(int_wt)
            resolved_branch = branch_name if branch_name is not None else git_current_branch(int_wt)
            state["last_integrated_detached"] = resolved_branch is None
            state["last_integrated_branch"] = resolved_branch
        single_shard = len(state["shards"]) == 1
        for sdata in state["shards"].values():
            sdata["status"] = "passed"
            sdata["worker_strikes"] = 0
            sdata["pending_dead_end_sig"] = None
            sdata["passed_code_tree_sha256"] = eff_code_hash
            sdata["retry_scope"] = None
            if tooling is not None:
                sdata["passed_tooling"] = tooling
            else:
                sdata.pop("passed_tooling", None)
            if single_shard:
                sdata["has_passed_individually"] = True
                if head_sha:
                    sdata["last_green_sha"] = head_sha
        _apply_post_pass_status(state, prev_status, eff_code_hash)
        state["history"].append(
            {
                "at": utc_now(),
                "event": "all-shards-passed",
                "code_tree_sha256": eff_code_hash,
                "status": state["status"],
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def retry_shard(
    repo: Path,
    run_id: str,
    shard_id: str,
    *,
    check_name: str,
    error_output: str,
    worktree: Path | None = None,
    integrated: bool = False,
    rollback: bool = False,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """Record a strike and apply retry limits, with optional safe rollback at the limit."""
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        prev_run_status = state["status"]
        _require_status(
            state,
            ("in_progress", "retrying", "gate_passed", "safeguard_pending", "ready_to_merge"),
            "retry-shard",
        )
        shard = _require_shard(state, shard_id)
        if shard["status"] not in ("in_progress", "retrying", "passed"):
            raise IllegalTransitionError(
                f"cannot retry shard {shard_id!r} from status {shard['status']!r}"
            )

        # Determine whether this retry applies to the integrated checkout (e.g. post-review safeguard failure)
        # versus the shard-local worker worktree.
        shard_wt_resolved = Path(shard["worktree_path"]).resolve() if shard.get("worktree_path") else None
        recorded_int_wt = (
            Path(state["last_integrated_worktree"]).resolve()
            if state.get("last_integrated_worktree")
            else None
        )
        is_integrated_retry = bool(integrated) or shard.get("retry_scope") == "integrated"
        if integrated and rollback and not state.get("last_integrated_sha"):
            raise IllegalTransitionError(
                "cannot run integrated retry-shard before an integrated check --all-shards pass exists"
            )
        if not is_integrated_retry and state.get("last_integrated_sha"):
            if worktree is not None:
                wt_res = worktree.resolve()
                if recorded_int_wt is not None and wt_res == recorded_int_wt and (
                    shard_wt_resolved is None or wt_res != shard_wt_resolved
                ):
                    is_integrated_retry = True
                elif recorded_int_wt is None and wt_res == repo.resolve() and (
                    shard_wt_resolved is None or wt_res != shard_wt_resolved
                ):
                    is_integrated_retry = True
            elif prev_run_status in ("gate_passed", "safeguard_pending", "ready_to_merge"):
                is_integrated_retry = True
        if is_integrated_retry:
            shard["retry_scope"] = "integrated"

        limits = state.get("limits") or _limits_from_repo(repo)
        max_strikes = int(limits.get("max_worker_strikes", DEFAULT_MAX_WORKER_STRIKES))
        max_replan = int(limits.get("max_replan_count", DEFAULT_MAX_REPLAN_COUNT))
        max_total = int(limits.get("max_total_attempts", DEFAULT_MAX_TOTAL_ATTEMPTS))

        sig = normalize_failure(check_name, error_output)
        shard["attempts"] = int(shard.get("attempts") or 0) + 1
        shard["worker_strikes"] = int(shard.get("worker_strikes") or 0) + 1
        shard["pending_dead_end_sig"] = sig
        shard["passed_code_tree_sha256"] = None
        if not is_integrated_retry:
            _clear_shard_pass(shard)
        state["total_attempts"] = int(state.get("total_attempts") or 0) + 1

        needs_rollback = (
            shard["worker_strikes"] >= max_strikes or state["total_attempts"] >= max_total
        )
        rolled_back_to: str | None = None
        rollback_commit: str | None = None
        if needs_rollback and rollback:
            if is_integrated_retry:
                target_sha = state.get("last_integrated_sha")
                if not target_sha:
                    raise UnsafeRollbackError(
                        "refusing integrated rollback: last_integrated_sha is missing"
                    )
                if worktree is not None:
                    rollback_wt = worktree.resolve()
                elif recorded_int_wt is not None:
                    rollback_wt = recorded_int_wt
                else:
                    raise UnsafeRollbackError(
                        "refusing integrated rollback: last_integrated_worktree is not recorded; pass --worktree"
                    )
                if not rollback_wt.is_dir():
                    raise UnsafeRollbackError(
                        f"refusing integrated rollback: worktree {rollback_wt} does not exist"
                    )
                expected_detached = state.get("last_integrated_detached")
                expected_branch = state.get("last_integrated_branch")
                if expected_detached is None:
                    raise UnsafeRollbackError(
                        "refusing integrated rollback: last_integrated_detached state is not recorded"
                    )
                actual_branch = git_current_branch(rollback_wt)
                actual_detached = actual_branch is None
                if bool(expected_detached) != actual_detached:
                    raise UnsafeRollbackError(
                        f"refusing integrated rollback: worktree {rollback_wt} detached={actual_detached} "
                        f"(branch={actual_branch!r}), expected detached={bool(expected_detached)} "
                        f"(branch={expected_branch!r})"
                    )
                if not actual_detached and actual_branch != expected_branch:
                    raise UnsafeRollbackError(
                        f"refusing integrated rollback: worktree {rollback_wt} is on branch {actual_branch!r}, "
                        f"expected {expected_branch!r}"
                    )
                if not git_is_ancestor(rollback_wt, target_sha, "HEAD"):
                    raise UnsafeRollbackError(
                        f"refusing integrated rollback: worktree {rollback_wt} HEAD does not descend from "
                        f"last_integrated_sha {target_sha[:8]}"
                    )
                rb_allowed = list(state.get("allowed_paths") or [])
                for sdata in (state.get("shards") or {}).values():
                    rb_allowed.extend(sdata.get("allowed_paths") or [])
            else:
                rollback_wt = worktree or (Path(shard["worktree_path"]) if shard.get("worktree_path") else repo)
                target_sha = shard.get("last_green_sha") or state.get("base_sha")
                rb_allowed = list(shard.get("allowed_paths") or [])
            # Safe rollback check: exit 4 on ANY dirty/untracked edit anywhere in worktree
            # (excluding only this run's own checkpoint file if rolling back in the coordinator repo).
            assert_clean_worktree_for_rollback(rollback_wt, allow_paths={f".autoloop/runs/{run_id}.json"})
            if target_sha:
                rollback_commit = git_restore_to_commit(
                    rollback_wt,
                    target_sha,
                    run_id=run_id,
                    allowed_paths=rb_allowed,
                )
                rolled_back_to = target_sha

        if state["total_attempts"] >= max_total:
            shard["status"] = "blocked_human"
            reason = f"max_total_attempts ({max_total}) reached"
        elif shard["worker_strikes"] >= max_strikes:
            if int(state.get("replan_count") or 0) < max_replan:
                shard["status"] = "needs_replan"
                reason = f"max_worker_strikes ({max_strikes}) reached; replan required"
            else:
                shard["status"] = "blocked_human"
                reason = f"max_worker_strikes ({max_strikes}) and max_replan_count ({max_replan}) exhausted"
        else:
            shard["status"] = "retrying"
            reason = f"strike {shard['worker_strikes']}/{max_strikes}"

        state["status"] = "in_progress"
        state["status"] = derive_run_status(state)
        hist_entry: dict[str, Any] = {
            "at": utc_now(),
            "event": "retry-shard",
            "shard": shard_id,
            "signature": sig,
            "reason": reason,
            "status": state["status"],
        }
        if rolled_back_to:
            hist_entry["rolled_back_to"] = rolled_back_to
        if rollback_commit:
            hist_entry["rollback_commit"] = rollback_commit
        state["history"].append(hist_entry)
        _save_run_unlocked(repo, run_id, state)
        return state


def log_dead_end(
    repo: Path,
    run_id: str,
    shard_id: str,
    *,
    signature: str | None = None,
    hypothesis: str,
    outcome: str,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        shard = _require_shard(state, shard_id)
        eff_sig = signature or shard.get("pending_dead_end_sig")
        if not eff_sig:
            raise IllegalTransitionError("dead-end requires a failure signature")
        entry = {
            "at": utc_now(),
            "shard": shard_id,
            "signature": eff_sig,
            "hypothesis": hypothesis,
            "outcome": outcome,
        }
        state["dead_ends"].append(entry)
        shard["pending_dead_end_sig"] = None
        if shard["status"] == "retrying":
            shard["status"] = "in_progress"
            state["status"] = derive_run_status(state)
        state["history"].append({"at": utc_now(), "event": "log-dead-end", "shard": shard_id, "signature": eff_sig})
        record_lesson(
            repo, run_id, kind="dead_end", summary=f"{hypothesis}\nOutcome: {outcome}",
            evidence=f".autoloop/runs/{run_id}.json", applies_to=shard["allowed_paths"],
            created_at=entry["at"], draft=True,
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def _require_unused_shard(repo: Path, state: dict[str, Any], shard_id: str) -> None:
    shard = _require_shard(state, shard_id)
    if any(
        record.get("shard", record.get("shard_id")) == shard_id
        for register in ("findings", "oracle_mismatches")
        for record in (state.get(register) or {}).values()
    ):
        raise IllegalTransitionError(f"cannot drop shard {shard_id!r}: it has findings")
    if shard.get("worktree_path"):
        raise IllegalTransitionError(f"cannot drop shard {shard_id!r}: it has a registered worktree")

    start = None
    for event in reversed(state["history"]):
        if event.get("event") == "resolve-contract-change" and any(
            item["shard"] == shard_id for item in event.get("dropped_shards", [])
        ):
            break
        if event.get("event") == "start-shard" and event.get("shard") == shard_id:
            start = event
            break
    if start is None and shard["status"] == "locked":
        return

    base = state.get("base_sha")
    if not base or shard.get("last_green_sha") not in (None, base):
        raise IllegalTransitionError(f"cannot drop shard {shard_id!r}: it has commit evidence")
    refs = ["HEAD"]
    for branch in {f"shard/{shard_id}", (start or {}).get("branch")} - {None}:
        ref = f"refs/heads/{branch}"
        if _run_git(repo, ["show-ref", "--verify", "--quiet", ref], check=False).returncode == 0:
            refs.append(ref)
    changed = _run_git(repo, [
        "log", "-m", "--full-history", "--name-only", "--format=", "--no-renames", "-z", *refs, f"^{base}", "--",
    ]).stdout
    if any(
        matches_any_glob(path.decode("utf-8", errors="surrogateescape"), shard["allowed_paths"])
        for path in changed.split(b"\0") if path
    ):
        raise IllegalTransitionError(f"cannot drop shard {shard_id!r}: it has commits in its allowed paths")


def request_contract_change(
    repo: Path,
    run_id: str,
    shard_id: str,
    *,
    reason: str,
    proposed_paths: list[str] | None = None,
    drop: bool = False,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(
            state,
            (
                "locked",
                "in_progress",
                "retrying",
                "needs_replan",
                "gate_passed",
                "safeguard_pending",
                "ready_to_merge",
            ),
            "request-contract-change",
        )
        shard = state["shards"].get(shard_id)
        if shard is None and (not RUN_ID_RE.fullmatch(shard_id) or ".." in shard_id):
            raise IllegalTransitionError(f"invalid new shard ID {shard_id!r}")
        pending = state["pending_contract_amendments"]
        if any(item["shard"] == shard_id and (drop or item.get("drop")) for item in pending):
            raise IllegalTransitionError(f"conflicting contract change already requested for {shard_id!r}")
        if drop:
            if not reason.strip() or proposed_paths:
                raise IllegalTransitionError("--drop requires a non-empty reason and cannot include proposed paths")
            _require_unused_shard(repo, state, shard_id)
            if len(state["shards"]) <= 1 + sum(bool(item.get("drop")) for item in pending):
                raise IllegalTransitionError("cannot drop the last active shard")
        req_id = f"amend-{len(pending) + 1}"
        pending.append(
            {
                "id": req_id,
                "shard": shard_id,
                "reason": reason,
                "proposed_paths": list(proposed_paths or []),
                "requested_at": utc_now(),
                **({"drop": True} if drop else {}),
            }
        )
        if shard is not None and not drop:
            shard["status"] = "needs_contract_amendment"
        state["status"] = "in_progress"
        state["status"] = derive_run_status(state)
        state["history"].append(
            {"at": utc_now(), "event": "request-contract-change", "shard": shard_id, "id": req_id}
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def apply_replan(
    repo: Path,
    run_id: str,
    *,
    plan_summary: str,
    shard_id: str | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("needs_replan",), "apply-replan")
        limits = state.get("limits") or _limits_from_repo(repo)
        max_replan = int(limits.get("max_replan_count", DEFAULT_MAX_REPLAN_COUNT))
        if int(state.get("replan_count") or 0) >= max_replan:
            raise IllegalTransitionError(f"max_replan_count ({max_replan}) already reached")
        state["replan_count"] = int(state.get("replan_count") or 0) + 1
        target_shards = [shard_id] if shard_id else [
            sid for sid, s in state["shards"].items() if s["status"] == "needs_replan"
        ]
        if not target_shards:
            raise IllegalTransitionError("no shards in needs_replan state")
        for sid in target_shards:
            shard = _require_shard(state, sid)
            shard["status"] = "locked"
            shard["worker_strikes"] = 0
            shard["passed_code_tree_sha256"] = None
            shard["has_passed_individually"] = False
            shard["retry_scope"] = None
        state["status"] = derive_run_status(state)
        state["history"].append(
            {
                "at": utc_now(),
                "event": "apply-replan",
                "plan_summary": plan_summary,
                "replan_count": state["replan_count"],
                "status": state["status"],
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def resolve_contract_change(
    repo: Path,
    run_id: str,
    *,
    decision: str,
    add_paths: dict[str, list[str]] | None = None,
    allow_policy_paths: list[str] | None = None,
    shard_checks: dict[str, list[str]] | None = None,
    new_base_sha: str | None = None,
    new_base_branch: str | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise IllegalTransitionError(f"decision must be 'approve' or 'reject', got {decision!r}")
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("needs_contract_amendment",), "resolve-contract-change")
        drops = []
        if decision == "approve":
            shards = state["shards"]
            pending = state["pending_contract_amendments"]
            drop_ids = {r["shard"] for r in pending if r.get("drop")}
            if drop_ids & (set(add_paths or {}) | set(shard_checks or {})):
                raise IllegalTransitionError("cannot update paths or checks for a shard being dropped")
            if drop_ids and not (shards.keys() - drop_ids):
                raise IllegalTransitionError("cannot drop the last active shard")
            for request in pending:
                if not request.get("drop"):
                    continue
                sid = request["shard"]
                _require_unused_shard(repo, state, sid)
                drops.append({
                    "shard": sid, "reason": request["reason"],
                    "allowed_paths": list(shards[sid]["allowed_paths"]),
                    "contract_revision": state["contract_revision"] + 1,
                })
                del shards[sid]
            if drops:
                state.setdefault("dropped_shards", []).extend(drops)
            requested_new = {r["shard"] for r in pending if not r.get("drop")} - shards.keys()
            supplied_new = set(add_paths or {}) - shards.keys()
            if supplied_new != requested_new:
                raise IllegalTransitionError(
                    "approve requires --add-shard-paths for every requested new shard and only requested new shards"
                )
            if add_paths:
                for sid, extra in add_paths.items():
                    if not extra or any(not path.strip() for path in extra):
                        raise IllegalTransitionError(f"shard {sid!r} must have non-empty allowed_paths")
                    if sid in shards:
                        shard = shards[sid]
                        shard["allowed_paths"] = list(dict.fromkeys(shard["allowed_paths"] + list(extra)))
                        _reset_shard_gate(shard)
                    else:
                        shards[sid] = _new_shard(extra, [], state.get("base_sha"))
                    for dropped in state.get("dropped_shards", []):
                        restored = [
                            path for path in extra
                            if any(globs_overlap(path, former) for former in dropped["allowed_paths"])
                        ]
                        if restored:
                            readded = dropped.setdefault("readded", {})
                            readded[sid] = list(dict.fromkeys([*readded.get(sid, []), *restored]))
            for sid in sorted(requested_new):
                for other, other_shard in shards.items():
                    if other != sid and any(
                        globs_overlap(a, b)
                        for a in shards[sid]["allowed_paths"] for b in other_shard["allowed_paths"]
                    ):
                        raise IllegalTransitionError(f"new shard {sid!r} overlaps allowed_paths of shard {other!r}")
            if allow_policy_paths:
                existing_ap = list(state.get("allow_policy_paths") or [])
                state["allow_policy_paths"] = list(dict.fromkeys(existing_ap + list(allow_policy_paths)))
            if shard_checks:
                for sid, cmds in shard_checks.items():
                    shard = _require_shard(state, sid)
                    shard["checks"] = list(cmds)
                    _reset_shard_gate(shard)
            if new_base_branch is not None and not new_base_sha:
                raise IllegalTransitionError("--new-base-branch requires --new-base-sha")
            if new_base_sha:
                _set_base(repo, state, new_base_sha, new_base_branch)
            _revise_contract(repo, state)

        state["pending_contract_amendments"] = []
        for sdata in state["shards"].values():
            if sdata["status"] == "needs_contract_amendment":
                sdata["status"] = "locked"
                sdata["worker_strikes"] = 0
                sdata["passed_code_tree_sha256"] = None
                sdata["has_passed_individually"] = False
        state["status"] = "in_progress"
        state["status"] = derive_run_status(state)
        state["history"].append(
            {
                "at": utc_now(),
                "event": "resolve-contract-change",
                "decision": decision,
                "contract_revision": state["contract_revision"],
                "status": state["status"],
                **({"dropped_shards": drops} if drops else {}),
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def human_resolve(
    repo: Path,
    run_id: str,
    *,
    action: str,
    note: str = "",
    shard_id: str | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    if action not in ("resume", "terminate"):
        raise IllegalTransitionError(f"human-resolve action must be 'resume' or 'terminate', got {action!r}")
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("blocked_human",), "human-resolve")
        if action == "terminate":
            state["status"] = "terminated"
        else:
            targets = [shard_id] if shard_id else [
                sid for sid, s in state["shards"].items() if s["status"] == "blocked_human"
            ]
            for sid in targets:
                shard = _require_shard(state, sid)
                shard["status"] = "locked"
                shard["worker_strikes"] = 0
                shard["passed_code_tree_sha256"] = None
                shard["has_passed_individually"] = False
            state["status"] = derive_run_status(state)
        state["history"].append(
            {"at": utc_now(), "event": "human-resolve", "action": action, "note": note, "status": state["status"]}
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def _gated_head(state: dict[str, Any]) -> str | None:
    shards = state["shards"]
    if len(shards) == 1:
        return next(iter(shards.values())).get("last_green_sha")
    return state.get("last_integrated_sha")


def submit_for_review(repo: Path, run_id: str, *, lock_dir: Path | None = None) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("gate_passed",), "submit-for-review")
        if state.get("pending_contract_amendments"):
            raise IllegalTransitionError("cannot submit-for-review with pending contract amendments")
        open_findings = [
            fid for fid, f in (state.get("findings") or {}).items() if (f or {}).get("status") == "open"
        ]
        if open_findings:
            raise IllegalTransitionError(
                f"cannot submit-for-review with open findings: {', '.join(sorted(open_findings))}"
            )
        gate_head = _gated_head(state)
        for fid, finding in (state.get("findings") or {}).items():
            fixed_by = finding.get("fixed_by")
            if finding["status"] == "fixed_pending_review" and fixed_by:
                if not gate_head or not git_is_ancestor(repo, fixed_by, gate_head):
                    raise IllegalTransitionError(f"finding {fid!r} fix {fixed_by} is missing from the gated commit")
        reviewers = state.get("reviewers") or {}
        cloud_enabled = bool(reviewers.get("cloud", True))
        deep_enabled = bool(reviewers.get("deep", True))
        passed_hashes = {s.get("passed_code_tree_sha256") for s in state["shards"].values()}
        code_hash = next(iter(passed_hashes)) if len(passed_hashes) == 1 else None
        open_mismatches = open_oracle_mismatches(state, code_hash)
        if state.get("oracle_mismatches"):
            for record in state["oracle_mismatches"].values():
                if not gate_head or git_read_blob(repo, gate_head, record["test"]) is None:
                    raise IllegalTransitionError(f"oracle mismatch test {record['test']!r} is missing at the gated commit")

        if cloud_enabled:
            state["status"] = "awaiting_cloud_review"
        elif deep_enabled or open_mismatches:
            state["status"] = "awaiting_deep_review"
        else:
            # Zero-reviewer direct route (V3 §3)
            # Any fixed_pending_review findings advance to closed_needs_safeguard when reviewers are disabled.
            for fdata in (state.get("findings") or {}).values():
                if fdata.get("status") == "fixed_pending_review":
                    fdata["status"] = "closed_needs_safeguard"
                    fdata["verified_at"] = utc_now()
            state["status"] = "safeguard_pending" if _unsafeguarded_findings(state) else "ready_to_merge"

        state["history"].append({"at": utc_now(), "event": "submit-for-review", "status": state["status"]})
        _save_run_unlocked(repo, run_id, state)
        return {**state, "open_oracle_mismatches": open_mismatches}


def record_oracle_mismatch(
    repo: Path, run_id: str, shard_id: str, *, upstream_ref: str, summary: str,
    why_ours_is_correct: str, test: str, lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(
            state, ("locked", "in_progress", "retrying", "gate_passed", "awaiting_cloud_review",
                    "awaiting_deep_review", "safeguard_pending", "ready_to_merge"),
            "record-oracle-mismatch",
        )
        record = {
            "shard": shard_id, "upstream_ref": upstream_ref.strip(), "summary": summary.strip(),
            "why_ours_is_correct": why_ours_is_correct.strip(), "test": test.strip(),
        }
        validate_oracle_mismatch(record, state["shards"])
        mismatch_id = oracle_mismatch_id(record)
        register = state.setdefault("oracle_mismatches", {})
        if mismatch_id not in register:
            register[mismatch_id] = record
            _revise_contract(repo, state)
            state["status"] = "in_progress"
            state["status"] = derive_run_status(state)
            state["history"].append({"at": utc_now(), "event": "record-oracle-mismatch", "id": mismatch_id})
            _save_run_unlocked(repo, run_id, state)
        return {**state, "oracle_mismatch_id": mismatch_id}


def record_finding(
    repo: Path,
    run_id: str,
    finding_id: str,
    *,
    severity: str,
    source: str,
    summary: str,
    evidence: str,
    fixed_by: str | None = None,
    shard_id: str | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    finding_id, summary, evidence = finding_id.strip(), summary.strip(), evidence.strip()
    if not finding_id or not summary or not evidence:
        raise IllegalTransitionError("record-finding requires a non-empty finding ID, summary, and evidence")
    if severity not in FINDING_SEVERITIES or source not in FINDING_SOURCES:
        raise IllegalTransitionError(f"invalid finding severity or source: {severity!r}, {source!r}")
    if fixed_by is not None:
        if not fixed_by.strip():
            raise IllegalTransitionError("--fixed-by requires a commit")
        # Resolve an object name only, without interpreting option-like input.
        fixed_by = git_rev_parse(repo, fixed_by.strip())

    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        if state["status"] == "terminated":
            raise IllegalTransitionError("cannot record-finding when run status is 'terminated'")
        findings = state.setdefault("findings", {})
        if finding_id in findings:
            raise IllegalTransitionError(f"finding {finding_id!r} already exists")
        now = utc_now()
        finding = {
            "finding_id": finding_id,
            "severity": severity,
            "source": source,
            "summary": summary,
            "evidence": evidence,
            "status": "open",
            "opened_at": now,
        }
        if shard_id is not None:
            _require_shard(state, shard_id)
            finding["shard"] = shard_id
        if fixed_by is not None:
            finding.update(status="fixed_pending_review", fixed_by=fixed_by, resolved_at=now)
        findings[finding_id] = finding
        if state["status"] in ("awaiting_cloud_review", "awaiting_deep_review", "safeguard_pending", "ready_to_merge"):
            state["status"] = "gate_passed"
        state["history"].append({
            "at": now, "event": "record-finding", "finding_id": finding_id, "source": source,
            "fixed_by": fixed_by, "status": state["status"],
        })
        _save_run_unlocked(repo, run_id, state)
        return state


def resolve_finding(
    repo: Path,
    run_id: str,
    finding_id: str,
    *,
    fix_summary: str,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        findings = state.get("findings") or {}
        if finding_id not in findings:
            raise IllegalTransitionError(f"unknown finding_id {finding_id!r}")
        finding = findings[finding_id]
        if finding.get("status") != "open":
            raise IllegalTransitionError(
                f"cannot resolve finding {finding_id!r} from status {finding.get('status')!r} (expected 'open')"
            )
        finding["status"] = "fixed_pending_review"
        finding["fix_summary"] = fix_summary
        finding["resolved_at"] = utc_now()
        state["history"].append(
            {"at": utc_now(), "event": "resolve-finding", "finding_id": finding_id}
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def record_review(
    repo: Path,
    run_id: str,
    *,
    stage: str,
    verdict: str,
    reviewer: str,
    code_tree_sha256: str,
    findings: list[dict[str, Any]] | None = None,
    oracle_mismatch_ids: list[str] | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    if stage not in ("cloud", "deep"):
        raise IllegalTransitionError(f"review stage must be 'cloud' or 'deep', got {stage!r}")
    if verdict not in ("pass", "reject"):
        raise IllegalTransitionError(f"review verdict must be 'pass' or 'reject', got {verdict!r}")
    expected_status = "awaiting_cloud_review" if stage == "cloud" else "awaiting_deep_review"

    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, (expected_status,), f"record-review({stage})")

        shard_hashes = {s.get("passed_code_tree_sha256") for s in state["shards"].values()}
        if shard_hashes != {code_tree_sha256}:
            raise IllegalTransitionError(
                f"review code_tree_sha256 {code_tree_sha256!r} does not match passed shards {shard_hashes!r}"
            )
        mismatch_ids = sorted(set(oracle_mismatch_ids or []))
        if mismatch_ids and (
            verdict != "pass" or not reviewer.strip() or set(mismatch_ids) - set(state.get("oracle_mismatches", {}))
        ):
            raise IllegalTransitionError("oracle review requires a passing named reviewer and known mismatch IDs")

        state["reviews"].append(
            {
                "at": utc_now(),
                "stage": stage,
                "verdict": verdict,
                "reviewer": reviewer,
                "code_tree_sha256": code_tree_sha256,
                "contract_revision": state["contract_revision"],
                "base_sha": state["base_sha"],
                "oracle_mismatch_ids": mismatch_ids,
            }
        )

        if verdict == "reject":
            if not findings:
                raise IllegalTransitionError("rejected review must provide at least one finding")
            for item in findings:
                fid = str(item.get("finding_id") or "").strip()
                desc = str(item.get("summary") or "").strip()
                if not fid or not desc:
                    raise IllegalTransitionError("each finding requires non-empty finding_id and summary")
                previous = state["findings"].get(fid, {})
                state["findings"][fid] = {
                    "finding_id": fid,
                    "stage": stage,
                    "reviewer": reviewer,
                    "summary": desc,
                    "severity": str(item.get("severity") or previous.get("severity", "P1")),
                    "source": previous.get("source", "review"),
                    "status": "open",
                    "opened_at": utc_now(),
                }
                if item.get("evidence") or previous.get("evidence"):
                    state["findings"][fid]["evidence"] = str(item.get("evidence") or previous["evidence"])
                reported_shard = item.get("shard") or previous.get("shard")
                if reported_shard is not None:
                    _require_shard(state, reported_shard)
                    if previous.get("shard") not in (None, reported_shard):
                        raise IllegalTransitionError(f"finding {fid!r} is already assigned to another shard")
                    state["findings"][fid]["shard"] = reported_shard
            # Re-open execution so worker must fix and re-pass gates
            for sdata in state["shards"].values():
                sdata["status"] = "in_progress"
                sdata["passed_code_tree_sha256"] = None
                sdata["retry_scope"] = None
            state["status"] = "in_progress"
        else:
            # Review passed: transition fixed_pending_review findings -> closed_needs_safeguard
            for fdata in state["findings"].values():
                if fdata.get("status") == "fixed_pending_review":
                    fdata["status"] = "closed_needs_safeguard"
                    fdata["verified_at"] = utc_now()

            deep_enabled = bool((state.get("reviewers") or {}).get("deep", True))
            if stage == "cloud" and deep_enabled:
                state["status"] = "awaiting_deep_review"
            else:
                if _open_or_pending_review_findings(state):
                    raise IllegalTransitionError("cannot complete review while open findings exist")
                outstanding = open_oracle_mismatches(state, code_tree_sha256)
                if outstanding:
                    raise IllegalTransitionError(f"unreviewed oracle mismatches: {', '.join(sorted(outstanding))}")
                state["status"] = "safeguard_pending" if _unsafeguarded_findings(state) else "ready_to_merge"

        state["history"].append(
            {
                "at": utc_now(),
                "event": "record-review",
                "stage": stage,
                "verdict": verdict,
                "status": state["status"],
            }
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def record_safeguard(
    repo: Path,
    run_id: str,
    finding_id: str,
    *,
    layer: str,
    artifact: str,
    summary: str,
    rule_id: str | None = None,
    why_not_static: str | None = None,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    if layer not in SAFEGUARD_LAYERS:
        raise IllegalTransitionError(
            f"invalid safeguard layer {layer!r} (must be one of {', '.join(SAFEGUARD_LAYERS)})"
        )
    if layer == "layer_4_runtime_test" and not (why_not_static and why_not_static.strip()):
        raise IllegalTransitionError(
            "layer_4_runtime_test requires non-empty --why-not-static justification"
        )
    if not artifact.strip() or not summary.strip():
        raise IllegalTransitionError("safeguard requires a non-empty artifact reference and summary")
    if layer != "layer_3_static_lint" and rule_id is not None:
        raise IllegalTransitionError("--rule-id applies only to layer_3_static_lint")

    with acquire_mutex(_state_mutex_path(repo, run_id, lock_dir)):
        state = load_run(repo, run_id)
        _require_status(state, ("safeguard_pending", "ready_to_merge"), "record-safeguard")
        limits = state.get("limits") or _limits_from_repo(repo)
        max_prose = int(limits.get("max_safeguard_prose_lines", DEFAULT_MAX_PROSE_LINES))

        prose_lines = len((summary or "").splitlines()) + len((why_not_static or "").splitlines())
        if prose_lines > max_prose:
            raise IllegalTransitionError(
                f"safeguard prose exceeds {max_prose}-line cap ({prose_lines} lines)"
            )

        findings = state.get("findings") or {}
        if finding_id not in findings:
            raise IllegalTransitionError(f"unknown finding_id {finding_id!r}")
        finding = findings[finding_id]
        refreshing_static_proof = (
            finding.get("status") == "safeguarded"
            and finding.get("safeguard", {}).get("layer") == layer == "layer_3_static_lint"
        )
        if finding.get("status") != "closed_needs_safeguard" and not refreshing_static_proof:
            raise IllegalTransitionError(
                f"cannot record safeguard for finding {finding_id!r} in status {finding.get('status')!r} "
                "(expected 'closed_needs_safeguard')"
            )

        shard_hashes = {s.get("passed_code_tree_sha256") for s in state["shards"].values()}
        current_code_hash = next(iter(shard_hashes)) if len(shard_hashes) == 1 else None
        proof = {}
        if layer == "layer_3_static_lint":
            gate_head = _gated_head(state)
            if not gate_head or not current_code_hash:
                raise IllegalTransitionError("static safeguard requires a gated commit")
            proof = verify_rule_fixture(repo, gate_head, artifact, rule_id or finding_id)
        finding["status"] = "safeguarded"
        finding["safeguard"] = {
            "layer": layer,
            "artifact": artifact,
            "summary": summary,
            "why_not_static": why_not_static,
            "safeguard_code_tree_sha256": current_code_hash,
            "recorded_at": utc_now(),
            **proof,
        }
        state["safeguards"].append({"finding_id": finding_id, **finding["safeguard"]})

        if not _unsafeguarded_findings(state) and not open_oracle_mismatches(state, current_code_hash):
            state["status"] = "ready_to_merge"

        state["history"].append(
            {
                "at": utc_now(),
                "event": "record-safeguard",
                "finding_id": finding_id,
                "layer": layer,
                "status": state["status"],
            }
        )
        record_lesson(
            repo, run_id, kind="safeguard", summary=f"{finding['summary']}\nSafeguard: {summary}",
            evidence=artifact, applies_to=sorted({
                path for shard in state["shards"].values() for path in shard["allowed_paths"]
            }),
            created_at=finding["safeguard"]["recorded_at"], draft=True,
        )
        _save_run_unlocked(repo, run_id, state)
        return state


def resume_run(repo: Path, run_id: str) -> dict[str, Any]:
    state = load_run(repo, run_id)
    next_actions: list[str] = []
    status = state["status"]
    if status == "draft":
        next_actions.append("complete-grill")
    elif status == "grilled":
        next_actions.append("lock")
    elif status in ("locked", "in_progress"):
        for sid, sdata in sorted(state["shards"].items()):
            if sdata["status"] == "locked":
                next_actions.append(f"start-shard --shard {sid}")
            elif sdata["status"] in ("in_progress", "retrying"):
                next_actions.append(f"check --run-id {run_id} --shard {sid}")
    elif status == "gate_passed":
        next_actions.append("submit-for-review")
    elif status == "needs_replan":
        next_actions.append("apply-replan")
    elif status == "needs_contract_amendment":
        next_actions.append("resolve-contract-change")
    elif status == "blocked_human":
        next_actions.append("human-resolve")
    elif status == "awaiting_cloud_review":
        next_actions.append("record-review --stage cloud")
    elif status == "awaiting_deep_review":
        next_actions.append("record-review --stage deep")
    elif status == "safeguard_pending":
        for fid in _unsafeguarded_findings(state):
            next_actions.append(f"record-safeguard --finding-id {fid}")
    elif status == "ready_to_merge":
        next_actions.append("verify-untampered")

    return {
        "run_id": run_id,
        "status": status,
        "contract_revision": state.get("contract_revision"),
        "shards": {sid: s["status"] for sid, s in sorted(state["shards"].items())},
        "unsafeguarded_findings": _unsafeguarded_findings(state),
        "next_actions": next_actions,
    }
