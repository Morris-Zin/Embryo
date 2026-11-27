#!/usr/bin/env python3
"""CLI entrypoint for the V3 opt-in autonomous workflow loop (/autoloop)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

# Allow running directly as `python tools/autoloop/cli.py ...` without PYTHONPATH tweaks.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoloop import EXIT_ERROR, EXIT_OK
from autoloop.guard import (
    GitHubAPIClient,
    GuardError,
    assert_clean_worktree_for_check,
    assert_safe_local_path,
    compute_code_tree_sha256,
    git_current_branch,
    git_is_ancestor,
    git_rev_parse,
    load_config,
    verify_pr_integrity,
    verify_untampered,
)
from autoloop.leases import LeaseError, acquire_lease, release_lease, renew_lease
from autoloop.state import (
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
from autoloop.static_rules import lint_repo


def _default_repo() -> Path:
    return Path(__file__).resolve().parents[2]


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _parse_shard_specs(specs: list[str]) -> dict[str, list[str]]:
    """Parse `--shard shard_a=glob1,glob2` specs."""
    shards: dict[str, list[str]] = {}
    for raw in specs:
        if "=" not in raw:
            raise IllegalTransitionError(f"invalid --shard spec {raw!r}; expected shard_id=glob1,glob2")
        sid, globs_raw = raw.split("=", 1)
        sid = sid.strip()
        globs = [g.strip() for g in globs_raw.split(",") if g.strip()]
        if not sid or not globs:
            raise IllegalTransitionError(f"invalid --shard spec {raw!r}")
        shards[sid] = globs
    return shards


def _parse_shard_checks(specs: list[str]) -> dict[str, list[str]]:
    """Parse `--shard-check shard_id=command` specs."""
    checks: dict[str, list[str]] = {}
    for raw in specs:
        if "=" not in raw:
            raise IllegalTransitionError(f"invalid --shard-check spec {raw!r}; expected shard_id=command")
        sid, cmd = raw.split("=", 1)
        sid = sid.strip()
        cmd = cmd.strip()
        if not sid or not cmd:
            raise IllegalTransitionError(f"invalid --shard-check spec {raw!r}")
        checks.setdefault(sid, []).append(cmd)
    return checks


def _skill_destinations(repo: Path, home: Path | None = None) -> list[tuple[Path, Path]]:
    """Return list of (containment_root, destination_path) for cross-agent skill sync."""
    user_home = home or Path.home()
    return [
        (repo, repo / ".devin/skills/autoloop/SKILL.md"),
        (user_home, user_home / ".claude/skills/autoloop/SKILL.md"),
        (user_home, user_home / ".codex/skills/autoloop/SKILL.md"),
        (user_home, user_home / ".gemini/antigravity/skills/autoloop/SKILL.md"),
        (user_home, user_home / ".agents/skills/autoloop/SKILL.md"),
    ]


def cmd_sync_skill(repo: Path, *, install: bool = False, home: Path | None = None) -> dict[str, Any]:
    source = repo / "tools/autoloop/skill/SKILL.md"
    assert_safe_local_path(repo, source, allow_missing_leaf=False)
    content = source.read_bytes()
    if content.startswith(b"\xef\xbb\xbf") or b"\r" in content:
        raise GuardError(f"{source} must use LF without UTF-8 BOM")

    results: list[dict[str, Any]] = []
    for root, dest in _skill_destinations(repo, home=home):
        safe_dest = assert_safe_local_path(root, dest, allow_missing_leaf=True)
        in_sync = safe_dest.is_file() and safe_dest.read_bytes() == content
        action = "in_sync" if in_sync else ("write" if install else "would_write")
        if install and not in_sync:
            safe_dest.parent.mkdir(parents=True, exist_ok=True)
            safe_dest.write_bytes(content)
        results.append(
            {
                "destination": str(safe_dest),
                "in_sync": in_sync,
                "action": action,
            }
        )
    return {
        "mode": "install" if install else "dry-run",
        "source": str(source),
        "targets": results,
    }


def cmd_bootstrap_repo(repo: Path, *, repo_slug: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    labels = [
        ("autoloop", "0E8A16", "PR enrolled in the opt-in /autoloop autonomous workflow"),
        ("allow-rule-edit", "D93F0B", "Owner-authorized edit to critical autoloop policy paths"),
        ("allow-auto-merge", "5319E7", "Owner-authorized auto-merge after required gates pass"),
    ]
    ruleset_payload = {
        "name": "Autoloop Main Protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": [f"refs/heads/{config.get('default_branch', 'main')}"], "exclude": []}},
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "require_code_owner_review": True,
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "autoloop-preflight", "integration_id": 15368},
                        {"context": "autoloop-candidate-checks", "integration_id": 15368},
                        {"context": "autoloop-integrity", "integration_id": 15368},
                    ],
                },
            },
        ],
    }

    if dry_run:
        return {
            "mode": "dry-run",
            "repo": repo_slug or "(current gh repo)",
            "labels": [{"name": n, "color": c, "description": d} for n, c, d in labels],
            "ruleset": ruleset_payload,
        }

    gh_repo_args = ["--repo", repo_slug] if repo_slug else []
    created_labels: list[str] = []
    for name, color, desc in labels:
        subprocess.run(
            ["gh", "label", "create", name, "--color", color, "--description", desc, "--force", *gh_repo_args],
            cwd=repo,
            check=True,
        )
        created_labels.append(name)

    target_slug = repo_slug
    if not target_slug:
        target_slug = subprocess.check_output(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=repo,
            text=True,
        ).strip()

    existing_rulesets = json.loads(
        subprocess.check_output(["gh", "api", f"repos/{target_slug}/rulesets"], cwd=repo, text=True)
    )
    existing = next((r for r in existing_rulesets if r.get("name") == ruleset_payload["name"]), None)
    if existing:
        endpoint = f"repos/{target_slug}/rulesets/{existing['id']}"
        method = "PUT"
    else:
        endpoint = f"repos/{target_slug}/rulesets"
        method = "POST"
    proc = subprocess.run(
        ["gh", "api", "--method", method, endpoint, "--input", "-"],
        cwd=repo,
        input=json.dumps(ruleset_payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        "mode": "applied",
        "repo": target_slug,
        "labels": created_labels,
        "ruleset_response": json.loads(proc.stdout) if proc.stdout.strip() else {},
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoloop", description="Opt-in autonomous workflow loop CLI (V3)")
    parser.add_argument("--repo", type=Path, default=_default_repo(), help="Repository root path")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize a new /autoloop run in draft state")
    p_init.add_argument("--run-id", required=True)
    p_init.add_argument("--goal", required=True)
    p_init.add_argument("--issue", type=int, default=None)
    p_init.add_argument("--pr", type=int, default=None)
    p_init.add_argument("--base-branch", default="main")
    p_init.add_argument("--base-sha", default=None)
    p_init.add_argument("--merge-mode", choices=("human", "auto"), default="human")
    p_init.add_argument("--no-cloud-review", action="store_true")
    p_init.add_argument("--no-deep-review", action="store_true")
    p_init.add_argument("--models-json", default="{}")

    # complete-grill
    p_grill = sub.add_parser("complete-grill", help="Record grill interview outcome and shard boundaries")
    p_grill.add_argument("--run-id", required=True)
    p_grill.add_argument("--summary", required=True)
    p_grill.add_argument("--shard", action="append", required=True, help="shard_id=glob1,glob2")
    p_grill.add_argument("--allowed-path", action="append", default=[])
    p_grill.add_argument("--allow-policy-path", action="append", default=[])
    p_grill.add_argument("--shard-check", action="append", default=[], help="shard_id=command")
    p_grill.add_argument("--models-json", default=None)
    p_grill.add_argument("--no-cloud-review", action="store_true")
    p_grill.add_argument("--no-deep-review", action="store_true")

    # lock
    p_lock = sub.add_parser("lock", help="Freeze FrozenContractV1 and transition grilled -> locked")
    p_lock.add_argument("--run-id", required=True)
    p_lock.add_argument("--base-sha", default=None)
    p_lock.add_argument("--pr", type=int, default=None)

    # bind-pr
    p_bind = sub.add_parser("bind-pr", help="Bind PR number into FrozenContractV1 and bump contract_revision")
    p_bind.add_argument("--run-id", required=True)
    p_bind.add_argument("--pr", type=int, required=True)

    # refresh-base
    p_rbase = sub.add_parser("refresh-base", help="Rebind run to an advanced base SHA and reopen shards for recheck")
    p_rbase.add_argument("--run-id", required=True)
    p_rbase.add_argument("--new-base-sha", required=True)

    # publish-contract
    p_pub = sub.add_parser("publish-contract", help="Emit FrozenContractV1 markdown comment for GitHub Issue")
    p_pub.add_argument("--run-id", required=True)

    # start-shard
    p_start = sub.add_parser("start-shard", help="Start a locked shard (supports concurrent multi-shard runs)")
    p_start.add_argument("--run-id", required=True)
    p_start.add_argument("--shard", required=True)
    p_start.add_argument("--worktree", default=None)
    p_start.add_argument("--last-green-sha", default=None)

    # retry-shard
    p_retry = sub.add_parser("retry-shard", help="Record a failed shard attempt with safe rollback guard")
    p_retry.add_argument("--run-id", required=True)
    p_retry.add_argument("--shard", required=True)
    p_retry.add_argument("--check-name", required=True)
    p_retry.add_argument("--error-output", required=True)
    p_retry.add_argument("--worktree", type=Path, default=None)
    p_retry.add_argument("--integrated", action="store_true", help="Roll back the integrated branch to last_integrated_sha")

    # verify-untampered
    p_vu = sub.add_parser("verify-untampered", help="Verify local branch/shard against FrozenContractV1")
    p_vu.add_argument("--run-id", required=True)
    p_vu.add_argument("--shard", default=None)
    p_vu.add_argument("--base-ref", default=None)
    p_vu.add_argument("--head-ref", default="HEAD")
    p_vu.add_argument("--main-ref", default="origin/main")
    p_vu.add_argument("--worktree", type=Path, default=None)

    # verify-pr-integrity
    p_vpi = sub.add_parser("verify-pr-integrity", help="CI trusted PR integrity check (read-only over Git blobs)")
    p_vpi.add_argument("--event-path", type=Path, default=None)
    p_vpi.add_argument("--repo-slug", "--repo-name", dest="repo_slug", default=None)
    p_vpi.add_argument("--base-sha", default=None)
    p_vpi.add_argument("--head-sha", default=None)
    p_vpi.add_argument("--pr-number", type=int, default=None)
    p_vpi.add_argument("--pr-author", default=None)
    p_vpi.add_argument("--pr-body", default=None)
    p_vpi.add_argument("--label", action="append", default=[])
    p_vpi.add_argument("--main-ref", default="origin/main")
    p_vpi.add_argument("--base-branch", default=None)
    p_vpi.add_argument("--mode", choices=("preflight", "finalize"), default="finalize")
    p_vpi.add_argument("--candidate-checks-result", default=None)
    p_vpi.add_argument("--trusted-shard-results-b64", default=None)
    p_vpi.add_argument("--github-output", type=Path, default=None)
    p_vpi.add_argument("--offline", action="store_true")

    # lease
    p_lease = sub.add_parser("lease", help="Acquire, renew, or release a cross-platform resource lease")
    p_lease.add_argument("action", choices=("acquire", "renew", "release"))
    p_lease.add_argument("--resource", required=True)
    p_lease.add_argument("--owner", required=True)
    p_lease.add_argument("--ttl", type=float, default=300.0)
    p_lease.add_argument("--holder-pid", type=int, default=None)
    p_lease.add_argument("--token", default=None)

    # lint
    p_lint = sub.add_parser("lint", help="Run full-tree static rules from .autoloop/rules/*.py")
    p_lint.add_argument("--tree", default=None, help="Optional Git tree-ish to lint pure blobs")

    # check
    p_check = sub.add_parser("check", help="Run local lint + untampered verification and record shard pass")
    p_check.add_argument("--run-id", required=True)
    p_check.add_argument("--shard", default=None)
    p_check.add_argument("--all-shards", action="store_true")
    p_check.add_argument("--base-ref", default=None)
    p_check.add_argument("--head-ref", default="HEAD")
    p_check.add_argument("--worktree", type=Path, default=None)
    p_check.add_argument("--skip-fast-check", action="store_true")

    # log-dead-end
    p_dead = sub.add_parser("log-dead-end", help="Record a dead-end hypothesis and outcome")
    p_dead.add_argument("--run-id", required=True)
    p_dead.add_argument("--shard", required=True)
    p_dead.add_argument("--signature", default=None)
    p_dead.add_argument("--hypothesis", required=True)
    p_dead.add_argument("--outcome", required=True)

    # request-contract-change
    p_rc = sub.add_parser("request-contract-change", help="Request FrozenContractV1 amendment")
    p_rc.add_argument("--run-id", required=True)
    p_rc.add_argument("--shard", required=True)
    p_rc.add_argument("--reason", required=True)
    p_rc.add_argument("--proposed-path", action="append", default=[])

    # apply-replan
    p_replan = sub.add_parser("apply-replan", help="Apply a bounded replan after worker strikes")
    p_replan.add_argument("--run-id", required=True)
    p_replan.add_argument("--plan-summary", required=True)
    p_replan.add_argument("--shard", default=None)

    # resolve-contract-change
    p_res_cc = sub.add_parser("resolve-contract-change", help="Approve or reject pending contract amendments")
    p_res_cc.add_argument("--run-id", required=True)
    p_res_cc.add_argument("--decision", choices=("approve", "reject"), required=True)
    p_res_cc.add_argument("--add-shard-paths", action="append", default=[], help="shard_id=glob1,glob2")
    p_res_cc.add_argument("--allow-policy-path", action="append", default=[])
    p_res_cc.add_argument("--shard-check", action="append", default=[], help="shard_id=command")
    p_res_cc.add_argument("--new-base-sha", default=None)

    # human-resolve
    p_hr = sub.add_parser("human-resolve", help="Resume or terminate a blocked_human run")
    p_hr.add_argument("--run-id", required=True)
    p_hr.add_argument("--action", choices=("resume", "terminate"), required=True)
    p_hr.add_argument("--note", default="")
    p_hr.add_argument("--shard", default=None)

    # submit-for-review
    p_sub = sub.add_parser("submit-for-review", help="Submit a gate_passed run for review (or direct zero-reviewer route)")
    p_sub.add_argument("--run-id", required=True)

    # resolve-finding
    p_rf = sub.add_parser("resolve-finding", help="Mark an open review finding as fixed_pending_review")
    p_rf.add_argument("--run-id", required=True)
    p_rf.add_argument("--finding-id", required=True)
    p_rf.add_argument("--fix-summary", required=True)

    # record-review
    p_rev = sub.add_parser("record-review", help="Record cloud or deep review pass/reject outcome")
    p_rev.add_argument("--run-id", required=True)
    p_rev.add_argument("--stage", choices=("cloud", "deep"), required=True)
    p_rev.add_argument("--verdict", choices=("pass", "reject"), required=True)
    p_rev.add_argument("--reviewer", required=True)
    p_rev.add_argument("--code-tree-sha256", default=None)
    p_rev.add_argument("--findings-json", default=None)

    # record-safeguard
    p_sg = sub.add_parser("record-safeguard", help="Record safeguard for a closed_needs_safeguard finding")
    p_sg.add_argument("--run-id", required=True)
    p_sg.add_argument("--finding-id", required=True)
    p_sg.add_argument(
        "--layer",
        choices=("layer_1_architecture", "layer_2_type", "layer_3_static_lint", "layer_4_runtime_test"),
        required=True,
    )
    p_sg.add_argument("--artifact", required=True)
    p_sg.add_argument("--summary", required=True)
    p_sg.add_argument("--why-not-static", default=None)

    # resume
    p_resume = sub.add_parser("resume", help="Inspect current run state and next legal actions")
    p_resume.add_argument("--run-id", required=True)

    # sync-skill
    p_sync = sub.add_parser("sync-skill", help="Preview (default --dry-run) or install cross-agent SKILL.md copies")
    p_sync.add_argument("--dry-run", action="store_true", help="Default mode: preview target paths without writing")
    p_sync.add_argument("--install", action="store_true", help="Write synced SKILL.md copies to all targets")

    # bootstrap-repo
    p_boot = sub.add_parser("bootstrap-repo", help="Provision GitHub labels and main branch ruleset")
    p_boot.add_argument("--repo-slug", default=None)
    p_boot.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo: Path = args.repo.resolve()

    try:
        cmd = args.command
        if cmd == "init":
            models = json.loads(args.models_json) if args.models_json else {}
            res = init_run(
                repo,
                args.run_id,
                args.goal,
                issue_number=args.issue,
                pr_number=args.pr,
                base_branch=args.base_branch,
                base_sha=args.base_sha,
                merge_mode=args.merge_mode,
                cloud_review=not args.no_cloud_review,
                deep_review=not args.no_deep_review,
                models=models,
            )
            _emit(res)
        elif cmd == "complete-grill":
            shards = _parse_shard_specs(args.shard)
            shard_checks = _parse_shard_checks(args.shard_check) if args.shard_check else None
            models = json.loads(args.models_json) if args.models_json is not None else None
            res = complete_grill(
                repo,
                args.run_id,
                summary=args.summary,
                shards=shards,
                allowed_paths=args.allowed_path,
                allow_policy_paths=args.allow_policy_path,
                shard_checks=shard_checks,
                models=models,
                cloud_review=False if args.no_cloud_review else None,
                deep_review=False if args.no_deep_review else None,
            )
            _emit(res)
        elif cmd == "lock":
            _emit(lock_run(repo, args.run_id, base_sha=args.base_sha, pr_number=args.pr))
        elif cmd == "bind-pr":
            _emit(bind_pr(repo, args.run_id, pr_number=args.pr))
        elif cmd == "refresh-base":
            _emit(refresh_base(repo, args.run_id, new_base_sha=args.new_base_sha))
        elif cmd == "publish-contract":
            _emit(publish_contract(repo, args.run_id))
        elif cmd == "start-shard":
            _emit(
                start_shard(
                    repo,
                    args.run_id,
                    args.shard,
                    worktree_path=args.worktree,
                    last_green_sha=args.last_green_sha,
                )
            )
        elif cmd == "retry-shard":
            _emit(
                retry_shard(
                    repo,
                    args.run_id,
                    args.shard,
                    check_name=args.check_name,
                    error_output=args.error_output,
                    worktree=args.worktree,
                    integrated=args.integrated,
                )
            )
        elif cmd == "verify-untampered":
            target_wt = args.worktree.resolve() if args.worktree is not None else None
            if target_wt is None and args.shard:
                run_data = load_run(repo, args.run_id)
                wt_str = ((run_data.get("shards") or {}).get(args.shard) or {}).get("worktree_path")
                if wt_str:
                    target_wt = Path(wt_str).resolve()
            _emit(
                verify_untampered(
                    repo,
                    args.run_id,
                    shard_id=args.shard,
                    base_ref=args.base_ref,
                    head_ref=args.head_ref,
                    main_ref=args.main_ref,
                    git_repo=target_wt,
                )
            )
        elif cmd == "verify-pr-integrity":
            base_sha = args.base_sha
            head_sha = args.head_sha
            pr_number = args.pr_number
            pr_author = args.pr_author
            pr_body = args.pr_body
            base_branch = args.base_branch
            labels = list(args.label or [])
            repo_slug = args.repo_slug

            if args.event_path is not None:
                event = json.loads(args.event_path.read_text(encoding="utf-8"))
                pr = event.get("pull_request") or {}
                base_sha = base_sha or (pr.get("base") or {}).get("sha")
                base_branch = base_branch or (pr.get("base") or {}).get("ref")
                head_sha = head_sha or (pr.get("head") or {}).get("sha")
                pr_number = pr_number or pr.get("number")
                pr_author = pr_author or (pr.get("user") or {}).get("login")
                if pr_body is None:
                    pr_body = pr.get("body") or ""
                if not labels:
                    labels = [lb["name"] for lb in (pr.get("labels") or []) if isinstance(lb, dict) and lb.get("name")]
                repo_slug = repo_slug or (event.get("repository") or {}).get("full_name")

            if not (base_sha and head_sha and pr_number and pr_author):
                raise GuardError("verify-pr-integrity requires base_sha, head_sha, pr_number, and pr_author")

            trusted_shard_results: dict[str, str] | None = None
            if args.trusted_shard_results_b64:
                try:
                    decoded = json.loads(base64.b64decode(args.trusted_shard_results_b64).decode("utf-8"))
                except Exception as exc:
                    raise GuardError(f"invalid --trusted-shard-results-b64 payload: {exc}") from exc
                if not isinstance(decoded, dict):
                    raise GuardError("--trusted-shard-results-b64 must decode to a JSON object")
                trusted_shard_results = {str(k): str(v) for k, v in decoded.items()}

            client = None
            if not args.offline and repo_slug:
                client = GitHubAPIClient(repo_slug)

            result = verify_pr_integrity(
                repo,
                base_sha=base_sha,
                head_sha=head_sha,
                pr_number=int(pr_number),
                pr_author=str(pr_author),
                pr_labels=labels,
                main_ref=args.main_ref,
                base_branch=base_branch,
                github_client=client,
                trusted_contracts_dir=(repo / ".autoloop/trusted-contracts") if args.offline else None,
                mode=args.mode,
                candidate_checks_result=args.candidate_checks_result,
                trusted_shard_results=trusted_shard_results,
                pr_body=str(pr_body or ""),
            )
            if args.github_output is not None:
                shard_checks_list = result.get("shard_checks") or []
                manifest_bytes = json.dumps(
                    shard_checks_list, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                shard_b64 = base64.b64encode(manifest_bytes).decode("ascii")
                shard_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
                matrix_entries = [
                    {
                        "shard_id": str(item.get("shard_id") or ""),
                        "shard_key": hashlib.sha256(str(item.get("shard_id") or "").encode("utf-8")).hexdigest()[:16],
                    }
                    for item in shard_checks_list
                ]
                matrix_json = json.dumps(matrix_entries, sort_keys=True, separators=(",", ":"))
                with args.github_output.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(f"enrolled={'true' if result['enrolled'] else 'false'}\n")
                    handle.write("bootstrap=false\n")
                    handle.write(f"run_id={result.get('run_id') or ''}\n")
                    handle.write(f"code_tree_sha256={result.get('code_tree_sha256') or ''}\n")
                    handle.write(f"shard_checks_b64={shard_b64}\n")
                    handle.write(f"shard_checks_sha256={shard_sha256}\n")
                    handle.write(f"shard_matrix_json={matrix_json}\n")
            _emit(result)
        elif cmd == "lease":
            if args.action == "acquire":
                _emit(
                    acquire_lease(
                        args.resource,
                        args.owner,
                        ttl_s=args.ttl,
                        holder_pid=args.holder_pid,
                        token=args.token,
                    )
                )
            elif args.action == "renew":
                if not args.token:
                    raise LeaseError("lease renew requires --token")
                _emit(renew_lease(args.resource, args.owner, ttl_s=args.ttl, token=args.token))
            elif args.action == "release":
                if not args.token:
                    raise LeaseError("lease release requires --token")
                _emit({"released": release_lease(args.resource, args.owner, token=args.token)})
        elif cmd == "lint":
            violations = lint_repo(repo, tree_ish=args.tree)
            if violations:
                for v in violations:
                    print(f"{v.rule_id}: {v.path}:{v.line}: {v.message}", file=sys.stderr)
                return 3
            _emit({"ok": True, "violations": 0})
        elif cmd == "check":
            if bool(args.shard) == bool(args.all_shards):
                raise IllegalTransitionError("check requires exactly one of --shard <id> or --all-shards")
            run_data = load_run(repo, args.run_id)
            run_shards = run_data.get("shards") or {}
            if args.worktree is not None:
                target_wt = args.worktree.resolve()
            elif args.shard and (run_shards.get(args.shard) or {}).get("worktree_path"):
                target_wt = Path(run_shards[args.shard]["worktree_path"]).resolve()
            else:
                target_wt = repo
            if not target_wt.is_dir():
                raise GuardError(f"target worktree {target_wt} does not exist")

            head_actual = git_rev_parse(target_wt, "HEAD")
            head_requested = git_rev_parse(target_wt, args.head_ref)
            if head_actual != head_requested:
                raise GuardError(
                    f"refusing check: target checkout HEAD ({head_actual}) != --head-ref ({head_requested})"
                )
            assert_clean_worktree_for_check(target_wt, run_id=args.run_id)

            untampered = verify_untampered(
                repo,
                args.run_id,
                shard_id=None if args.all_shards else args.shard,
                base_ref=args.base_ref,
                head_ref=head_requested,
                git_repo=target_wt,
            )
            verified_head = untampered["head_sha"]

            if args.all_shards and len(run_shards) > 1:
                for sid, sdata in sorted(run_shards.items()):
                    if sdata.get("status") not in ("in_progress", "retrying", "passed"):
                        raise IllegalTransitionError(
                            f"check --all-shards requires shard {sid!r} to be started first "
                            f"(current status={sdata.get('status')!r})"
                        )
                    if not sdata.get("has_passed_individually"):
                        raise IllegalTransitionError(
                            f"check --all-shards requires shard {sid!r} to pass individually first "
                            f"(current status={sdata.get('status')!r})"
                        )
                    lg = sdata.get("last_green_sha")
                    if lg and not git_is_ancestor(target_wt, lg, verified_head):
                        raise GuardError(
                            f"shard {sid!r} last_green_sha {lg[:8]} is not an ancestor of integration HEAD {verified_head[:8]}"
                        )

            if not args.skip_fast_check:
                fast_check_path = target_wt / "tools/fast_check.py"
                if fast_check_path.is_file():
                    proc = subprocess.run(
                        [sys.executable, "-B", str(fast_check_path)],
                        cwd=target_wt,
                    )
                    if proc.returncode != 0:
                        raise GuardError(f"fast_check.py failed with exit {proc.returncode}")

            # Always execute authenticated FrozenContractV1 shard checks before advancing pass/rollback state.
            frozen_shards = untampered.get("contract_shards") or {}
            check_shards = sorted(frozen_shards) if args.all_shards else [args.shard]
            for sid in check_shards:
                for cmd_str in (frozen_shards.get(sid) or {}).get("checks") or []:
                    proc = subprocess.run(cmd_str, shell=True, cwd=target_wt)
                    if proc.returncode != 0:
                        raise GuardError(f"shard {sid!r} check {cmd_str!r} failed with exit {proc.returncode}")

            if args.all_shards:
                state = record_all_shards_pass(
                    repo,
                    args.run_id,
                    code_tree_sha256=untampered["code_tree_sha256"],
                    head_sha=verified_head,
                    worktree_path=str(target_wt),
                    branch_name=git_current_branch(target_wt),
                )
            else:
                state = record_shard_pass(
                    repo,
                    args.run_id,
                    args.shard,
                    code_tree_sha256=untampered["code_tree_sha256"],
                    head_sha=verified_head,
                )
            _emit(
                {
                    "ok": True,
                    "status": state["status"],
                    "code_tree_sha256": untampered["code_tree_sha256"],
                    "head_sha": verified_head,
                    "worktree": str(target_wt),
                }
            )
        elif cmd == "log-dead-end":
            _emit(
                log_dead_end(
                    repo,
                    args.run_id,
                    args.shard,
                    signature=args.signature,
                    hypothesis=args.hypothesis,
                    outcome=args.outcome,
                )
            )
        elif cmd == "request-contract-change":
            _emit(
                request_contract_change(
                    repo,
                    args.run_id,
                    args.shard,
                    reason=args.reason,
                    proposed_paths=args.proposed_path,
                )
            )
        elif cmd == "apply-replan":
            _emit(apply_replan(repo, args.run_id, plan_summary=args.plan_summary, shard_id=args.shard))
        elif cmd == "resolve-contract-change":
            add_paths = _parse_shard_specs(args.add_shard_paths) if args.add_shard_paths else None
            shard_checks = _parse_shard_checks(args.shard_check) if args.shard_check else None
            _emit(
                resolve_contract_change(
                    repo,
                    args.run_id,
                    decision=args.decision,
                    add_paths=add_paths,
                    allow_policy_paths=args.allow_policy_path or None,
                    shard_checks=shard_checks,
                    new_base_sha=args.new_base_sha,
                )
            )
        elif cmd == "human-resolve":
            _emit(
                human_resolve(
                    repo,
                    args.run_id,
                    action=args.action,
                    note=args.note,
                    shard_id=args.shard,
                )
            )
        elif cmd == "submit-for-review":
            _emit(submit_for_review(repo, args.run_id))
        elif cmd == "resolve-finding":
            _emit(resolve_finding(repo, args.run_id, args.finding_id, fix_summary=args.fix_summary))
        elif cmd == "record-review":
            code_hash = args.code_tree_sha256 or compute_code_tree_sha256(repo, "HEAD", run_id=args.run_id)
            findings = json.loads(args.findings_json) if args.findings_json else None
            _emit(
                record_review(
                    repo,
                    args.run_id,
                    stage=args.stage,
                    verdict=args.verdict,
                    reviewer=args.reviewer,
                    code_tree_sha256=code_hash,
                    findings=findings,
                )
            )
        elif cmd == "record-safeguard":
            _emit(
                record_safeguard(
                    repo,
                    args.run_id,
                    args.finding_id,
                    layer=args.layer,
                    artifact=args.artifact,
                    summary=args.summary,
                    why_not_static=args.why_not_static,
                )
            )
        elif cmd == "resume":
            _emit(resume_run(repo, args.run_id))
        elif cmd == "sync-skill":
            install = bool(args.install) and not bool(args.dry_run)
            _emit(cmd_sync_skill(repo, install=install))
        elif cmd == "bootstrap-repo":
            _emit(cmd_bootstrap_repo(repo, repo_slug=args.repo_slug, dry_run=args.dry_run))
        else:
            parser.error(f"unknown command: {cmd}")
        return EXIT_OK
    except (IllegalTransitionError, GuardError, LeaseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return int(getattr(exc, "exit_code", EXIT_ERROR))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
