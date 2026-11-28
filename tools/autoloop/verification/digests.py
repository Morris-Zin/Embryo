"""Canonical digest helpers shared by verification evidence and the check cache."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
from typing import Any

from .schemas import ToolingSource


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def tooling_sources_digest(sources: tuple[ToolingSource, ...] | list[ToolingSource]) -> str:
    """Digest the exact sorted tooling source set bound in verification evidence."""

    rows = []
    seen: set[str] = set()
    for source in sources:
        if not source.path or not source.blob_sha256:
            raise ValueError("every tooling source requires a path and blob digest")
        if source.path in seen:
            raise ValueError(f"duplicate tooling source path {source.path!r}")
        seen.add(source.path)
        rows.append({"path": source.path, "blob_sha256": source.blob_sha256})
    rows.sort(key=lambda item: item["path"])
    return sha256_json({"tooling_sources": rows})


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value
