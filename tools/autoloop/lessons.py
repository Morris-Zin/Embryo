"""Small, repo-scoped lessons and matching across shard path patterns."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .guard import GuardError, RUN_ID_RE, _match_segment, _normalize_posix_path, assert_safe_local_path
from .leases import atomic_write_json

LESSON_KINDS = ("dead_end", "review_finding", "safeguard", "perf_insight", "process")
LESSONS_DIR = ".autoloop/lessons"
DRAFTS_DIR = ".autoloop/tmp/lessons"


def _tokens(pattern: str) -> list[str]:
    tokens = []
    start = 0
    while start < len(pattern):
        end = pattern.find("]", start + 1) if pattern[start] == "[" else -1
        stop = end + 1 if end != -1 else start + 1
        tokens.append(pattern[start:stop])
        start = stop
    return tokens


@lru_cache(maxsize=2048)
def _segments_overlap(left: str, right: str) -> bool:
    # Literal and bracket predicates only change at character/range boundaries.
    # Test one representative from each interval, excluding path separators.
    boundaries = {1, ord("/"), ord("/") + 1, ord("\\"), ord("\\") + 1}
    for char in left + right:
        boundaries.update((ord(char), ord(char) + 1))
    alphabet = [chr(n) for n in sorted(boundaries) if 0 < n <= 0x10FFFF and chr(n) not in "/\\"]
    a, b = _tokens(left), _tokens(right)

    @lru_cache(None)
    def visit(i: int, j: int, consumed: bool) -> bool:
        if i == len(a) or j == len(b):
            return consumed and all(t == "*" for t in a[i:] + b[j:])
        x, y = a[i], b[j]
        if x == "*" and visit(i + 1, j, consumed):
            return True
        if y == "*" and visit(i, j + 1, consumed):
            return True
        if x == y == "*":
            return not consumed and visit(i, j, True)
        return any(_match_segment(c, x) and _match_segment(c, y) for c in alphabet) and visit(
            i + (x != "*"), j + (y != "*"), True
        )

    return visit(0, 0, False)


def globs_overlap(left: str, right: str) -> bool:
    """Whether the guard's segment glob syntax admits a common path."""
    left, right = _normalize_posix_path(left), _normalize_posix_path(right)
    if not left or not right:
        return False
    a, b = left.split("/"), right.split("/")

    @lru_cache(None)
    def visit(i: int, j: int) -> bool:
        if i == len(a) or j == len(b):
            return all(p == "**" for p in a[i:] + b[j:])
        x, y = a[i], b[j]
        if x == y == "**":
            return visit(i + 1, j) or visit(i, j + 1)
        if x == "**":
            return visit(i + 1, j) or (_segments_overlap("*", y) and visit(i, j + 1))
        if y == "**":
            return visit(i, j + 1) or (_segments_overlap(x, "*") and visit(i + 1, j))
        return _segments_overlap(x, y) and visit(i + 1, j + 1)

    return visit(0, 0)


def record_lesson(
    repo: Path, run_id: str, *, kind: str, summary: str, evidence: str,
    applies_to: list[str], created_at: str, draft: bool = False,
) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id) or ".." in run_id or kind not in LESSON_KINDS:
        raise GuardError("invalid lesson run_id or kind")
    if not summary.strip() or not evidence.strip() or not applies_to or any(not p.strip() for p in applies_to):
        raise GuardError("lesson requires summary, evidence, and at least one applies_to glob")
    directory = DRAFTS_DIR if draft else LESSONS_DIR
    path = assert_safe_local_path(repo, repo / directory / f"{run_id}-{uuid4().hex}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    lesson = {
        "run_id": run_id, "kind": kind, "summary": summary.strip(), "evidence": evidence.strip(),
        "applies_to": applies_to, "created_at": created_at, "draft": draft,
    }
    atomic_write_json(path, lesson)
    return {**lesson, "file": path.relative_to(repo).as_posix()}


def matching_lessons(repo: Path, paths: list[str]) -> list[dict[str, Any]]:
    lessons = []
    for relative_dir in (LESSONS_DIR, DRAFTS_DIR):
        directory = assert_safe_local_path(repo, repo / relative_dir)
        for path in directory.glob("*.json"):
            assert_safe_local_path(repo, path, allow_missing_leaf=False)
            lesson = json.loads(path.read_text(encoding="utf-8"))
            if not paths or any(globs_overlap(a, b) for a in lesson["applies_to"] for b in paths):
                lessons.append({**lesson, "file": path.relative_to(repo).as_posix()})
    return sorted(lessons, key=lambda item: (item["created_at"], item["file"]), reverse=True)
