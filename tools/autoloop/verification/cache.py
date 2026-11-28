"""Content-addressed deterministic check cache in the coordinator broker store.

The cache is deliberately opt-in.  A check is eligible only when its caller
sets ``cacheable=True`` and its kind is not live, timing, or evaluator.  Every
lookup revalidates the original execution receipt through the receipts shard's
validator interface before evidence may be reused.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..leases import resource_lock_dir
from .digests import canonical_json_bytes, sha256_json
from .receipts import ReceiptValidator
from .schemas import NEVER_CACHEABLE_CHECK_KINDS

CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InputDigest:
    """Digest of an additional readable input outside the code-tree hash."""

    path: str
    digest: str


@dataclass(frozen=True)
class CheckCacheKey:
    code_tree_sha256: str
    tooling_digest: str
    argv: tuple[str, ...]
    interpreter_dependency_lock_digest: str
    env_allowlist_digest: str
    platform: str
    working_directory: str = "."
    input_digests: tuple[InputDigest, ...] = ()


@dataclass(frozen=True)
class CheckOutcome:
    evidence_id: str
    evidence_digest: str
    output_digest: str
    exit_code: int
    completed_at: int


@dataclass(frozen=True)
class FreshCheckResult:
    outcome: CheckOutcome
    receipt_id: str


@dataclass(frozen=True)
class CachedCheckEvidence:
    evidence_id: str
    cached: bool
    cache_key_digest: str
    original_evidence_id: str
    original_evidence_digest: str
    original_receipt_id: str
    output_digest: str


@dataclass(frozen=True)
class CacheLookup:
    hit: bool
    evidence: CachedCheckEvidence | None = None
    outcome: CheckOutcome | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CacheExecution:
    cached: bool
    outcome: CheckOutcome
    evidence: CachedCheckEvidence | None
    stored: bool
    elapsed_seconds: float


def env_allowlist_digest(environment: Mapping[str, str], allowed_names: tuple[str, ...] | list[str]) -> str:
    """Digest the exact names and values visible to a deterministic check.

    Missing names are represented explicitly as null so adding a name to the
    allowlist changes the key even when the variable is absent.
    """

    names = tuple(sorted(set(allowed_names)))
    return sha256_json({"env": {name: environment.get(name) for name in names}, "names": list(names)})


def default_check_cache_dir() -> Path:
    """Host-wide broker cache authority, outside every clone."""

    return resource_lock_dir() / "check-cache"


class CheckCache:
    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        return default_check_cache_dir()

    def key_digest(self, key: CheckCacheKey) -> str:
        _validate_key(key)
        return sha256_json(_canonical_key(key))

    def lookup(
        self,
        key: CheckCacheKey,
        receipt_validator: ReceiptValidator,
        *,
        cacheable: bool = False,
        kind: str = "deterministic",
    ) -> CacheLookup:
        ineligible = _ineligible_reason(cacheable=cacheable, kind=kind)
        if ineligible:
            return CacheLookup(False, reason=ineligible)
        key_digest = self.key_digest(key)
        path = self._entry_path(key_digest)
        if not path.exists():
            return CacheLookup(False, reason="miss")
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return CacheLookup(False, reason="invalid-entry")

        outcome = _outcome_from_entry(entry)
        if (
            not isinstance(entry, dict)
            or entry.get("schema_version") != CACHE_SCHEMA_VERSION
            or entry.get("key_digest") != key_digest
            or entry.get("key") != _canonical_key(key)
            or outcome is None
            or not isinstance(entry.get("receipt_id"), str)
            or not entry.get("receipt_id")
        ):
            return CacheLookup(False, reason="invalid-entry")

        receipt_id = entry["receipt_id"]
        if not _receipt_valid(receipt_id, receipt_validator):
            return CacheLookup(False, reason="invalid-receipt")
        evidence = _cached_evidence(key_digest, outcome, receipt_id)
        if not self._record_cached_evidence(evidence):
            return CacheLookup(False, reason="evidence-recording-failed")
        return CacheLookup(True, evidence=evidence, outcome=outcome)

    def store(
        self,
        key: CheckCacheKey,
        outcome: CheckOutcome,
        receipt_id: str,
        receipt_validator: ReceiptValidator,
        *,
        cacheable: bool = False,
        kind: str = "deterministic",
    ) -> bool:
        if _ineligible_reason(cacheable=cacheable, kind=kind):
            return False
        if not receipt_id or not _receipt_valid(receipt_id, receipt_validator):
            return False
        _validate_outcome(outcome)
        key_digest = self.key_digest(key)
        entry = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "key_digest": key_digest,
            "key": _canonical_key(key),
            "receipt_id": receipt_id,
            "outcome": _canonical_outcome(outcome),
        }
        payload = canonical_json_bytes(entry) + b"\n"
        path = self._entry_path(key_digest)
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError:
                existing = b""
            if existing == payload:
                return True
            try:
                existing_entry = json.loads(existing.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                existing_entry = None
            if isinstance(existing_entry, dict) and existing_entry.get("schema_version") == CACHE_SCHEMA_VERSION:
                return False
        _atomic_write(path, payload)
        return True

    def run(
        self,
        key: CheckCacheKey,
        runner: Callable[[], FreshCheckResult],
        receipt_validator: ReceiptValidator,
        *,
        cacheable: bool = False,
        kind: str = "deterministic",
    ) -> CacheExecution:
        started = time.monotonic()
        found = self.lookup(key, receipt_validator, cacheable=cacheable, kind=kind)
        if found.hit and found.outcome is not None:
            return CacheExecution(
                True,
                found.outcome,
                found.evidence,
                False,
                time.monotonic() - started,
            )
        fresh = runner()
        stored = self.store(
            key,
            fresh.outcome,
            fresh.receipt_id,
            receipt_validator,
            cacheable=cacheable,
            kind=kind,
        )
        return CacheExecution(False, fresh.outcome, None, stored, time.monotonic() - started)

    def _entry_path(self, key_digest: str) -> Path:
        return self.root / key_digest[:2] / f"{key_digest[2:]}.json"

    def _cached_evidence_path(self, evidence_id: str) -> Path:
        suffix = evidence_id.removeprefix("cached-check-evidence-")
        return self.root / "cached-evidence" / suffix[:2] / f"{suffix[2:]}.json"

    def _record_cached_evidence(self, evidence: CachedCheckEvidence) -> bool:
        payload = canonical_json_bytes({"schema_version": CACHE_SCHEMA_VERSION, "cached_evidence": evidence}) + b"\n"
        path = self._cached_evidence_path(evidence.evidence_id)
        try:
            if path.exists():
                return path.read_bytes() == payload
            _atomic_write(path, payload)
        except OSError:
            return False
        return True


def _canonical_key(key: CheckCacheKey) -> dict[str, Any]:
    inputs = sorted(({"path": item.path, "digest": item.digest} for item in key.input_digests), key=lambda item: item["path"])
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "code_tree_sha256": key.code_tree_sha256,
        "tooling_digest": key.tooling_digest,
        "argv": list(key.argv),
        "interpreter_dependency_lock_digest": key.interpreter_dependency_lock_digest,
        "env_allowlist_digest": key.env_allowlist_digest,
        "platform": key.platform,
        "working_directory": key.working_directory,
        "input_digests": inputs,
    }


def _canonical_outcome(outcome: CheckOutcome) -> dict[str, Any]:
    return {
        "evidence_id": outcome.evidence_id,
        "evidence_digest": outcome.evidence_digest,
        "output_digest": outcome.output_digest,
        "exit_code": outcome.exit_code,
        "completed_at": outcome.completed_at,
    }


def _outcome_from_entry(entry: Any) -> CheckOutcome | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("outcome"), dict):
        return None
    raw = entry["outcome"]
    try:
        outcome = CheckOutcome(
            evidence_id=raw["evidence_id"],
            evidence_digest=raw["evidence_digest"],
            output_digest=raw["output_digest"],
            exit_code=raw["exit_code"],
            completed_at=raw["completed_at"],
        )
    except (KeyError, TypeError):
        return None
    try:
        _validate_outcome(outcome)
    except ValueError:
        return None
    return outcome


def _validate_key(key: CheckCacheKey) -> None:
    missing = []
    for name in (
        "code_tree_sha256",
        "tooling_digest",
        "interpreter_dependency_lock_digest",
        "env_allowlist_digest",
        "platform",
        "working_directory",
    ):
        if not getattr(key, name):
            missing.append(name)
    if not key.argv or any(not isinstance(part, str) or not part for part in key.argv):
        missing.append("argv")
    if missing:
        raise ValueError("check cache key is missing " + ", ".join(missing))
    input_paths = [item.path for item in key.input_digests]
    if len(input_paths) != len(set(input_paths)):
        raise ValueError("additional input paths must be unique")
    for item in key.input_digests:
        if not item.path or not item.digest:
            raise ValueError("every additional input requires a path and digest")


def _validate_outcome(outcome: CheckOutcome) -> None:
    if not outcome.evidence_id or not outcome.evidence_digest or not outcome.output_digest:
        raise ValueError("check outcome requires evidence ID, evidence digest, and output digest")
    if not isinstance(outcome.exit_code, int):
        raise ValueError("check outcome exit_code must be an integer")
    if not isinstance(outcome.completed_at, int):
        raise ValueError("check outcome completed_at must be an integer timestamp")


def _ineligible_reason(*, cacheable: bool, kind: str) -> str | None:
    if kind in NEVER_CACHEABLE_CHECK_KINDS:
        return "never-cacheable-kind"
    if not cacheable:
        return "check-not-cacheable"
    return None


def _receipt_valid(receipt_id: str, validator: ReceiptValidator) -> bool:
    if not receipt_id:
        return False
    try:
        return bool(validator(receipt_id))
    except Exception:
        return False


def _cached_evidence(key_digest: str, outcome: CheckOutcome, receipt_id: str) -> CachedCheckEvidence:
    evidence_id = "cached-check-evidence-" + sha256_json(
        {
            "cache_key_digest": key_digest,
            "original_evidence_id": outcome.evidence_id,
            "original_receipt_id": receipt_id,
        }
    )
    return CachedCheckEvidence(
        evidence_id=evidence_id,
        cached=True,
        cache_key_digest=key_digest,
        original_evidence_id=outcome.evidence_id,
        original_evidence_digest=outcome.evidence_digest,
        original_receipt_id=receipt_id,
        output_digest=outcome.output_digest,
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
