"""Full-tree static rule loader and evaluator (pure Python stdlib, zero candidate execution)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
import importlib.util
from pathlib import Path
import re
import subprocess
from typing import Any


@dataclass(frozen=True)
class RuleViolation:
    rule_id: str
    path: str
    line: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RuleFn = Callable[[dict[str, bytes]], list[RuleViolation] | list[dict[str, Any]]]
_RULE_FILE = re.compile(r"\.autoloop/rules/[A-Za-z0-9][A-Za-z0-9_-]*\.py")
_RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _load_rule_file(rule_file: Path, source: bytes) -> list[tuple[str, RuleFn]]:
    module_name = f"_autoloop_trusted_rule_{rule_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, rule_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load rule module {rule_file}")
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, str(rule_file), "exec"), module.__dict__)
    rules = getattr(module, "RULES", None)
    if not isinstance(rules, (list, tuple)):
        raise RuntimeError(f"rule module {rule_file.name} must define RULES list/tuple")
    loaded = []
    for entry in rules:
        if isinstance(entry, tuple) and len(entry) == 2:
            rule_id, fn = entry
        else:
            fn = entry
            rule_id = getattr(fn, "rule_id", getattr(fn, "__name__", "unknown-rule"))
        loaded.append((str(rule_id), fn))
    return loaded


def load_rules_from_dir(rules_dir: Path) -> list[tuple[str, RuleFn]]:
    """Load all static rule callables from trusted `.autoloop/rules/*.py`."""
    if not rules_dir.is_dir():
        return []
    loaded: list[tuple[str, RuleFn]] = []
    for rule_file in sorted(rules_dir.glob("*.py")):
        if rule_file.name.startswith("_"):
            continue
        loaded.extend(_load_rule_file(rule_file, rule_file.read_bytes()))
    return loaded


def evaluate_file_map(rules_dir: Path, file_map: dict[str, bytes]) -> list[RuleViolation]:
    """Evaluate all registered rules against a full `dict[posix_path, bytes]` file map."""
    return _evaluate_rules(load_rules_from_dir(rules_dir), file_map)


def _evaluate_rules(rules: list[tuple[str, RuleFn]], file_map: dict[str, bytes]) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for rule_id, fn in rules:
        results = fn(file_map) or []
        for item in results:
            if isinstance(item, RuleViolation):
                violations.append(item)
            elif isinstance(item, dict):
                violations.append(
                    RuleViolation(
                        rule_id=str(item.get("rule_id") or rule_id),
                        path=str(item.get("path") or ""),
                        line=int(item.get("line") or 0),
                        message=str(item.get("message") or ""),
                    )
                )
            else:
                raise TypeError(f"rule {rule_id} returned unsupported violation item: {type(item)!r}")
    violations.sort(key=lambda v: (v.rule_id, v.path, v.line, v.message))
    return violations


def rule_fixture_proof(file_map: dict[str, bytes], artifact: str, rule_id: str) -> dict[str, Any]:
    """Describe the exact committed rule and mirrored fixture inputs."""
    from .guard import GuardError

    if not _RULE_FILE.fullmatch(artifact) or artifact not in file_map:
        raise GuardError(f"static safeguard needs a committed rule module under .autoloop/rules/: {artifact!r}")
    if not _RULE_ID.fullmatch(rule_id) or ".." in rule_id:
        raise GuardError(f"invalid static safeguard rule ID {rule_id!r}")
    prefix = f".autoloop/rules/fixtures/{rule_id}/"
    fixtures = {path: raw for path, raw in sorted(file_map.items()) if path.startswith(prefix)}
    if not fixtures:
        raise GuardError(f"missing bad-example fixture under {prefix}")
    for path in fixtures:
        target = path.removeprefix(prefix)
        if target not in file_map or target.startswith(".autoloop/"):
            raise GuardError(f"bad-example fixture {path!r} must mirror a gated source path")
    return {
        "rule_id": rule_id,
        "rule_sha256": sha256(file_map[artifact]).hexdigest(),
        "fixture_sha256": {path: sha256(raw).hexdigest() for path, raw in fixtures.items()},
    }


def verify_rule_fixture(repo: Path, tree_ish: str, artifact: str, rule_id: str) -> dict[str, Any]:
    """Require the named rule to pass clean code and flag its committed bad example."""
    from .guard import GuardError, assert_safe_local_path, git_read_tree_blobs

    file_map = git_read_tree_blobs(repo, tree_ish)
    proof = rule_fixture_proof(file_map, artifact, rule_id)
    rule_file = repo / artifact
    assert_safe_local_path(repo, rule_file, allow_missing_leaf=False)
    # Execute the coordinator's checked-out rule, only if identical to the gated source.
    if not rule_file.is_file() or (source := rule_file.read_bytes()) != file_map[artifact]:
        raise GuardError(f"check out the gated rule module {artifact!r} before recording its safeguard")
    try:
        rules = [entry for entry in _load_rule_file(rule_file, source) if entry[0] == rule_id]
        if len(rules) != 1:
            raise GuardError(f"expected exactly one registered rule {rule_id!r} in {artifact!r}")
        if _evaluate_rules(rules, file_map):
            raise GuardError(f"rule {rule_id!r} fails on the gated tree")
        prefix = f".autoloop/rules/fixtures/{rule_id}/"
        examples = {path.removeprefix(prefix): file_map[path] for path in proof["fixture_sha256"]}
        violations = _evaluate_rules(rules, {**file_map, **examples})
    except GuardError:
        raise
    except (OSError, RuntimeError, SyntaxError, TypeError, ValueError) as exc:
        raise GuardError(f"could not evaluate static safeguard {rule_id!r}: {exc}") from exc
    if not any(v.rule_id == rule_id and v.path in examples for v in violations):
        raise GuardError(f"rule {rule_id!r} does not flag its bad-example fixture")
    return proof


def load_working_tree_file_map(repo: Path, max_blob_bytes: int = 10_000_000) -> dict[str, bytes]:
    """Build a full repository file map from git tracked + untracked non-ignored files."""
    from .guard import GuardError

    raw = subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={repo}",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=repo,
    )
    paths = sorted(set(filter(None, raw.decode("utf-8", errors="surrogateescape").split("\0"))))
    file_map: dict[str, bytes] = {}
    for rel in paths:
        posix_rel = rel.replace("\\", "/")
        full_path = repo / posix_rel
        try:
            if full_path.is_symlink() or not full_path.is_file():
                continue
            stat = full_path.stat()
            if stat.st_size > max_blob_bytes:
                raise GuardError(
                    f"file {posix_rel!r} ({stat.st_size} bytes) exceeds max_blob_bytes ({max_blob_bytes}); cannot skip static lint"
                )
            file_map[posix_rel] = full_path.read_bytes()
        except GuardError:
            raise
        except OSError as exc:
            raise GuardError(f"failed to read {posix_rel!r} for static lint: {exc}") from exc
    return file_map


def lint_repo(
    repo: Path,
    *,
    rules_dir: Path | None = None,
    tree_ish: str | None = None,
) -> list[RuleViolation]:
    """Run all static rules over either a Git tree-ish (pure blobs) or the local working tree."""
    effective_rules_dir = rules_dir or (repo / ".autoloop/rules")
    if tree_ish is not None:
        from .guard import git_read_tree_blobs

        file_map = git_read_tree_blobs(repo, tree_ish)
    else:
        file_map = load_working_tree_file_map(repo)
    return evaluate_file_map(effective_rules_dir, file_map)
