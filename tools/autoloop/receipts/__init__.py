"""Host-wide delegation receipts for the autoloop shadow broker."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from ..leases import (
    LeaseError,
    _is_wsl,
    acquire_mutex,
    atomic_write_json,
    resource_lock_dir,
)


class ReceiptError(LeaseError):
    """Invalid or conflicting broker operation."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _fingerprint(value: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), str) and value[key] for key in ("base", "head", "tree")
    ):
        raise ReceiptError("Git fingerprint requires base, head, and tree")
    return value


def _number(value: Any, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ReceiptError(f"{name} must be finite and non-negative")


def authority_dir() -> Path:
    """Share the resource lease authority across checkouts and fail closed on WSL."""
    if _is_wsl():
        raise ReceiptError("WSL receipts must use Windows Python through interop or PowerShell")
    locks = resource_lock_dir()
    return (locks.parent if sys.platform == "win32" else locks) / "receipts"


def checkout_repo_id(checkout: Path) -> str | None:
    """Identify a checkout by its configured origin, independent of the clone path."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode:
        return None
    origin = result.stdout.strip()
    if not origin:
        return None
    if "://" in origin:
        url = urlsplit(origin)
        if url.scheme == "file":
            return (checkout / url2pathname(origin.removeprefix("file:"))).resolve().as_uri()
        if url.hostname:
            host = url.hostname.lower()
            port = url.port
            if port is not None and port != {"https": 443, "http": 80, "ssh": 22}.get(url.scheme):
                host = f"{host}:{port}"
            path = unquote(url.path).strip("/").removesuffix(".git")
            return f"{host}/{path.lower() if host == 'github.com' else path}"
        return None
    ssh = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", origin)
    if ssh and len(ssh[1]) > 1:  # Exclude Windows drive letters.
        host = ssh[1].lower()
        path = ssh[2].strip("/").removesuffix(".git")
        return f"{host}/{path.lower() if host == 'github.com' else path}"
    return (checkout / origin).resolve().as_uri()


class Broker:
    """Transactional reservation and settlement scoped to one stable repository ID."""

    def __init__(self, repo_id: str, *, _root: Path | None = None) -> None:
        if not repo_id.strip():
            raise ReceiptError("a stable repository ID is required")
        root = (_root if _root is not None else authority_dir()).resolve()
        self.repo_id = repo_id
        key = _digest(repo_id)
        self.path = (root / f"{key}.json").resolve()
        self._mutex = root / f"{key}.mutex"
        if any((parent / ".git").exists() for parent in self.path.parents):
            raise ReceiptError("the receipts ledger must be outside Git worktrees")

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return {"schema": 1, "slots": {}, "reservations": {}, "requests": {}, "receipts": {}}
        if data["schema"] != 1:
            raise ReceiptError("unsupported receipts ledger schema")
        return data

    @staticmethod
    def _slot_key(run: str, shard: str, role: str) -> str:
        return _digest([run, shard, role])

    @staticmethod
    def _entry(data: dict[str, Any], reservation_id: str) -> dict[str, Any]:
        try:
            return data["reservations"][reservation_id]
        except KeyError as exc:
            raise ReceiptError("unknown reservation") from exc

    def _slot(self, data: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        return data["slots"][self._slot_key(record["run"], record["shard"], record["role"])]

    def reserve(
        self, *, run: str, shard: str, role: str, generation: int, request_id: str,
        request_digest: str, brief_digest: str, task_class: str, difficulty: str,
        contract_revision: int, contract_hash: str, policy: str, epoch: str,
        git: dict[str, str], provider: str, model: str, settings: dict[str, Any],
        limits: dict[str, Any], expires_at: float,
    ) -> dict[str, Any]:
        """Commit one dispatch reservation. Duplicate keys always lose the race."""
        required = (run, shard, role, request_id, request_digest, brief_digest, task_class,
                    difficulty, contract_hash, policy, epoch, provider, model)
        if not all(isinstance(value, str) and value.strip() for value in required):
            raise ReceiptError("reservation identity and binding fields must be non-empty strings")
        if any(type(value) is not int or value < 1 for value in (generation, contract_revision)):
            raise ReceiptError("generation and contract revision must be positive integers")
        if not isinstance(settings, dict) or not isinstance(limits, dict):
            raise ReceiptError("settings and limits must be objects")
        _number(expires_at, "expiry")
        with acquire_mutex(self._mutex):
            data = self._load()
            key = self._slot_key(run, shard, role)
            slot = data["slots"].get(key, {"generation": 0, "publication": None})
            if generation != slot["generation"] + 1 or request_id in data["requests"]:
                raise ReceiptError("generation or request ID already used, or generation out of sequence")
            if slot["generation"]:
                previous = self._entry(data, slot["reservation_id"])
                if previous["dispatch"] in {"dispatching", "quarantined"}:
                    raise ReceiptError("reconcile the uncertain dispatch before replacing the worker")
            now = time.time()
            if expires_at <= now:
                raise ReceiptError("reservation has already expired")
            reservation_id = _digest([self.repo_id, run, shard, role, generation])
            record = {
                "reservation_id": reservation_id, "repo": self.repo_id, "run": run, "shard": shard,
                "role": role, "generation": generation, "request_id": request_id,
                "request_digest": request_digest, "brief_digest": brief_digest,
                "task_class": task_class, "difficulty": difficulty,
                "contract_revision": contract_revision, "contract_hash": contract_hash,
                "policy": policy, "epoch": epoch, "git": _fingerprint(git), "provider": provider,
                "model": model, "settings": settings, "limits": limits,
                "expires_at": expires_at, "reserved_at": now,
                "expected_publication": slot["publication"],
            }
            record = json.loads(_canonical(record))
            data["reservations"][reservation_id] = {
                "record": record, "dispatch": "reserved", "task_id": None,
                "revocation": None, "receipt_id": None,
            }
            data["requests"][request_id] = reservation_id
            data["slots"][key] = {**slot, "generation": generation, "reservation_id": reservation_id}
            atomic_write_json(self.path, data)
            return record

    def begin_dispatch(self, reservation_id: str) -> None:
        """Persist the send intent before crossing the T3 boundary. Call only once."""
        with acquire_mutex(self._mutex):
            data = self._load()
            entry = self._entry(data, reservation_id)
            record = entry["record"]
            now = time.time()
            if (
                entry["dispatch"] != "reserved" or entry["revocation"]
                or record["expires_at"] <= now
                or self._slot(data, record)["reservation_id"] != reservation_id
            ):
                raise ReceiptError("reservation cannot dispatch")
            entry["dispatch"] = "dispatching"
            entry["dispatch_started_at"] = now
            atomic_write_json(self.path, data)

    def by_request(self, request_id: str) -> dict[str, Any] | None:
        """Recover the reservation and dispatch state using the persisted T3 request ID."""
        if not self.path.exists():
            return None
        with acquire_mutex(self._mutex):
            data = self._load()
            reservation_id = data["requests"].get(request_id)
            return self._entry(data, reservation_id) if reservation_id is not None else None

    def attach(self, reservation_id: str, task_id: str) -> None:
        """Record a confirmed T3 task, including recovery after a lost response."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ReceiptError("a T3 task ID is required")
        with acquire_mutex(self._mutex):
            data = self._load()
            entry = self._entry(data, reservation_id)
            if entry["task_id"] == task_id:
                return
            if entry["dispatch"] not in {"dispatching", "quarantined"}:
                raise ReceiptError("reservation cannot attach this task")
            entry.update(dispatch="attached", task_id=task_id, attached_at=time.time())
            atomic_write_json(self.path, data)

    def revoke(self, reservation_id: str, reason: str) -> None:
        """Fence publication on cancellation or an external binding change."""
        if not reason:
            raise ReceiptError("revocation requires a reason")
        with acquire_mutex(self._mutex):
            data = self._load()
            entry = self._entry(data, reservation_id)
            if entry["revocation"] is None:
                entry["revocation"] = {"reason": reason, "at": time.time()}
                atomic_write_json(self.path, data)

    def _seal(self, data: dict[str, Any], entry: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        record = entry["record"]
        slot = self._slot(data, record)
        now = time.time()
        publication = "not_requested"
        if result["publish"]:
            if slot["reservation_id"] != record["reservation_id"]:
                publication = "superseded"
            elif entry["revocation"]:
                publication = "revoked"
            elif record["expires_at"] <= now:
                publication = "expired"
            elif (slot["publication"] != record["expected_publication"] or
                  result["git_before"] != record["git"] or
                  result["git_after"]["base"] != record["git"]["base"]):
                publication = "stale"
            else:
                publication = "published"
        payload = {
            **result, "reservation_id": record["reservation_id"], "task_id": entry["task_id"],
            "publication": publication, "settled_at": now,
        }
        receipt_id = _digest(payload)
        receipt = {**payload, "receipt_id": receipt_id}
        data["receipts"][receipt_id] = receipt
        entry["receipt_id"] = receipt_id
        if publication == "published":
            slot["publication"] = receipt_id
        atomic_write_json(self.path, data)
        return receipt

    def settle(
        self, reservation_id: str, *, status: str, disposition: str, duration_s: float,
        usage: dict[str, Any], git_before: dict[str, str], git_after: dict[str, str],
        changed_paths: list[str], evidence_ids: list[str], reason: str | None = None,
        started_at: float | None = None, ended_at: float | None = None,
        worker: dict[str, Any] | None = None, publish: bool = False,
    ) -> dict[str, Any]:
        """Keep a result once, whether or not its generation can publish."""
        if status == "not_run":
            raise ReceiptError("use reconcile to record work that did not run")
        if not all(isinstance(value, str) and value.strip() for value in (status, disposition)):
            raise ReceiptError("settlement requires a status and disposition")
        if type(publish) is not bool:
            raise ReceiptError("publish must be a boolean")
        for values in (changed_paths, evidence_ids):
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
                raise ReceiptError("changed paths and evidence IDs must be lists of non-empty strings")
        if worker is not None and (not isinstance(worker, dict) or any(
            not isinstance(worker[key], str) for key in ("provider", "model") if key in worker
        )):
            raise ReceiptError("worker must be an object with optional provider and model names")
        if publish and (status != "completed" or disposition != "accepted" or not evidence_ids):
            raise ReceiptError("publication requires a completed, accepted result with evidence")
        _number(duration_s, "duration")
        for name, value in (("started_at", started_at), ("ended_at", ended_at)):
            if value is not None:
                _number(value, name)
        if started_at is not None and ended_at is not None and ended_at < started_at:
            raise ReceiptError("end precedes start")
        if not isinstance(usage, dict):
            raise ReceiptError("usage must be an object")
        source = usage.get("source")
        cost = usage.get("cost")
        if source not in ("provider", "estimated", "not_observed"):
            raise ReceiptError("usage requires a recognized source")
        if cost is not None:
            _number(cost, "cost")
            if source == "not_observed":
                raise ReceiptError("unobserved usage cannot include a cost")
        result = json.loads(_canonical({
            "status": status, "disposition": disposition, "duration_s": duration_s,
            "usage": usage, "git_before": _fingerprint(git_before), "git_after": _fingerprint(git_after),
            "changed_paths": changed_paths, "evidence_ids": evidence_ids, "reason": reason,
            "started_at": started_at, "ended_at": ended_at, "worker": worker, "publish": publish,
        }))
        with acquire_mutex(self._mutex):
            data = self._load()
            entry = self._entry(data, reservation_id)
            if entry["receipt_id"] is not None:
                existing = data["receipts"][entry["receipt_id"]]
                if _canonical({key: existing[key] for key in result}) != _canonical(result):
                    raise ReceiptError("reservation already has a different settlement")
                return existing
            if entry["dispatch"] != "attached":
                raise ReceiptError("attach a confirmed task before settling")
            return self._seal(data, entry, result)

    def reconcile(self, reservation_id: str, *, no_dispatch_evidence_id: str | None = None) -> str:
        """Recover interrupted work after the owner stops sending for this reservation."""
        if no_dispatch_evidence_id is not None and (
            not isinstance(no_dispatch_evidence_id, str) or not no_dispatch_evidence_id.strip()
        ):
            raise ReceiptError("a T3 evidence ID is required to confirm that dispatch never occurred")
        with acquire_mutex(self._mutex):
            data = self._load()
            entry = self._entry(data, reservation_id)
            dispatch = entry["dispatch"]
            if dispatch == "attached" or entry["receipt_id"] is not None:
                return dispatch
            if dispatch == "reserved" or no_dispatch_evidence_id is not None:
                entry["dispatch"] = "not_run"
                reason = ("interrupted before dispatch" if dispatch == "reserved"
                          else "T3 confirmed no dispatch for the request ID")
                self._seal(data, entry, {
                    "status": "not_run", "disposition": "not_run", "duration_s": 0,
                    "usage": {"source": "not_observed", "cost": None, "reason": reason},
                    "git_before": entry["record"]["git"], "git_after": None,
                    "changed_paths": [], "evidence_ids": [no_dispatch_evidence_id] if no_dispatch_evidence_id else [],
                    "reason": reason,
                    "started_at": None, "ended_at": None, "worker": None, "publish": False,
                })
            elif dispatch == "dispatching":
                entry.update(dispatch="quarantined", quarantined_at=time.time())
                atomic_write_json(self.path, data)
            return entry["dispatch"]

    def current_publication(self, run: str, shard: str, role: str) -> dict[str, Any] | None:
        """Return the currently fenced-in publication, if any."""
        if not self.path.exists():
            return None
        with acquire_mutex(self._mutex):
            data = self._load()
            slot = data["slots"].get(self._slot_key(run, shard, role))
            if slot and slot["publication"]:
                receipt = data["receipts"][slot["publication"]]
                entry = self._entry(data, slot["reservation_id"])
                if receipt["reservation_id"] == slot["reservation_id"] and not entry["revocation"]:
                    return receipt
            return None

    def settled(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Read reservation/receipt pairs in one consistent snapshot, including late spend."""
        if not self.path.exists():
            return []
        with acquire_mutex(self._mutex):
            data = self._load()
            return [
                (entry["record"], data["receipts"][entry["receipt_id"]])
                for entry in data["reservations"].values() if entry["receipt_id"] is not None
            ]
