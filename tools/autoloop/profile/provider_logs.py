from __future__ import annotations

import json
from pathlib import Path

from .common import finish_record
from .model import ScanNotice, ScanResult, UsageRecord
from .util import canonical_provider, is_since, safe_float, safe_int, since_datetime


def read_t3_provider_logs(log_dir: object, since: str) -> ScanResult:
    result = ScanResult()
    root = Path(str(log_dir)).expanduser()
    if not root.exists():
        result.notices.append(ScanNotice("t3", "t3_provider_log", f"T3 provider log directory not found: {root}", "warning"))
        return result
    since_dt = since_datetime(since)
    files = sorted(path for path in root.glob("events*.log*") if path.is_file())
    acp_no_usage_files: set[str] = set()
    for path in files:
        turn_counters: dict[str, int] = {}
        codex_turn_counters: dict[str, int] = {}
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line_number, line in enumerate(handle, 1):
                if len(line) < 2 or line[0] != "[":
                    continue
                observed = line[1 : line.find("]")] if "]" in line else None
                if not is_since(observed, since_dt):
                    continue
                pos = line.find("{")
                if pos < 0:
                    continue
                try:
                    outer = json.loads(line[pos:])
                except json.JSONDecodeError:
                    continue
                event = outer.get("event") if isinstance(outer.get("event"), dict) else {}
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                provider_raw = outer.get("provider") or event.get("provider")
                provider = canonical_provider(provider_raw)
                if payload.get("type") == "result" and isinstance(payload.get("model_usage_since_last_result"), dict):
                    session_id = str(payload.get("session_id") or "") or None
                    counter_key = str(path) + ":" + str(session_id)
                    turn_counters[counter_key] = turn_counters.get(counter_key, 0) + 1
                    turn = turn_counters[counter_key]
                    result_uuid = str(payload.get("uuid") or f"{path.name}:{line_number}")
                    for model, model_usage in payload["model_usage_since_last_result"].items():
                        if not isinstance(model_usage, dict):
                            continue
                        record = UsageRecord(
                            provider="claude",
                            source="t3_provider_log",
                            record_id=f"t3-claude-result:{result_uuid}:{model}",
                            granularity="turn",
                            observed_at=observed,
                            native_session_id=session_id,
                            covered_request_ids=tuple(str(value) for value in payload.get("begun_message_ids") or [] if value),
                            model=str(model),
                            turn=turn,
                            cached_input=safe_int(model_usage.get("cacheReadInputTokens")),
                            cache_write=safe_int(model_usage.get("cacheCreationInputTokens")),
                            uncached_input=safe_int(model_usage.get("inputTokens")),
                            output=safe_int(model_usage.get("outputTokens")),
                            reasoning=safe_int(model_usage.get("thinkingTokens")),
                            cost=safe_float(model_usage.get("costUSD")),
                            currency="USD" if safe_float(model_usage.get("costUSD")) is not None else None,
                            units="provider_reported_incremental_result_tokens",
                            coverage="full",
                            inclusion={"cached_input_in_input": False, "cache_write_in_input": False, "reasoning_in_output": True},
                        )
                        if record.cost is None:
                            record.unknowns["cost"] = "incremental_cost_not_reported"
                            record.unknowns["currency"] = "incremental_cost_not_reported"
                        record.unknowns["retry"] = "retry_not_observed_in_t3_provider_log"
                        result.records.append(finish_record(record))
                        result.providers.add("claude")
                elif payload.get("method") == "thread/tokenUsage/updated":
                    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                    token_usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
                    usage = token_usage.get("last") if isinstance(token_usage.get("last"), dict) else {}
                    if not usage:
                        continue
                    native_thread = str(params.get("threadId") or "") or None
                    key = str(path) + ":" + str(native_thread) + ":" + str(params.get("turnId"))
                    codex_turn_counters[key] = codex_turn_counters.get(key, 0) + 1
                    input_total = safe_int(usage.get("inputTokens"))
                    cached = safe_int(usage.get("cachedInputTokens"))
                    cache_write = safe_int(usage.get("cacheWriteInputTokens"))
                    uncached = None
                    if input_total is not None and cached is not None and input_total >= cached + (cache_write or 0):
                        uncached = input_total - cached - (cache_write or 0)
                    record = UsageRecord(
                        provider="codex",
                        source="t3_provider_log",
                        record_id=f"t3-codex-usage:{path.name}:{line_number}",
                        granularity="request",
                        observed_at=observed,
                        native_session_id=native_thread,
                        native_turn_id=str(params.get("turnId") or "") or None,
                        turn=codex_turn_counters[key],
                        cached_input=cached,
                        cache_write=cache_write,
                        uncached_input=uncached,
                        output=safe_int(usage.get("outputTokens")),
                        reasoning=safe_int(usage.get("reasoningOutputTokens")),
                        units="provider_reported_response_tokens",
                        coverage="full" if uncached is not None else "partial",
                        inclusion={"cached_input_in_input": True, "cache_write_in_input": True, "reasoning_in_output": True},
                    )
                    record.unknowns["cost"] = "cost_not_reported_in_t3_codex_log"
                    record.unknowns["currency"] = "currency_not_reported_in_t3_codex_log"
                    record.unknowns["retry"] = "retry_not_observed_in_t3_provider_log"
                    result.records.append(finish_record(record))
                    result.providers.add("codex")
                elif provider == "devin":
                    acp_no_usage_files.add(path.name)
    if acp_no_usage_files:
        result.providers.add("devin")
        result.notices.append(
            ScanNotice("devin", "t3_provider_log", f"ACP/Devin events observed in {len(acp_no_usage_files)} provider log files, but no usage counters were present", "warning")
        )
    return result
