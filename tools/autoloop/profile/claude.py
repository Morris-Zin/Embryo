from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .common import finish_record, tool_read_hashes
from .model import ScanNotice, ScanResult, ToolActivity, UsageRecord
from .util import is_since, payload_bytes, project_text_hit, safe_int, since_datetime


def _message_content_is_tool_result(content: object) -> bool:
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)


def read_claude_transcripts(project_dir: object, since: str, project_filter: str | None = None) -> ScanResult:
    result = ScanResult()
    provider = "claude"
    result.providers.add(provider)
    root = Path(str(project_dir)).expanduser()
    if not root.exists():
        result.notices.append(ScanNotice(provider, "claude_transcript", f"Claude transcript directory not found: {root}", "warning"))
        return result
    since_dt = since_datetime(since)
    unknown_time_activities = 0
    for path in sorted(root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        session_id = path.stem
        current_turn = 0
        usage_rows: dict[str, tuple[dict[str, Any], dict[str, Any], int]] = {}
        duplicate_usage_rows = 0
        pending_tools: dict[str, dict[str, Any]] = {}
        seen_tool_use: set[str] = set()
        turn_times: dict[int, str] = {}
        session_time: str | None = None
        project_hit_file = project_text_hit(project_filter, str(path), str(root))
        saw_cost_state = False
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj_type = obj.get("type")
            if session_time is None and isinstance(obj.get("timestamp"), str):
                session_time = obj["timestamp"]
            if isinstance(obj.get("cwd"), str):
                project_hit_file = project_hit_file or project_text_hit(project_filter, obj.get("cwd"))
            if obj_type == "cost-state":
                saw_cost_state = True
            if obj_type == "user":
                content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else None
                if not _message_content_is_tool_result(content) and not obj.get("isMeta"):
                    current_turn += 1
                    if isinstance(obj.get("timestamp"), str):
                        turn_times[current_turn] = obj["timestamp"]
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "tool_result":
                            continue
                        tool_id = str(item.get("tool_use_id") or "")
                        call = pending_tools.get(tool_id, {})
                        input_value = call.get("input")
                        activity_turn = call.get("turn") or current_turn or None
                        activity_time = obj.get("timestamp") or call.get("observed") or turn_times.get(activity_turn or 0) or session_time
                        if activity_time is None:
                            unknown_time_activities += 1
                            continue
                        if not is_since(activity_time, since_dt):
                            continue
                        activity = ToolActivity(
                            provider=provider,
                            source="claude_transcript",
                            activity_id=f"claude-tool:{session_id}:{tool_id or line_number}",
                            observed_at=activity_time,
                            native_session_id=str(obj.get("sessionId") or session_id),
                            turn=activity_turn,
                            tool_name=str(call.get("name") or "unknown"),
                            input_bytes=payload_bytes(input_value),
                            output_bytes=payload_bytes(item.get("content")),
                            read_path_hashes=tool_read_hashes(input_value),
                            project_hit=project_hit_file,
                        )
                        result.activities.append(activity)
                continue
            if obj_type != "assistant":
                continue
            observed = obj.get("timestamp")
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    tool_id = str(item.get("id") or "")
                    if tool_id and tool_id in seen_tool_use:
                        continue
                    if tool_id:
                        seen_tool_use.add(tool_id)
                    pending_tools[tool_id] = {
                        "name": item.get("name"),
                        "input": item.get("input"),
                        "turn": current_turn or None,
                        "observed": observed,
                    }
            if current_turn and isinstance(observed, str):
                turn_times.setdefault(current_turn, observed)
            if not is_since(observed, since_dt):
                continue
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
            if not usage:
                continue
            request_id = str(obj.get("requestId") or message.get("id") or obj.get("uuid") or line_number)
            if request_id in usage_rows:
                duplicate_usage_rows += 1
            usage_rows[request_id] = (obj, usage, current_turn or 1)
        if duplicate_usage_rows:
            result.notices.append(
                ScanNotice(provider, "claude_transcript", f"{duplicate_usage_rows} duplicate streamed usage rows collapsed in {path.name}")
            )
        for request_id, (obj, usage, turn) in usage_rows.items():
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
            record = UsageRecord(
                provider=provider,
                source="claude_transcript",
                record_id="claude-request:" + request_id,
                granularity="request",
                observed_at=obj.get("timestamp"),
                native_session_id=str(obj.get("sessionId") or session_id),
                native_request_id=str(message.get("id") or obj.get("requestId") or "") or None,
                model=message.get("model") or obj.get("requestedModel"),
                role="sidechain" if obj.get("isSidechain") else "worker",
                turn=turn,
                cached_input=safe_int(usage.get("cache_read_input_tokens")),
                cache_write=safe_int(usage.get("cache_creation_input_tokens")),
                uncached_input=safe_int(usage.get("input_tokens")),
                output=safe_int(usage.get("output_tokens")),
                reasoning=safe_int(details.get("thinking_tokens")),
                units="provider_reported_message_tokens",
                coverage="full",
                inclusion={"cached_input_in_input": False, "cache_write_in_input": False, "reasoning_in_output": True},
                project_hit=project_hit_file,
            )
            if "thinking_tokens" not in details:
                record.unknowns["reasoning"] = "thinking_tokens_not_reported"
            if saw_cost_state:
                record.unknowns["cost"] = "cost_state_is_session_aggregate_not_turn_attributable"
                record.unknowns["currency"] = "cost_state_is_session_aggregate_not_turn_attributable"
            else:
                record.unknowns["cost"] = "cost_not_reported_in_claude_transcript"
                record.unknowns["currency"] = "currency_not_reported_in_claude_transcript"
            record.unknowns["retry"] = "retry_not_observed_in_claude_transcript"
            result.records.append(finish_record(record))
    if unknown_time_activities:
        result.notices.append(
            ScanNotice(provider, "claude_transcript", f"{unknown_time_activities} unknown-time tool activities excluded from windowed stats", "warning")
        )
    return result
