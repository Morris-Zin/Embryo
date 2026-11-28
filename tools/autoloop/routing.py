"""Append-only local attempt evidence and route summaries."""

from __future__ import annotations

from collections import defaultdict
import json
import math
import os
from pathlib import Path
from statistics import median
from typing import Any

from .guard import GuardError
from .leases import acquire_mutex

REQUIRED_FIELDS = ("task_id", "attempt")
NUMBER_FIELDS = ("cost", "review_minutes", "elapsed_to_acceptance_minutes")
OPTIONAL_FIELDS = (
    "task_class", "difficulty", "provider", "model", "effort", "speed", "disposition",
    "checks_passed", "evidence_id", *NUMBER_FIELDS,
)
MIN_ROUTE_SAMPLES = 5


def attempt_log_path(repo: Path, override: Path | None) -> Path:
    if override is None:
        if os.name == "nt":
            root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
            override = root / "KhitmeePerf/agent-attempts.jsonl"
        else:
            root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
            override = root / "khitmee-perf/agent-attempts.jsonl"
    path = override.resolve()
    if path.is_relative_to(repo.resolve()) or any((p / ".git").exists() for p in path.parents):
        raise GuardError("agent attempt log must be outside the repository and all Git worktrees")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise GuardError("agent attempt log must be a regular file with exactly one link")
    return path


def _read_attempts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def record_attempt(repo: Path, override: Path | None, **values: Any) -> dict[str, Any]:
    if not isinstance(values.get("attempt"), int) or values["attempt"] < 1:
        raise GuardError("attempt must be a positive integer")
    if not str(values.get("task_id") or "").strip():
        raise GuardError("attempt requires task_id")
    for field in NUMBER_FIELDS:
        value = values.get(field)
        if value is not None and (not math.isfinite(value) or value < 0):
            raise GuardError(f"{field} must be finite and non-negative")
    record = {key: values.get(key) for key in (*REQUIRED_FIELDS, *OPTIONAL_FIELDS)}
    path = attempt_log_path(repo, override)
    mutex = path.with_name(f".{path.name}.lock")
    with acquire_mutex(mutex):
        for existing in _read_attempts(path):
            if (existing["task_id"], existing["attempt"]) == (record["task_id"], record["attempt"]):
                if existing != record:
                    raise GuardError("task_id and attempt already recorded with different evidence")
                return {"log": str(path), "record": record}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            if os.fstat(handle.fileno()).st_nlink != 1:
                raise GuardError("agent attempt log must have exactly one link")
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return {"log": str(path), "record": record}


def routing_report(repo: Path, override: Path | None) -> dict[str, Any]:
    path = attempt_log_path(repo, override)
    if not path.exists():
        return {"log": str(path), "minimum_first_attempts": MIN_ROUTE_SAMPLES, "routes": []}
    mutex = path.with_name(f".{path.name}.lock")
    with acquire_mutex(mutex):
        attempts = _read_attempts(path)
    first_acceptance = {}
    by_route = defaultdict(list)
    for row in attempts:
        task_id = row["task_id"]
        if row.get("disposition") == "accepted" and (
            task_id not in first_acceptance or row["attempt"] < first_acceptance[task_id]["attempt"]
        ):
            first_acceptance[task_id] = row
        by_route[row["task_class"], row["model"]].append(row)
    routes = []
    for (task_class, model), rows in sorted(by_route.items(), key=lambda item: tuple(v or "" for v in item[0])):
        first = [row for row in rows if row["attempt"] == 1]
        accepted_first = sum(row.get("disposition") == "accepted" for row in first)
        accepted_eventually = [first_acceptance[row["task_id"]] for row in first if row["task_id"] in first_acceptance]
        times = [
            row["elapsed_to_acceptance_minutes"] for row in accepted_eventually
            if row.get("elapsed_to_acceptance_minutes") is not None
        ]
        costs = [row["cost"] for row in rows if row.get("cost") is not None]
        routes.append({
            "task_class": task_class, "model": model, "attempts": len(rows), "first_attempts": len(first),
            "first_attempt_acceptance": accepted_first / len(first) if first else None,
            "eventual_acceptance": len(accepted_eventually) / len(first) if first else None,
            "median_elapsed_to_acceptance_minutes": median(times) if times else None,
            "elapsed_samples": len(times), "spend": math.fsum(costs) if costs else None,
            "cost_samples": len(costs), "too_few_samples": len(first) < MIN_ROUTE_SAMPLES,
        })
    return {"log": str(path), "minimum_first_attempts": MIN_ROUTE_SAMPLES, "routes": routes}
