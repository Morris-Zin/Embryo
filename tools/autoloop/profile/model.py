from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UsageRecord:
    provider: str
    source: str
    record_id: str
    granularity: str
    observed_at: str | None = None
    native_session_id: str | None = None
    native_request_id: str | None = None
    native_turn_id: str | None = None
    covered_request_ids: tuple[str, ...] = ()
    thread_id: str | None = None
    run: str | None = None
    shard: str | None = None
    task: str | None = None
    model: str | None = None
    role: str | None = None
    retry: int | None = None
    turn: int | None = None
    cached_input: int | None = None
    cache_write: int | None = None
    uncached_input: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cost: float | None = None
    currency: str | None = None
    units: str = "provider_reported_tokens"
    coverage: str = "partial"
    inclusion: dict[str, object] = field(default_factory=dict)
    unknowns: dict[str, str] = field(default_factory=dict)
    project_hit: bool = False
    duplicate: bool = False

    def mark_unknown(self, field_name: str, reason: str) -> None:
        setattr(self, field_name, None)
        self.unknowns[field_name] = reason

    def has_usage(self) -> bool:
        return any(
            value is not None
            for value in (
                self.cached_input,
                self.cache_write,
                self.uncached_input,
                self.output,
                self.reasoning,
            )
        )

    def raw_measured_tokens(self) -> int | None:
        parts = [self.uncached_input, self.cached_input, self.cache_write, self.output]
        if all(value is None for value in parts):
            return None
        total = sum(value or 0 for value in parts)
        if self.reasoning is not None and self.inclusion.get("reasoning_in_output") is False:
            total += self.reasoning
        return total

    def measured_tokens(self) -> int | None:
        if self.coverage == "unresolved_overlap":
            return None
        return self.raw_measured_tokens()


@dataclass
class ToolActivity:
    provider: str
    source: str
    activity_id: str
    observed_at: str | None = None
    native_session_id: str | None = None
    thread_id: str | None = None
    run: str | None = None
    role: str | None = None
    retry: int | None = None
    turn: int | None = None
    tool_name: str | None = None
    input_bytes: int | None = None
    output_bytes: int | None = None
    read_path_hashes: tuple[str, ...] = ()
    project_hit: bool = False


@dataclass
class ScanNotice:
    provider: str
    source: str
    message: str
    severity: str = "info"


@dataclass
class ScanResult:
    records: list[UsageRecord] = field(default_factory=list)
    activities: list[ToolActivity] = field(default_factory=list)
    notices: list[ScanNotice] = field(default_factory=list)
    providers: set[str] = field(default_factory=set)

    def extend(self, other: ScanResult) -> None:
        self.records.extend(other.records)
        self.activities.extend(other.activities)
        self.notices.extend(other.notices)
        self.providers.update(other.providers)
