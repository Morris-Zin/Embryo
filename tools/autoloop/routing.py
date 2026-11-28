"""Append-only local attempt evidence and route summaries."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from .guard import GuardError
from .leases import _is_wsl, acquire_mutex
from .receipts import Broker, authority_dir, checkout_repo_id

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


def _receipt_attempts(repo: Path) -> list[dict[str, Any]]:
    if _is_wsl():
        return []  # Legacy logs still work in WSL; read receipts using Windows Python.
    root = authority_dir()
    if not root.is_dir():
        return []
    repo_id = checkout_repo_id(repo)
    if repo_id is None:
        return []
    rows = []
    settled = [
        (record, receipt) for record, receipt in Broker(repo_id, _root=root).settled()
        if "cache_key_digest" not in record
    ]
    first_reserved = {
        (record["run"], record["shard"], record["role"]): record["reserved_at"]
        for record, _ in settled if record["generation"] == 1
    }
    for record, receipt in settled:
        assignment = (record["run"], record["shard"], record["role"])
        usage = receipt["usage"]
        disposition = receipt["disposition"]
        if disposition == "accepted" and receipt["publication"] != "published":
            disposition = receipt["publication"]
        elapsed = None
        started = first_reserved.get(assignment)
        ended = receipt["ended_at"]
        if disposition == "accepted" and started is not None and ended is not None and ended >= started:
            elapsed = (ended - started) / 60
        worker = receipt["worker"] or {}
        settings = record["settings"]
        rows.append({
            "task_id": assignment, "attempt": record["generation"],
            "task_class": record["task_class"], "difficulty": record["difficulty"],
            "provider": worker.get("provider") or record["provider"],
            "model": worker.get("model") or record["model"],
            "effort": settings.get("effort"), "speed": settings.get("speed"),
            "disposition": disposition, "checks_passed": None, "evidence_id": receipt["receipt_id"],
            "cost": usage.get("cost") if usage["source"] == "provider" else None,
            "estimated_cost": usage.get("cost") if usage["source"] == "estimated" else None,
            "review_minutes": None, "elapsed_to_acceptance_minutes": elapsed,
        })
    return rows


def routing_report(repo: Path, override: Path | None) -> dict[str, Any]:
    path = attempt_log_path(repo, override)
    attempts = []
    if path.exists():
        mutex = path.with_name(f".{path.name}.lock")
        with acquire_mutex(mutex):
            historical = {}
            for record in _read_attempts(path):
                digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode("ascii")).hexdigest()
                historical.setdefault(digest, record)
            attempts = list(historical.values())
    attempts.extend(_receipt_attempts(repo))
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
        route = {
            "task_class": task_class, "model": model, "attempts": len(rows), "first_attempts": len(first),
            "first_attempt_acceptance": accepted_first / len(first) if first else None,
            "eventual_acceptance": len(accepted_eventually) / len(first) if first else None,
            "median_elapsed_to_acceptance_minutes": median(times) if times else None,
            "elapsed_samples": len(times), "spend": math.fsum(costs) if costs else None,
            "cost_samples": len(costs), "too_few_samples": len(first) < MIN_ROUTE_SAMPLES,
        }
        estimates = [row["estimated_cost"] for row in rows if row.get("estimated_cost") is not None]
        if estimates:
            route.update(estimated_spend=math.fsum(estimates), estimated_cost_samples=len(estimates))
        routes.append(route)
    return {"log": str(path), "minimum_first_attempts": MIN_ROUTE_SAMPLES, "routes": routes}
