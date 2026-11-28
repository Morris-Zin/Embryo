"""Typed evidence and target schemas for the Phase 2 verification predicate.

The schemas are deliberately small immutable values.  They describe evidence
that a trusted caller gathered; they do not gather evidence themselves and they
do not encode a verdict beyond objective facts such as exit status and expiry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Obligation(str, Enum):
    """Ordered verification obligations."""

    SHARD = "shard"
    INTEGRATED = "integrated"
    REVIEW_SUBMISSION = "review_submission"
    MERGE_ELIGIBILITY = "merge_eligibility"
    POLICY_ACTIVATION = "policy_activation"


VerificationTarget = Obligation

OBLIGATION_ORDER: tuple[Obligation, ...] = (
    Obligation.SHARD,
    Obligation.INTEGRATED,
    Obligation.REVIEW_SUBMISSION,
    Obligation.MERGE_ELIGIBILITY,
    Obligation.POLICY_ACTIVATION,
)

NEVER_CACHEABLE_CHECK_KINDS = frozenset({"live", "timing", "evaluator"})


def obligation_rank(obligation: Obligation) -> int:
    return OBLIGATION_ORDER.index(obligation)


def obligations_through(target: Obligation) -> tuple[Obligation, ...]:
    return OBLIGATION_ORDER[: obligation_rank(target) + 1]


@dataclass(frozen=True)
class Target:
    """The obligation being verified for one shard in an enrolled run."""

    obligation: Obligation
    shard_id: str


@dataclass(frozen=True)
class VerificationFailure:
    obligation: Obligation
    reason: str
    detail: str
    evidence_id: str | None = None


@dataclass(frozen=True)
class Decision:
    ok: bool
    obligations_met: tuple[Obligation, ...]
    failures: tuple[VerificationFailure, ...]


@dataclass(frozen=True)
class CheckRequirement:
    check_id: str
    obligation: Obligation
    kind: str = "deterministic"


@dataclass(frozen=True)
class ShardContract:
    shard_id: str
    checks: tuple[CheckRequirement, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalRequirement:
    obligation: Obligation
    action: str


@dataclass(frozen=True)
class PolicyContract:
    campaign_id: str
    epoch: int
    evaluator_version: str
    signer_key_id: str


@dataclass(frozen=True)
class FrozenContract:
    """Trusted frozen contract identity and requirements used by verify()."""

    repo: str
    run_id: str
    issue_number: int | None
    pr_number: int | None
    revision: int
    digest: str
    shards: tuple[ShardContract, ...]
    approvals: tuple[ApprovalRequirement, ...] = field(default_factory=tuple)
    review_required: bool = True
    policy: PolicyContract | None = None


@dataclass(frozen=True)
class Binding:
    """Identity and mutable-ref tuple every relied-on evidence item must match."""

    repo: str
    run_id: str
    issue_number: int | None
    pr_number: int | None
    contract_revision: int
    contract_digest: str
    base_head: str
    shard_head: str


@dataclass(frozen=True)
class Generations:
    shard: int = 0
    integration: int = 0
    review: int = 0
    merge: int = 0
    policy: int = 0


@dataclass(frozen=True)
class EvidenceMeta:
    evidence_id: str
    binding: Binding
    generation: int
    created_at: int
    expires_at: int | None = None
    status: str = "active"
    superseded_by: str | None = None


@dataclass(frozen=True)
class ToolingSource:
    path: str
    blob_sha256: str


@dataclass(frozen=True)
class ToolingEvidence:
    meta: EvidenceMeta
    tooling_digest: str
    sources: tuple[ToolingSource, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ShardEvidence:
    meta: EvidenceMeta
    shard_id: str
    tree_digest: str
    implementation_receipt_id: str


@dataclass(frozen=True)
class IntegrationEvidence:
    meta: EvidenceMeta
    integration_commit: str
    integration_tree_digest: str
    integration_receipt_id: str


@dataclass(frozen=True)
class CheckEvidence:
    meta: EvidenceMeta
    check_id: str
    obligation: Obligation
    kind: str
    tree_digest: str
    tooling_digest: str
    completed: bool
    exit_code: int | None
    output_digest: str
    receipt_id: str
    cached: bool = False
    original_evidence_id: str | None = None
    original_receipt_id: str | None = None


@dataclass(frozen=True)
class ReviewEvidence:
    meta: EvidenceMeta
    reviewed_tree: str
    review_digest: str
    finding_digests: tuple[str, ...] = field(default_factory=tuple)
    reviewer_identity: str | None = None


@dataclass(frozen=True)
class FindingEvidence:
    meta: EvidenceMeta
    finding_id: str
    reviewed_tree: str
    artifact_digest: str
    blocking: bool = True
    resolved: bool = False
    resolution_digest: str | None = None
    safeguard_digests: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SafeguardEvidence:
    meta: EvidenceMeta
    finding_id: str
    tree_digest: str
    artifact_digest: str


@dataclass(frozen=True)
class OwnerApprovalEvidence:
    meta: EvidenceMeta
    obligation: Obligation
    action: str
    tree_digest: str
    decision: str = "approve"


@dataclass(frozen=True)
class CampaignEvidence:
    meta: EvidenceMeta
    campaign_id: str
    epoch: int
    tree_digest: str
    assignment_manifest_digest: str
    completion_bitmap_digest: str
    completed_tasks: int
    total_tasks: int


@dataclass(frozen=True)
class EvaluatorEvidence:
    meta: EvidenceMeta
    campaign_id: str
    epoch: int
    tree_digest: str
    evaluator_version: str
    signer_key_id: str
    result_digest: str
    signature_digest: str
    nonce: str
    verdict: bool


@dataclass(frozen=True)
class EvidenceSnapshot:
    """A caller-gathered, point-in-time evidence snapshot.

    ``observed_at`` is input, not wall-clock access inside verify(), which keeps
    expiry decisions deterministic and side-effect free.
    """

    observed_at: int
    binding: Binding
    generations: Generations
    integration_commit: str | None = None
    integration_tree_digest: str | None = None
    shard_tree_digest: str | None = None
    tooling_digest: str | None = None
    evaluator_result_digest: str | None = None
    evaluator_signature_digest: str | None = None
    tooling: tuple[ToolingEvidence, ...] = field(default_factory=tuple)
    shard_results: tuple[ShardEvidence, ...] = field(default_factory=tuple)
    integrations: tuple[IntegrationEvidence, ...] = field(default_factory=tuple)
    reviews: tuple[ReviewEvidence, ...] = field(default_factory=tuple)
    checks: tuple[CheckEvidence, ...] = field(default_factory=tuple)
    findings: tuple[FindingEvidence, ...] = field(default_factory=tuple)
    safeguards: tuple[SafeguardEvidence, ...] = field(default_factory=tuple)
    approvals: tuple[OwnerApprovalEvidence, ...] = field(default_factory=tuple)
    campaigns: tuple[CampaignEvidence, ...] = field(default_factory=tuple)
    evaluator_results: tuple[EvaluatorEvidence, ...] = field(default_factory=tuple)
