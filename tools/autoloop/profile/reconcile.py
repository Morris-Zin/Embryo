from __future__ import annotations

from collections import Counter
from typing import Optional

from .model import ScanNotice, ScanResult, UsageRecord
from .util import parse_time

TOKEN_FIELDS = ("cached_input", "cache_write", "uncached_input", "output", "reasoning")


def session_key(record: UsageRecord) -> tuple[str, str] | None:
    if not record.native_session_id:
        return None
    return (record.provider, record.native_session_id)


DETAILED_SOURCES = {"claude_transcript", "codex_rollout"}


def token_shape(record: UsageRecord) -> tuple[int | None, ...]:
    return tuple(getattr(record, field_name) for field_name in TOKEN_FIELDS)


def same_timestamp(left: object, right: object) -> bool:
    left_time = parse_time(left)
    right_time = parse_time(right)
    return left_time is not None and right_time is not None and left_time == right_time


def classify_provider_overlap(record: UsageRecord, detailed_records: list[UsageRecord]) -> str:
    if not detailed_records or not record.has_usage():
        return "no_detailed_usage"

    detailed_request_ids = {detail.native_request_id for detail in detailed_records if detail.native_request_id}
    covered_ids = {value for value in record.covered_request_ids if value}
    if covered_ids:
        matched = [detail for detail in detailed_records if detail.native_request_id in covered_ids]
        if matched and covered_ids.issubset(detailed_request_ids):
            return "overlap"
        if not matched and all(detail.native_request_id for detail in detailed_records):
            return "unmatched"
        return "unresolved"

    if record.native_request_id:
        if any(detail.native_request_id == record.native_request_id for detail in detailed_records):
            return "overlap"
        if all(detail.native_request_id for detail in detailed_records):
            return "unmatched"
        return "unresolved"

    shape = token_shape(record)
    if record.native_turn_id:
        same_turn = [detail for detail in detailed_records if detail.native_turn_id == record.native_turn_id]
        if any(token_shape(detail) == shape for detail in same_turn):
            return "overlap"
        if same_turn:
            return "unresolved"
        if all(detail.native_turn_id for detail in detailed_records):
            return "unmatched"
        return "unresolved"

    if any(token_shape(detail) == shape and same_timestamp(record.observed_at, detail.observed_at) for detail in detailed_records):
        return "overlap"
    return "unresolved"


def reconcile_scan(scan: ScanResult) -> ScanResult:
    metadata: dict[tuple[str, str], dict[str, object]] = {}
    for record in scan.records:
        if record.source != "t3_state":
            continue
        key = session_key(record)
        if key is None:
            continue
        entry = metadata.setdefault(key, {"project_hit": False, "_retry_values": set(), "_run_values": set()})
        entry["project_hit"] = bool(entry.get("project_hit")) or record.project_hit
        if record.retry is not None:
            entry["_retry_values"].add(record.retry)
        if record.run is not None:
            entry["_run_values"].add(record.run)
        for field_name in ("role", "shard", "task", "thread_id", "model"):
            if not entry.get(field_name) and getattr(record, field_name):
                entry[field_name] = getattr(record, field_name)

    for record in scan.records:
        if record.source == "t3_state":
            continue
        key = session_key(record)
        if key is None or key not in metadata:
            continue
        entry = metadata[key]
        if entry.get("role") and (record.role in (None, "worker", "sidechain")):
            record.role = str(entry["role"])
        for field_name in ("shard", "task", "thread_id", "model"):
            if getattr(record, field_name) is None and entry.get(field_name):
                setattr(record, field_name, entry[field_name])
                record.unknowns.pop(field_name, None)
        retry_values = entry.get("_retry_values") or set()
        if record.retry is None and len(retry_values) == 1:
            record.retry = next(iter(retry_values))
            record.unknowns.pop("retry", None)
        run_values = entry.get("_run_values") or set()
        if record.run is None and len(run_values) == 1:
            record.run = next(iter(run_values))
            record.unknowns.pop("run", None)
        record.project_hit = record.project_hit or bool(entry.get("project_hit"))

    for activity in scan.activities:
        key = (activity.provider, activity.native_session_id) if activity.native_session_id else None
        if key is None or key not in metadata:
            continue
        entry = metadata[key]
        if activity.role in (None, "worker") and entry.get("role"):
            activity.role = str(entry["role"])
        if activity.thread_id is None and entry.get("thread_id"):
            activity.thread_id = str(entry["thread_id"])
        retry_values = entry.get("_retry_values") or set()
        if activity.retry is None and len(retry_values) == 1:
            activity.retry = next(iter(retry_values))
        run_values = entry.get("_run_values") or set()
        if activity.run is None and len(run_values) == 1:
            activity.run = next(iter(run_values))
        activity.project_hit = activity.project_hit or bool(entry.get("project_hit"))

    detailed_activity_keys = {
        (activity.provider, activity.native_session_id)
        for activity in scan.activities
        if activity.native_session_id and activity.source in {"claude_transcript", "codex_rollout"}
    }
    before_activities = len(scan.activities)
    scan.activities = [
        activity
        for activity in scan.activities
        if not (
            activity.source == "t3_state"
            and activity.native_session_id
            and (activity.provider, activity.native_session_id) in detailed_activity_keys
        )
    ]
    shadowed_activities = before_activities - len(scan.activities)

    detailed_by_session: dict[tuple[str, str], list[UsageRecord]] = {}
    for detailed in scan.records:
        key = session_key(detailed)
        if key is not None and detailed.source in DETAILED_SOURCES and detailed.has_usage():
            detailed_by_session.setdefault(key, []).append(detailed)
    provider_log_usage_keys = {
        key
        for key, record in ((session_key(record), record) for record in scan.records)
        if key is not None and record.source == "t3_provider_log" and record.has_usage()
    }

    kept: list[UsageRecord] = []
    dropped = Counter()
    cost_only = 0
    unresolved_overlap = 0
    unresolved_tokens = 0
    seen: set[tuple[str, str]] = set()
    for record in scan.records:
        key = session_key(record)
        if record.source == "t3_state" and key is not None and (key in detailed_by_session or key in provider_log_usage_keys):
            dropped["t3_state_shadowed_by_finer_usage"] += 1
            continue
        if record.source == "t3_provider_log" and key is not None and key in detailed_by_session:
            overlap = classify_provider_overlap(record, detailed_by_session[key])
            if overlap == "overlap":
                if record.provider == "claude" and record.cost is not None:
                    for field_name in TOKEN_FIELDS:
                        setattr(record, field_name, None)
                        record.unknowns[field_name] = "tokens_counted_once_in_detailed_claude_transcript"
                    record.coverage = "cost_only"
                    record.granularity = "turn_cost"
                    cost_only += 1
                else:
                    dropped["provider_log_shadowed_by_detailed_usage"] += 1
                    continue
            elif overlap == "unresolved":
                unresolved_overlap += 1
                unresolved_tokens += record.raw_measured_tokens() or 0
                record.coverage = "unresolved_overlap"
                record.unknowns["overlap"] = "overlap_with_detailed_usage_could_not_be_decided"
        dedupe_key = (record.source, record.record_id)
        if dedupe_key in seen:
            dropped["duplicate_record_id"] += 1
            continue
        seen.add(dedupe_key)
        kept.append(record)

    scan.records = kept
    if shadowed_activities:
        scan.notices.append(ScanNotice("all", "reconcile", f"{shadowed_activities} T3 tool activities suppressed: detailed_transcript_or_rollout_activity_preferred"))
    for reason, count in sorted(dropped.items()):
        scan.notices.append(ScanNotice("all", "reconcile", f"{count} records suppressed: {reason}"))
    if cost_only:
        scan.notices.append(ScanNotice("claude", "reconcile", f"{cost_only} turn records retained as cost-only to avoid double-counting detailed tokens"))
    if unresolved_overlap:
        scan.notices.append(
            ScanNotice(
                "all",
                "reconcile",
                f"{unresolved_overlap} provider-log records have unresolved coverage against detailed usage; {unresolved_tokens} tokens excluded from measured totals",
                "warning",
            )
        )
    return scan


def filter_project(scan: ScanResult, project_filter: str | None) -> ScanResult:
    if not project_filter:
        return scan
    scan.records = [record for record in scan.records if record.project_hit]
    scan.activities = [activity for activity in scan.activities if activity.project_hit]
    return scan
