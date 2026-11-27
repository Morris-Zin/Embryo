"""Deterministic failure signature normalizer for dead-end tracking."""

from __future__ import annotations

import hashlib
import re

_ISO_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_CLOCK_TS_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b", re.IGNORECASE)
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PID_RE = re.compile(r"\bpid[=:\s]+\d+\b", re.IGNORECASE)
_TEMP_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\(?:Users\\[^\\\s]+\\AppData\\Local\\Temp|Temp)\\[^\s:\"'`)]+"
    r"|(?:/tmp|/var/tmp|/private/tmp|/var/folders/[^\s:\"'`)]+)/[^\s:\"'`)]+)",
    re.IGNORECASE,
)
_LINE_COL_RE = re.compile(r"(?::\d+(?::\d+)?\b|\bline\s+\d+\b|\bL\d+(?:-L\d+)?\b)", re.IGNORECASE)

_ERROR_CODE_PATTERNS = (
    re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning|Failure))\b"),
    re.compile(r"\berror\[(E\d{4})\]"),
    re.compile(r"\b(E\d{4}|TS\d{4}|ERR_[A-Z0-9_]+|HTTP[ _]?\d{3})\b"),
    re.compile(r"\bexit(?:ed with)?(?: code)?\s+(\d+)\b", re.IGNORECASE),
)

_PY_TRACE_FILE_RE = re.compile(r'File\s+"([^"]+)"(?:,\s*line\s+\d+)?(?:,\s*in\s+([A-Za-z0-9_<>.]+))?')
_TEST_METHOD_RE = re.compile(r"\b(?:FAIL|ERROR):\s+([A-Za-z0-9_.-]+)(?:\s+\(([^)]+)\))?")
_PATH_TOKEN_RE = re.compile(
    r"([A-Za-z0-9_./\\-]+\.(?:py|js|mjs|ts|tsx|rs|sh|ya?ml|json|toml|md|slint|sql))\b"
)


def strip_volatile_tokens(text: str) -> str:
    """Remove timestamps, temp paths, hex addresses, UUIDs, PIDs, and line numbers."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _ISO_TS_RE.sub("<TS>", cleaned)
    cleaned = _CLOCK_TS_RE.sub("<TIME>", cleaned)
    cleaned = _TEMP_PATH_RE.sub("<TMP>", cleaned)
    cleaned = _UUID_RE.sub("<UUID>", cleaned)
    cleaned = _HEX_ADDR_RE.sub("<HEX>", cleaned)
    cleaned = _PID_RE.sub("pid=<PID>", cleaned)
    cleaned = _DURATION_RE.sub("<DUR>", cleaned)
    cleaned = _LINE_COL_RE.sub("", cleaned)
    return cleaned


def _normalize_path_or_symbol(raw: str) -> str:
    value = raw.strip().replace("\\", "/")
    # Strip Windows drive letter or leading temp/worktree prefixes down to repo-like relative path.
    value = re.sub(r"^[A-Za-z]:/", "", value)
    for anchor in (
        "tools/",
        "apps/",
        "perf-suite/",
        "print-companion/",
        "desktop-frontend/",
        "desktop-client/",
        ".autoloop/",
        ".github/",
        ".devin/",
        "docs/",
    ):
        idx = value.find(anchor)
        if idx >= 0:
            value = value[idx:]
            break
    else:
        if "/" in value:
            value = value.rsplit("/", 1)[-1]
    value = re.sub(r"[^A-Za-z0-9_./<>-]", "_", value).strip("_")
    return value or "unknown"


def _extract_error_code(cleaned: str) -> str:
    for pattern in _ERROR_CODE_PATTERNS:
        matches = pattern.findall(cleaned)
        if matches:
            code = matches[-1]
            if code.isdigit():
                return f"EXIT_{code}"
            return re.sub(r"\s+", "_", code)
    if "FAIL" in cleaned:
        return "FAIL"
    return "UNKNOWN"


def _extract_symbol_or_file(raw_text: str, cleaned: str) -> str:
    test_matches = _TEST_METHOD_RE.findall(raw_text)
    if test_matches:
        method, cls = test_matches[-1]
        if cls:
            return _normalize_path_or_symbol(f"{cls}.{method}")
        return _normalize_path_or_symbol(method)

    trace_matches = _PY_TRACE_FILE_RE.findall(raw_text)
    if trace_matches:
        file_path, symbol = trace_matches[-1]
        norm_file = _normalize_path_or_symbol(file_path)
        if symbol and symbol != "<module>":
            return f"{norm_file}:{symbol}"
        return norm_file

    path_matches = _PATH_TOKEN_RE.findall(cleaned)
    if path_matches:
        return _normalize_path_or_symbol(path_matches[-1])

    compact_lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    fingerprint_source = "\n".join(compact_lines[-5:]) if compact_lines else cleaned.strip()
    digest = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:12]
    return f"hash_{digest}"


def normalize_failure(check_name: str, output: str) -> str:
    """Produce a deterministic signature `<check_name>::<error_code>::<symbol_or_file>`."""
    safe_check = re.sub(r"[^A-Za-z0-9_.-]+", "_", (check_name or "check").strip()) or "check"
    raw = output or ""
    cleaned = strip_volatile_tokens(raw)
    error_code = _extract_error_code(cleaned)
    symbol_or_file = _extract_symbol_or_file(raw, cleaned)
    return f"{safe_check}::{error_code}::{symbol_or_file}"
