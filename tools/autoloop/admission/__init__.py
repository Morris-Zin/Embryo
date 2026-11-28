"""Shadow admission ledger. Only the trusted coordinator supplies bounds and usage.

The caller persists begin_dispatch before sending the request, then attaches the
task ID. A lost response must be reconciled by request ID before replacement.
Stopped dispatches remain visible for cancellation, including late attachments.
The receipts broker will own actual sends and the publication fence.

The shadow ledger accepts only per-delegation ceilings supplied by the trusted
coordinator. A route backed only by a provider/account quota is excluded.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from graphlib import CycleError, TopologicalSorter
import json
import math
from pathlib import Path
import time
from typing import Any

from ..leases import (
    LeaseConflictError,
    LeaseError,
    acquire_lease,
    acquire_mutex,
    atomic_write_json,
    coordinator_store_dir,
    read_lease,
    release_lease,
    resource_key_hash,
    resource_lease_key,
    resource_lock_dir,
)
from ..state import DEFAULT_MAX_REPLAN_COUNT, DEFAULT_MAX_TOTAL_ATTEMPTS, DEFAULT_MAX_WORKER_STRIKES


# Receipts use "implementation"; the shadow scheduler also accepts worker labels.
_IMPLEMENTATION_ROLES = {"implementation", "implementer", "worker"}


class AdmissionError(LeaseError):
    """Invalid admission configuration or transition."""


@dataclass(frozen=True)
class Usage:
    """Integer tokens, millionths of the campaign currency, and wall milliseconds."""

    tokens: int
    cost_micros: int
    wall_ms: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in asdict(self).values()):
            raise AdmissionError("usage must be known non-negative integers")


@dataclass(frozen=True)
class EnforcedBounds:
    """Worst case for one delegation, supported by trusted enforcement references.

    The token reference must cover all billable input and output over every turn
    of the delegation. The cost reference must cover those tokens and any other
    billable work. A wall deadline cannot substitute for either reference. The
    wall cap covers send and execution from begin_dispatch, including any provider
    retries or delays. Use the same currency throughout a campaign.
    """

    limits: Usage
    token_ceiling: str | None
    cost_ceiling: str | None
    wall_ceiling: str | None
    expires_at: float

    def known(self) -> bool:
        return (
            isinstance(self.limits, Usage)
            and self.limits.wall_ms > 0
            and all(isinstance(ref, str) and ref.strip() for ref in (
                self.token_ceiling, self.cost_ceiling, self.wall_ceiling,
            ))
            and self.token_ceiling != self.wall_ceiling
            and self.cost_ceiling != self.wall_ceiling
        )


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str | None = None
    reservation_id: str | None = None


def _identifier(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _deadline(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise AdmissionError("deadline must be a finite positive timestamp")
    return value


def _maximum(old: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    return {unit: max(value, new[unit]) for unit, value in old.items()}


def _exceeds(usage: dict[str, int], limits: dict[str, int]) -> bool:
    return any(value > limits[unit] for unit, value in usage.items())


def _worst_case(entry: dict[str, Any]) -> dict[str, int]:
    observed = entry["observed"]
    return _maximum(entry["limits"], observed) if observed is not None else entry["limits"]


class Controller:
    """One ledger per stable repository ID, shared across clones on the host."""

    def __init__(self, repo_id: str, *, _lock_dir: Path | None = None) -> None:
        if not _identifier(repo_id):
            raise AdmissionError("a stable repository ID is required")
        self._locks = (_lock_dir if _lock_dir is not None else resource_lock_dir()).resolve()
        root = coordinator_store_dir("admission", lock_dir=self._locks).resolve()
        key = resource_key_hash(repo_id)
        self.path = root / f"{key}.json"
        self._mutex = root / f"{key}.mutex"
        self.repo_id = repo_id
        if any((parent / ".git").exists() for parent in (root, *root.parents, self._locks)):
            raise AdmissionError("the admission authority must be outside Git worktrees")

    @contextmanager
    def _transaction(self) -> Iterator[tuple[dict[str, Any], float, list[dict[str, Any]]]]:
        with acquire_mutex(self._mutex):
            try:
                data = json.loads(self.path.read_bytes())
            except FileNotFoundError:
                data = {"schema": 1, "campaigns": {}, "runs": {}, "reservations": {}, "requests": {}}
            if data["schema"] != 1:
                raise AdmissionError("unsupported admission ledger schema")
            now = time.time()
            self._refresh(data, now)
            acquired: list[dict[str, Any]] = []
            try:
                yield data, now, acquired
                releases = []
                for entry in data["reservations"].values():
                    if entry["dispatch"] in {"settled", "not_run"}:
                        releases.extend(entry["leases"])
                        entry["leases"] = []
                atomic_write_json(self.path, data)
            except BaseException:
                # Any lease acquired by an uncommitted reservation can be dropped.
                self._release(acquired)
                raise
            self._release(releases)

    def _release(self, leases: list[dict[str, Any]]) -> None:
        for lease in leases:
            try:
                release_lease(
                    lease["resource"], lease["owner"], token=lease["token"], lock_dir=self._locks,
                )
            except (LeaseError, OSError):
                # A replacement holder keeps its lease. An unavailable store can
                # reclaim the abandoned hold at its deadline.
                continue

    @staticmethod
    def _register(records: dict[str, Any], key: str, config: dict[str, Any]) -> None:
        if not _identifier(key):
            raise AdmissionError("campaign and run IDs must be non-empty")
        existing = records.setdefault(key, {"config": config, "terminal_reason": None})
        if existing["config"] != config:
            raise AdmissionError(f"configuration already frozen for {key!r}")

    def register_campaign(self, campaign: str, limits: Usage, *, expires_at: float) -> None:
        if not isinstance(limits, Usage):
            raise AdmissionError("campaign limits are required")
        with self._transaction() as (data, _, _):
            self._register(data["campaigns"], campaign, {"limits": asdict(limits), "expires_at": _deadline(expires_at)})

    def register_run(
        self, run: str, campaign: str, limits: Usage, *, expires_at: float,
        dag: Mapping[str, tuple[str, ...]], resources: Mapping[str, tuple[str, ...]] | None = None,
        max_worker_strikes: int = DEFAULT_MAX_WORKER_STRIKES, max_replans: int = DEFAULT_MAX_REPLAN_COUNT,
        max_total_attempts: int = DEFAULT_MAX_TOTAL_ATTEMPTS,
    ) -> None:
        resources = resources or {}
        if not isinstance(limits, Usage):
            raise AdmissionError("run limits are required")
        if not dag or not all(_identifier(shard) for shard in dag):
            raise AdmissionError("the shard DAG must have named nodes")
        if set(resources) - dag.keys() or any(dep not in dag for deps in dag.values() for dep in deps):
            raise AdmissionError("unknown shard in dependency or resource configuration")
        try:
            TopologicalSorter(dag).prepare()
        except CycleError as exc:
            raise AdmissionError("the shard DAG contains a cycle") from exc
        lease_keys = {}
        for shard, names in resources.items():
            if any(not _identifier(name) for name in names):
                raise AdmissionError("resource names must be non-empty")
            lease_keys[shard] = sorted({resource_lease_key(name) for name in names})
        for value, lower, upper in (
            (max_worker_strikes, 1, DEFAULT_MAX_WORKER_STRIKES),
            (max_replans, 0, DEFAULT_MAX_REPLAN_COUNT),
            (max_total_attempts, 1, DEFAULT_MAX_TOTAL_ATTEMPTS),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise AdmissionError("retry limits must stay within existing upper bounds")
        config = {
            "campaign": campaign, "limits": asdict(limits), "expires_at": _deadline(expires_at),
            "dag": {shard: sorted(set(deps)) for shard, deps in dag.items()}, "resources": lease_keys,
            "max_worker_strikes": max_worker_strikes, "max_replans": max_replans,
            "max_total_attempts": max_total_attempts,
        }
        with self._transaction() as (data, _, _):
            if campaign not in data["campaigns"]:
                raise AdmissionError("unknown campaign")
            self._register(data["runs"], run, config)
            data["runs"][run].setdefault("replans", 0)
            data["runs"][run].setdefault("strike_epochs", {})

    @staticmethod
    def _stop(scope: dict[str, Any], reason: str, now: float) -> None:
        if scope["terminal_reason"] is None:
            scope.update(terminal_reason=reason, stopped_at=now)

    @staticmethod
    def _entries(data: dict[str, Any], scope: str, key: str) -> list[dict[str, Any]]:
        return [entry for entry in data["reservations"].values() if entry[scope] == key]

    @staticmethod
    def _usage(entries: list[dict[str, Any]], *, charged_only: bool = False) -> dict[str, int]:
        totals = asdict(Usage(0, 0, 0))
        for entry in entries:
            usage = entry["charge"]
            if usage is None:
                if charged_only:
                    continue
                usage = entry["limits"]
            for unit, value in usage.items():
                totals[unit] += value
        return totals

    def _refresh(self, data: dict[str, Any], now: float) -> None:
        for scope in ("campaign", "run"):
            for key, record in data[f"{scope}s"].items():
                if now >= record["config"]["expires_at"]:
                    self._stop(record, "quota_expired", now)
                    continue
                spent = self._usage(self._entries(data, scope, key), charged_only=True)
                if any(cap > 0 and spent[unit] >= cap for unit, cap in record["config"]["limits"].items()):
                    self._stop(record, "exhausted", now)
        for entry in data["reservations"].values():
            if entry["dispatch"] in {"dispatching", "attached"} and now >= entry["dispatch_deadline"]:
                self._stop(data["runs"][entry["run"]], "stalled", now)
        for entry in data["reservations"].values():
            if entry["dispatch"] in {"settled", "not_run"} or entry["terminal_reason"]:
                continue
            reason = self._reason(data, entry["run"])
            if not reason and now >= entry["expires_at"]:
                reason = entry["expiry_reason"]
            if not reason:
                continue
            entry["terminal_reason"] = reason
            if entry["dispatch"] == "reserved":
                entry.update(dispatch="not_run", charge=asdict(Usage(0, 0, 0)))
            else:
                entry["charge"] = _worst_case(entry)

    @staticmethod
    def _reason(data: dict[str, Any], run: str) -> str | None:
        record = data["runs"][run]
        campaign = data["campaigns"][record["config"]["campaign"]]
        return record["terminal_reason"] or campaign["terminal_reason"]

    def reserve(
        self, *, run: str, shard: str, role: str, generation: int, request_id: str,
        bounds: EnforcedBounds, expires_at: float,
    ) -> Decision:
        """Hold budget and leases for one request and shard-role generation."""
        if not all(_identifier(value) for value in (run, shard, role, request_id)):
            raise AdmissionError("reservation bindings must be non-empty")
        if not isinstance(bounds, EnforcedBounds) or not bounds.known():
            return Decision("denied", "unenforceable_bounds")
        limits = asdict(bounds.limits)
        with self._transaction() as (data, now, acquired):
            if reason := self._reason(data, run):
                return Decision("denied", reason)
            record = data["runs"][run]
            config = record["config"]
            campaign = config["campaign"]
            campaign_config = data["campaigns"][campaign]["config"]
            quota_deadline = min(_deadline(bounds.expires_at), config["expires_at"], campaign_config["expires_at"])
            deadline = min(_deadline(expires_at), quota_deadline)
            if bounds.limits.wall_ms >= (deadline - now) * 1000:
                return Decision("denied", "quota_expired" if deadline == quota_deadline else "reservation_expired")
            if shard not in config["dag"]:
                raise AdmissionError("unknown shard")
            entries = self._entries(data, "run", run)
            shard_entries = [entry for entry in entries if entry["shard"] == shard]
            previous = [entry for entry in shard_entries if entry["role"] == role]
            if (type(generation) is not int or generation != len(previous) + 1
                    or request_id in data["requests"]):
                return Decision("denied", "duplicate_or_out_of_order")
            is_implementation = role in _IMPLEMENTATION_ROLES
            active = [entry for entry in shard_entries if entry["dispatch"] in {"reserved", "dispatching", "attached"}]
            if any(
                entry["role"] == role or (is_implementation and entry["role"] in _IMPLEMENTATION_ROLES)
                for entry in active
            ):
                return Decision("waiting", "pending_reservation")
            passed = {entry["shard"] for entry in entries
                      if entry["role"] in _IMPLEMENTATION_ROLES and entry.get("accepted")}
            if is_implementation and shard in passed:
                return Decision("denied", "shard_passed")
            if any(entry.get("accepted") for entry in previous):
                return Decision("denied", "role_passed")
            dependencies = set(config["dag"][shard])
            if not is_implementation:
                dependencies.add(shard)
            if dependencies - passed:
                return Decision("waiting", "dependencies")
            strike_epoch = record["strike_epochs"].get(shard, 0)
            strikes = sum(entry.get("strike", False) for entry in shard_entries if entry["replan"] == strike_epoch)
            if strikes >= config["max_worker_strikes"]:
                return Decision("denied", "strike_limit")
            if strikes + len(active) >= config["max_worker_strikes"]:
                return Decision("waiting", "strike_reserved")
            attempt_slots = sum(entry.get("strike", False) or entry["dispatch"] in {
                "reserved", "dispatching", "attached",
            } for entry in entries)
            if attempt_slots >= config["max_total_attempts"]:
                return Decision("denied", "attempt_limit")
            scopes = ((entries, config["limits"]),
                      (self._entries(data, "campaign", campaign), campaign_config["limits"]))
            for charged_only in (True, False):
                for scope_entries, cap in scopes:
                    used = self._usage(scope_entries, charged_only=charged_only)
                    if _exceeds({unit: used[unit] + value for unit, value in limits.items()}, cap):
                        return Decision("denied" if charged_only else "waiting",
                                        "budget_limit" if charged_only else "budget_reserved")

            reservation_id = resource_key_hash(json.dumps([self.repo_id, run, shard, role, generation]))
            try:
                for resource in config["resources"].get(shard, ()):
                    acquired.append(acquire_lease(
                        resource, reservation_id, ttl_s=deadline - now, lock_dir=self._locks,
                    ))
            except LeaseConflictError:
                self._release(acquired)
                acquired.clear()
                return Decision("waiting", "resource_lease")
            data["requests"][request_id] = reservation_id
            data["reservations"][reservation_id] = {
                "reservation_id": reservation_id, "repo": self.repo_id, "campaign": campaign, "run": run,
                "shard": shard, "role": role, "generation": generation, "request_id": request_id,
                "limits": limits, "enforcement": asdict(bounds), "reserved_at": now, "expires_at": deadline,
                "expiry_reason": "quota_expired" if deadline == quota_deadline else "reservation_expired",
                "dispatch": "reserved", "task_id": None, "observed": None,
                "charge": None, "terminal_reason": None, "leases": acquired.copy(), "replan": strike_epoch,
            }
            return Decision("reserved", reservation_id=reservation_id)

    def begin_dispatch(self, reservation_id: str) -> Decision:
        with self._transaction() as (data, now, _):
            entry = data["reservations"][reservation_id]
            if entry["dispatch"] != "reserved":
                return Decision("denied", entry["terminal_reason"] or "already_dispatched", reservation_id)
            deadline = now + entry["limits"]["wall_ms"] / 1000
            if deadline >= entry["expires_at"]:
                reason = entry["expiry_reason"]
                entry.update(dispatch="not_run", terminal_reason=reason, charge=asdict(Usage(0, 0, 0)))
                return Decision("denied", reason, reservation_id)
            for lease in entry["leases"]:
                current = read_lease(lease["resource"], lock_dir=self._locks)
                if not current or current.get("token") != lease["token"] or current["expires_at"] <= deadline:
                    entry.update(dispatch="not_run", terminal_reason="lease_lost", charge=asdict(Usage(0, 0, 0)))
                    return Decision("waiting", "resource_lease", reservation_id)
            entry.update(dispatch="dispatching", dispatch_started_at=now, dispatch_deadline=deadline)
            return Decision("dispatching", reservation_id=reservation_id)

    def attach(self, reservation_id: str, task_id: str) -> Decision:
        if not _identifier(task_id):
            raise AdmissionError("a task ID is required")
        with self._transaction() as (data, _, _):
            entry = data["reservations"][reservation_id]
            if entry["dispatch"] == "settled" and entry["task_id"] == task_id:
                return Decision("settled", entry["terminal_reason"], reservation_id)
            if entry["dispatch"] not in {"dispatching", "attached"} or entry["task_id"] not in (None, task_id):
                raise AdmissionError("reservation cannot attach this task")
            entry.update(dispatch="attached", task_id=task_id)
            if entry["terminal_reason"]:
                return Decision("cancel", entry["terminal_reason"], reservation_id)
            return Decision("attached", reservation_id=reservation_id)

    def stop(self, reason: str, *, run: str | None = None, campaign: str | None = None) -> None:
        """Fence a run or campaign. Inspect its snapshot for tasks to cancel or reconcile."""
        if reason not in {"killed", "stalled", "exhausted"} or (run is None) == (campaign is None):
            raise AdmissionError("choose one scope and a kill, stall or exhaustion reason")
        with self._transaction() as (data, now, _):
            scope = data["runs"][run] if run is not None else data["campaigns"][campaign]
            self._stop(scope, reason, now)
            self._refresh(data, now)

    def observe(self, reservation_id: str, usage: Usage) -> None:
        """Retain cumulative spend even when an in-flight task has been fenced."""
        with self._transaction() as (data, now, _):
            entry = data["reservations"][reservation_id]
            if entry["dispatch"] not in {"dispatching", "attached"}:
                raise AdmissionError("usage requires a dispatch attempt")
            self._observe(data, entry, asdict(usage), now)
            self._refresh(data, now)

    def _observe(self, data: dict[str, Any], entry: dict[str, Any], usage: dict[str, int], now: float) -> None:
        entry["observed"] = _maximum(entry["observed"], usage) if entry["observed"] is not None else usage
        if entry["charge"] is not None:
            entry["charge"] = _maximum(entry["charge"], entry["observed"])
        if _exceeds(entry["observed"], entry["limits"]):
            entry["bound_exceeded"] = True
            self._stop(data["campaigns"][entry["campaign"]], "bound_exceeded", now)

    def settle(
        self, reservation_id: str, *, outcome: str, actual: Usage | None = None,
        no_dispatch_evidence_id: str | None = None,
    ) -> Decision:
        """Record a final result, or release a reserved request with outcome not_run.

        Once dispatch begins, a not_run settlement requires external evidence.
        Missing usage for a started request keeps the worst-case charge.
        """
        if outcome == "not_run":
            if actual is not None or (no_dispatch_evidence_id is not None
                                      and not _identifier(no_dispatch_evidence_id)):
                raise AdmissionError("invalid not_run settlement")
        elif outcome not in {"accepted", "failed", "cancelled"} or no_dispatch_evidence_id is not None:
            raise AdmissionError("invalid settlement outcome")
        settlement = {"outcome": outcome, "actual": asdict(actual) if actual is not None else None,
                      "no_dispatch_evidence_id": no_dispatch_evidence_id}
        with self._transaction() as (data, now, _):
            entry = data["reservations"][reservation_id]
            if "settlement" in entry:
                if entry["settlement"] != settlement:
                    raise AdmissionError("reservation already has a different settlement")
                return Decision("settled", entry["terminal_reason"], reservation_id)
            if outcome == "not_run" and entry["dispatch"] == "reserved":
                reason = "cancelled_before_dispatch"
                entry.update(
                    dispatch="not_run", settled_at=now, settlement=settlement, charge=asdict(Usage(0, 0, 0)),
                    terminal_reason=reason, accepted=False, strike=False,
                )
                return Decision("settled", reason, reservation_id)
            if entry["dispatch"] not in {"dispatching", "attached"}:
                raise AdmissionError("settlement requires a dispatch attempt")
            if outcome == "not_run" and no_dispatch_evidence_id is None:
                raise AdmissionError("an uncertain send requires evidence of no dispatch")
            if outcome == "not_run" and (entry["task_id"] or any((entry["observed"] or {}).values())):
                raise AdmissionError("confirmed work cannot settle as not_run")
            if outcome != "not_run" and entry["dispatch"] != "attached":
                raise AdmissionError("attach a confirmed task before settling")
            if actual is not None:
                self._observe(data, entry, settlement["actual"], now)
            reason = entry["terminal_reason"] or self._reason(data, entry["run"])
            if outcome == "not_run":
                charge = asdict(Usage(0, 0, 0))
            elif actual is not None:
                charge = entry["observed"]
            else:
                charge = _worst_case(entry)
            entry.update(
                dispatch="settled", settled_at=now, settlement=settlement, charge=charge, terminal_reason=reason,
                accepted=outcome == "accepted" and reason is None, strike=outcome == "failed" and reason is None,
            )
            self._refresh(data, now)
            return Decision("settled", reason, reservation_id)

    def replan(self, run: str, *, shard: str | None = None) -> None:
        """Reset the strike round after the existing coordinator authorizes a replan."""
        with self._transaction() as (data, _, _):
            record = data["runs"][run]
            config = record["config"]
            entries = self._entries(data, "run", run)
            if self._reason(data, run) or record["replans"] >= config["max_replans"]:
                raise AdmissionError("run cannot replan")
            if any(entry["dispatch"] in {"reserved", "dispatching", "attached"} for entry in entries):
                raise AdmissionError("settle current reservations before replanning")
            eligible = {
                node for node in config["dag"]
                if sum(entry.get("strike", False) for entry in entries
                       if entry["shard"] == node and entry["replan"] == record["strike_epochs"].get(node, 0))
                >= config["max_worker_strikes"]
            }
            targets = eligible if shard is None else {shard}
            if not targets or targets - eligible:
                raise AdmissionError("no shard has reached the strike limit")
            record["replans"] += 1
            record["strike_epochs"].update(dict.fromkeys(targets, record["replans"]))

    def snapshot(self) -> dict[str, Any]:
        """Refresh deadlines and return accounting plus pending cancellation state."""
        with self._transaction() as (data, _, _):
            return data
