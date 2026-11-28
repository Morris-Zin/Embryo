from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from typing import Any, Optional

from .model import ScanResult, ToolActivity, UsageRecord

COVERAGE_FIELDS = (
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


def coverage_table(records: list[UsageRecord], providers: set[str]) -> list[dict[str, Any]]:
    all_providers = sorted(providers | {record.provider for record in records})
    rows: list[dict[str, Any]] = []
    for provider in all_providers:
        provider_records = [record for record in records if record.provider == provider]
        row: dict[str, Any] = {"provider": provider, "records": len(provider_records)}
        for field_name in COVERAGE_FIELDS:
            if not provider_records:
                state = "not observed"
                fraction = 0.0
            else:
                observed = sum(1 for record in provider_records if getattr(record, field_name) is not None)
                fraction = observed / len(provider_records)
                if observed == 0:
                    state = "not observed"
                elif fraction >= 0.8:
                    state = "observed"
                else:
                    state = "partial"
            row[field_name] = state
            row[field_name + "_fraction"] = round(fraction, 4)
        rows.append(row)
    return rows


def _sum_tokens(records: list[UsageRecord]) -> tuple[int | None, int]:
    values = [record.measured_tokens() for record in records]
    measured = [value for value in values if value is not None]
    if not measured:
        return None, 0
    return sum(measured), len(measured)


def _sum_cost(records: list[UsageRecord]) -> tuple[float | None, int]:
    measured = [record.cost for record in records if record.cost is not None]
    if not measured:
        return None, 0
    return sum(measured), len(measured)


def _token_sink(kind: str, key: str, records: list[UsageRecord]) -> dict[str, Any]:
    tokens, measured_count = _sum_tokens(records)
    cost, cost_count = _sum_cost(records)
    return {
        "scope": "tokens",
        "kind": kind,
        "key": key,
        "records": len(records),
        "measured_records": measured_count,
        "tokens": tokens,
        "cost": cost,
        "cost_records": cost_count,
        "tool_output_bytes": None,
        "reads": 0,
        "extra_reads": 0,
    }


def build_sinks(records: list[UsageRecord], activities: list[ToolActivity]) -> list[dict[str, Any]]:
    token_rows: list[dict[str, Any]] = []
    delegation = [
        record
        for record in records
        if record.role in {"delegation", "sidechain", "subagent"} or str(record.thread_id or "").startswith("thread:delegated-task")
    ]
    if delegation:
        token_rows.append(_token_sink("delegation", "delegated_or_sidechain", delegation))
    review = [record for record in records if record.role == "review"]
    if review:
        token_rows.append(_token_sink("review", "review", review))
    retried = [record for record in records if record.retry is not None and record.retry > 0]
    if retried:
        token_rows.append(_token_sink("retry", "retry_gt_0", retried))
    by_role: dict[str, list[UsageRecord]] = defaultdict(list)
    for record in records:
        if record.role:
            by_role[str(record.role)].append(record)
    for role, rows in sorted(by_role.items()):
        if role not in {"review", "delegation", "sidechain", "subagent"}:
            token_rows.append(_token_sink("role", role, rows))

    token_rows.sort(key=lambda row: (row["tokens"] is not None, row["tokens"] or 0, row["records"]), reverse=True)

    tool_groups: dict[str, list[ToolActivity]] = defaultdict(list)
    for activity in activities:
        tool_groups[str(activity.tool_name or "unknown")].append(activity)
    tool_rows: list[dict[str, Any]] = []
    for tool_name, rows in tool_groups.items():
        output_values = [row.output_bytes for row in rows if row.output_bytes is not None]
        input_values = [row.input_bytes for row in rows if row.input_bytes is not None]
        tool_rows.append(
            {
                "scope": "tool_output_bytes",
                "kind": "tool_output",
                "key": tool_name,
                "records": len(rows),
                "measured_records": 0,
                "tokens": None,
                "cost": None,
                "cost_records": 0,
                "tool_output_bytes": sum(output_values) if output_values else None,
                "tool_input_bytes": sum(input_values) if input_values else None,
                "reads": sum(len(row.read_path_hashes) for row in rows),
                "extra_reads": 0,
            }
        )
    tool_rows.sort(key=lambda row: (row["tool_output_bytes"] or 0, row["records"]), reverse=True)

    read_counts: Counter[tuple[str, str, str]] = Counter()
    read_output: Counter[tuple[str, str, str]] = Counter()
    for activity in activities:
        for digest in activity.read_path_hashes:
            key = (activity.provider, activity.native_session_id or "unknown-session", digest)
            read_counts[key] += 1
            if len(activity.read_path_hashes) == 1 and activity.output_bytes is not None:
                read_output[key] += activity.output_bytes
    repeat_rows: list[dict[str, Any]] = []
    for (provider, session, digest), count in read_counts.items():
        if count < 2:
            continue
        repeat_rows.append(
            {
                "scope": "read_count",
                "kind": "repeated_file_read",
                "key": f"{provider}:{digest}:{hashlib.sha256(session.encode('utf-8', errors='replace')).hexdigest()[:8]}",
                "records": count,
                "measured_records": 0,
                "tokens": None,
                "cost": None,
                "cost_records": 0,
                "tool_output_bytes": read_output.get(key, 0),
                "reads": count,
                "extra_reads": count - 1,
                "session_count": 1,
            }
        )
    repeat_rows.sort(key=lambda row: (row["extra_reads"], row["tool_output_bytes"] or 0), reverse=True)
    return token_rows + tool_rows + repeat_rows


def select_top_sinks(sinks: list[dict[str, Any]], top: int = 10) -> list[dict[str, Any]]:
    token_rows = [row for row in sinks if row["scope"] == "tokens"]
    tool_rows = [row for row in sinks if row["scope"] == "tool_output_bytes"]
    repeat_rows = [row for row in sinks if row["scope"] == "read_count"]
    selected: list[dict[str, Any]] = []
    selected.extend(token_rows[:top])
    remaining = top - len(selected)
    tool_quota = min(3, len(tool_rows), remaining)
    repeat_quota = min(3, len(repeat_rows), max(0, remaining - tool_quota))
    selected.extend(tool_rows[:tool_quota])
    selected.extend(repeat_rows[:repeat_quota])
    pools = [tool_rows[tool_quota:], repeat_rows[repeat_quota:]]
    while len(selected) < top and (pools[0] or pools[1]):
        for pool in pools:
            if len(selected) >= top:
                break
            if pool:
                selected.append(pool.pop(0))
    return selected


def build_report(scan: ScanResult, since: str, project_filter: str | None) -> dict[str, Any]:
    coverage = coverage_table(scan.records, scan.providers)
    sinks = build_sinks(scan.records, scan.activities)
    total_tokens, measured_records = _sum_tokens(scan.records)
    total_cost, cost_records = _sum_cost(scan.records)
    return {
        "since": since,
        "project_filter": project_filter,
        "record_count": len(scan.records),
        "activity_count": len(scan.activities),
        "measured_token_records": measured_records,
        "measured_tokens": total_tokens,
        "cost": total_cost,
        "cost_records": cost_records,
        "coverage": coverage,
        "sinks": sinks,
        "top_sinks": select_top_sinks(sinks, 10),
        "notices": [
            {"provider": notice.provider, "source": notice.source, "severity": notice.severity, "message": notice.message}
            for notice in scan.notices
        ],
    }


def _fmt(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def render_markdown(report: dict[str, Any], top: int = 10) -> str:
    lines: list[str] = []
    lines.append("# Agent usage profile")
    lines.append("")
    lines.append(
        f"since={report['since']} project={report.get('project_filter') or 'all'} records={report['record_count']} "
        f"activities={report['activity_count']} measured_tokens={_fmt(report['measured_tokens'])} cost={_fmt(report['cost'])}"
    )
    lines.append("")
    lines.append("## Coverage")
    headers = ["provider", "records", *COVERAGE_FIELDS]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "---|" * len(headers))
    for row in report["coverage"]:
        lines.append("| " + " | ".join(_fmt(row.get(field)) for field in headers) + " |")
    lines.append("")
    lines.append(f"## Top {top} sinks")
    sink_headers = ["rank", "scope", "kind", "key", "records", "measured_records", "tokens", "cost", "tool_output_bytes", "reads", "extra_reads"]
    lines.append("| " + " | ".join(sink_headers) + " |")
    lines.append("|" + "---|" * len(sink_headers))
    sink_rows = report.get("top_sinks") if top == 10 else select_top_sinks(report["sinks"], top)
    for index, row in enumerate(sink_rows[:top], 1):
        values = [index, row.get("scope"), row.get("kind"), row.get("key"), row.get("records"), row.get("measured_records"), row.get("tokens"), row.get("cost"), row.get("tool_output_bytes"), row.get("reads"), row.get("extra_reads")]
        lines.append("| " + " | ".join(_fmt(value) for value in values) + " |")
    if report.get("notices"):
        lines.append("")
        lines.append("## Notices")
        for notice in report["notices"]:
            lines.append(f"- {notice['severity']}: {notice['provider']}/{notice['source']}: {notice['message']}")
    return "\n".join(lines) + "\n"
