from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .common import finish_record, tool_read_hashes
from .model import ScanNotice, ScanResult, ToolActivity, UsageRecord
from .util import is_since, payload_bytes, project_text_hit, safe_int, since_datetime


def read_codex_rollouts(codex_home: object, since: str, project_filter: str | None = None) -> ScanResult:
    result = ScanResult()
    provider = "codex"
    result.providers.add(provider)
    root = Path(str(codex_home)).expanduser()
    sessions = root / "sessions"
    if not sessions.exists():
        result.notices.append(ScanNotice(provider, "codex_rollout", f"Codex sessions directory not found: {sessions}", "warning"))
        return result
    since_dt = since_datetime(since)
    files = sorted(path for path in sessions.rglob("*.jsonl") if path.is_file())
    since_date = since[:10]
    compressed = []
    for path in sorted(path for path in sessions.rglob("*.jsonl.zst") if path.is_file()):
        parts = path.parts
        file_date = None
        if len(parts) >= 4:
            file_date = "-".join(parts[-4:-1])[:10]
        if file_date is None or file_date >= since_date:
            compressed.append(path)
    if compressed:
        result.notices.append(
            ScanNotice(provider, "codex_rollout", f"{len(compressed)} compressed .zst rollout files not decoded by the stdlib adapter", "warning")
        )
    unknown_time_activities = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        session_id = path.stem
        cwd: str | None = None
        turn_models: dict[str, str] = {}
        turn_sequence: dict[str, int] = {}
        turn_times: dict[str, str] = {}
        session_time: str | None = None
        calls: dict[str, dict[str, Any]] = {}
        malformed = 0
        project_hit_file = project_text_hit(project_filter, str(path))
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            obj_type = obj.get("type")
            if obj_type == "session_meta":
                session_id = str(payload.get("session_id") or payload.get("id") or session_id)
                session_time = session_time or (obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None)
                cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
                project_hit_file = project_hit_file or project_text_hit(project_filter, cwd)
            elif obj_type == "turn_context":
                turn_id = payload.get("turn_id") or payload.get("root_turn_id")
                model = payload.get("model")
                if turn_id and isinstance(model, str):
                    turn_models[str(turn_id)] = model
                if turn_id and isinstance(obj.get("timestamp"), str):
                    turn_times[str(turn_id)] = obj["timestamp"]
                if isinstance(payload.get("cwd"), str):
                    cwd = payload.get("cwd")
                    project_hit_file = project_hit_file or project_text_hit(project_filter, cwd)
            elif obj_type == "token_usage_record":
                observed = obj.get("timestamp")
                if not is_since(observed, since_dt):
                    continue
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                if not usage:
                    continue
                response_id = str(payload.get("response_id") or f"{path}:{line_number}")
                turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or "") or None
                if turn_id and turn_id not in turn_sequence:
                    turn_sequence[turn_id] = len(turn_sequence) + 1
                input_total = safe_int(usage.get("input_tokens"))
                cached = safe_int(usage.get("cached_input_tokens"))
                cache_write = safe_int(usage.get("cache_write_input_tokens"))
                output = safe_int(usage.get("output_tokens"))
                reasoning = safe_int(usage.get("reasoning_output_tokens"))
                uncached: int | None = None
                if input_total is not None and cached is not None:
                    subtract_write = cache_write or 0
                    if input_total >= cached + subtract_write:
                        uncached = input_total - cached - subtract_write
                record = UsageRecord(
                    provider=provider,
                    source="codex_rollout",
                    record_id="codex-response:" + response_id,
                    granularity="request",
                    observed_at=observed,
                    native_session_id=str(payload.get("session_id") or payload.get("thread_id") or session_id),
                    native_request_id=response_id,
                    native_turn_id=turn_id,
                    model=turn_models.get(turn_id or ""),
                    turn=turn_sequence.get(turn_id or ""),
                    cached_input=cached,
                    cache_write=cache_write,
                    uncached_input=uncached,
                    output=output,
                    reasoning=reasoning,
                    units="provider_reported_response_tokens",
                    coverage="full" if None not in (cached, cache_write, uncached, output, reasoning) else "partial",
                    inclusion={"cached_input_in_input": True, "cache_write_in_input": True, "reasoning_in_output": True},
                    project_hit=project_hit_file,
                )
                if input_total is None:
                    record.unknowns["uncached_input"] = "input_tokens_not_observed"
                elif uncached is None:
                    record.unknowns["uncached_input"] = "input_decomposition_ambiguous"
                record.unknowns["cost"] = "cost_not_reported_in_codex_rollout"
                record.unknowns["currency"] = "currency_not_reported_in_codex_rollout"
                record.unknowns["retry"] = "retry_not_observed_in_codex_rollout"
                result.records.append(finish_record(record))
            elif obj_type == "response_item" and payload.get("type") == "custom_tool_call":
                call_id = str(payload.get("call_id") or payload.get("id") or line_number)
                calls[call_id] = {
                    "input": payload.get("input"),
                    "name": payload.get("name"),
                    "observed": obj.get("timestamp"),
                    "turn_id": payload.get("turn_id"),
                    "line": line_number,
                }
            elif obj_type == "response_item" and payload.get("type") == "custom_tool_call_output":
                call_id = str(payload.get("call_id") or "")
                call = calls.get(call_id, {})
                turn_id = call.get("turn_id")
                if turn_id and str(turn_id) not in turn_sequence:
                    turn_sequence[str(turn_id)] = len(turn_sequence) + 1
                activity_time = obj.get("timestamp") or call.get("observed") or turn_times.get(str(turn_id or "")) or session_time
                if activity_time is None:
                    unknown_time_activities += 1
                    continue
                if not is_since(activity_time, since_dt):
                    continue
                activity = ToolActivity(
                    provider=provider,
                    source="codex_rollout",
                    activity_id=f"codex-tool:{session_id}:{call_id or line_number}",
                    observed_at=activity_time,
                    native_session_id=session_id,
                    turn=turn_sequence.get(str(turn_id or "")),
                    tool_name=str(call.get("name") or "unknown"),
                    input_bytes=payload_bytes(call.get("input")),
                    output_bytes=payload_bytes(payload.get("output")),
                    read_path_hashes=tool_read_hashes(call.get("input")),
                    project_hit=project_hit_file,
                )
                result.activities.append(activity)
        if malformed:
            result.notices.append(ScanNotice(provider, "codex_rollout", f"{malformed} malformed JSONL rows skipped in {path.name}", "warning"))
    if unknown_time_activities:
        result.notices.append(
            ScanNotice(provider, "codex_rollout", f"{unknown_time_activities} unknown-time tool activities excluded from windowed stats", "warning")
        )
    return result
