"""Materialize the coordinator's trusted gate sources from immutable Git blobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .guard import (
    EXTRA_SKILL_POLICY_GLOBS,
    GuardError,
    assert_safe_local_path,
    check_tree_modes,
    contract_sha256,
    detect_case_collisions,
    git_diff_tree_entries,
    git_is_ancestor,
    git_ls_tree,
    git_read_tree_blobs,
    git_rev_parse,
    matches_any_glob,
)
from .lessons import globs_overlap

TOOLING_PATHS = ("tools", ".autoloop/config.json", ".autoloop/rules")


def materialize_tooling(
    repo: Path, destination: Path, state: dict[str, Any], contract: dict[str, Any],
    *, checking_shard: str | None,
) -> dict[str, Any]:
    if not state.get("base_sha"):
        raise GuardError("check requires a recorded run base SHA for trusted tooling")
    base = git_rev_parse(repo, state["base_sha"])
    if base != state["base_sha"]:
        raise GuardError("trusted tooling requires a full base commit SHA; use refresh-base")
    entries = {entry.path: entry for entry in git_ls_tree(repo, base, TOOLING_PATHS)}
    config_path = ".autoloop/config.json"
    blobs = git_read_tree_blobs(repo, base, tree_entries=list(entries.values()))
    config_blob = blobs.get(config_path)
    if config_blob is None:
        raise GuardError(f"run base {base} is missing trusted {config_path}")
    policy = [*(json.loads(config_blob).get("critical_policy_globs") or []), *EXTRA_SKILL_POLICY_GLOBS]
    approved = contract.get("allow_policy_paths") or []
    policy_shas = {}
    overlays: dict[str, str] = {}
    updated_blobs = []
    for shard_id, shard in sorted(state["shards"].items()):
        if shard_id == checking_shard or shard["status"] != "passed" or not shard.get("has_passed_individually"):
            continue
        sha = shard.get("trusted_policy_pass_sha")
        if not sha:
            continue
        paths = contract["shards"][shard_id]["allowed_paths"]
        if not any(globs_overlap(path, protected) for path in paths for protected in policy):
            continue
        if not git_is_ancestor(repo, base, sha):
            raise GuardError(f"passed policy shard {shard_id!r} does not descend from the run base")
        changed = [
            item for item in git_diff_tree_entries(repo, base, sha)
            if any(item.path == prefix or item.path.startswith(prefix + "/") for prefix in TOOLING_PATHS)
            and matches_any_glob(item.path, paths) and matches_any_glob(item.path, policy)
            and (approved is True or matches_any_glob(item.path, approved))
        ]
        if not changed:
            continue
        errors = check_tree_modes(changed)
        if errors:
            raise GuardError("invalid trusted policy overlay: " + ", ".join(errors[:5]))
        tree = {entry.path: entry for entry in git_ls_tree(repo, sha, TOOLING_PATHS)}
        for item in changed:
            if item.path in overlays:
                raise GuardError(f"passed policy shards overlap at trusted tooling path {item.path!r}")
            overlays[item.path] = shard_id
            if item.path in tree:
                entry = tree[item.path]
                entries[item.path] = entry
                updated_blobs.append(entry)
            else:
                entries.pop(item.path, None)
                blobs.pop(item.path, None)
        policy_shas[shard_id] = sha
    errors = check_tree_modes(entries.values()) + detect_case_collisions(entries)
    if errors:
        raise GuardError("invalid trusted tooling tree: " + ", ".join(errors[:5]))
    if not {config_path, "tools/autoloop/guard.py", "tools/autoloop/static_rules.py"} <= entries.keys():
        raise GuardError("trusted tooling is missing its config, guard, or static lint")
    if "tools/fast_check.py" in overlays and "tools/fast_check.py" not in entries:
        raise GuardError("passed policy shard removed the trusted fast_check.py")
    blobs.update(git_read_tree_blobs(repo, base, tree_entries=updated_blobs))
    for relative, content in blobs.items():
        path = assert_safe_local_path(destination, destination / relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {
        "sha256": contract_sha256({path: entry.sha for path, entry in entries.items()}),
        "base_sha": base, "policy_shas": policy_shas,
    }
