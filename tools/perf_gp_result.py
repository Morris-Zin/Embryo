"""Fast Gross Profit execute() contract shared by the perf runners."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ComputedGrossProfit:
    columns: list[Any]
    rows: list[Any]


@dataclass(frozen=True, slots=True)
class UnavailableGrossProfit:
    columns: list[Any]
    status: Literal["pending", "gated"]
    reason: str


GrossProfitResult = ComputedGrossProfit | UnavailableGrossProfit

# The report's three-element response carries the reason but omits the mode
# and counts. Pending reasons come from query_profit and evaluate_work_budget.
PENDING_REASONS = frozenset({
    "too_many_dirty_or_missing_summary_days",
    "blocking_repost_item_valuation",
    "combined_live_rows_exceeds_sync_row_budget",
    "Profit summary repost triggers are not installed",
})


def classify_gross_profit_result(result: Any) -> GrossProfitResult:
    if (
        not isinstance(result, (list, tuple))
        or len(result) < 2
        or not isinstance(result[0], list)
        or not isinstance(result[1], list)
    ):
        return UnavailableGrossProfit([], "gated", "malformed report response")
    columns, rows = result[:2]
    if len(result) == 2 or rows:
        return ComputedGrossProfit(columns, rows)
    reason = str(result[2] or "").strip() or "report returned no rows and no reason"
    status = "pending" if reason in PENDING_REASONS else "gated"
    return UnavailableGrossProfit(columns, status, reason)


def build_report_runner_source(runner_path: Path, dependencies: tuple[str, ...]) -> bytes:
    """Package a runner and its sibling modules for the container's Python stdin."""
    sources = {
        name: runner_path.with_name(name + ".py").read_text(encoding="utf-8")
        for name in dependencies
    }
    runner_source = runner_path.read_text(encoding="utf-8")
    bootstrap = (
        "import sys as _runner_sys, types as _runner_types\n"
        f"for _name, _source in {sources!r}.items():\n"
        "    _module = _runner_types.ModuleType(_name)\n"
        "    _module.__file__ = f'<embedded {_name}.py>'\n"
        "    _runner_sys.modules[_name] = _module\n"
        "    exec(compile(_source, _module.__file__, 'exec'), _module.__dict__)\n"
        f"exec(compile({runner_source!r}, {runner_path.name!r}, 'exec'))\n"
    )
    return bootstrap.encode("utf-8")
