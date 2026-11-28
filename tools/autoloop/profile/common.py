from __future__ import annotations

import json
from typing import Any

from .model import UsageRecord

CORE_FIELDS = (
    "run",
    "shard",
    "task",
    "model",
    "role",
    "retry",
    "turn",
    "cached_input",
    "cache_write",
    "uncached_input",
    "output",
    "reasoning",
    "cost",
    "currency",
)


def finish_record(record: UsageRecord, default_reason: str = "not_observed") -> UsageRecord:
    for field_name in CORE_FIELDS:
        if getattr(record, field_name) is None and field_name not in record.unknowns:
            record.unknowns[field_name] = default_reason
    return record


def loads_object(text: object) -> dict[str, Any]:
    if not isinstance(text, str) or not text:
        return {}
    try:
        value = json.loads(text)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def has_numeric_usage(values: object) -> bool:
    if not isinstance(values, dict):
        return False
    return any(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values.values())


def tool_read_hashes(tool_input: object) -> tuple[str, ...]:
    from .util import path_hash, read_path_hashes_from_command

    hashes: list[str] = []
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "notebook_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                hashes.append(path_hash(value))
        for key in ("command", "cmd", "input"):
            value = tool_input.get(key)
            if isinstance(value, str):
                hashes.extend(read_path_hashes_from_command(value))
    elif isinstance(tool_input, str):
        hashes.extend(read_path_hashes_from_command(tool_input))
    return tuple(dict.fromkeys(hashes))
