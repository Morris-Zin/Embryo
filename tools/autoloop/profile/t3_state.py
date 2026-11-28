from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from .common import finish_record, has_numeric_usage, loads_object, tool_read_hashes
from .model import ScanNotice, ScanResult, ToolActivity, UsageRecord
from .util import canonical_provider, infer_role, infer_shard, is_since, payload_bytes, project_text_hit, safe_int, since_datetime


def _sqlite_uri(path: Path) -> str:
    return "file:" + quote(str(path).replace("\\", "/"), safe="/:") + "?mode=ro"


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
    return row is not None


def read_t3_state(db_path: object, since: str, project_filter: str | None = None) -> ScanResult:
    result = ScanResult()
    path = Path(str(db_path)).expanduser()
    if not path.exists():
        result.notices.append(ScanNotice("t3", "t3_state", f"T3 state database not found: {path}", "warning"))
        return result
    since_dt = since_datetime(since)
    try:
        con = sqlite3.connect(_sqlite_uri(path), uri=True)
        con.row_factory = sqlite3.Row
    except Exception as exc:
        result.notices.append(ScanNotice("t3", "t3_state", f"T3 state database unreadable: {type(exc).__name__}", "warning"))
        return result

    try:
        required = {
            "projection_projects",
            "orchestration_v2_projection_threads",
            "orchestration_v2_projection_provider_threads",
            "orchestration_v2_projection_provider_turns",
            "orchestration_v2_projection_runs",
            "orchestration_v2_projection_run_attempts",
            "orchestration_v2_projection_turn_items",
        }
        missing = sorted(name for name in required if not _table_exists(con, name))
        if missing:
            result.notices.append(ScanNotice("t3", "t3_state", "T3 state tables missing: " + ",".join(missing), "warning"))
            return result

        target_project_ids: set[str] = set()
        for row in con.execute("select project_id,title,workspace_root from projection_projects"):
            if project_text_hit(project_filter, row["title"], row["workspace_root"]):
                target_project_ids.add(str(row["project_id"]))

        threads: dict[str, dict[str, Any]] = {}
        for row in con.execute("select thread_id,project_id,title,payload_json from orchestration_v2_projection_threads"):
            payload = loads_object(row["payload_json"])
            worktree = payload.get("worktreePath")
            project_hit = str(row["project_id"] or "") in target_project_ids or project_text_hit(
                project_filter, worktree, payload.get("title"), row["title"]
            )
            model_selection = payload.get("modelSelection") if isinstance(payload.get("modelSelection"), dict) else {}
            threads[str(row["thread_id"])] = {
                "project_id": row["project_id"],
                "title": row["title"] or payload.get("title"),
                "worktree": worktree,
                "project_hit": project_hit,
                "model": model_selection.get("model"),
                "payload": payload,
            }

        provider_threads: dict[str, dict[str, Any]] = {}
        for row in con.execute("select provider_thread_id,thread_id,provider,provider_session_id,payload_json from orchestration_v2_projection_provider_threads"):
            payload = loads_object(row["payload_json"])
            native_ref = payload.get("nativeThreadRef") if isinstance(payload.get("nativeThreadRef"), dict) else {}
            provider_threads[str(row["provider_thread_id"])] = {
                "thread_id": row["thread_id"],
                "provider": row["provider"],
                "provider_session_id": row["provider_session_id"],
                "native_id": native_ref.get("nativeId"),
                "payload": payload,
            }

        runs: dict[str, dict[str, Any]] = {}
        for row in con.execute("select run_id,thread_id,ordinal,provider,payload_json from orchestration_v2_projection_runs"):
            payload = loads_object(row["payload_json"])
            model_selection = payload.get("modelSelection") if isinstance(payload.get("modelSelection"), dict) else {}
            runs[str(row["run_id"])] = {
                "thread_id": row["thread_id"],
                "ordinal": row["ordinal"],
                "provider": row["provider"],
                "model": model_selection.get("model"),
                "payload": payload,
            }

        attempts: dict[str, dict[str, Any]] = {}
        for row in con.execute("select attempt_id,run_id,attempt_ordinal,provider,payload_json from orchestration_v2_projection_run_attempts"):
            attempts[str(row["attempt_id"])] = {
                "run_id": row["run_id"],
                "attempt_ordinal": row["attempt_ordinal"],
                "provider": row["provider"],
                "payload": loads_object(row["payload_json"]),
            }

        since_text = since[:10]
        for row in con.execute(
            "select * from orchestration_v2_projection_provider_turns where coalesce(started_at, completed_at, '') >= ?",
            (since_text,),
        ):
            observed = row["started_at"] or row["completed_at"]
            if not is_since(observed, since_dt):
                continue
            payload = loads_object(row["payload_json"])
            provider_thread = provider_threads.get(str(row["provider_thread_id"]), {})
            attempt = attempts.get(str(row["run_attempt_id"]), {})
            run = runs.get(str(attempt.get("run_id") or ""), {})
            thread_id = str(row["thread_id"] or provider_thread.get("thread_id") or run.get("thread_id") or "") or None
            thread_meta = threads.get(thread_id or "", {})
            title = thread_meta.get("title")
            provider = canonical_provider(attempt.get("provider") or run.get("provider") or provider_thread.get("provider"))
            result.providers.add(provider)
            usage = payload.get("tokenUsage") if isinstance(payload.get("tokenUsage"), dict) else {}
            input_total = safe_int(usage.get("inputTokens"))
            cached = safe_int(usage.get("cachedInputTokens"))
            output = safe_int(usage.get("outputTokens"))
            reasoning = safe_int(usage.get("reasoningOutputTokens"))
            uncached: int | None = None
            if input_total is not None and cached is not None and input_total >= cached:
                uncached = input_total - cached
            ordinal = safe_int(row["ordinal"])
            attempt_ordinal = safe_int(attempt.get("attempt_ordinal"))
            retry = attempt_ordinal - 1 if attempt_ordinal is not None and attempt_ordinal > 0 else None
            native_id = provider_thread.get("native_id")
            record = UsageRecord(
                provider=provider,
                source="t3_state",
                record_id="t3-state:" + str(row["provider_turn_id"]),
                granularity="turn_snapshot",
                observed_at=usage.get("updatedAt") or observed,
                native_session_id=str(native_id) if native_id else None,
                thread_id=thread_id,
                run=str(attempt.get("run_id") or "") or None,
                shard=infer_shard(title),
                task=thread_id,
                model=run.get("model") or thread_meta.get("model"),
                role=infer_role(thread_id, title),
                retry=retry,
                turn=ordinal,
                cached_input=cached,
                uncached_input=uncached,
                output=output,
                reasoning=reasoning,
                units="provider_reported_turn_snapshot_tokens",
                coverage="partial",
                inclusion={"cached_input_in_input": True, "reasoning_in_output": True, "cache_write_in_input": None},
                project_hit=bool(thread_meta.get("project_hit")) or project_text_hit(project_filter, thread_meta.get("worktree")),
            )
            if input_total is None:
                record.unknowns["uncached_input"] = "input_tokens_not_observed"
            if cached is None:
                record.unknowns["cached_input"] = "cached_input_not_observed"
            record.unknowns["cache_write"] = "cache_write_not_reported_by_t3_snapshot"
            if output is None:
                record.unknowns["output"] = "output_not_observed"
            if reasoning is None:
                record.unknowns["reasoning"] = "reasoning_not_observed"
            record.unknowns["cost"] = "cost_not_reported_by_t3_snapshot"
            record.unknowns["currency"] = "currency_not_reported_by_t3_snapshot"
            if retry is None:
                record.unknowns["retry"] = "attempt_ordinal_not_observed"
            if not has_numeric_usage(usage):
                record.coverage = "none"
                for field_name in ("cached_input", "uncached_input", "output", "reasoning"):
                    record.unknowns.setdefault(field_name, "empty_token_usage_object")
            result.records.append(finish_record(record))

        tool_types = {"command_execution", "dynamic_tool", "web_search", "file_search"}
        for row in con.execute(
            "select * from orchestration_v2_projection_turn_items where updated_at >= ?",
            (since_text,),
        ):
            if not is_since(row["updated_at"], since_dt):
                continue
            item_type = str(row["type"] or "")
            if item_type not in tool_types:
                continue
            payload = loads_object(row["payload_json"])
            thread_id = str(row["thread_id"] or "") or None
            thread_meta = threads.get(thread_id or "", {})
            provider_thread = provider_threads.get(str(row["provider_thread_id"] or ""), {})
            provider = canonical_provider(provider_thread.get("provider") or "t3")
            tool_input = payload.get("input") if "input" in payload else payload.get("arguments")
            output_value = payload.get("output") if "output" in payload else payload.get("result")
            activity = ToolActivity(
                provider=provider,
                source="t3_state",
                activity_id="t3-state-item:" + str(row["turn_item_id"]),
                observed_at=row["updated_at"],
                native_session_id=str(provider_thread.get("native_id")) if provider_thread.get("native_id") else None,
                thread_id=thread_id,
                run=str(row["run_id"] or "") or None,
                role=infer_role(thread_id, thread_meta.get("title")),
                turn=safe_int(payload.get("ordinal")),
                tool_name=item_type,
                input_bytes=payload_bytes(tool_input),
                output_bytes=payload_bytes(output_value),
                read_path_hashes=tool_read_hashes(tool_input),
                project_hit=bool(thread_meta.get("project_hit")) or project_text_hit(project_filter, thread_meta.get("worktree")),
            )
            result.activities.append(activity)
            result.providers.add(provider)
    finally:
        con.close()
    return result
