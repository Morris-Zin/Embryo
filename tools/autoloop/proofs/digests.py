"""Canonical digest helpers for typed proof artifacts.

This module intentionally mirrors the verification shard's canonical-JSON
vocabulary without importing it.  Integration can later replace this small
implementation with a shared trusted helper at the designated boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON for primitive, dataclass and enum values."""

    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_map_digest(file_map: dict[str, bytes]) -> str:
    """Digest a posix-path to bytes mapping."""

    rows = [{"path": path, "sha256": sha256_bytes(file_map[path])} for path in sorted(file_map)]
    return sha256_json({"file_map": rows})


def directory_file_map(root: Path) -> dict[str, bytes]:
    """Read a small deterministic snapshot tree into memory."""

    result: dict[str, bytes] = {}
    if not root.is_dir():
        raise ValueError(f"snapshot input is not a directory: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"snapshot input contains a symlink: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, Path):
        return value.as_posix()
    return value
