"""Pure deterministic review-rule supervisor for enrolled autoloop runs."""

from __future__ import annotations

from typing import Any
from collections.abc import Iterable, Mapping

from .importer import import_finding
from .model import Action, ActionKind, Budget, Disposition, EvidenceNature, Finding, SupervisionContract

_ACTIVE_DISPOSITIONS = {Disposition.OPEN.value, Disposition.REOPENED.value}
_SEVERITY_RANK = {"P0": 3, "P1": 2, "P2": 1}
_IMPACT_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def next_action(
    findings: Iterable[Finding | Mapping[str, Any]],
    history: Iterable[Mapping[str, Any]],
    budget: Budget | Mapping[str, Any] | None,
    contract: SupervisionContract | Mapping[str, Any] | None,
) -> Action:
    """Choose the next review action using the ADR 0005 precedence.

    The function is pure and deterministic: it performs no I/O, reads no clock,
    launches no reproduction and makes no model call.  ``fixed_pending_review``
    is treated as a resolution that may proceed to the next obligation; open or
    reopened required findings are blockers.  Explicit false-positive and
    waived dispositions never block and are never dropped.
    """

    normalized_findings = tuple(sorted((_coerce_finding(item) for item in _finding_values(findings)), key=lambda item: item.finding_id))
    normalized_history = _ordered_events(tuple(dict(event) for event in history))
    normalized_budget = coerce_budget(budget)
    normalized_contract = coerce_contract(contract)

    missing_owner = _missing_owner_decisions(normalized_findings, normalized_contract)
    if missing_owner:
        return Action(
            ActionKind.CLARIFY,
            "owner-decision",
            "missing-owner-decision",
            missing_owner,
        )

    cap_reason = _cap_hit(normalized_budget, normalized_contract)
    if cap_reason is not None:
        return Action(ActionKind.ESCALATE, "cap", cap_reason, _active_ids(normalized_findings, normalized_contract))

    active = _active_blockers(normalized_findings, normalized_contract)
    oscillating = detect_oscillation(normalized_findings, normalized_history) if active else ()
    if oscillating:
        return Action(
            ActionKind.ESCALATE,
            "oscillation",
            "repeated-blocker-set-without-new-evidence",
            oscillating,
        )

    clarification = _select_clarification(active, normalized_history, normalized_budget, normalized_contract)
    if clarification is not None:
        return clarification

    if active:
        fix_induced = _fix_induced_themes(normalized_findings, normalized_history)
        induced_ids = tuple(
            finding.finding_id for finding in active if finding.fix_induced or finding.canonical_theme in fix_induced
        )
        reason = "fix-induced-blocker" if induced_ids else "new-blocker"
        return Action(
            ActionKind.ITERATE,
            "repair",
            reason,
            tuple(finding.finding_id for finding in active),
        )

    if not normalized_contract.checks_valid:
        return Action(ActionKind.ITERATE, "restore-checks", "checks-invalid", ())

    return Action(ActionKind.PASS, "next-obligation", "required-findings-resolved-and-checks-valid", ())


def detect_oscillation(
    findings: Iterable[Finding | Mapping[str, Any]], history: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Return finding IDs whose complete blocker/evidence set recurred unchanged.

    A review round is the maximal contiguous batch of ``record-finding`` events
    in checkpoint order.  The round signature is the complete set of
    ``(finding_id, evidence_id_or_digest)`` pairs newly reported in that batch.
    Only an exact repeat of that complete set is oscillation.  Partial overlap,
    an additional blocker, or changed evidence selects iteration instead.
    """

    normalized_findings = tuple(_coerce_finding(item) for item in _finding_values(findings))
    signatures = review_round_signatures(normalized_findings, history)
    if len(signatures) < 2:
        return ()
    latest = signatures[-1]
    if latest not in signatures[:-1]:
        return ()
    return tuple(sorted(finding_id for finding_id, _evidence in latest))


def review_round_signatures(
    findings: Iterable[Finding | Mapping[str, Any]], history: Iterable[Mapping[str, Any]]
) -> tuple[tuple[tuple[str, str], ...], ...]:
    normalized_findings = {finding.finding_id: finding for finding in (_coerce_finding(item) for item in _finding_values(findings))}
    events = _ordered_events(tuple(dict(event) for event in history))
    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("event") == "record-finding":
            current.append(event)
            continue
        if current:
            batches.append(current)
            current = []
    if current:
        batches.append(current)

    signatures: list[tuple[tuple[str, str], ...]] = []
    for batch in batches:
        entries: set[tuple[str, str]] = set()
        for event in batch:
            fid = str(event.get("finding_id") or "")
            if not fid:
                continue
            finding = normalized_findings.get(fid)
            if finding is not None and (not finding.blocking or finding.disposition == Disposition.FALSE_POSITIVE.value):
                continue
            evidence = str(
                event.get("evidence_id")
                or event.get("evidence_digest")
                or event.get("evidence")
                or (getattr(finding, "evidence_digest", None) if finding else "")
                or (finding.evidence_id if finding else "")
            )
            entries.add((fid, evidence))
        if entries:
            signatures.append(tuple(sorted(entries)))
    return tuple(signatures)


def coerce_budget(value: Budget | Mapping[str, Any] | None) -> Budget:
    if isinstance(value, Budget):
        return value
    data = dict(value or {})
    return Budget(
        rounds_used=int(data.get("rounds_used", data.get("round", 0)) or 0),
        max_rounds=(int(data["max_rounds"]) if data.get("max_rounds") is not None else None),
        repair_tokens_remaining=(
            int(data["repair_tokens_remaining"]) if data.get("repair_tokens_remaining") is not None else None
        ),
        investigation_tokens_remaining=int(
            data.get("investigation_tokens_remaining", data.get("investigate_tokens_remaining", 0)) or 0
        ),
    )


def coerce_contract(value: SupervisionContract | Mapping[str, Any] | None) -> SupervisionContract:
    if isinstance(value, SupervisionContract):
        return value
    data = dict(value or {})
    required = data.get("required_finding_ids")
    owner_decisions = data.get("owner_decisions", ())
    if isinstance(owner_decisions, Mapping):
        owner_decisions = tuple(sorted(key for key, decision in owner_decisions.items() if decision))
    return SupervisionContract(
        checks_valid=bool(data.get("checks_valid", True)),
        required_finding_ids=(tuple(sorted(str(item) for item in required)) if required is not None else None),
        required_owner_decisions=tuple(sorted(str(item) for item in data.get("required_owner_decisions", ()))),
        owner_decisions=tuple(sorted(str(item) for item in owner_decisions)),
        reproduction_min_severity=str(data.get("reproduction_min_severity", "P1")).upper(),
        reproduction_min_impact=str(data.get("reproduction_min_impact", "high")).lower(),
        cheap_reproduction_token_cap=int(data.get("cheap_reproduction_token_cap", 0) or 0),
        max_investigation_tokens=(
            int(data["max_investigation_tokens"]) if data.get("max_investigation_tokens") is not None else None
        ),
        max_rounds=(int(data["max_rounds"]) if data.get("max_rounds") is not None else None),
    )


def _finding_values(findings: Iterable[Finding | Mapping[str, Any]] | Mapping[str, Mapping[str, Any]]) -> tuple[Finding | Mapping[str, Any], ...]:
    if isinstance(findings, Mapping):
        values = []
        for finding_id, data in findings.items():
            if isinstance(data, Mapping):
                item = dict(data)
                item.setdefault("finding_id", finding_id)
                values.append(item)
            else:
                values.append(data)
        return tuple(values)
    return tuple(findings)


def _coerce_finding(item: Finding | Mapping[str, Any]) -> Finding:
    if isinstance(item, Finding):
        return item
    data = dict(item)
    fid = str(data.get("finding_id") or data.get("id") or "")
    return import_finding(fid, data)


def _ordered_events(history: tuple[dict, ...]) -> tuple[dict, ...]:
    indexed = enumerate(dict(event) for event in history)
    return tuple(event for _index, event in sorted(indexed, key=lambda item: (str(item[1].get("at") or ""), item[0])))


def _required(finding: Finding, contract: SupervisionContract) -> bool:
    return contract.required_finding_ids is None or finding.finding_id in contract.required_finding_ids


def _active_blockers(findings: tuple[Finding, ...], contract: SupervisionContract) -> tuple[Finding, ...]:
    return tuple(
        finding
        for finding in findings
        if finding.blocking and _required(finding, contract) and finding.disposition in _ACTIVE_DISPOSITIONS
    )


def _active_ids(findings: tuple[Finding, ...], contract: SupervisionContract) -> tuple[str, ...]:
    return tuple(finding.finding_id for finding in _active_blockers(findings, contract))


def _missing_owner_decisions(findings: tuple[Finding, ...], contract: SupervisionContract) -> tuple[str, ...]:
    decided = set(contract.owner_decisions)
    missing: set[str] = set()
    required_by_contract = set(contract.required_owner_decisions)
    terminal = {Disposition.FALSE_POSITIVE.value, Disposition.RESOLVED.value, Disposition.WAIVED.value}
    for finding in findings:
        if finding.disposition in terminal:
            continue
        needs = finding.owner_decision_required or finding.finding_id in required_by_contract
        if needs and not finding.owner_decision and finding.finding_id not in decided:
            missing.add(finding.finding_id)
    for finding_id in required_by_contract - {finding.finding_id for finding in findings}:
        if finding_id not in decided:
            missing.add(finding_id)
    return tuple(sorted(missing))


def _cap_hit(budget: Budget, contract: SupervisionContract) -> str | None:
    round_caps = tuple(cap for cap in (budget.max_rounds, contract.max_rounds) if cap is not None)
    if round_caps and budget.rounds_used >= min(round_caps):
        return "round-cap-hit"
    if budget.repair_tokens_remaining is not None and budget.repair_tokens_remaining <= 0:
        return "budget-cap-hit"
    return None


def _select_clarification(
    active: tuple[Finding, ...],
    history: tuple[dict, ...],
    budget: Budget,
    contract: SupervisionContract,
) -> Action | None:
    candidates = tuple(
        sorted(
            (finding for finding in active if _needs_clarification(finding, budget)),
            key=lambda finding: (
                0 if _meets_threshold(finding, contract) else 1,
                -_SEVERITY_RANK.get(finding.severity, 0),
                -_IMPACT_RANK.get(finding.impact, 0),
                finding.finding_id,
            ),
        )
    )
    if not candidates:
        return None
    finding = candidates[0]
    ambiguous = _is_ambiguous(finding)
    if _meets_threshold(finding, contract):
        if (
            not finding.reproduction_attempted
            and finding.deterministic_reproduction_available
            and finding.reproduction_cost_tokens <= contract.cheap_reproduction_token_cap
        ):
            return Action(
                ActionKind.CLARIFY,
                "reproduce",
                "ambiguous-or-allowance-consuming-finding",
                (finding.finding_id,),
                finding.reproduction_cost_tokens,
            )
        if ambiguous:
            if not _investigation_used(finding, history) and budget.investigation_tokens_remaining > 0:
                cap = budget.investigation_tokens_remaining
                if contract.max_investigation_tokens is not None:
                    cap = min(cap, contract.max_investigation_tokens)
                return Action(
                    ActionKind.CLARIFY,
                    "investigate",
                    "still-ambiguous-after-deterministic-reproduction-rule",
                    (finding.finding_id,),
                    cap,
                )
            return Action(
                ActionKind.CLARIFY,
                "ask_owner",
                "ambiguous-and-no-funded-investigation-remains",
                (finding.finding_id,),
            )
    if ambiguous:
        return Action(
            ActionKind.CLARIFY,
            "ask_owner",
            "ambiguous-below-reproduction-threshold",
            (finding.finding_id,),
        )
    return Action(
        ActionKind.CLARIFY,
        "ask_owner",
        "fix-would-consume-remaining-repair-allowance",
        (finding.finding_id,),
    )


def _needs_clarification(finding: Finding, budget: Budget) -> bool:
    if finding.reproduced:
        return False
    consumes_allowance = (
        finding.fix_cost_estimate_tokens is not None
        and budget.repair_tokens_remaining is not None
        and finding.fix_cost_estimate_tokens >= budget.repair_tokens_remaining
    )
    return _is_ambiguous(finding) or consumes_allowance


def _is_ambiguous(finding: Finding) -> bool:
    return finding.ambiguous or finding.evidence_nature == EvidenceNature.ASSUMPTION.value


def _meets_threshold(finding: Finding, contract: SupervisionContract) -> bool:
    severity = _SEVERITY_RANK.get(finding.severity, 0)
    required_severity = _SEVERITY_RANK.get(contract.reproduction_min_severity, 0)
    impact = _IMPACT_RANK.get(finding.impact, 0)
    required_impact = _IMPACT_RANK.get(contract.reproduction_min_impact, 0)
    return severity >= required_severity and impact >= required_impact


def _investigation_used(finding: Finding, history: tuple[dict, ...]) -> bool:
    if finding.investigation_attempted:
        return True
    return any(
        event.get("finding_id") == finding.finding_id and event.get("event") in {"investigate", "record-investigation"}
        for event in history
    )


def _fix_induced_themes(findings: tuple[Finding, ...], history: tuple[dict, ...]) -> set[str]:
    theme_by_id = {finding.finding_id: finding.canonical_theme for finding in findings}
    resolved: set[str] = set()
    recurring: set[str] = set()
    for event in history:
        theme = theme_by_id.get(str(event.get("finding_id") or ""))
        if theme is None:
            continue
        if event.get("event") == "resolve-finding":
            resolved.add(theme)
        elif event.get("event") == "record-finding" and theme in resolved:
            recurring.add(theme)
    return recurring
