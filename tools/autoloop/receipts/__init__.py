"""Host-wide delegation and check receipts for the autoloop shadow broker."""

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

_OUTCOME_FIELDS = ("evidence_id", "evidence_digest", "output_digest", "exit_code", "completed_at")


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
    def _slot_parts(run: str, shard: str, role: str, cache_key_digest: str | None) -> list[str]:
        parts = [run, shard, role]
        if cache_key_digest is not None:
            parts.append(cache_key_digest)
        return parts

    @staticmethod
    def _entry(data: dict[str, Any], reservation_id: str) -> dict[str, Any]:
        try:
            return data["reservations"][reservation_id]
        except KeyError as exc:
            raise ReceiptError("unknown reservation") from exc

    def _slot(self, data: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        parts = self._slot_parts(record["run"], record["shard"], record["role"], record.get("cache_key_digest"))
        return data["slots"][_digest(parts)]

    def reserve(
        self, *, run: str, shard: str, role: str, generation: int, request_id: str,
        request_digest: str, brief_digest: str, task_class: str, difficulty: str,
        contract_revision: int, contract_hash: str, policy: str, epoch: str,
        git: dict[str, str], provider: str, model: str, settings: dict[str, Any],
        limits: dict[str, Any], expires_at: float, cache_key_digest: str | None = None,
    ) -> dict[str, Any]:
        """Commit one delegation or local check reservation before execution."""
        required = (run, shard, role, request_id, request_digest, brief_digest, task_class,
                    difficulty, contract_hash, policy, epoch, provider, model)
        if not all(isinstance(value, str) and value.strip() for value in required):
            raise ReceiptError("reservation identity and binding fields must be non-empty strings")
        if cache_key_digest is not None and (not isinstance(cache_key_digest, str) or not cache_key_digest.strip()):
            raise ReceiptError("a local execution requires a cache key digest")
        if any(type(value) is not int or value < 1 for value in (generation, contract_revision)):
            raise ReceiptError("generation and contract revision must be positive integers")
        if not isinstance(settings, dict) or not isinstance(limits, dict):
            raise ReceiptError("settings and limits must be objects")
        _number(expires_at, "expiry")
        with acquire_mutex(self._mutex):
            data = self._load()
            parts = self._slot_parts(run, shard, role, cache_key_digest)
            key = _digest(parts)
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
            reservation_id = _digest([self.repo_id, *parts, generation])
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
            if cache_key_digest is not None:
                record["cache_key_digest"] = cache_key_digest
            record = json.loads(_canonical(record))
            data["reservations"][reservation_id] = {
                "record": record, "dispatch": "local" if cache_key_digest is not None else "reserved", "task_id": None,
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

    @staticmethod
    def _inactive_reason(slot: dict[str, Any], entry: dict[str, Any], now: float) -> str | None:
        record = entry["record"]
        if slot["reservation_id"] != record["reservation_id"]:
            return "superseded"
        if entry["revocation"]:
            return "revoked"
        if record["expires_at"] <= now:
            return "expired"
        return None

    def _seal(self, data: dict[str, Any], entry: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        record = entry["record"]
        slot = self._slot(data, record)
        now = time.time()
        publication = "not_requested"
        if result["publish"]:
            publication = self._inactive_reason(slot, entry, now)
            if publication is None:
                stale = (
                    slot["publication"] != record["expected_publication"]
                    or result["git_before"] != record["git"]
                    or result["git_after"]["base"] != record["git"]["base"]
                    or ("cache_key_digest" in record and result["git_after"] != record["git"])
                )
                publication = "stale" if stale else "published"
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
            if entry["dispatch"] == "local":
                raise ReceiptError("use settle_execution for a local check")
            if entry["dispatch"] != "attached" and entry["receipt_id"] is None:
                raise ReceiptError("attach a confirmed task before settling")
            return self._settle_once(data, entry, result)

    def _settle_once(self, data: dict[str, Any], entry: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if entry["receipt_id"] is not None:
            existing = data["receipts"][entry["receipt_id"]]
            if _canonical({key: existing[key] for key in result}) != _canonical(result):
                raise ReceiptError("reservation already has a different settlement")
            return existing
        return self._seal(data, entry, result)

    def settle_execution(
        self, reservation_id: str, *, exit_code: int, output_digest: str, evidence_id: str,
        evidence_digest: str, completed_at: int, duration_s: float,
        git_before: dict[str, str], git_after: dict[str, str],
    ) -> dict[str, Any]:
        """Attest a coordinator-run check, including a completed check that exited nonzero."""
        if type(exit_code) is not int or type(completed_at) is not int or completed_at < 0:
            raise ReceiptError("a check outcome requires an integer exit code and completion timestamp")
        if not all(isinstance(value, str) and value.strip() for value in (output_digest, evidence_id, evidence_digest)):
            raise ReceiptError("a check outcome requires an evidence ID, evidence digest, and output digest")
        _number(duration_s, "duration")
        outcome = {
            "evidence_id": evidence_id, "evidence_digest": evidence_digest, "output_digest": output_digest,
            "exit_code": exit_code, "completed_at": completed_at,
        }
        # Match the verification cache's canonical UTF-8 outcome bytes. Ledger IDs retain their existing encoding.
        canonical = json.dumps(outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        result = {
            **outcome, "outcome_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "status": "completed", "disposition": "executed", "duration_s": duration_s,
            "usage": {"source": "not_observed", "cost": None, "reason": "local check"},
            "git_before": _fingerprint(git_before), "git_after": _fingerprint(git_after),
            "changed_paths": [], "evidence_ids": [evidence_id], "reason": None,
            "started_at": None, "ended_at": completed_at, "worker": None, "publish": True,
        }
        with acquire_mutex(self._mutex):
            data = self._load()
            entry = self._entry(data, reservation_id)
            if entry["dispatch"] != "local":
                raise ReceiptError("local settlement requires a cache-key reservation")
            result["cache_key_digest"] = entry["record"]["cache_key_digest"]
            return self._settle_once(data, entry, result)

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
            if dispatch in {"attached", "local"} or entry["receipt_id"] is not None:
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

    def _current_receipt(self, data: dict[str, Any], slot: dict[str, Any] | None) -> dict[str, Any] | None:
        if not slot or not slot["publication"]:
            return None
        receipt = data["receipts"][slot["publication"]]
        entry = self._entry(data, receipt["reservation_id"])
        if self._inactive_reason(slot, entry, time.time()) is not None:
            return None
        return receipt

    def current_publication(
        self, run: str, shard: str, role: str, *, cache_key_digest: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the currently fenced-in publication, if any."""
        if not self.path.exists():
            return None
        with acquire_mutex(self._mutex):
            data = self._load()
            parts = self._slot_parts(run, shard, role, cache_key_digest)
            return self._current_receipt(data, data["slots"].get(_digest(parts)))

    def validate_execution_receipt(self, receipt_id: str, attestation: Any) -> bool:
        """Implement the verification cache's structural receipt validator."""
        if not self.path.exists():
            return False
        with acquire_mutex(self._mutex):
            data = self._load()
            receipt = data["receipts"].get(receipt_id)
            if receipt is None or "cache_key_digest" not in receipt:
                return False
            entry = self._entry(data, receipt["reservation_id"])
            current = self._current_receipt(data, self._slot(data, entry["record"]))
            if current is None or current["receipt_id"] != receipt_id:
                return False
            return all(
                type(value := getattr(attestation, name, None)) is type(receipt[name]) and value == receipt[name]
                for name in ("cache_key_digest", "outcome_digest", *_OUTCOME_FIELDS)
            )

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
