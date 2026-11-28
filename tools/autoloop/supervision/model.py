"""Structured values for deterministic autoloop review supervision."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EvidenceNature(str, Enum):
    OBSERVATION = "observation"
    ASSUMPTION = "assumption"


class Disposition(str, Enum):
    OPEN = "open"
    REOPENED = "reopened"
    FIXED_PENDING_REVIEW = "fixed_pending_review"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"
    WAIVED = "waived"


class ActionKind(str, Enum):
    CLARIFY = "clarify"
    ESCALATE = "escalate"
    ITERATE = "iterate"
    PASS = "pass"


@dataclass(frozen=True)
class Finding:
    """Canonical review finding.

    ``resolution`` is retained for every non-open disposition.  In particular,
    a false positive is a terminal explicit disposition rather than a reason to
    delete the finding or forget its evidence.
    """

    finding_id: str
    canonical_theme: str
    severity: str
    evidence_nature: str
    implicated_path: str | None
    implicated_tree: str | None
    evidence_id: str
    protected_invariant: str | None
    disposition: str
    resolution: str | None
    summary: str = ""
    source: str = "review"
    opened_at: str | None = None
    resolved_at: str | None = None
    implicated_line: int | None = None
    blocking: bool = True
    ambiguous: bool = False
    impact: str = "medium"
    deterministic_reproduction_available: bool = False
    reproduction_cost_tokens: int = 0
    reproduction_attempted: bool = False
    reproduced: bool = False
    investigation_attempted: bool = False
    fix_cost_estimate_tokens: int | None = None
    owner_decision_required: bool = False
    owner_decision: str | None = None
    fix_induced: bool = False


@dataclass(frozen=True)
class Budget:
    """Remaining enforceable allowances supplied by the trusted caller."""

    rounds_used: int = 0
    max_rounds: int | None = None
    repair_tokens_remaining: int | None = None
    investigation_tokens_remaining: int = 0


@dataclass(frozen=True)
class SupervisionContract:
    """Frozen review-policy inputs for the pure supervisor."""

    checks_valid: bool = True
    required_finding_ids: tuple[str, ...] | None = None
    required_owner_decisions: tuple[str, ...] = ()
    owner_decisions: tuple[str, ...] = ()
    reproduction_min_severity: str = "P1"
    reproduction_min_impact: str = "high"
    cheap_reproduction_token_cap: int = 0
    max_investigation_tokens: int | None = None
    max_rounds: int | None = None

    def __post_init__(self) -> None:
        if self.required_finding_ids is not None:
            object.__setattr__(self, "required_finding_ids", tuple(sorted(self.required_finding_ids)))
        object.__setattr__(self, "required_owner_decisions", tuple(sorted(self.required_owner_decisions)))
        object.__setattr__(self, "owner_decisions", tuple(sorted(self.owner_decisions)))


@dataclass(frozen=True)
class Action:
    """Next deterministic supervision action.

    ``kind`` is the ADR review-rule precedence category.  ``method`` is the
    concrete zero-model step inside that category, such as ``reproduce``,
    ``investigate`` or ``ask_owner`` for a clarification.
    """

    kind: ActionKind
    method: str
    reason: str
    finding_ids: tuple[str, ...] = ()
    token_cap: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "method": self.method,
            "reason": self.reason,
            "finding_ids": list(self.finding_ids),
            "token_cap": self.token_cap,
        }


@dataclass(frozen=True)
class ImportedRun:
    findings: tuple[Finding, ...]
    history: tuple[dict, ...]
