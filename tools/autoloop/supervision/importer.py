"""Import legacy autoloop run-state findings without losing dispositions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from .model import Disposition, EvidenceNature, Finding, ImportedRun

_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|sh|js|jsx|ts|tsx|json|sql|md|yml|yaml|toml|txt))(?::(?P<line>\d+))?"
)

_RESOLVED_STATUSES = {"resolved", "closed", "closed_needs_safeguard", "safeguarded"}


def load_run_state(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical_theme(finding_id: str, explicit: object = None) -> str:
    value = str(explicit or finding_id).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or finding_id


def import_run_state(state: Mapping[str, Any], *, as_of: str | None = None) -> ImportedRun:
    """Convert a real run-state dictionary into canonical findings and events.

    The accepted input is the checkpoint shape used by autoloop: ``findings``
    is keyed by finding ID and each value carries fields such as severity,
    source, summary, evidence and status.  ``history`` contains events such as
    ``record-finding``, ``resolve-finding`` and ``shard-passed``.  When
    ``as_of`` is supplied, finding membership and open/fixed state are projected
    from those events through that ISO timestamp while immutable fields are
    copied from the final checkpoint record.
    """

    history = tuple(dict(event) for event in (state.get("history") or ()))
    ordered_history = _ordered_events(history)
    if as_of is not None:
        ordered_history = tuple(event for event in ordered_history if str(event.get("at") or "") <= as_of)
    raw_findings = state.get("findings") or {}
    projected = _project_raw_findings(raw_findings, ordered_history) if as_of is not None else raw_findings
    tree_by_id = _tree_by_finding(ordered_history)
    findings = tuple(
        sorted(
            (import_finding(fid, data, implicated_tree=tree_by_id.get(fid)) for fid, data in projected.items()),
            key=lambda finding: finding.finding_id,
        )
    )
    return ImportedRun(findings=findings, history=tuple(dict(event) for event in ordered_history))


def import_finding(
    finding_id: str,
    data: Mapping[str, Any],
    *,
    implicated_tree: str | None = None,
) -> Finding:
    raw = dict(data or {})
    status = str(raw.get("status") or "open").strip().lower().replace("-", "_")
    disposition_text = str(raw.get("disposition") or "").strip().lower().replace("-", "_")
    if disposition_text:
        disposition = disposition_text
    elif status in _RESOLVED_STATUSES:
        disposition = Disposition.RESOLVED.value
    elif status == "fixed_pending_review":
        disposition = Disposition.FIXED_PENDING_REVIEW.value
    elif status == "false_positive":
        disposition = Disposition.FALSE_POSITIVE.value
    elif status == "waived":
        disposition = Disposition.WAIVED.value
    else:
        disposition = Disposition.OPEN.value

    resolution = raw.get("resolution")
    if resolution is None:
        resolution = raw.get("fix_summary") or raw.get("fixed_by") or raw.get("resolved_at")
    if resolution is not None:
        resolution = str(resolution)
    if disposition == Disposition.FALSE_POSITIVE.value and resolution is None:
        resolution = "false positive recorded explicitly"

    nature = str(
        raw.get("evidence_nature")
        or raw.get("observation_assumption")
        or (EvidenceNature.ASSUMPTION.value if raw.get("assumption") else EvidenceNature.OBSERVATION.value)
    ).strip().lower()

    summary = str(raw.get("summary") or "")
    path, line = _extract_path(summary)
    explicit_path = raw.get("implicated_path") or raw.get("path")
    if explicit_path:
        path = str(explicit_path)
    explicit_line = raw.get("implicated_line") or raw.get("line")
    if explicit_line is not None:
        try:
            line = int(explicit_line)
        except (TypeError, ValueError):
            line = None

    tree = raw.get("implicated_tree") or raw.get("tree") or implicated_tree
    return Finding(
        finding_id=str(raw.get("finding_id") or finding_id),
        canonical_theme=canonical_theme(
            str(raw.get("finding_id") or finding_id), raw.get("canonical_theme") or raw.get("theme")
        ),
        severity=str(raw.get("severity") or "P2").upper(),
        evidence_nature=nature,
        implicated_path=path,
        implicated_tree=str(tree) if tree else None,
        evidence_id=str(raw.get("evidence_id") or raw.get("evidence") or ""),
        protected_invariant=(str(raw["protected_invariant"]) if raw.get("protected_invariant") is not None else None),
        disposition=disposition,
        resolution=resolution,
        summary=summary,
        source=str(raw.get("source") or "review"),
        opened_at=(str(raw["opened_at"]) if raw.get("opened_at") is not None else None),
        resolved_at=(str(raw["resolved_at"]) if raw.get("resolved_at") is not None else None),
        implicated_line=line,
        blocking=bool(raw.get("blocking", True)),
        ambiguous=bool(raw.get("ambiguous", False)),
        impact=str(raw.get("impact") or "medium").lower(),
        deterministic_reproduction_available=bool(raw.get("deterministic_reproduction_available", False)),
        reproduction_cost_tokens=int(raw.get("reproduction_cost_tokens") or 0),
        reproduction_attempted=bool(raw.get("reproduction_attempted", False)),
        reproduced=bool(raw.get("reproduced", False)),
        investigation_attempted=bool(raw.get("investigation_attempted", False)),
        fix_cost_estimate_tokens=(
            int(raw["fix_cost_estimate_tokens"]) if raw.get("fix_cost_estimate_tokens") is not None else None
        ),
        owner_decision_required=bool(raw.get("owner_decision_required", False)),
        owner_decision=(str(raw["owner_decision"]) if raw.get("owner_decision") is not None else None),
        fix_induced=bool(raw.get("fix_induced", False)),
    )


def _extract_path(summary: str) -> tuple[str | None, int | None]:
    match = _PATH_RE.search(summary)
    if match is None:
        return None, None
    line = match.group("line")
    return match.group("path"), int(line) if line else None


def _ordered_events(history: tuple[dict, ...]) -> tuple[dict, ...]:
    return tuple(sorted((dict(event) for event in history), key=_event_key))


def _event_key(event: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(event.get("at") or ""),
        str(event.get("event") or ""),
        str(event.get("finding_id") or event.get("shard") or ""),
        json.dumps(event, sort_keys=True, separators=(",", ":"), default=str),
    )


def _project_raw_findings(raw_findings: Mapping[str, Any], history: tuple[dict, ...]) -> dict[str, dict[str, Any]]:
    projected: dict[str, dict[str, Any]] = {}
    for event in history:
        if event.get("event") != "record-finding":
            continue
        fid = str(event.get("finding_id") or "")
        if not fid or fid not in raw_findings:
            continue
        item = dict(raw_findings[fid] or {})
        item["opened_at"] = event.get("at") or item.get("opened_at")
        if event.get("fixed_by"):
            item["status"] = "fixed_pending_review"
            item["fixed_by"] = event.get("fixed_by")
            item["resolved_at"] = event.get("at") or item.get("resolved_at")
        else:
            item["status"] = "open"
            item.pop("fixed_by", None)
            item.pop("fix_summary", None)
            item.pop("resolved_at", None)
        projected[fid] = item

    for event in history:
        if event.get("event") != "resolve-finding":
            continue
        fid = str(event.get("finding_id") or "")
        if fid not in projected:
            continue
        item = dict(projected[fid])
        item["status"] = "fixed_pending_review"
        item["resolved_at"] = event.get("at") or item.get("resolved_at")
        original = dict(raw_findings.get(fid) or {})
        if original.get("fix_summary"):
            item["fix_summary"] = original["fix_summary"]
        if original.get("fixed_by"):
            item["fixed_by"] = original["fixed_by"]
        projected[fid] = item
    return projected


def _tree_by_finding(history: tuple[dict, ...]) -> dict[str, str]:
    latest_tree: str | None = None
    result: dict[str, str] = {}
    for event in history:
        tree = event.get("code_tree_sha256") or event.get("tree")
        if tree:
            latest_tree = str(tree)
        if event.get("event") == "record-finding" and event.get("finding_id") and latest_tree:
            result.setdefault(str(event["finding_id"]), latest_tree)
    return result
