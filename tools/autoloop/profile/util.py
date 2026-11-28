from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Optional


def canonical_provider(provider: object) -> str:
    text = str(provider or "unknown")
    lowered = text.lower()
    if "claude" in lowered:
        return "claude"
    if "codex" in lowered:
        return "codex"
    if "devin" in lowered or "acpregistry" in lowered:
        return "devin"
    return text


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if len(text) == 10 and text[4] == "-":
        text += "T00:00:00Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def since_datetime(value: str) -> datetime:
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid --since value: {value!r}")
    return parsed


def is_since(value: object, since: datetime) -> bool:
    parsed = parse_time(value)
    return parsed is not None and parsed >= since


def safe_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def safe_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def payload_bytes(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="replace"))
    if isinstance(value, list):
        return sum(payload_bytes(item) for item in value)
    if isinstance(value, dict):
        return sum(payload_bytes(item) for item in value.values())
    return len(str(value).encode("utf-8", errors="replace"))


def normalized_path_key(path: str) -> str:
    text = path.strip().strip('"').strip("'")
    text = text.replace("/", "\\")
    text = os.path.normpath(text)
    return os.path.normcase(text)


def path_hash(path: str) -> str:
    return hashlib.sha256(normalized_path_key(path).encode("utf-8", errors="replace")).hexdigest()[:16]


def _split_shell_words(command: str) -> list[str]:
    try:
        words = shlex.split(command, posix=False)
    except ValueError:
        words = command.split()
    cleaned: list[str] = []
    for word in words:
        if len(word) >= 2 and word[0] == word[-1] and word[0] in {"'", '"'}:
            word = word[1:-1]
        cleaned.append(word)
    return cleaned


def extract_read_paths(command: object) -> tuple[str, ...]:
    if not isinstance(command, str) or not command.strip():
        return ()
    paths: list[str] = []
    words = _split_shell_words(command)
    if not words:
        return ()
    read_commands = {"cat", "head", "tail", "less", "more", "type"}
    for index, word in enumerate(words):
        base = word.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base in read_commands and index + 1 < len(words):
            for candidate in words[index + 1 :]:
                if candidate.startswith("-"):
                    continue
                if candidate not in {"&&", ";", "|", "||"}:
                    paths.append(candidate)
                break
        elif base == "sed" and index + 1 < len(words):
            positional: list[str] = []
            skip_next = False
            for candidate in words[index + 1 :]:
                if skip_next:
                    skip_next = False
                    continue
                if candidate in {"-e", "-f", "-n"}:
                    if candidate in {"-e", "-f"}:
                        skip_next = True
                    continue
                if candidate.startswith("-"):
                    continue
                positional.append(candidate)
            if len(positional) >= 2:
                paths.append(positional[-1])
        elif base.lower() in {"get-content", "gc"} and index + 1 < len(words):
            for candidate in words[index + 1 :]:
                if not candidate.startswith("-"):
                    paths.append(candidate)
                    break
    return tuple(dict.fromkeys(paths))


def read_path_hashes_from_command(command: object) -> tuple[str, ...]:
    return tuple(path_hash(path) for path in extract_read_paths(command))


def infer_role(thread_id: object, title: object, sidechain: bool = False) -> str:
    if sidechain:
        return "sidechain"
    thread_text = str(thread_id or "").lower()
    title_text = str(title or "").lower()
    if "review" in title_text:
        return "review"
    if thread_text.startswith("thread:delegated-task") or "delegate-task" in thread_text:
        return "delegation"
    return "worker"


def infer_shard(title: object) -> str | None:
    if not isinstance(title, str):
        return None
    match = re.search(r"\bshard\s*[/=:-]\s*([a-z0-9_.-]+)", title, flags=re.I)
    if match:
        return match.group(1).strip("._-").lower() or None
    return None


def project_text_hit(project_filter: str | None, *values: object) -> bool:
    if not project_filter:
        return True
    needle = project_filter.lower().replace("\\", "/")
    for value in values:
        if value is None:
            continue
        text = str(value).lower().replace("\\", "/")
        if needle in text:
            return True
        compact = needle.replace("-", "").replace("_", "")
        if compact and compact in text.replace("-", "").replace("_", ""):
            return True
    return False


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
