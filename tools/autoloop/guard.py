"""Git boundary guards, path safety, FrozenContractV1 verification, and CI integrity gate."""

from __future__ import annotations

import ctypes
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints
import urllib.error
import urllib.parse
import urllib.request

from . import EXIT_POLICY_OR_INTEGRITY, EXIT_UNSAFE_ROLLBACK
from .static_rules import evaluate_file_map

FORBIDDEN_GIT_MODES = {
    "120000": "symlink",
    "160000": "gitlink",
}

GOVERNED_LF_GLOBS = (
    "tools/autoloop/**",
    ".autoloop/**",
    ".devin/skills/autoloop/**",
    ".claude/skills/autoloop/**",
    ".agents/skills/autoloop/**",
    ".github/workflows/**",
    ".github/CODEOWNERS",
    "AGENTS.md",
    "tools/**/*.sh",
)
EXTRA_SKILL_POLICY_GLOBS = (".claude/skills/autoloop/**", ".agents/skills/autoloop/**")

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

EXPERIMENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUN_FILE_RE = re.compile(r"^\.autoloop/runs/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$")

CONTRACT_MARKER_RE = re.compile(
    r"<!--\s*autoloop-contract:v1\s+rev=(\d+)\s+sha256=([0-9a-f]{64})\s*-->"
)
CONTRACT_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
RULE_EDIT_APPROVAL_RE = re.compile(
    r"<!--\s*autoloop-rule-edit-approval:v1\s+pr=(\d+)\s+head_sha=([0-9a-f]{40})\s*-->"
)
AUTO_MERGE_APPROVAL_RE = re.compile(
    r"<!--\s*autoloop-auto-merge-approval:v1\s+pr=(\d+)\s+head_sha=([0-9a-f]{40})\s*-->"
)
REVIEW_APPROVAL_RE = re.compile(
    r"<!--\s*autoloop-review:v1\s+pr=(\d+)\s+rev=(\d+)\s+base_sha=([0-9a-f]{40})\s+code_tree_sha256=([0-9a-f]{64})\s*-->"
)
FINDINGS_LIST_RE = re.compile(
    r"<!--\s*autoloop-findings:v1\s+pr=(\d+)\s+ids=([A-Za-z0-9_.,-]+)\s*-->"
)


class GuardError(RuntimeError):
    exit_code = EXIT_POLICY_OR_INTEGRITY


class CheckCommandError(GuardError):
    def __init__(self, check_name: str, message: str, output: str):
        super().__init__(message)
        self.check_name = check_name
        self.output = f"{output}\n{message}"


class UnsafeRollbackError(GuardError):
    exit_code = EXIT_UNSAFE_ROLLBACK


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    obj_type: str
    sha: str
    path: str
    size: int | None = None


@dataclass(frozen=True)
class DiffEntry:
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    status: str
    path: str


def _normalize_posix_path(path: str) -> str:
    cleaned = path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _match_segment(segment: str, pattern: str) -> bool:
    """Case-sensitive single path-segment glob matcher (* and ? never cross /)."""
    memo: dict[tuple[int, int], bool] = {}

    def _dp(si: int, pi: int) -> bool:
        key = (si, pi)
        if key in memo:
            return memo[key]
        if pi == len(pattern):
            res = si == len(segment)
        elif pattern[pi] == "*":
            res = _dp(si, pi + 1) or (si < len(segment) and _dp(si + 1, pi))
        elif pattern[pi] == "?":
            res = si < len(segment) and _dp(si + 1, pi + 1)
        elif pattern[pi] == "[":
            end = pattern.find("]", pi + 1)
            if end == -1:
                res = si < len(segment) and segment[si] == "[" and _dp(si + 1, pi + 1)
            else:
                if si >= len(segment):
                    res = False
                else:
                    body = pattern[pi + 1 : end]
                    negate = body.startswith(("!", "^"))
                    if negate:
                        body = body[1:]
                    matched = False
                    ch = segment[si]
                    idx = 0
                    while idx < len(body):
                        if idx + 2 < len(body) and body[idx + 1] == "-":
                            if body[idx] <= ch <= body[idx + 2]:
                                matched = True
                            idx += 3
                        else:
                            if ch == body[idx]:
                                matched = True
                            idx += 1
                    if negate:
                        matched = not matched
                    res = matched and _dp(si + 1, end + 1)
        else:
            res = si < len(segment) and segment[si] == pattern[pi] and _dp(si + 1, pi + 1)
        memo[key] = res
        return res

    return _dp(0, 0)


def glob_match(path: str, pattern: str) -> bool:
    """Case-sensitive `/` segment glob matcher. NEVER uses fnmatch.

    - `*` and `?` never cross `/`.
    - `**` matches 0 or more complete path segments.
    - Exact case comparison on both Windows and Linux.
    """
    norm_path = _normalize_posix_path(path)
    norm_pat = _normalize_posix_path(pattern)
    if not norm_path or not norm_pat:
        return False

    path_parts = norm_path.split("/")
    pat_parts = norm_pat.split("/")
    memo: dict[tuple[int, int], bool] = {}

    def _dp(si: int, pi: int) -> bool:
        key = (si, pi)
        if key in memo:
            return memo[key]
        if pi == len(pat_parts):
            res = si == len(path_parts)
        elif pat_parts[pi] == "**":
            # Match 0 segments OR consume 1 segment and stay on **.
            res = _dp(si, pi + 1) or (si < len(path_parts) and _dp(si + 1, pi))
        elif si == len(path_parts):
            res = False
        else:
            res = _match_segment(path_parts[si], pat_parts[pi]) and _dp(si + 1, pi + 1)
        memo[key] = res
        return res

    return _dp(0, 0)


def matches_any_glob(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_match(path, pat) for pat in patterns)


def detect_case_collisions(paths: Iterable[str]) -> list[str]:
    """Detect case-insensitive file collisions AND directory-prefix collisions."""
    violations: list[str] = []
    seen_files: dict[str, str] = {}
    seen_dirs: dict[str, str] = {}
    recorded: set[tuple[str, str]] = set()

    for raw in sorted(set(paths)):
        norm = _normalize_posix_path(raw)
        if not norm:
            continue
        folded_file = norm.casefold()

        # File vs file collision
        prev_file = seen_files.get(folded_file)
        if prev_file is not None and prev_file != norm:
            pair = tuple(sorted((prev_file, norm)))
            if pair not in recorded:
                recorded.add(pair)
                violations.append(f"case-insensitive file collision: {prev_file!r} vs {norm!r}")
        else:
            seen_files[folded_file] = norm

        # File vs existing directory prefix collision
        prev_dir_for_file = seen_dirs.get(folded_file)
        if prev_dir_for_file is not None:
            pair = tuple(sorted((prev_dir_for_file, norm)))
            if pair not in recorded:
                recorded.add(pair)
                violations.append(f"case-insensitive file/directory collision: {norm!r} vs directory {prev_dir_for_file!r}")

        # Walk every ancestor directory prefix
        parts = norm.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            folded_prefix = prefix.casefold()
            prev_dir = seen_dirs.get(folded_prefix)
            if prev_dir is not None and prev_dir != prefix:
                pair = tuple(sorted((prev_dir, prefix)))
                if pair not in recorded:
                    recorded.add(pair)
                    violations.append(
                        f"case-insensitive directory prefix collision: {prev_dir!r} vs {prefix!r}"
                    )
            else:
                seen_dirs[folded_prefix] = prefix

            prev_file_for_dir = seen_files.get(folded_prefix)
            if prev_file_for_dir is not None:
                pair = tuple(sorted((prev_file_for_dir, prefix)))
                if pair not in recorded:
                    recorded.add(pair)
                    violations.append(
                        f"case-insensitive file/directory collision: {prev_file_for_dir!r} vs directory {prefix!r}"
                    )

    return violations


def check_tree_modes(entries: Iterable[TreeEntry | DiffEntry]) -> list[str]:
    violations: list[str] = []
    for entry in entries:
        modes: list[str] = []
        if isinstance(entry, TreeEntry):
            modes = [entry.mode]
        else:
            if entry.status != "D":
                modes = [entry.new_mode]
        for mode in modes:
            if mode in FORBIDDEN_GIT_MODES:
                violations.append(
                    f"forbidden git mode {mode} ({FORBIDDEN_GIT_MODES[mode]}) at {entry.path!r}"
                )
    return violations


def is_windows_reparse_point(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_attrs = kernel32.GetFileAttributesW
        get_attrs.argtypes = [wintypes.LPCWSTR]
        get_attrs.restype = wintypes.DWORD
        attrs = int(get_attrs(str(path)))
    except Exception:
        return False
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def assert_safe_local_path(root: Path, target: Path, *, allow_missing_leaf: bool = True) -> Path:
    """Verify `target` stays contained within `root` and crosses no symlink or Windows reparse point."""
    root_abs = Path(os.path.abspath(str(root)))
    if not target.is_absolute():
        candidate = root_abs / target
    else:
        candidate = target

    # Reject explicit '..' traversal components before resolving.
    if ".." in candidate.parts:
        raise GuardError(f"path traversal component '..' is forbidden: {target}")

    cand_abs = Path(os.path.abspath(str(candidate)))
    try:
        rel = cand_abs.relative_to(root_abs)
    except ValueError as exc:
        raise GuardError(f"path {target} escapes containment root {root_abs}") from exc

    # Check root itself and every intermediate segment down to target.
    current = root_abs
    if current.exists() and (current.is_symlink() or is_windows_reparse_point(current)):
        raise GuardError(f"containment root {current} is a symlink or reparse point")

    for part in rel.parts:
        current = current / part
        if current.is_symlink() or is_windows_reparse_point(current):
            raise GuardError(f"symlink or Windows reparse point forbidden at {current}")
        if not current.exists() and not allow_missing_leaf:
            raise GuardError(f"required path component does not exist: {current}")

    return cand_abs


def check_governed_lf_and_bom(file_map: dict[str, bytes]) -> list[str]:
    violations: list[str] = []
    for path, content in sorted(file_map.items()):
        if not matches_any_glob(path, GOVERNED_LF_GLOBS):
            continue
        if content.startswith(b"\xef\xbb\xbf"):
            violations.append(f"{path}: UTF-8 BOM is forbidden on governed text files")
        if b"\r" in content:
            violations.append(f"{path}: CR/CRLF line endings are forbidden (LF required)")
    return violations


def is_valid_experiment_path(path: str, run_id: str) -> bool:
    norm = _normalize_posix_path(path)
    prefix = f".autoloop/experiments/{run_id}-"
    if not norm.startswith(prefix) or not norm.endswith(".md"):
        return False
    slug = norm[len(prefix) : -3]
    return bool(EXPERIMENT_SLUG_RE.match(slug))


_LESSON_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_lesson_path(path: str, run_id: str) -> bool:
    """Lessons written by ``record-lesson`` for this run: ``.autoloop/lessons/<run_id>-<32 hex>.json``."""
    norm = _normalize_posix_path(path)
    prefix = f".autoloop/lessons/{run_id}-"
    if not norm.startswith(prefix) or not norm.endswith(".json"):
        return False
    return bool(_LESSON_ID_RE.match(norm[len(prefix) : -5]))


def is_framework_metadata_path(path: str, run_id: str) -> bool:
    norm = _normalize_posix_path(path)
    return (
        norm == f".autoloop/runs/{run_id}.json"
        or is_valid_experiment_path(norm, run_id)
        or is_valid_lesson_path(norm, run_id)
    )


def _resolve_git_bin() -> str:
    if sys.platform == "win32":
        git_path = shutil.which("git")
        if git_path:
            mingw_git = Path(git_path).resolve().parent.parent / "mingw64/bin/git.exe"
            if mingw_git.is_file():
                return str(mingw_git)
    return "git"


_GIT_BIN = _resolve_git_bin()


def _run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    cmd = [_GIT_BIN, "-c", f"safe.directory={repo}", *args]
    proc = subprocess.run(
        cmd,
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GuardError(f"git {' '.join(args)} failed (exit {proc.returncode}): {err}")
    return proc


def git_rev_parse(repo: Path, rev: str) -> str:
    proc = _run_git(repo, ["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"])
    return proc.stdout.decode("utf-8").strip()


def git_merge_base(repo: Path, rev_a: str, rev_b: str) -> str:
    proc = _run_git(repo, ["merge-base", rev_a, rev_b], check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GuardError(
            f"merge-base failed for {rev_a} and {rev_b} (shallow or unrelated history): {err}"
        )
    oid = proc.stdout.decode("utf-8").strip()
    if not oid:
        raise GuardError(f"empty merge-base for {rev_a} and {rev_b}")
    return oid


def git_diff_tree_entries(repo: Path, base_rev: str, head_rev: str) -> list[DiffEntry]:
    """Run `git diff-tree -r --no-renames -z` (Git 2.50+ safe, NO `--no-find-copies`)."""
    proc = _run_git(repo, ["diff-tree", "-r", "--no-renames", "-z", base_rev, head_rev])
    tokens = proc.stdout.split(b"\0")
    entries: list[DiffEntry] = []
    idx = 0
    while idx < len(tokens):
        header = tokens[idx]
        idx += 1
        if not header:
            continue
        text = header.decode("utf-8", errors="surrogateescape")
        if not text.startswith(":"):
            continue
        meta = text[1:].split(" ")
        if len(meta) < 5 or idx >= len(tokens):
            raise GuardError(f"malformed git diff-tree record: {text!r}")
        path = tokens[idx].decode("utf-8", errors="surrogateescape")
        idx += 1
        entries.append(
            DiffEntry(
                old_mode=meta[0],
                new_mode=meta[1],
                old_sha=meta[2],
                new_sha=meta[3],
                status=meta[4],
                path=_normalize_posix_path(path),
            )
        )
    return entries


def git_ls_tree(repo: Path, tree_ish: str, paths: Iterable[str] = ()) -> list[TreeEntry]:
    proc = _run_git(repo, ["ls-tree", "-r", "-l", "-z", tree_ish, "--", *paths])
    entries: list[TreeEntry] = []
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        tab = record.find(b"\t")
        if tab < 0:
            continue
        header = record[:tab].decode("utf-8")
        path = record[tab + 1 :].decode("utf-8", errors="surrogateescape")
        parts = header.split()
        if len(parts) < 3:
            continue
        mode, obj_type, sha = parts[0], parts[1], parts[2]
        size: int | None = None
        if len(parts) >= 4 and parts[3].isdigit():
            size = int(parts[3])
        entries.append(TreeEntry(mode=mode, obj_type=obj_type, sha=sha, path=_normalize_posix_path(path), size=size))
    return entries


def git_merge_tree(repo: Path, base_sha: str, head_sha: str) -> str:
    """Run `git merge-tree --write-tree` and require exit 0 (clean merge)."""
    proc = _run_git(repo, ["merge-tree", "--write-tree", base_sha, head_sha], check=False)
    if proc.returncode != 0:
        detail = proc.stdout.decode("utf-8", errors="replace").strip() or proc.stderr.decode("utf-8", errors="replace").strip()
        raise GuardError(
            f"git merge-tree --write-tree failed (exit {proc.returncode}, merge conflict or invalid tree): {detail[:400]}"
        )
    first_line = proc.stdout.decode("utf-8", errors="replace").splitlines()[0].strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", first_line):
        raise GuardError(f"unexpected merge-tree OID: {first_line!r}")
    return first_line


def git_read_tree_blobs(
    repo: Path,
    tree_ish: str,
    max_blob_bytes: int = 10_000_000,
    *,
    tree_entries: list[TreeEntry] | None = None,
) -> dict[str, bytes]:
    """Read all blobs from a Git tree OID via `git cat-file --batch`, failing closed on oversized blobs."""
    source_entries = tree_entries if tree_entries is not None else git_ls_tree(repo, tree_ish)
    entries = [e for e in source_entries if e.obj_type == "blob"]
    if not entries:
        return {}
    # Pre-check blob sizes from `ls-tree -l` before loading payloads into memory so oversized blobs fail closed.
    for entry in entries:
        if entry.size is not None and entry.size > max_blob_bytes:
            raise GuardError(
                f"blob {entry.path!r} ({entry.size} bytes) exceeds max_blob_bytes ({max_blob_bytes}); cannot skip policy evaluation"
            )
    batch_input = "".join(f"{e.sha}\n" for e in entries).encode("utf-8")

    proc = subprocess.Popen(
        [_GIT_BIN, "-c", f"safe.directory={repo}", "cat-file", "--batch"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stdout_bytes, stderr_bytes = proc.communicate(batch_input)
    if proc.returncode != 0:
        raise GuardError(f"git cat-file --batch failed: {stderr_bytes.decode('utf-8', errors='replace')}")

    file_map: dict[str, bytes] = {}
    offset = 0
    total = len(stdout_bytes)
    for entry in entries:
        nl = stdout_bytes.find(b"\n", offset)
        if nl < 0:
            raise GuardError("truncated git cat-file --batch header")
        header = stdout_bytes[offset:nl].decode("utf-8", errors="replace")
        offset = nl + 1
        parts = header.split()
        if len(parts) < 3 or parts[1] == "missing":
            continue
        size = int(parts[2])
        if size > max_blob_bytes:
            raise GuardError(
                f"blob {entry.path!r} ({size} bytes) exceeds max_blob_bytes ({max_blob_bytes}); cannot skip policy evaluation"
            )
        if offset + size > total:
            raise GuardError("truncated git cat-file --batch payload")
        payload = stdout_bytes[offset : offset + size]
        offset += size + 1  # trailing LF after each object
        file_map[entry.path] = payload
    return file_map


def git_read_blob(repo: Path, tree_ish: str, path: str) -> bytes | None:
    proc = _run_git(repo, ["cat-file", "blob", f"{tree_ish}:{_normalize_posix_path(path)}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def advise_test_integrity(repo: Path, base: str, head: str, *, now: str) -> None:
    """Report protected test changes from committed trees without affecting the legacy gate."""
    from .proofs import ProtectedKind, evaluate_integrity, trusted_manifest_for_files

    try:
        kinds = {}
        for change in git_diff_tree_entries(repo, base, head):
            path = Path(change.path)
            if path.suffix not in (".py", ".js", ".mjs", ".ts", ".jsx", ".tsx"):
                continue
            if path.name in {"fast_check.py", "perf_parity.py", "perf_eval_ledger.py"}:
                kinds[change.path] = ProtectedKind.EVALUATOR
            elif "tests" in path.parts or path.name.startswith("test_") or ".test." in path.name or ".spec." in path.name:
                kinds[change.path] = ProtectedKind.TEST
        if not kinds:
            return
        base_files = git_read_tree_blobs(repo, base, tree_entries=[
            entry for entry in git_ls_tree(repo, base) if entry.path in kinds
        ])
        candidate = git_read_tree_blobs(repo, head, tree_entries=[
            entry for entry in git_ls_tree(repo, head) if entry.path in kinds
        ])
        report = evaluate_integrity(base_files, candidate, trusted_manifest_for_files(base_files, kinds), now=now)
        for violation in report.violations:
            print(f"SHADOW-INTEGRITY {violation.rule_id} {violation.path}: {violation.message}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"SHADOW-INTEGRITY unavailable: {exc}", file=sys.stderr)


def git_is_ancestor(repo: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    proc = _run_git(repo, ["merge-base", "--is-ancestor", ancestor_sha, descendant_sha], check=False)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    raise GuardError(f"git merge-base --is-ancestor failed: {err}")


def compute_code_tree_sha256(
    repo: Path,
    tree_ish: str,
    *,
    run_id: str | None = None,
    tree_entries: list[TreeEntry] | None = None,
) -> str:
    """Hash sorted NUL-delimited `git ls-tree -r -z` records excluding only run checkpoint & valid experiment logs."""
    entries = list(tree_entries) if tree_entries is not None else git_ls_tree(repo, tree_ish)
    entries.sort(key=lambda e: e.path.encode("utf-8"))
    hasher = hashlib.sha256()

    for entry in entries:
        if run_id and is_framework_metadata_path(entry.path, run_id):
            continue
        record = f"{entry.mode} {entry.obj_type} {entry.sha}\t{entry.path}\0".encode("utf-8")
        hasher.update(record)
    return hasher.hexdigest()


def git_current_branch(repo: Path) -> str | None:
    """Return the checked-out short branch name, or None if HEAD is detached."""
    proc = _run_git(repo, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
    if proc.returncode != 0:
        return None
    name = proc.stdout.decode("utf-8", errors="replace").strip()
    return name or None


def _find_dirty_worktree_entries(
    worktree: Path,
    *,
    allow_paths: set[str] | None = None,
    allow_run_id: str | None = None,
) -> list[str]:
    """Parse `git status --porcelain=v1 -z` checking BOTH rename/copy endpoints."""
    proc = _run_git(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    ignored = {_normalize_posix_path(p) for p in (allow_paths or set())}
    dirty_lines: list[str] = []
    tokens = proc.stdout.decode("utf-8", errors="replace").split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        xy = entry[:2]
        path1 = entry[3:]
        endpoints = [_normalize_posix_path(path1)]
        if ("R" in xy or "C" in xy) and i < len(tokens) and tokens[i]:
            path2 = tokens[i]
            i += 1
            endpoints.append(_normalize_posix_path(path2))

        def _is_exempt(p: str) -> bool:
            if p in ignored:
                return True
            if allow_run_id and is_framework_metadata_path(p, allow_run_id):
                return True
            return False

        if all(_is_exempt(ep) for ep in endpoints):
            continue
        dirty_lines.append(f"{xy} {' -> '.join(reversed(endpoints))}" if len(endpoints) == 2 else f"{xy} {endpoints[0]}")
    return dirty_lines


def assert_clean_worktree_for_check(worktree: Path, *, run_id: str | None = None) -> None:
    """Refuse `check` if `worktree` has dirty or untracked non-metadata edits (ensures HEAD matches tested code)."""
    dirty_lines = _find_dirty_worktree_entries(worktree, allow_run_id=run_id)
    if dirty_lines:
        first_lines = "; ".join(dirty_lines[:5])
        raise GuardError(
            f"refusing check: worktree {worktree} has uncommitted or untracked changes ({first_lines}); "
            "commit changes before running check"
        )


def git_restore_to_commit(
    worktree: Path,
    target_sha: str,
    *,
    run_id: str,
    allowed_paths: list[str],
) -> str | None:
    """Safely restore `worktree` code state back to `target_sha` while preserving framework metadata."""
    resolved_target = git_rev_parse(worktree, target_sha)
    resolved_head = git_rev_parse(worktree, "HEAD")
    if resolved_head == resolved_target:
        return None

    diff = git_diff_tree_entries(worktree, resolved_target, resolved_head)
    mode_errors = check_tree_modes(diff)
    if mode_errors:
        raise UnsafeRollbackError("; ".join(mode_errors[:5]))

    outside: list[str] = []
    added_paths: list[str] = []
    restore_paths: list[str] = []
    for entry in diff:
        if is_framework_metadata_path(entry.path, run_id):
            continue
        if not matches_any_glob(entry.path, allowed_paths):
            outside.append(entry.path)
            continue
        if entry.status == "A":
            added_paths.append(entry.path)
        else:
            restore_paths.append(entry.path)

    if outside:
        raise UnsafeRollbackError(
            f"refusing rollback to {resolved_target[:8]}: commit range touches paths outside shard allowed_paths: "
            + ", ".join(outside[:5])
        )
    if not added_paths and not restore_paths:
        return None

    if added_paths:
        _run_git(worktree, ["rm", "-f", "--ignore-unmatch", "--", *added_paths])
    if restore_paths:
        _run_git(worktree, ["checkout", resolved_target, "--", *restore_paths])

    rollback_paths = sorted(set(added_paths) | set(restore_paths))
    has_staged = _run_git(worktree, ["diff", "--cached", "--quiet", "--", *rollback_paths], check=False).returncode != 0
    if not has_staged:
        return None

    _run_git(
        worktree,
        [
            "-c",
            "user.name=autoloop",
            "-c",
            "user.email=autoloop@local",
            "commit",
            "-m",
            f"autoloop: rollback to {resolved_target[:8]}",
            "--",
            *rollback_paths,
        ],
    )
    return git_rev_parse(worktree, "HEAD")


def assert_clean_worktree_for_rollback(worktree: Path, *, allow_paths: set[str] | None = None) -> None:
    """Refuse rollback if ANY dirty/untracked edit exists anywhere in `worktree` (exit 4)."""
    dirty_lines = _find_dirty_worktree_entries(worktree, allow_paths=allow_paths)
    if dirty_lines:
        first_lines = "; ".join(dirty_lines[:5])
        raise UnsafeRollbackError(
            f"refusing rollback: worktree {worktree} has uncommitted or untracked changes ({first_lines})"
        )


def git_paths_ever_touched_in_range(repo: Path, base_sha: str, head_sha: str, pathspec: str) -> list[str]:
    proc = _run_git(
        repo,
        ["log", "--name-only", "--format=", f"{base_sha}..{head_sha}", "--", pathspec],
        check=True,
    )
    return sorted({_normalize_posix_path(line) for line in proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()})


def git_commits_touching_paths(repo: Path, base_sha: str, head_sha: str, paths: list[str]) -> list[dict[str, str]]:
    if not paths:
        return []
    proc = _run_git(
        repo,
        ["log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00", f"{base_sha}..{head_sha}", "--", *paths],
        check=True,
    )
    commits: list[dict[str, str]] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\0")
        if len(parts) >= 5:
            commits.append(
                {
                    "sha": parts[0].strip(),
                    "author_name": parts[1].strip(),
                    "author_email": parts[2].strip(),
                    "committer_name": parts[3].strip(),
                    "committer_email": parts[4].strip(),
                }
            )
    return commits


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def contract_sha256(contract: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(contract)).hexdigest()


_VERIFICATION_NAMESPACE = "autoloop-verification-v1"
_VERIFICATION_COMMENT = re.compile(r"<!--\s*autoloop-verification:v1\s+([A-Za-z0-9+/=]+)\s*-->")


def verification_check_id(shard: str, index: int, command: str) -> str:
    return f"{shard}:{index}:{contract_sha256({'command': command})}"


def read_verification_key(path: Path) -> str:
    value = path.read_text(encoding="utf-8").split()
    if len(value) < 2 or value[0] != "ssh-ed25519":
        raise GuardError("verification signing key must be an OpenSSH Ed25519 public key")
    key = " ".join(value[:2])
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", key):
        raise GuardError("invalid verification signing key")
    return key


def _decode_verification_value(schema: Any, value: Any) -> Any:
    """Decode the signed JSON against the service's public evidence dataclasses."""
    origin, args = get_origin(schema), get_args(schema)
    if origin is UnionType and type(None) in args:
        return None if value is None else _decode_verification_value(next(t for t in args if t is not type(None)), value)
    if origin is tuple and isinstance(value, list):
        return tuple(_decode_verification_value(args[0], item) for item in value)
    if isinstance(schema, type):
        if issubclass(schema, Enum):
            return schema(value)
        if is_dataclass(schema) and isinstance(value, dict):
            hints = get_type_hints(schema)
            if value.keys() - {field.name for field in fields(schema)}:
                raise ValueError(f"unknown {schema.__name__} field")
            return schema(**{key: _decode_verification_value(hints[key], item) for key, item in value.items()})
        if type(value) is schema:
            return value
    raise ValueError(f"invalid verification evidence value for {schema}")


def _authenticated_snapshots(contract: dict[str, Any], envelope: dict[str, Any] | None) -> dict[str, Any]:
    if envelope is None:
        return {}
    from .verification import EvidenceSnapshot

    key = contract.get("verification_key") or ""
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", key):
        raise GuardError("signed evidence requires a verification_key in the frozen contract")
    snapshots = envelope["snapshots"]
    if not isinstance(snapshots, dict):
        raise ValueError("signed snapshots must be an object keyed by shard ID")
    with TemporaryDirectory(prefix="autoloop-signature-") as temp:
        signers = Path(temp) / "signers"
        signature = Path(temp) / "signature"
        signers.write_text(f"autoloop {key}\n", encoding="ascii", newline="\n")
        signature.write_text(envelope["signature"], encoding="ascii", newline="\n")
        proc = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", "autoloop",
             "-n", _VERIFICATION_NAMESPACE, "-s", str(signature)],
            input=canonical_json_bytes(snapshots), capture_output=True, timeout=10, check=False, cwd=temp,
        )
    if proc.returncode:
        raise GuardError("verification evidence signature was rejected")
    return {sid: _decode_verification_value(EvidenceSnapshot, data) for sid, data in snapshots.items()}


def verification_envelope(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("verification evidence must be a signed JSON object")
    return value


def verification_comment_envelope(comments: list[dict], trusted_owners: set[str]) -> dict[str, Any] | None:
    import base64

    for comment in reversed(comments):
        if _is_comment_owner_authentic(comment, trusted_owners):
            if match := _VERIFICATION_COMMENT.search(str(comment.get("body") or "")):
                return json.loads(base64.b64decode(match[1], validate=True))
    return None


def verify_phase2(
    repo: Path, run: dict[str, Any], contract: dict[str, Any], *, obligation: str,
    head: str, base: str, shard_id: str | None = None, repo_id: str | None = None,
    envelope: dict[str, Any] | None = None, legacy_ok: bool | None = None,
    auto_merge_approved: bool | None = None,
    checked_tooling_digest: str | None = None,
) -> dict[str, Any]:
    """Use current Git observations and authenticated evidence for local and CI shadow verification."""
    from .receipts import checkout_repo_id
    from . import verification as v

    level = v.Obligation(obligation)
    ids = [shard_id] if shard_id is not None else sorted(contract["shards"])
    identity = repo_id or checkout_repo_id(repo) or repo.resolve().as_uri()
    revision = int(contract["contract_revision"])
    digest = contract_sha256(contract)
    reviewers = contract.get("reviewers") or {}
    merge_auto = contract.get("merge_mode") == "auto" or auto_merge_approved is not None
    if level is v.Obligation.MERGE_ELIGIBILITY and merge_auto and auto_merge_approved is None and envelope is not None:
        auto_merge_approved = _observe_auto_merge_approval(repo, identity, contract, head)
    requirements = (
        (v.ApprovalRequirement(v.Obligation.MERGE_ELIGIBILITY, "auto-merge"),) if merge_auto else ()
    )
    frozen = v.FrozenContract(
        identity, run["run_id"], contract.get("issue_number"), contract.get("pr_number"), revision, digest,
        tuple(v.ShardContract(sid, tuple(
            v.CheckRequirement(verification_check_id(sid, index, command), gate)
            for gate in (v.Obligation.SHARD, v.Obligation.INTEGRATED)
            for index, command in enumerate(["fast_check.py", *spec.get("checks", [])])
        )) for sid, spec in sorted(contract["shards"].items())),
        approvals=requirements, require_delegation_receipts=bool(contract.get("require_delegation_receipts")),
        review_required=bool(reviewers.get("cloud", True) or reviewers.get("deep", True) or contract.get("oracle_mismatches")),
    )
    observed_at = int(time.time())
    code_hash = compute_code_tree_sha256(repo, head, run_id=run["run_id"])
    evidence_error = None
    try:
        signed = _authenticated_snapshots(contract, envelope)
    except (OSError, ValueError, KeyError, TypeError, GuardError, subprocess.TimeoutExpired) as exc:
        signed = {}
        evidence_error = str(exc)
    decisions = {}
    for sid in ids:
        shard = run["shards"][sid]
        shard_head = head if level is v.Obligation.SHARD or len(run["shards"]) == 1 else shard.get("last_green_sha")
        shard_tree = (compute_code_tree_sha256(repo, shard_head, run_id=run["run_id"])
                      if shard_head and shard_head != head else code_hash)
        binding = v.Binding(identity, run["run_id"], run.get("issue_number"), run.get("pr_number"),
                            int(run["contract_revision"]), run["contract_sha256"], base, shard_head or "")
        generations = v.Generations(
            shard=int(shard.get("attempts") or 0), integration=int(run.get("total_attempts") or 0),
            review=len(run.get("reviews") or []), merge=len(run.get("reviews") or []),
        )
        snapshot = signed.get(sid)
        signed_tooling = set() if snapshot is None else {
            item.tooling_digest for item in snapshot.tooling if item.gate == level
        } or {snapshot.tooling_digest}
        if snapshot is not None and (
            snapshot.binding != binding or snapshot.generations != generations
            or snapshot.shard_tree_digest != shard_tree
            or (level is not v.Obligation.SHARD and
                (snapshot.integration_commit, snapshot.integration_tree_digest) != (head, code_hash))
            or (checked_tooling_digest is not None and level in (v.Obligation.SHARD, v.Obligation.INTEGRATED)
                and signed_tooling != {checked_tooling_digest})
            or (level is v.Obligation.MERGE_ELIGIBILITY
                and {item.finding_id for item in snapshot.findings} != set(run.get("findings") or {}))
        ):
            evidence_error = "signed evidence tuple differs from the current contract, tree or generation"
            snapshot = None
        if snapshot is None:
            snapshot = v.EvidenceSnapshot(observed_at, binding, generations)
        approvals = snapshot.approvals if auto_merge_approved is not False else tuple(
            replace(a, meta=replace(a.meta, status="revoked")) if a.action == "auto-merge" else a
            for a in snapshot.approvals
        )
        snapshot = replace(
            snapshot, observed_at=observed_at, binding=binding, generations=generations,
            integration_commit=head if level is not v.Obligation.SHARD else None,
            integration_tree_digest=code_hash if level is not v.Obligation.SHARD else None,
            shard_tree_digest=shard_tree, approvals=approvals,
        )
        decision = v.verify(v.Target(level, sid), frozen, snapshot)
        decisions[sid] = asdict(decision)
    result = {"obligation": obligation, "ok": all(item["ok"] for item in decisions.values()), "decisions": decisions}
    result["digest"] = contract_sha256(result)
    if evidence_error is not None:
        result["evidence_error"] = evidence_error
    if legacy_ok is not None and result["ok"] != legacy_ok:
        print(f"SHADOW-DIVERGENCE {obligation}: legacy={legacy_ok} verification={result['ok']} "
              f"digest={result['digest']}", file=sys.stderr)
    return result


def _observe_auto_merge_approval(repo: Path, identity: str, contract: dict[str, Any], head: str) -> bool:
    if not identity.startswith("github.com/") or not contract.get("pr_number"):
        return False
    try:
        owners = set(load_config(repo).get("trusted_owners") or [])
        client = GitHubAPIClient(identity.removeprefix("github.com/"))
        pr = int(contract["pr_number"])
        return _has_owner_label_event(client.get_pr_events(pr), "allow-auto-merge", owners) and (
            _has_owner_comment_match(client.get_issue_comments_graphql(pr), AUTO_MERGE_APPROVAL_RE,
                                     (str(pr), head), owners)
        )
    except (GuardError, OSError, ValueError):
        return False


def oracle_mismatch_id(record: dict[str, Any]) -> str:
    fields = ("shard", "upstream_ref", "summary", "why_ours_is_correct", "test")
    return "oracle-" + contract_sha256({key: record.get(key) for key in fields})[:16]


def validate_oracle_mismatch(record: dict[str, Any], shards: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise GuardError("invalid oracle mismatch record")
    upstream_ref = record.get("upstream_ref", "")
    if not isinstance(upstream_ref, str) or not re.fullmatch(r"[^:@\s]+:[1-9]\d*@[^\s@]+", upstream_ref):
        raise GuardError("oracle mismatch requires --upstream-ref file:line@version")
    if record.get("shard") not in shards:
        raise GuardError("oracle mismatch references an unknown shard")
    for field in ("summary", "why_ours_is_correct", "test"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise GuardError(f"oracle mismatch requires a non-empty {field}")
    path = record["test"]
    leaf = path.rsplit("/", 1)[-1].lower()
    if (any(part in ("", ".", "..") for part in path.split("/"))
            or any(ch in path for ch in "\\:\x00")
            or path.startswith((".autoloop/runs/", ".autoloop/experiments/"))
            or not leaf.endswith(_EXECUTABLE_TEST_EXTS)
            or not ("test" in leaf or "/tests/" in f"/{path}" or "/test/" in f"/{path}"
                    or path.startswith("perf-suite/k6/scenarios/"))):
        raise GuardError("oracle mismatch --test must be a repo-relative executable test path")


def open_oracle_mismatches(state: dict[str, Any], code_hash: str | None) -> dict[str, Any]:
    register = state.get("oracle_mismatches", {})
    if not isinstance(register, dict):
        raise GuardError("invalid oracle mismatch register")
    if not register:
        return {}
    reviewed = {
        mismatch_id
        for review in state.get("reviews", [])
        if code_hash and review.get("code_tree_sha256") == code_hash
        and review.get("contract_revision") == state.get("contract_revision")
        and review.get("base_sha") == state.get("base_sha")
        and review.get("stage") in ("cloud", "deep")
        and review.get("verdict") == "pass"
        and str(review.get("reviewer") or "").strip()
        for mismatch_id in review.get("oracle_mismatch_ids", [])
    }
    for mismatch_id, record in register.items():
        validate_oracle_mismatch(record, state.get("shards", {}))
        if mismatch_id != oracle_mismatch_id(record):
            raise GuardError(f"oracle mismatch {mismatch_id!r} does not match its record")
    return {key: record for key, record in register.items() if key not in reviewed}


def format_contract_comment(contract: dict[str, Any]) -> str:
    rev = int(contract["contract_revision"])
    digest = contract_sha256(contract)
    pretty = json.dumps(contract, indent=2, sort_keys=True)
    pr_line = ""
    if contract.get("pr_number") is not None:
        pr_line = f"Bound PR: #{int(contract['pr_number'])}\n"
    return f"<!-- autoloop-contract:v1 rev={rev} sha256={digest} -->\n{pr_line}```json\n{pretty}\n```\n"


def _is_comment_owner_authentic(comment: dict[str, Any], trusted_owners: set[str]) -> bool:
    author = str(comment.get("author") or "")
    if author not in trusted_owners:
        return False
    # GraphQL authenticity: unedited (lastEditedAt is None) OR editor is also a trusted owner.
    last_edited_at = comment.get("lastEditedAt")
    editor = comment.get("editor")
    if last_edited_at:
        if not editor or str(editor) not in trusted_owners:
            return False
    elif editor and str(editor) not in trusted_owners:
        return False
    return True


def parse_latest_owner_contract(
    comments: list[dict[str, Any]],
    trusted_owners: set[str],
    *,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    by_rev: dict[int, dict[str, dict[str, Any]]] = {}
    for comment in comments:
        if not _is_comment_owner_authentic(comment, trusted_owners):
            continue
        body = str(comment.get("body") or "")
        marker = CONTRACT_MARKER_RE.search(body)
        if not marker:
            continue
        rev = int(marker.group(1))
        expected_sha = marker.group(2)
        block = CONTRACT_JSON_BLOCK_RE.search(body)
        if not block:
            raise GuardError("owner contract comment missing ```json``` block")
        try:
            contract = json.loads(block.group(1))
        except json.JSONDecodeError as exc:
            raise GuardError(f"invalid JSON in owner contract comment: {exc}") from exc
        if not isinstance(contract, dict):
            raise GuardError("owner contract payload must be a JSON object")
        if run_id is not None and contract.get("run_id") != run_id:
            continue
        if int(contract.get("contract_revision") or 0) != rev or rev < 1:
            raise GuardError("owner contract revision mismatch or < 1")
        actual_sha = contract_sha256(contract)
        if actual_sha != expected_sha:
            raise GuardError(
                f"owner contract sha256 mismatch: marker={expected_sha} actual={actual_sha}"
            )
        by_rev.setdefault(rev, {})[actual_sha] = contract
    if not by_rev:
        return None
    max_rev = max(by_rev)
    if len(by_rev[max_rev]) > 1:
        raise GuardError(
            f"conflicting owner contracts at contract_revision={max_rev} for run {run_id!r}"
        )
    return next(iter(by_rev[max_rev].values()))


def load_config(repo: Path) -> dict[str, Any]:
    config_path = repo / ".autoloop/config.json"
    if not config_path.is_file():
        raise GuardError(f"missing trusted config at {config_path}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"failed to parse {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardError("config.json must be a JSON object")
    return data


def _is_bot_identity(name: str, email: str = "", login: str = "") -> bool:
    for field in (name, email, login):
        lowered = (field or "").lower()
        if "[bot]" in lowered or lowered.endswith("-bot") or "bot@" in lowered:
            return True
    return False


class GraphQLNotFoundError(GuardError):
    """Raised when a GitHub GraphQL lookup returns only NOT_FOUND errors."""


class GitHubAPIClient:
    """Read-only GitHub REST + GraphQL client using stdlib urllib."""

    def __init__(self, repo_slug: str, token: str | None = None, api_url: str = "https://api.github.com") -> None:
        self.repo_slug = repo_slug
        self.owner, self.repo_name = repo_slug.split("/", 1)
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.api_url = api_url.rstrip("/")

    def _request_json(self, url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "autoloop-v3-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise GuardError(f"GitHub API {method} {url} failed ({exc.code}): {body}") from exc
        except urllib.error.URLError as exc:
            raise GuardError(f"GitHub API request failed: {exc}") from exc

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        result = self._request_json(
            f"{self.api_url}/graphql",
            method="POST",
            payload={"query": query, "variables": variables},
        )
        if result.get("errors"):
            errors = result["errors"]
            if isinstance(errors, list) and errors and all(
                isinstance(e, dict) and e.get("type") == "NOT_FOUND" for e in errors
            ):
                raise GraphQLNotFoundError(f"GitHub GraphQL NOT_FOUND: {errors}")
            raise GuardError(f"GitHub GraphQL error: {errors}")
        return result.get("data") or {}

    def get_pr_events(self, pr_number: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request_json(
                f"{self.api_url}/repos/{self.repo_slug}/issues/{pr_number}/events?per_page=100&page={page}"
            )
            if not batch:
                break
            for ev in batch:
                events.append(
                    {
                        "event": ev.get("event"),
                        "label": (ev.get("label") or {}).get("name"),
                        "actor": (ev.get("actor") or {}).get("login"),
                    }
                )
            if len(batch) < 100:
                break
            page += 1
        return events

    def get_issue_comments_graphql(self, issue_or_pr_number: int) -> list[dict[str, Any]]:
        query = """
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $name) {
            issueOrPullRequest(number: $number) {
              ... on Issue {
                comments(first: 100, after: $after) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    body
                    author { login }
                    editor { login }
                    lastEditedAt
                  }
                }
              }
              ... on PullRequest {
                comments(first: 100, after: $after) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    body
                    author { login }
                    editor { login }
                    lastEditedAt
                  }
                }
              }
            }
          }
        }
        """
        comments: list[dict[str, Any]] = []
        after = None
        while True:
            data = self._graphql(
                query,
                {"owner": self.owner, "name": self.repo_name, "number": int(issue_or_pr_number), "after": after},
            )
            node = ((data.get("repository") or {}).get("issueOrPullRequest") or {}).get("comments") or {}
            for item in node.get("nodes") or []:
                comments.append(
                    {
                        "body": item.get("body") or "",
                        "author": (item.get("author") or {}).get("login") or "",
                        "editor": (item.get("editor") or {}).get("login"),
                        "lastEditedAt": item.get("lastEditedAt"),
                    }
                )
            page_info = node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return comments

    def get_pr_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request_json(
                f"{self.api_url}/repos/{self.repo_slug}/pulls/{pr_number}/reviews?per_page=100&page={page}"
            )
            if not batch:
                break
            for item in batch:
                reviews.append(
                    {
                        "id": item.get("id"),
                        "user": (item.get("user") or {}).get("login") or "",
                        "state": item.get("state") or "",
                        "commit_id": item.get("commit_id") or "",
                        "submitted_at": item.get("submitted_at") or "",
                    }
                )
            if len(batch) < 100:
                break
            page += 1
        return reviews

    def get_pr_commits(self, pr_number: int) -> list[dict[str, Any]]:
        commits: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request_json(
                f"{self.api_url}/repos/{self.repo_slug}/pulls/{pr_number}/commits?per_page=100&page={page}"
            )
            if not batch:
                break
            for item in batch:
                verification = ((item.get("commit") or {}).get("verification") or {})
                commits.append(
                    {
                        "sha": item.get("sha") or "",
                        "author_login": (item.get("author") or {}).get("login") or "",
                        "committer_login": (item.get("committer") or {}).get("login") or "",
                        "author_type": (item.get("author") or {}).get("type") or "",
                        "committer_type": (item.get("committer") or {}).get("type") or "",
                        "verified": bool(verification.get("verified")),
                    }
                )
            if len(batch) < 100:
                break
            page += 1
        return commits

    def get_commit(self, sha: str) -> dict[str, Any] | None:
        try:
            item = self._request_json(f"{self.api_url}/repos/{self.repo_slug}/commits/{sha}")
        except GuardError:
            return None
        if not isinstance(item, dict):
            return None
        verification = ((item.get("commit") or {}).get("verification") or {})
        return {
            "sha": item.get("sha") or sha,
            "author_login": (item.get("author") or {}).get("login") or "",
            "committer_login": (item.get("committer") or {}).get("login") or "",
            "author_type": (item.get("author") or {}).get("type") or "",
            "committer_type": (item.get("committer") or {}).get("type") or "",
            "verified": bool(verification.get("verified")),
        }

    def get_check_runs(self, ref: str) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request_json(
                f"{self.api_url}/repos/{self.repo_slug}/commits/{ref}/check-runs?per_page=100&page={page}"
            )
            batch = (data.get("check_runs") or []) if isinstance(data, dict) else []
            if not batch:
                break
            for item in batch:
                runs.append(
                    {
                        "name": item.get("name") or "",
                        "status": item.get("status") or "",
                        "conclusion": item.get("conclusion") or "",
                        "app_id": (item.get("app") or {}).get("id"),
                    }
                )
            if len(batch) < 100:
                break
            page += 1
        return runs

    def get_pr_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
        query = """
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  isResolved
                  comments(first: 20) {
                    nodes {
                      author { login }
                      body
                    }
                  }
                }
              }
            }
          }
        }
        """
        threads: list[dict[str, Any]] = []
        after = None
        while True:
            data = self._graphql(
                query,
                {"owner": self.owner, "name": self.repo_name, "number": int(pr_number), "after": after},
            )
            node = ((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}
            for item in node.get("nodes") or []:
                c_nodes = (item.get("comments") or {}).get("nodes") or []
                threads.append(
                    {
                        "id": item.get("id") or "",
                        "isResolved": bool(item.get("isResolved")),
                        "comments": [
                            {"author": (c.get("author") or {}).get("login") or "", "body": c.get("body") or ""}
                            for c in c_nodes
                        ],
                    }
                )
            page_info = node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return threads

    def find_contract_issue_numbers(self, pr_number: int) -> list[int]:
        """Discover same-repository issue numbers linked via PR timeline or containing autoloop-contract markers.

        Uses only `issues: read` + `pull-requests: read` so PR body/commit edits cannot sever
        issue-only enrollment, while strictly ignoring cross-repository issue references.
        """
        found: set[int] = set()
        expected_repo = self.repo_slug.lower()
        timeline_query = """
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              timelineItems(first: 100, after: $after, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  __typename
                  ... on CrossReferencedEvent {
                    isCrossRepository
                    source {
                      __typename
                      ... on Issue {
                        number
                        repository { nameWithOwner }
                      }
                    }
                  }
                  ... on ConnectedEvent {
                    isCrossRepository
                    subject {
                      __typename
                      ... on Issue {
                        number
                        repository { nameWithOwner }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        after = None
        while True:
            data = self._graphql(
                timeline_query,
                {"owner": self.owner, "name": self.repo_name, "number": int(pr_number), "after": after},
            )
            node = ((data.get("repository") or {}).get("pullRequest") or {}).get("timelineItems") or {}
            for item in node.get("nodes") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("isCrossRepository") is True:
                    continue
                src = item.get("source") if item.get("__typename") == "CrossReferencedEvent" else item.get("subject")
                if not (isinstance(src, dict) and src.get("__typename") == "Issue" and isinstance(src.get("number"), int)):
                    continue
                repo_nwo = ((src.get("repository") or {}).get("nameWithOwner") or "").lower()
                if repo_nwo and repo_nwo != expected_repo:
                    continue
                found.add(int(src["number"]))
            page_info = node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")

        search_query = """
        query($q: String!, $after: String) {
          search(query: $q, type: ISSUE, first: 100, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              __typename
              ... on Issue {
                number
                repository { nameWithOwner }
              }
            }
          }
        }
        """
        q_str = f'repo:{self.repo_slug} is:issue "autoloop.v1" "pr_number" {int(pr_number)}'
        after = None
        while True:
            data = self._graphql(search_query, {"q": q_str, "after": after})
            node = data.get("search") or {}
            for item in node.get("nodes") or []:
                if not (isinstance(item, dict) and item.get("__typename") == "Issue" and isinstance(item.get("number"), int)):
                    continue
                repo_nwo = ((item.get("repository") or {}).get("nameWithOwner") or "").lower()
                if repo_nwo and repo_nwo != expected_repo:
                    continue
                found.add(int(item["number"]))
            page_info = node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return sorted(found)


def _has_owner_label_event(events: list[dict[str, Any]], label_name: str, trusted_owners: set[str]) -> bool:
    """Return True iff the most recent labeling event for `label_name` was applied by a trusted owner."""
    last_actor: str | None = None
    applied = False
    for ev in events:
        if ev.get("label") != label_name:
            continue
        if ev.get("event") == "labeled":
            applied = True
            last_actor = str(ev.get("actor") or "")
        elif ev.get("event") == "unlabeled":
            applied = False
            last_actor = None
    return applied and (last_actor in trusted_owners)


def _ever_owner_labeled(events: list[dict[str, Any]], label_name: str, trusted_owners: set[str]) -> bool:
    return any(
        ev.get("event") == "labeled"
        and ev.get("label") == label_name
        and str(ev.get("actor") or "") in trusted_owners
        for ev in events
    )


def _has_owner_comment_match(
    comments: list[dict[str, Any]],
    pattern: re.Pattern[str],
    expected_groups: tuple[str, ...],
    trusted_owners: set[str],
) -> bool:
    for comment in comments:
        if not _is_comment_owner_authentic(comment, trusted_owners):
            continue
        body = str(comment.get("body") or "")
        for match in pattern.finditer(body):
            if match.groups() == expected_groups:
                return True
    return False


def _parse_all_owner_contracts(
    comments: list[dict[str, Any]],
    trusted_owners: set[str],
    *,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return all authentic owner FrozenContractV1 payloads sorted descending by revision."""
    contracts: list[dict[str, Any]] = []
    for comment in comments:
        if not _is_comment_owner_authentic(comment, trusted_owners):
            continue
        body = str(comment.get("body") or "")
        marker = CONTRACT_MARKER_RE.search(body)
        if not marker:
            continue
        rev = int(marker.group(1))
        expected_sha = marker.group(2)
        block = CONTRACT_JSON_BLOCK_RE.search(body)
        if not block:
            raise GuardError("owner contract comment missing ```json``` block")
        try:
            contract = json.loads(block.group(1))
        except json.JSONDecodeError as exc:
            raise GuardError(f"invalid JSON in owner contract comment: {exc}") from exc
        if not isinstance(contract, dict):
            raise GuardError("owner contract payload must be a JSON object")
        if run_id is not None and contract.get("run_id") != run_id:
            continue
        if int(contract.get("contract_revision") or 0) != rev or rev < 1:
            raise GuardError("owner contract revision mismatch or < 1")
        actual_sha = contract_sha256(contract)
        if actual_sha != expected_sha:
            raise GuardError(
                f"owner contract sha256 mismatch: marker={expected_sha} actual={actual_sha}"
            )
        contracts.append(contract)
    contracts.sort(key=lambda c: int(c.get("contract_revision") or 0), reverse=True)
    return contracts


_ISSUE_REF_RE = re.compile(r"(?<![A-Za-z0-9_/#])#(\d+)\b")


def _git_log_messages_and_run_paths(repo: Path, base_sha: str, head_sha: str) -> tuple[list[str], str]:
    """Single `git log` pass returning touched `.autoloop/runs/*` paths and combined commit messages."""
    proc = _run_git(
        repo,
        ["log", "--name-only", "--format=%x1e%B%x1f", f"{base_sha}..{head_sha}"],
        check=True,
    )
    text = proc.stdout.decode("utf-8", errors="replace")
    messages: list[str] = []
    run_paths: set[str] = set()
    for chunk in text.split("\x1e"):
        if not chunk:
            continue
        if "\x1f" in chunk:
            msg_part, files_part = chunk.split("\x1f", 1)
        else:
            msg_part, files_part = chunk, ""
        messages.append(msg_part)
        for line in files_part.splitlines():
            norm = _normalize_posix_path(line.strip())
            if norm and RUN_FILE_RE.match(norm):
                run_paths.add(norm)
    return sorted(run_paths), "\n".join(messages)


def _extract_referenced_issue_numbers(
    repo: Path,
    mb_target: str,
    head_sha: str,
    pr_body: str,
    pr_comments: list[dict[str, Any]],
    present_run_paths: list[str],
    trusted_owners: set[str],
    *,
    commit_messages: str | None = None,
) -> list[int]:
    """Collect candidate GitHub issue numbers from PR body, commits, run blobs, and owner comments."""
    found: set[int] = set()
    for raw in _ISSUE_REF_RE.findall(pr_body or ""):
        found.add(int(raw))
    if commit_messages is None:
        log_proc = _run_git(repo, ["log", "--format=%B", f"{mb_target}..{head_sha}"], check=True)
        commit_messages = log_proc.stdout.decode("utf-8", errors="replace")
    for raw in _ISSUE_REF_RE.findall(commit_messages):
        found.add(int(raw))
    for comment in pr_comments:
        if _is_comment_owner_authentic(comment, trusted_owners):
            for raw in _ISSUE_REF_RE.findall(str(comment.get("body") or "")):
                found.add(int(raw))
    for run_path in present_run_paths:
        blob = git_read_blob(repo, head_sha, run_path)
        if blob is not None:
            try:
                parsed = json.loads(blob.decode("utf-8"))
                inum = parsed.get("issue_number") if isinstance(parsed, dict) else None
                if isinstance(inum, int) and inum > 0:
                    found.add(inum)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
    return sorted(found)


def _resolve_effective_reviews(pr_reviews: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Compute each reviewer's effective decision from formal review state transitions."""
    effective: dict[str, dict[str, str]] = {}
    for item in pr_reviews:
        user = str(item.get("user") or "").strip()
        state = str(item.get("state") or "").upper()
        if not user or state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        effective[user] = {"state": state, "commit_id": str(item.get("commit_id") or "")}
    return effective


_EXECUTABLE_TEST_EXTS = (".py", ".js", ".ts", ".mjs", ".cjs", ".rs", ".sh", ".go", ".rb")


def _verify_safeguard_artifact(
    repo: Path,
    tree_ish: str,
    merge_file_map: dict[str, bytes],
    run_id: str,
    fid: str,
    fdata: dict[str, Any],
    max_prose_lines: int,
    *,
    changed_paths: set[str] | None = None,
) -> None:
    """Verify closed finding safeguard metadata aligns with actual tree artifacts and layer rules."""
    sg = fdata.get("safeguard")
    if not isinstance(sg, dict):
        raise GuardError(f"finding {fid!r} missing safeguard evidence object")
    layer = str(sg.get("layer") or "")
    artifact_raw = str(sg.get("artifact") or "").strip()
    summary = str(sg.get("summary") or "").strip()
    why_not_static = str(sg.get("why_not_static") or "").strip() if sg.get("why_not_static") is not None else ""

    if layer not in ("layer_1_architecture", "layer_2_type", "layer_3_static_lint", "layer_4_runtime_test"):
        raise GuardError(f"finding {fid!r} has invalid safeguard layer {layer!r}")
    if not artifact_raw or not summary:
        raise GuardError(f"finding {fid!r} safeguard missing non-empty artifact or summary")

    prose_lines = len(summary.splitlines()) + (len(why_not_static.splitlines()) if why_not_static else 0)
    if prose_lines > max_prose_lines:
        raise GuardError(f"finding {fid!r} safeguard prose ({prose_lines}) exceeds {max_prose_lines}-line cap")

    artifact = _normalize_posix_path(artifact_raw)
    if not artifact or is_framework_metadata_path(artifact, run_id) or artifact.startswith(".autoloop/runs/"):
        raise GuardError(f"finding {fid!r} safeguard artifact {artifact_raw!r} cannot reference run/experiment metadata")

    if git_read_blob(repo, tree_ish, artifact) is None or artifact not in merge_file_map:
        raise GuardError(f"finding {fid!r} safeguard artifact {artifact!r} does not exist in candidate commit tree")

    if layer == "layer_3_static_lint":
        from .static_rules import rule_fixture_proof

        if not (artifact.startswith(".autoloop/rules/") and artifact.endswith(".py")):
            raise GuardError(
                f"finding {fid!r} layer_3_static_lint safeguard artifact {artifact!r} must live under .autoloop/rules/*.py"
            )
        if changed_paths is not None and artifact not in changed_paths:
            raise GuardError(
                f"finding {fid!r} layer_3_static_lint safeguard artifact {artifact!r} was not introduced or updated in this PR"
            )
        proof = rule_fixture_proof(merge_file_map, artifact, str(sg.get("rule_id") or ""))
        if any(sg.get(key) != value for key, value in proof.items()):
            raise GuardError(f"finding {fid!r} static rule or fixture changed since safeguard recording")
    elif layer == "layer_4_runtime_test":
        if not why_not_static:
            raise GuardError(f"finding {fid!r} layer_4_runtime_test safeguard requires non-empty why_not_static")
        leaf = artifact.rsplit("/", 1)[-1].lower()
        has_test_ext = any(leaf.endswith(ext) for ext in _EXECUTABLE_TEST_EXTS)
        is_test_location = ("test" in leaf) or ("/tests/" in f"/{artifact}") or ("/test/" in f"/{artifact}")
        is_k6_scenario = artifact.startswith("perf-suite/k6/scenarios/") and leaf.endswith(".js")
        if not ((has_test_ext and is_test_location) or is_k6_scenario):
            raise GuardError(
                f"finding {fid!r} layer_4_runtime_test safeguard artifact {artifact!r} must reference an executable test file"
            )
        if changed_paths is not None and artifact not in changed_paths:
            raise GuardError(
                f"finding {fid!r} layer_4_runtime_test safeguard artifact {artifact!r} was not introduced or updated in this PR"
            )


def load_verified_local_contract(
    repo: Path, run_id: str, *, trusted_contracts_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_path = repo / ".autoloop/runs" / f"{run_id}.json"
    assert_safe_local_path(repo, run_path, allow_missing_leaf=False)
    if not run_path.is_file():
        raise GuardError(f"missing run checkpoint {run_path}")
    run_data = json.loads(run_path.read_text(encoding="utf-8"))

    contracts_dir = trusted_contracts_dir or (repo / ".autoloop/trusted-contracts")
    rev = int(run_data.get("contract_revision") or 0)
    contract_path = contracts_dir / f"{run_id}.rev{rev}.json"
    if not contract_path.is_file():
        contract_path = contracts_dir / f"{run_id}.json"
    assert_safe_local_path(contracts_dir, contract_path, allow_missing_leaf=False)
    try:
        contract_path.relative_to(repo)
        in_repo = True
    except ValueError:
        in_repo = False
    if trusted_contracts_dir is None or in_repo:
        assert_safe_local_path(repo, contract_path, allow_missing_leaf=False)
    if not contract_path.is_file():
        raise GuardError(f"missing local trusted FrozenContractV1 at {contract_path}")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if int(contract.get("contract_revision") or 0) != rev or rev < 1:
        raise GuardError("local FrozenContractV1 revision mismatch")
    if run_data.get("contract_sha256") != contract_sha256(contract):
        raise GuardError("run checkpoint contract_sha256 does not match FrozenContractV1")
    if run_data.get("base_branch", "main") != contract.get("base_branch", "main"):
        raise GuardError("run checkpoint base_branch does not match FrozenContractV1")
    if run_data.get("oracle_mismatches", {}) != contract.get("oracle_mismatches", {}):
        raise GuardError("run checkpoint oracle mismatches do not match FrozenContractV1")
    if run_data.get("dropped_shards", []) != contract.get("dropped_shards", []):
        raise GuardError("run checkpoint dropped shards do not match FrozenContractV1")

    # Verify mutable run checkpoint shard checks and allowed_paths match authenticated FrozenContractV1
    contract_shards_map = contract.get("shards") or {}
    run_shards_map = run_data.get("shards") or {}
    if set(contract_shards_map.keys()) != set(run_shards_map.keys()) or not contract_shards_map:
        raise GuardError("run checkpoint shards do not match FrozenContractV1")
    for sid_k, cshard_k in contract_shards_map.items():
        rshard_k = run_shards_map.get(sid_k) or {}
        if list(cshard_k.get("allowed_paths") or []) != list(rshard_k.get("allowed_paths") or []):
            raise GuardError(f"run checkpoint shard {sid_k!r} allowed_paths tampered vs FrozenContractV1")
        if list(cshard_k.get("checks") or []) != list(rshard_k.get("checks") or []):
            raise GuardError(f"run checkpoint shard {sid_k!r} checks tampered vs FrozenContractV1")
    return run_data, contract


def _dropped_scope_changes(diff, contract, run_id, *, shard_id=None):
    scopes = contract["shards"]
    former_scopes = [
        (
            drop["allowed_paths"],
            [
                path for sid, paths in drop.get("readded", {}).items()
                if sid in scopes and (shard_id is None or shard_id == sid)
                for path in paths if path in scopes[sid]["allowed_paths"]
            ],
        )
        for drop in contract.get("dropped_shards", [])
    ]
    return [
        entry.path for entry in diff
        if not is_framework_metadata_path(entry.path, run_id)
        and any(
            matches_any_glob(entry.path, dropped) and not matches_any_glob(entry.path, readded)
            for dropped, readded in former_scopes
        )
    ]


def verify_untampered(
    repo: Path,
    run_id: str,
    *,
    shard_id: str | None = None,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
    main_ref: str = "origin/main",
    trusted_contracts_dir: Path | None = None,
    git_repo: Path | None = None,
) -> dict[str, Any]:
    """Local pre-PR integrity verification against FrozenContractV1 and Git boundary rules."""
    config = load_config(repo)
    run_data, contract = load_verified_local_contract(repo, run_id, trusted_contracts_dir=trusted_contracts_dir)
    contract_shards_map = contract["shards"]
    target_repo = git_repo or repo
    eff_base = base_ref or run_data.get("base_sha") or contract.get("base_branch") or main_ref
    base_sha = git_rev_parse(target_repo, eff_base)
    head_sha = git_rev_parse(target_repo, head_ref)
    mb_target = git_merge_base(target_repo, base_sha, head_sha)
    target_diff = git_diff_tree_entries(target_repo, mb_target, head_sha)

    # Mode bans
    mode_errors = check_tree_modes(target_diff)
    head_entries = git_ls_tree(target_repo, head_sha)
    mode_errors.extend(check_tree_modes(head_entries))
    if mode_errors:
        raise GuardError("; ".join(mode_errors[:5]))

    # Case & directory prefix collisions
    collisions = detect_case_collisions(e.path for e in head_entries)
    if collisions:
        raise GuardError("; ".join(collisions[:5]))

    # Determine allowed_paths
    shards = contract.get("shards") or {}
    if shard_id is not None:
        if shard_id not in shards:
            raise GuardError(f"unknown shard {shard_id!r} in FrozenContractV1")
        active_paths = list(shards[shard_id].get("allowed_paths") or [])
        allowed_paths = active_paths
    else:
        active_paths = [path for sdata in shards.values() for path in sdata.get("allowed_paths") or []]
        allowed_paths = [*(contract.get("allowed_paths") or []), *active_paths]

    if contract.get("dropped_shards"):
        # A local comparison override cannot restore previously removed scope.
        scope_diff = target_diff
        if base_ref and run_data.get("base_sha") != base_sha:
            scope_base = git_merge_base(target_repo, run_data["base_sha"], head_sha)
            scope_diff = git_diff_tree_entries(target_repo, scope_base, head_sha)
        dropped = _dropped_scope_changes(scope_diff, contract, run_id, shard_id=shard_id)
        if dropped:
            raise GuardError(f"changes outside allowed_paths (dropped shard scope): {', '.join(dropped[:10])}")

    policy_globs = [*(config.get("critical_policy_globs") or []), *EXTRA_SKILL_POLICY_GLOBS]
    outside: list[str] = []
    policy_touched: list[str] = []
    for entry in target_diff:
        if matches_any_glob(entry.path, policy_globs):
            policy_touched.append(entry.path)
        if is_framework_metadata_path(entry.path, run_id):
            continue
        if not matches_any_glob(entry.path, allowed_paths):
            outside.append(entry.path)
    if outside:
        raise GuardError(
            f"changes outside allowed_paths: {', '.join(outside[:10])}. "
            "For an already-integrated, out-of-scope dependency PR, use "
            "--base-ref <integration-commit> (check also accepts --base-sha) for the local check. "
            "Use an authorized scope amendment for shard-owned changes."
        )
    if policy_touched:
        allow_policy = contract.get("allow_policy_paths")
        if isinstance(allow_policy, list):
            disallowed_policy = [p for p in policy_touched if not matches_any_glob(p, allow_policy)]
        elif allow_policy is True:
            disallowed_policy = []
        else:
            disallowed_policy = list(policy_touched)
        if disallowed_policy:
            raise GuardError(f"critical policy paths touched without approval: {', '.join(disallowed_policy[:10])}")

    # LF/BOM + full-tree static rules
    file_map = git_read_tree_blobs(target_repo, head_sha, tree_entries=head_entries)
    lf_errors = check_governed_lf_and_bom(file_map)
    if lf_errors:
        raise GuardError("; ".join(lf_errors[:5]))
    rules_dir = target_repo / ".autoloop/rules"
    if not rules_dir.is_dir():
        rules_dir = repo / ".autoloop/rules"
    violations = evaluate_file_map(rules_dir, file_map)
    if violations:
        raise GuardError(
            "; ".join(f"{v.rule_id}:{v.path}:{v.line}:{v.message}" for v in violations[:5])
        )

    return {
        "ok": True,
        "run_id": run_id,
        "shard_id": shard_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "code_tree_sha256": compute_code_tree_sha256(
            target_repo, head_sha, run_id=run_id, tree_entries=head_entries
        ),
        "changed_files": [e.path for e in target_diff],
        "worktree": str(target_repo),
        "contract_shards": {
            sid_k: {
                "allowed_paths": list(cshard_k.get("allowed_paths") or []),
                "checks": list(cshard_k.get("checks") or []),
            }
            for sid_k, cshard_k in sorted(contract_shards_map.items())
        },
    }


def verify_pr_integrity(
    repo: Path,
    *,
    base_sha: str,
    head_sha: str,
    pr_number: int,
    pr_author: str,
    pr_labels: list[str],
    main_ref: str = "origin/main",
    base_branch: str | None = None,
    github_client: Any = None,
    trusted_contracts_dir: Path | None = None,
    mode: str = "finalize",
    candidate_checks_result: str | None = None,
    trusted_shard_results: dict[str, str] | None = None,
    pr_body: str = "",
    shadow_verification: bool = False,
    shadow_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """V3 trusted PR integrity verifier (read-only over Git blobs, zero candidate code execution)."""
    if mode not in ("preflight", "finalize"):
        raise GuardError(f"invalid verify_pr_integrity mode {mode!r}; expected 'preflight' or 'finalize'")

    config = load_config(repo)
    trusted_owners = set(config.get("trusted_owners") or [])
    policy_globs = [*(config.get("critical_policy_globs") or []), *EXTRA_SKILL_POLICY_GLOBS]
    max_prose_lines = int((config.get("limits") or {}).get("max_safeguard_prose_lines", 15))

    rev_out = _run_git(repo, ["rev-parse", f"{base_sha}^{{commit}}", f"{head_sha}^{{commit}}", f"{main_ref}^{{commit}}"])
    rev_lines = [ln.strip() for ln in rev_out.stdout.decode("utf-8").splitlines() if ln.strip()]
    if len(rev_lines) != 3:
        raise GuardError(f"failed to resolve base/head/main commits: {rev_lines!r}")
    resolved_base, resolved_head, resolved_main = rev_lines[0], rev_lines[1], rev_lines[2]

    # 3 Git comparisons (Git 2.50+ safe)
    mb_target = git_merge_base(repo, resolved_base, resolved_head)
    mb_main = mb_target if resolved_base == resolved_main else git_merge_base(repo, resolved_main, resolved_head)
    merge_tree_oid = git_merge_tree(repo, resolved_base, resolved_head)

    target_diff = git_diff_tree_entries(repo, mb_target, resolved_head)
    main_diff = target_diff if mb_main == mb_target else git_diff_tree_entries(repo, mb_main, resolved_head)
    merge_entries = git_ls_tree(repo, merge_tree_oid)

    # Mode bans (120000 symlink, 160000 gitlink) across diffs and merge result
    mode_errors = check_tree_modes(target_diff) + check_tree_modes(main_diff) + check_tree_modes(merge_entries)
    if mode_errors:
        raise GuardError("; ".join(dict.fromkeys(mode_errors))[:600])

    # Case-insensitive file AND directory-prefix collision check on merge result tree
    collisions = detect_case_collisions(e.path for e in merge_entries)
    if collisions:
        raise GuardError("; ".join(collisions[:5]))

    # Load full merge-result file map for LF/BOM + full-tree trusted static rules
    merge_file_map = git_read_tree_blobs(repo, merge_tree_oid, tree_entries=merge_entries)
    lf_errors = check_governed_lf_and_bom(merge_file_map)
    if lf_errors:
        raise GuardError("; ".join(lf_errors[:5]))

    rule_violations = evaluate_file_map(repo / ".autoloop/rules", merge_file_map)
    if rule_violations:
        formatted = "; ".join(
            f"{v.rule_id}: {v.path}:{v.line}: {v.message}" for v in rule_violations[:10]
        )
        raise GuardError(f"static rule violations: {formatted}")

    # Collect GitHub PR metadata when client is provided
    pr_events: list[dict[str, Any]] = []
    pr_comments: list[dict[str, Any]] = []
    pr_reviews: list[dict[str, Any]] = []
    pr_commits_api: list[dict[str, Any]] = []
    pr_review_threads: list[dict[str, Any]] = []
    if github_client is not None:
        pr_events = github_client.get_pr_events(pr_number)
        pr_comments = github_client.get_issue_comments_graphql(pr_number)
        pr_reviews = github_client.get_pr_reviews(pr_number)
        pr_commits_api = github_client.get_pr_commits(pr_number)

    label_set = set(pr_labels)
    has_autoloop_label = "autoloop" in label_set
    ever_owner_autoloop_labeled = _ever_owner_labeled(pr_events, "autoloop", trusted_owners)

    # Detect any .autoloop/runs/*.json touched in diff OR in commit history since mb_target
    is_fast_forward = mb_target == resolved_base
    head_entries_cached = merge_entries if is_fast_forward else None
    head_blobs_cached: dict[str, bytes] | None = merge_file_map if is_fast_forward else None

    def _read_head_blob(rel_path: str) -> bytes | None:
        if head_blobs_cached is not None:
            return head_blobs_cached.get(rel_path)
        return git_read_blob(repo, resolved_head, rel_path)

    diff_run_paths = [e.path for e in target_diff if RUN_FILE_RE.match(e.path)]
    history_run_paths, commit_messages_text = _git_log_messages_and_run_paths(repo, mb_target, resolved_head)
    all_touched_run_paths = sorted(set(diff_run_paths) | set(history_run_paths))
    present_run_paths = [p for p in all_touched_run_paths if _read_head_blob(p) is not None]

    pr_contracts = _parse_all_owner_contracts(pr_comments, trusted_owners) if pr_comments else []
    pr_bound_contract = pr_contracts[0] if pr_contracts else None

    # Resolve linked issue comments BEFORE early non-enrolled exit so issue-only enrollment is sticky!
    issue_comments_cache: dict[int, list[dict[str, Any]]] = {}
    issue_bound_contracts: list[dict[str, Any]] = []
    if github_client is not None:
        incidental_issues = set(
            _extract_referenced_issue_numbers(
                repo,
                mb_target,
                resolved_head,
                pr_body,
                pr_comments,
                [],
                trusted_owners,
                commit_messages=commit_messages_text,
            )
        )
        authoritative_issues: set[int] = set()
        for run_path in present_run_paths:
            blob = _read_head_blob(run_path)
            if blob is not None:
                try:
                    parsed = json.loads(blob.decode("utf-8"))
                    inum = parsed.get("issue_number") if isinstance(parsed, dict) else None
                    if isinstance(inum, int) and inum > 0:
                        authoritative_issues.add(inum)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        if hasattr(github_client, "find_contract_issue_numbers"):
            for inum in github_client.find_contract_issue_numbers(pr_number):
                if isinstance(inum, int) and inum > 0:
                    authoritative_issues.add(inum)
        referenced_issues = sorted(incidental_issues | authoritative_issues)
        for inum in referenced_issues:
            if int(inum) == int(pr_number):
                continue
            try:
                icomm = github_client.get_issue_comments_graphql(int(inum))
            except GraphQLNotFoundError:
                if int(inum) not in authoritative_issues:
                    # Incidental `#123` mention in PR prose/commits that does not exist in this repo.
                    continue
                raise
            issue_comments_cache[int(inum)] = icomm
            for c in _parse_all_owner_contracts(icomm, trusted_owners):
                if c.get("pr_number") is not None and int(c["pr_number"]) == int(pr_number):
                    issue_bound_contracts.append(c)
    issue_bound_contract = issue_bound_contracts[0] if issue_bound_contracts else None

    enrolled = bool(
        has_autoloop_label
        or ever_owner_autoloop_labeled
        or all_touched_run_paths
        or pr_bound_contract is not None
        or issue_bound_contract is not None
    )

    # Stickiness check: removing autoloop label or deleting checkpoint fails closed!
    if ever_owner_autoloop_labeled and not has_autoloop_label:
        raise GuardError("enrollment stickiness violation: owner-applied 'autoloop' label was removed")

    # Critical Policy Path Guard (checked over mb_main..head_sha to block stacked-PR smuggling)
    touched_policy_paths = sorted(
        {e.path for e in main_diff if matches_any_glob(e.path, policy_globs)}
    )
    if touched_policy_paths:
        policy_allowed = False
        if not enrolled and pr_author in trusted_owners:
            # Verify every commit in mb_main..head_sha touching policy paths has verified trusted-owner provenance.
            git_commits = git_commits_touching_paths(repo, mb_main, resolved_head, touched_policy_paths)
            api_by_sha = {c["sha"]: c for c in pr_commits_api}
            bot_or_untrusted = False
            for c in git_commits:
                if _is_bot_identity(c["author_name"], c["author_email"]) or _is_bot_identity(
                    c["committer_name"], c["committer_email"]
                ):
                    bot_or_untrusted = True
                    break
                api_c = api_by_sha.get(c["sha"])
                if api_c is None and github_client is not None and hasattr(github_client, "get_commit"):
                    api_c = github_client.get_commit(c["sha"])
                if api_c is None:
                    bot_or_untrusted = True
                    break
                a_login = api_c.get("author_login") or ""
                c_login = api_c.get("committer_login") or ""
                a_type = (api_c.get("author_type") or "").lower()
                c_type = (api_c.get("committer_type") or "").lower()
                is_verified = api_c.get("verified") is True
                if (
                    not is_verified
                    or a_type == "bot"
                    or c_type == "bot"
                    or _is_bot_identity("", "", a_login)
                    or _is_bot_identity("", "", c_login)
                    or not a_login
                    or a_login not in trusted_owners
                    or (c_login and c_login not in trusted_owners and c_login != "web-flow")
                ):
                    bot_or_untrusted = True
                    break
            if not bot_or_untrusted:
                policy_allowed = True

        if not policy_allowed:
            # Check option (b): owner-applied allow-rule-edit label + owner-authored GraphQL-verified approval comment.
            has_label = "allow-rule-edit" in label_set and _has_owner_label_event(
                pr_events, "allow-rule-edit", trusted_owners
            )
            has_comment = _has_owner_comment_match(
                pr_comments,
                RULE_EDIT_APPROVAL_RE,
                (str(pr_number), resolved_head),
                trusted_owners,
            )
            if has_label and has_comment:
                policy_allowed = True

        if not policy_allowed:
            raise GuardError(
                f"critical policy paths modified without owner approval: {', '.join(touched_policy_paths[:10])}"
            )

    # Non-enrolled fast exit (<2s, zero candidate fast-check)
    if not enrolled:
        return {
            "verdict": "non-enrolled-pass",
            "enrolled": False,
            "pr_number": pr_number,
            "base_sha": resolved_base,
            "head_sha": resolved_head,
            "merge_tree_oid": merge_tree_oid,
            "shard_checks": [],
        }

    # --- Enrolled PR Verification ---
    if all_touched_run_paths and not present_run_paths:
        raise GuardError(
            "enrollment stickiness violation: .autoloop/runs/<id>.json was deleted in a later push"
        )
    if len(present_run_paths) != 1:
        raise GuardError(
            f"enrolled PR must have exactly one active run checkpoint in head_sha (found {present_run_paths})"
        )

    run_path = present_run_paths[0]
    match = RUN_FILE_RE.match(run_path)
    assert match is not None
    run_id = match.group(1)

    run_blob = _read_head_blob(run_path)
    if run_blob is None:
        raise GuardError(f"missing run checkpoint blob {run_path} at {resolved_head}")
    try:
        run_data = json.loads(run_blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError(f"invalid run checkpoint JSON {run_path}: {exc}") from exc

    if run_data.get("run_id") != run_id:
        raise GuardError(f"run_id mismatch in {run_path}: got {run_data.get('run_id')!r}")

    issue_number = run_data.get("issue_number")
    # Load authoritative FrozenContractV1 across ALL discovered issues + explicit issue_number + PR comments
    contract: dict[str, Any] | None = None
    if github_client is not None:
        search_comments: list[dict[str, Any]] = []
        if issue_number is not None:
            inum = int(issue_number)
            if inum not in issue_comments_cache and inum != int(pr_number):
                issue_comments_cache[inum] = github_client.get_issue_comments_graphql(inum)
        for cached_num in sorted(issue_comments_cache):
            search_comments.extend(issue_comments_cache[cached_num])
        search_comments.extend(pr_comments)
        contract = parse_latest_owner_contract(search_comments, trusted_owners, run_id=run_id)
    elif trusted_contracts_dir is not None:
        rev = int(run_data.get("contract_revision") or 0)
        cand = trusted_contracts_dir / f"{run_id}.rev{rev}.json"
        if not cand.is_file():
            cand = trusted_contracts_dir / f"{run_id}.json"
        assert_safe_local_path(trusted_contracts_dir, cand, allow_missing_leaf=False)
        try:
            cand.relative_to(repo)
            in_repo = True
        except ValueError:
            in_repo = False
        if in_repo:
            assert_safe_local_path(repo, cand, allow_missing_leaf=False)
        if cand.is_file():
            contract = json.loads(cand.read_text(encoding="utf-8"))

    if contract is None:
        raise GuardError(f"no owner-authenticated FrozenContractV1 found for run {run_id!r}")

    # Monotonic contract_revision check + equality verification
    contract_rev = int(contract.get("contract_revision") or 0)
    run_rev = int(run_data.get("contract_revision") or 0)
    if contract_rev < 1 or run_rev != contract_rev:
        raise GuardError(
            f"contract_revision mismatch: authoritative rev={contract_rev}, run rev={run_rev}"
        )
    expected_contract_sha = contract_sha256(contract)
    if run_data.get("contract_sha256") != expected_contract_sha:
        raise GuardError("run checkpoint contract_sha256 does not match authoritative FrozenContractV1")

    # Sticky PR-to-run binding MUST live in owner-authenticated contract (never fall back to candidate JSON)
    contract_pr = contract.get("pr_number")
    if contract_pr is None:
        raise GuardError("authoritative FrozenContractV1 missing required pr_number binding")
    if int(contract_pr) != int(pr_number):
        raise GuardError(f"PR binding mismatch: contract bound to PR #{contract_pr}, got PR #{pr_number}")
    if run_data.get("pr_number") is not None and int(run_data["pr_number"]) != int(pr_number):
        raise GuardError(
            f"PR binding mismatch: run checkpoint bound to PR #{run_data['pr_number']}, got PR #{pr_number}"
        )

    # Base-movement / PR retarget invalidation
    run_base_sha = run_data.get("base_sha")
    if not run_base_sha or run_base_sha != resolved_base:
        raise GuardError(
            f"base_sha moved or retargeted: evidence bound to {run_base_sha!r}, PR base is {resolved_base!r}"
        )
    # Legacy contracts used main as a default even when the PR targeted a stacked branch.
    frozen_base_branch = contract.get("base_branch", "main")
    if run_data.get("base_branch", "main") != frozen_base_branch or (
        contract.get("base_branch_bound") and base_branch is not None and base_branch != frozen_base_branch
    ):
        raise GuardError("base_branch moved or retargeted: publish the updated FrozenContractV1")
    if run_data.get("oracle_mismatches", {}) != contract.get("oracle_mismatches", {}):
        raise GuardError("run checkpoint oracle mismatches do not match FrozenContractV1")
    if run_data.get("dropped_shards", []) != contract.get("dropped_shards", []):
        raise GuardError("run checkpoint dropped shards do not match FrozenContractV1")

    # Verify FrozenContractV1 fields match run record
    contract_shards = contract.get("shards") or {}
    run_shards = run_data.get("shards") or {}
    if set(contract_shards.keys()) != set(run_shards.keys()) or not contract_shards:
        raise GuardError("run shards do not match FrozenContractV1 shards")
    active_paths: list[str] = []
    shard_checks_payload: list[dict[str, Any]] = []
    for sid, cshard in sorted(contract_shards.items()):
        c_allowed = list(cshard.get("allowed_paths") or [])
        r_allowed = list((run_shards[sid] or {}).get("allowed_paths") or [])
        if c_allowed != r_allowed:
            raise GuardError(f"shard {sid!r} allowed_paths tampered vs FrozenContractV1")
        active_paths.extend(c_allowed)
        shard_checks_payload.append({"shard_id": sid, "checks": list(cshard.get("checks") or [])})

    # Check PR-introduced scope (mb_target..head_sha) against allowed_paths + strict metadata
    dropped = _dropped_scope_changes(target_diff, contract, run_id)
    if dropped:
        raise GuardError(f"PR modifies paths outside FrozenContractV1 allowed_paths (dropped shard scope): {', '.join(dropped[:10])}")
    all_allowed_paths = [*(contract.get("allowed_paths") or []), *active_paths]
    outside_scope: list[str] = []
    for entry in target_diff:
        if is_framework_metadata_path(entry.path, run_id):
            continue
        if not matches_any_glob(entry.path, all_allowed_paths):
            outside_scope.append(entry.path)
    if outside_scope:
        raise GuardError(
            f"PR modifies paths outside FrozenContractV1 allowed_paths: {', '.join(outside_scope[:10])}"
        )

    # Compute authoritative code_tree_sha256
    actual_code_hash = compute_code_tree_sha256(
        repo, resolved_head, run_id=run_id, tree_entries=head_entries_cached
    )

    if run_data.get("pending_contract_amendments"):
        raise GuardError("enrolled PR has unresolved contract amendment requests")

    if mode == "preflight":
        result = {
            "verdict": "enrolled-preflight",
            "enrolled": True,
            "run_id": run_id,
            "contract_revision": contract_rev,
            "pr_number": pr_number,
            "base_sha": resolved_base,
            "head_sha": resolved_head,
            "code_tree_sha256": actual_code_hash,
            "merge_tree_oid": merge_tree_oid,
            "shard_checks": shard_checks_payload,
        }
        if shadow_verification:
            auto_merge_approved = None
            if contract.get("merge_mode") == "auto" or "allow-auto-merge" in label_set:
                auto_merge_approved = (
                    "allow-auto-merge" in label_set
                    and _has_owner_label_event(pr_events, "allow-auto-merge", trusted_owners)
                    and _has_owner_comment_match(
                        pr_comments, AUTO_MERGE_APPROVAL_RE, (str(pr_number), resolved_head), trusted_owners,
                    )
                )
            envelope = shadow_envelope or verification_comment_envelope(pr_comments, trusted_owners)
            for target in ("integrated", "merge_eligibility"):
                result[f"shadow_{target}"] = verify_phase2(
                    repo, run_data, contract, obligation=target, head=resolved_head, base=resolved_base,
                    envelope=envelope, auto_merge_approved=auto_merge_approved,
                )
        return result

    # --- Finalize Mode: Independent Shard Results, Review & Safeguard Artifacts ---
    if candidate_checks_result is not None and candidate_checks_result != "success":
        raise GuardError(
            f"enrolled PR requires passing autoloop-candidate-checks (got {candidate_checks_result!r})"
        )
    if trusted_shard_results is None:
        raise GuardError(
            "enrolled PR finalize requires per-shard trusted_shard_results attestation matching FrozenContractV1"
        )
    shard_exec_verified = all(
        trusted_shard_results.get(sid) == actual_code_hash for sid in contract_shards
    )
    if not shard_exec_verified:
        raise GuardError("trusted_shard_results missing or stale for one or more FrozenContractV1 shards")

    for sid, rshard in run_shards.items():
        if rshard.get("status") != "passed":
            raise GuardError(f"shard {sid!r} is not passed (status={rshard.get('status')!r})")
        if rshard.get("passed_code_tree_sha256") != actual_code_hash:
            raise GuardError(
                f"shard {sid!r} evidence stale: passed_code_tree_sha256={rshard.get('passed_code_tree_sha256')!r} "
                f"!= actual {actual_code_hash!r}"
            )

    # Verify terminal state & findings safeguarded
    if run_data.get("status") != "ready_to_merge":
        raise GuardError(f"enrolled PR run status must be 'ready_to_merge', got {run_data.get('status')!r}")
    open_mismatches = open_oracle_mismatches(run_data, actual_code_hash)
    if open_mismatches:
        raise GuardError(f"unreviewed oracle mismatches: {', '.join(sorted(open_mismatches))}")
    for mismatch in run_data.get("oracle_mismatches", {}).values():
        if mismatch["test"] not in merge_file_map:
            raise GuardError(f"oracle mismatch test {mismatch['test']!r} is missing from the candidate tree")

    # On the default branch (`main`), native GitHub Ruleset (`required_review_thread_resolution: true`) enforces
    # conversation resolution without deadlocking CI on thread-resolution events. On non-default/stacked target
    # branches lacking that ruleset, fail closed on unresolved threads here.
    default_branch = str(config.get("default_branch") or "main")
    is_default_target = (
        (base_branch == default_branch) if base_branch is not None else (resolved_base == resolved_main)
    )
    if github_client is not None and hasattr(github_client, "get_pr_review_threads"):
        pr_review_threads = github_client.get_pr_review_threads(pr_number)
    unresolved_threads = [t for t in pr_review_threads if not t.get("isResolved")]
    if not is_default_target and unresolved_threads:
        raise GuardError(
            f"stacked enrolled PR targeting non-default branch has {len(unresolved_threads)} unresolved review thread(s)"
        )

    # Cross-check independent owner-declared findings + run findings + safeguard artifacts on tree
    findings = run_data.get("findings") or {}
    declared_fids: set[str] = set()
    for comment in pr_comments:
        if _is_comment_owner_authentic(comment, trusted_owners):
            for fmatch in FINDINGS_LIST_RE.finditer(str(comment.get("body") or "")):
                if int(fmatch.group(1)) == int(pr_number):
                    for raw_fid in fmatch.group(2).split(","):
                        if raw_fid.strip():
                            declared_fids.add(raw_fid.strip())

    missing_fids = sorted(fid for fid in declared_fids if fid not in findings)
    if missing_fids:
        raise GuardError(
            f"findings declared on PR are missing from run checkpoint: {', '.join(missing_fids)}"
        )

    unsafeguarded = [
        fid for fid, fdata in findings.items() if (fdata or {}).get("status") != "safeguarded"
    ]
    if unsafeguarded:
        raise GuardError(f"enrolled PR has unsafeguarded findings: {', '.join(sorted(unsafeguarded))}")

    changed_pr_paths = {e.path for e in target_diff}
    for fid, fdata in sorted(findings.items()):
        _verify_safeguard_artifact(
            repo,
            resolved_head,
            merge_file_map,
            run_id,
            fid,
            fdata or {},
            max_prose_lines,
            changed_paths=changed_pr_paths,
        )

    # Verify independent review evidence unless reviewers explicitly disabled in FrozenContractV1
    reviewers_cfg = contract.get("reviewers") or {}
    cloud_enabled = bool(reviewers_cfg.get("cloud", True))
    deep_enabled = bool(reviewers_cfg.get("deep", True))
    if cloud_enabled or deep_enabled or run_data.get("oracle_mismatches"):
        committers = {pr_author}
        for c in pr_commits_api:
            if c.get("author_login"):
                committers.add(str(c["author_login"]))
            if c.get("committer_login"):
                committers.add(str(c["committer_login"]))

        effective_reviews = _resolve_effective_reviews(pr_reviews)
        blocking_reviewers = sorted(
            user
            for user, info in effective_reviews.items()
            if user not in committers and info["state"] == "CHANGES_REQUESTED"
        )
        if blocking_reviewers:
            raise GuardError(
                f"review changes requested by independent reviewer(s): {', '.join(blocking_reviewers)}"
            )

        has_gh_approval = any(
            user not in committers
            and info["state"] == "APPROVED"
            and info["commit_id"] == resolved_head
            for user, info in effective_reviews.items()
        )
        has_owner_review_comment = _has_owner_comment_match(
            pr_comments,
            REVIEW_APPROVAL_RE,
            (str(pr_number), str(contract_rev), resolved_base, actual_code_hash),
            trusted_owners,
        )
        if not (has_gh_approval or has_owner_review_comment):
            raise GuardError(
                "missing independently authenticated review approval for "
                f"(pr={pr_number}, rev={contract_rev}, base_sha={resolved_base}, code_tree_sha256={actual_code_hash})"
            )

    # Verify allow-auto-merge authorization if requested
    if contract.get("merge_mode") == "auto" or "allow-auto-merge" in label_set:
        if "allow-auto-merge" not in label_set or not _has_owner_label_event(
            pr_events, "allow-auto-merge", trusted_owners
        ):
            raise GuardError("auto-merge requires owner-applied 'allow-auto-merge' label")
        if not _has_owner_comment_match(
            pr_comments,
            AUTO_MERGE_APPROVAL_RE,
            (str(pr_number), resolved_head),
            trusted_owners,
        ):
            raise GuardError("auto-merge requires owner-authored unedited approval comment for current head_sha")

    return {
        "verdict": "enrolled-pass",
        "enrolled": True,
        "run_id": run_id,
        "contract_revision": contract_rev,
        "pr_number": pr_number,
        "base_sha": resolved_base,
        "head_sha": resolved_head,
        "code_tree_sha256": actual_code_hash,
        "merge_tree_oid": merge_tree_oid,
        "shard_checks": shard_checks_payload,
        "unresolved_review_threads": len(unresolved_threads),
    }
