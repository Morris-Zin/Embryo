"""Full-tree static rule loader and evaluator (pure Python stdlib, zero candidate execution)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.util
from pathlib import Path
import subprocess
from typing import Any, Callable


@dataclass(frozen=True)
class RuleViolation:
    rule_id: str
    path: str
    line: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RuleFn = Callable[[dict[str, bytes]], list[RuleViolation] | list[dict[str, Any]]]


def load_rules_from_dir(rules_dir: Path) -> list[tuple[str, RuleFn]]:
    """Load all static rule callables from trusted `.autoloop/rules/*.py`."""
    if not rules_dir.is_dir():
        return []
    loaded: list[tuple[str, RuleFn]] = []
    for rule_file in sorted(rules_dir.glob("*.py")):
        if rule_file.name.startswith("_"):
            continue
        module_name = f"_autoloop_trusted_rule_{rule_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, rule_file)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"failed to load rule module {rule_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rules = getattr(module, "RULES", None)
        if not isinstance(rules, (list, tuple)):
            raise RuntimeError(f"rule module {rule_file.name} must define RULES list/tuple")
        for entry in rules:
            if isinstance(entry, tuple) and len(entry) == 2:
                rule_id, fn = entry
            else:
                fn = entry
                rule_id = getattr(fn, "rule_id", getattr(fn, "__name__", "unknown-rule"))
            loaded.append((str(rule_id), fn))
    return loaded


def evaluate_file_map(rules_dir: Path, file_map: dict[str, bytes]) -> list[RuleViolation]:
    """Evaluate all registered rules against a full `dict[posix_path, bytes]` file map."""
    violations: list[RuleViolation] = []
    for rule_id, fn in load_rules_from_dir(rules_dir):
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


def load_working_tree_file_map(repo: Path, max_blob_bytes: int = 2_000_000) -> dict[str, bytes]:
    """Build a full repository file map from git tracked + untracked non-ignored files."""
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
                continue
            file_map[posix_rel] = full_path.read_bytes()
        except OSError:
            continue
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
