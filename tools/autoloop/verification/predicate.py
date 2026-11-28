"""Pure Phase 2 verification predicate.

``verify`` performs no I/O, reads no clock, mutates nothing, and treats every
inconsistency as a closed failure.  Its caller is responsible for gathering a
snapshot of objective evidence; this module is responsible for deciding whether
that evidence satisfies the ordered obligations.
"""

from __future__ import annotations

from dataclasses import dataclass

from .digests import tooling_sources_digest
from .schemas import (
    NEVER_CACHEABLE_CHECK_KINDS,
    Decision,
    EvidenceMeta,
    EvidenceSnapshot,
    FindingEvidence,
    FrozenContract,
    Generations,
    Obligation,
    ShardContract,
    Target,
    VerificationFailure,
    obligations_through,
)


@dataclass(frozen=True)
class _TargetError:
    reason: str
    detail: str


def verify(target: Target | Obligation | str, frozen_contract: FrozenContract, evidence_snapshot: EvidenceSnapshot) -> Decision:
    """Decide whether ``evidence_snapshot`` satisfies ``target``.

    ``target`` is normally a :class:`Target`.  For a single-shard contract an
    ``Obligation`` or obligation string is also accepted and the sole shard is
    inferred; multi-shard contracts require an explicit shard so callers cannot
    accidentally reinterpret scope.
    """

    target_value, target_error = _coerce_target(target, frozen_contract)
    if target_error is not None:
        failure = VerificationFailure(Obligation.SHARD, target_error.reason, target_error.detail)
        return Decision(False, (), (failure,))
    assert target_value is not None

    shard_contract = next((s for s in frozen_contract.shards if s.shard_id == target_value.shard_id), None)
    if shard_contract is None:
        failure = VerificationFailure(
            Obligation.SHARD,
            "unknown-shard",
            f"contract has no shard named {target_value.shard_id!r}",
        )
        return Decision(False, (), (failure,))

    foundation_failures = _validate_foundation(frozen_contract, evidence_snapshot)
    if foundation_failures:
        return Decision(False, (), tuple(foundation_failures))

    met: list[Obligation] = []
    shard_tree: str | None = None
    for obligation in obligations_through(target_value.obligation):
        failures: list[VerificationFailure] = []
        if obligation is Obligation.SHARD:
            shard_tree = _validate_shard_obligation(shard_contract, evidence_snapshot, failures)
        elif obligation is Obligation.INTEGRATED:
            _validate_integrated_obligation(evidence_snapshot, failures)
        elif obligation is Obligation.REVIEW_SUBMISSION:
            # ADR 0005: a run qualifies for review before any review exists. Submission
            # needs only the integrated gate evidence (plus its required checks below);
            # exact-tree review evidence is an obligation of merge eligibility.
            pass
        elif obligation is Obligation.MERGE_ELIGIBILITY:
            _validate_review_obligation(frozen_contract, evidence_snapshot, failures)
            _validate_merge_obligation(frozen_contract, evidence_snapshot, failures)
        elif obligation is Obligation.POLICY_ACTIVATION:
            _validate_policy_obligation(frozen_contract, evidence_snapshot, failures)

        failures.extend(_validate_required_checks(shard_contract, evidence_snapshot, obligation, shard_tree))
        if failures:
            return Decision(False, tuple(met), tuple(failures))
        met.append(obligation)

    return Decision(True, tuple(met), ())


def _coerce_target(
    target: Target | Obligation | str, frozen_contract: FrozenContract
) -> tuple[Target | None, _TargetError | None]:
    if isinstance(target, Target):
        try:
            obligation = Obligation(target.obligation)
        except ValueError:
            return None, _TargetError("invalid-target", f"unknown obligation {target.obligation!r}")
        if not target.shard_id:
            return None, _TargetError("invalid-target", "target shard_id must be non-empty")
        return Target(obligation, target.shard_id), None

    try:
        obligation = target if isinstance(target, Obligation) else Obligation(str(target))
    except ValueError:
        return None, _TargetError("invalid-target", f"unknown obligation {target!r}")
    shard_ids = [shard.shard_id for shard in frozen_contract.shards]
    if len(shard_ids) != 1:
        return None, _TargetError(
            "ambiguous-target",
            "an obligation without shard_id is valid only for a single-shard contract",
        )
    return Target(obligation, shard_ids[0]), None


def _validate_foundation(frozen_contract: FrozenContract, snapshot: EvidenceSnapshot) -> list[VerificationFailure]:
    failures: list[VerificationFailure] = []
    binding = snapshot.binding
    contract_fields = (
        ("repo", binding.repo, frozen_contract.repo),
        ("run", binding.run_id, frozen_contract.run_id),
        ("issue", binding.issue_number, frozen_contract.issue_number),
        ("pr", binding.pr_number, frozen_contract.pr_number),
        ("contract-revision", binding.contract_revision, frozen_contract.revision),
        ("contract-digest", binding.contract_digest, frozen_contract.digest),
    )
    for name, actual, expected in contract_fields:
        if actual != expected:
            failures.append(
                VerificationFailure(
                    Obligation.SHARD,
                    f"stale-{name}",
                    f"evidence {name} is {actual!r}, but the frozen contract requires {expected!r}",
                )
            )
    for name, value in (
        ("repo", binding.repo),
        ("run", binding.run_id),
        ("contract-digest", binding.contract_digest),
        ("base-head", binding.base_head),
        ("shard-head", binding.shard_head),
    ):
        if value in (None, ""):
            failures.append(
                VerificationFailure(Obligation.SHARD, f"missing-{name}", f"evidence snapshot lacks a {name} binding")
            )
    return failures


def _validate_shard_obligation(
    shard_contract: ShardContract,
    snapshot: EvidenceSnapshot,
    failures: list[VerificationFailure],
) -> str | None:
    _validate_tooling(snapshot, failures)

    matches = [item for item in snapshot.shard_results if item.shard_id == shard_contract.shard_id]
    if not matches:
        failures.append(
            VerificationFailure(
                Obligation.SHARD,
                "missing-shard-evidence",
                f"no shard completion evidence is present for {shard_contract.shard_id!r}",
            )
        )
        return snapshot.shard_tree_digest

    fingerprints = {(item.tree_digest, item.implementation_receipt_id) for item in matches}
    if len(fingerprints) > 1:
        failures.append(
            VerificationFailure(
                Obligation.SHARD,
                "contradictory-shard-evidence",
                "multiple active shard completion records disagree about tree or receipt",
            )
        )

    for item in matches:
        failures.extend(_validate_meta(item.meta, snapshot, snapshot.generations.shard, Obligation.SHARD, "shard evidence"))
        if not snapshot.shard_tree_digest:
            failures.append(
                VerificationFailure(Obligation.SHARD, "missing-shard-tree-digest", "snapshot lacks the actual shard code tree digest")
            )
        elif item.tree_digest != snapshot.shard_tree_digest:
            failures.append(
                VerificationFailure(
                    Obligation.SHARD,
                    "stale-shard-tree",
                    f"shard evidence tree {item.tree_digest!r} does not match current tree {snapshot.shard_tree_digest!r}",
                    item.meta.evidence_id,
                )
            )
        if not item.implementation_receipt_id:
            failures.append(
                VerificationFailure(
                    Obligation.SHARD,
                    "missing-implementation-receipt",
                    "shard completion evidence has no implementation receipt ID",
                    item.meta.evidence_id,
                )
            )
    return snapshot.shard_tree_digest


def _validate_tooling(snapshot: EvidenceSnapshot, failures: list[VerificationFailure]) -> None:
    if not snapshot.tooling_digest:
        failures.append(
            VerificationFailure(Obligation.SHARD, "missing-tooling-digest", "snapshot lacks the trusted tooling digest")
        )
        return
    if not snapshot.tooling:
        failures.append(
            VerificationFailure(Obligation.SHARD, "missing-tooling-evidence", "no trusted tooling evidence is present")
        )
        return
    digests_by_gate: dict[Obligation | None, set[str]] = {}
    for item in snapshot.tooling:
        digests_by_gate.setdefault(item.gate, set()).add(item.tooling_digest)
    for gate, digests in sorted(digests_by_gate.items(), key=lambda entry: "" if entry[0] is None else entry[0].value):
        if len(digests) > 1:
            label = "unbound" if gate is None else gate.value
            failures.append(
                VerificationFailure(
                    Obligation.SHARD,
                    "contradictory-tooling-evidence",
                    f"multiple {label} tooling records disagree about the trusted tooling digest",
                )
            )
    for item in snapshot.tooling:
        failures.extend(_validate_meta(item.meta, snapshot, None, Obligation.SHARD, "tooling evidence"))
        if item.gate is None and item.tooling_digest != snapshot.tooling_digest:
            failures.append(
                VerificationFailure(
                    Obligation.SHARD,
                    "stale-tooling-digest",
                    f"tooling evidence digest {item.tooling_digest!r} does not match current digest {snapshot.tooling_digest!r}",
                    item.meta.evidence_id,
                )
            )
        if not item.sources:
            failures.append(
                VerificationFailure(
                    Obligation.SHARD,
                    "missing-tooling-sources",
                    "trusted tooling evidence does not enumerate its source blobs",
                    item.meta.evidence_id,
                )
            )
        for source in item.sources:
            if not source.path or not source.blob_sha256:
                failures.append(
                    VerificationFailure(
                        Obligation.SHARD,
                        "missing-tooling-source-digest",
                        "every trusted tooling source requires a path and content digest",
                        item.meta.evidence_id,
                    )
                )
        try:
            sources_digest = tooling_sources_digest(item.sources)
        except ValueError as exc:
            if "duplicate" in str(exc):
                failures.append(
                    VerificationFailure(
                        Obligation.SHARD,
                        "contradictory-tooling-sources",
                        str(exc),
                        item.meta.evidence_id,
                    )
                )
        else:
            if sources_digest != item.tooling_digest:
                failures.append(
                    VerificationFailure(
                        Obligation.SHARD,
                        "contradictory-tooling-sources",
                        "tooling digest does not match the canonical digest of its enumerated sources",
                        item.meta.evidence_id,
                    )
                )


def _tooling_digest_for_gate(snapshot: EvidenceSnapshot, gate: Obligation) -> str | None:
    """Trusted tooling digest that checks of ``gate`` must have used.

    A record bound to the gate wins; otherwise the snapshot's current digest
    (which unbound records must equal) applies. Contradictory bound records are
    reported by ``_validate_tooling`` and make the foundation fail first.
    """
    bound = sorted({item.tooling_digest for item in snapshot.tooling if item.gate is gate})
    if bound:
        return bound[0]
    return snapshot.tooling_digest


def _validate_integrated_obligation(snapshot: EvidenceSnapshot, failures: list[VerificationFailure]) -> None:
    if not snapshot.integration_commit:
        failures.append(
            VerificationFailure(Obligation.INTEGRATED, "missing-integration-commit", "snapshot lacks the actual integration commit")
        )
    if not snapshot.integration_tree_digest:
        failures.append(
            VerificationFailure(
                Obligation.INTEGRATED, "missing-integration-tree-digest", "snapshot lacks the integrated code tree digest"
            )
        )
    if not snapshot.integrations:
        failures.append(
            VerificationFailure(Obligation.INTEGRATED, "missing-integration-evidence", "no integration evidence is present")
        )
        return

    fingerprints = {(item.integration_commit, item.integration_tree_digest, item.integration_receipt_id) for item in snapshot.integrations}
    if len(fingerprints) > 1:
        failures.append(
            VerificationFailure(
                Obligation.INTEGRATED,
                "contradictory-integration-evidence",
                "multiple integration records disagree about commit, tree, or receipt",
            )
        )
    for item in snapshot.integrations:
        failures.extend(
            _validate_meta(item.meta, snapshot, snapshot.generations.integration, Obligation.INTEGRATED, "integration evidence")
        )
        if item.integration_commit != snapshot.integration_commit:
            failures.append(
                VerificationFailure(
                    Obligation.INTEGRATED,
                    "stale-integration-commit",
                    f"integration evidence commit {item.integration_commit!r} does not match current commit {snapshot.integration_commit!r}",
                    item.meta.evidence_id,
                )
            )
        if item.integration_tree_digest != snapshot.integration_tree_digest:
            failures.append(
                VerificationFailure(
                    Obligation.INTEGRATED,
                    "stale-integration-tree",
                    f"integration evidence tree {item.integration_tree_digest!r} does not match current tree {snapshot.integration_tree_digest!r}",
                    item.meta.evidence_id,
                )
            )
        if not item.integration_receipt_id:
            failures.append(
                VerificationFailure(
                    Obligation.INTEGRATED,
                    "missing-integration-receipt",
                    "integration evidence has no trusted receipt ID",
                    item.meta.evidence_id,
                )
            )


def _validate_review_obligation(
    frozen_contract: FrozenContract, snapshot: EvidenceSnapshot, failures: list[VerificationFailure]
) -> None:
    if frozen_contract.review_required and not snapshot.reviews:
        failures.append(
            VerificationFailure(Obligation.MERGE_ELIGIBILITY, "missing-review-evidence", "the contract requires a review of the exact tree")
        )
    expected_findings = {finding.artifact_digest for finding in snapshot.findings}
    covered_findings: set[str] = set()
    for review in snapshot.reviews:
        failures.extend(
            _validate_meta(review.meta, snapshot, snapshot.generations.review, Obligation.MERGE_ELIGIBILITY, "review evidence")
        )
        if review.reviewed_tree != snapshot.integration_tree_digest:
            failures.append(
                VerificationFailure(
                    Obligation.MERGE_ELIGIBILITY,
                    "stale-reviewed-tree",
                    f"review covers tree {review.reviewed_tree!r}, not current integrated tree {snapshot.integration_tree_digest!r}",
                    review.meta.evidence_id,
                )
            )
        if not review.review_digest:
            failures.append(
                VerificationFailure(
                    Obligation.MERGE_ELIGIBILITY,
                    "missing-review-digest",
                    "review evidence has no canonical review digest",
                    review.meta.evidence_id,
                )
            )
        referenced = set(review.finding_digests)
        unknown = referenced - expected_findings
        if unknown:
            failures.append(
                VerificationFailure(
                    Obligation.MERGE_ELIGIBILITY,
                    "contradictory-findings-binding",
                    f"review references unknown finding artifact digests {sorted(unknown)!r}",
                    review.meta.evidence_id,
                )
            )
        covered_findings.update(referenced)

    uncovered = expected_findings - covered_findings
    if uncovered:
        failures.append(
            VerificationFailure(
                Obligation.MERGE_ELIGIBILITY,
                "unbound-finding-artifact",
                f"finding artifact digests are not covered by any exact-tree review: {sorted(uncovered)!r}",
            )
        )

    for finding in snapshot.findings:
        failures.extend(
            _validate_meta(finding.meta, snapshot, snapshot.generations.review, Obligation.MERGE_ELIGIBILITY, "finding evidence")
        )
        _validate_finding_binding(finding, snapshot, Obligation.MERGE_ELIGIBILITY, failures)


def _validate_finding_binding(
    finding: FindingEvidence,
    snapshot: EvidenceSnapshot,
    obligation: Obligation,
    failures: list[VerificationFailure],
) -> None:
    if finding.reviewed_tree != snapshot.integration_tree_digest:
        failures.append(
            VerificationFailure(
                obligation,
                "stale-finding-tree",
                f"finding {finding.finding_id!r} is bound to tree {finding.reviewed_tree!r}, not {snapshot.integration_tree_digest!r}",
                finding.meta.evidence_id,
            )
        )
    if not finding.artifact_digest:
        failures.append(
            VerificationFailure(
                obligation,
                "missing-finding-artifact-digest",
                f"finding {finding.finding_id!r} has no artifact digest",
                finding.meta.evidence_id,
            )
        )


def _validate_merge_obligation(
    frozen_contract: FrozenContract, snapshot: EvidenceSnapshot, failures: list[VerificationFailure]
) -> None:
    safeguard_ids = {item.finding_id for item in snapshot.safeguards}
    finding_ids = {item.finding_id for item in snapshot.findings}
    for unknown in sorted(safeguard_ids - finding_ids):
        failures.append(
            VerificationFailure(
                Obligation.MERGE_ELIGIBILITY,
                "contradictory-safeguard-evidence",
                f"safeguard evidence references unknown finding {unknown!r}",
            )
        )

    for finding in snapshot.findings:
        if not finding.blocking:
            continue
        if not finding.resolved:
            failures.append(
                VerificationFailure(
                    Obligation.MERGE_ELIGIBILITY,
                    "unresolved-blocking-finding",
                    f"blocking finding {finding.finding_id!r} is not resolved",
                    finding.meta.evidence_id,
                )
            )
        if not finding.resolution_digest:
            failures.append(
                VerificationFailure(
                    Obligation.MERGE_ELIGIBILITY,
                    "missing-resolution-digest",
                    f"blocking finding {finding.finding_id!r} lacks a resolution digest",
                    finding.meta.evidence_id,
                )
            )
        safeguards = [item for item in snapshot.safeguards if item.finding_id == finding.finding_id]
        if not safeguards:
            failures.append(
                VerificationFailure(
                    Obligation.MERGE_ELIGIBILITY,
                    "missing-safeguard-evidence",
                    f"blocking finding {finding.finding_id!r} has no safeguard artifact",
                    finding.meta.evidence_id,
                )
            )
        for safeguard in safeguards:
            failures.extend(
                _validate_meta(
                    safeguard.meta,
                    snapshot,
                    snapshot.generations.merge,
                    Obligation.MERGE_ELIGIBILITY,
                    "safeguard evidence",
                )
            )
            if safeguard.tree_digest != snapshot.integration_tree_digest:
                failures.append(
                    VerificationFailure(
                        Obligation.MERGE_ELIGIBILITY,
                        "stale-safeguard-tree",
                        f"safeguard for {finding.finding_id!r} is bound to tree {safeguard.tree_digest!r}",
                        safeguard.meta.evidence_id,
                    )
                )
            if not safeguard.artifact_digest:
                failures.append(
                    VerificationFailure(
                        Obligation.MERGE_ELIGIBILITY,
                        "missing-safeguard-artifact-digest",
                        f"safeguard for {finding.finding_id!r} has no artifact digest",
                        safeguard.meta.evidence_id,
                    )
                )
            elif safeguard.artifact_digest not in finding.safeguard_digests:
                failures.append(
                    VerificationFailure(
                        Obligation.MERGE_ELIGIBILITY,
                        "contradictory-safeguard-digest",
                        f"safeguard digest for {finding.finding_id!r} is not bound by the finding evidence",
                        safeguard.meta.evidence_id,
                    )
                )

    _validate_owner_approvals(frozen_contract, snapshot, Obligation.MERGE_ELIGIBILITY, failures)


def _validate_owner_approvals(
    frozen_contract: FrozenContract,
    snapshot: EvidenceSnapshot,
    obligation: Obligation,
    failures: list[VerificationFailure],
) -> None:
    requirements = [item for item in frozen_contract.approvals if item.obligation == obligation]
    for requirement in requirements:
        matches = [
            item
            for item in snapshot.approvals
            if item.obligation == obligation and item.action == requirement.action
        ]
        if not matches:
            failures.append(
                VerificationFailure(
                    obligation,
                    "missing-owner-approval",
                    f"no owner approval evidence for action {requirement.action!r}",
                )
            )
            continue
        fingerprints = {(item.decision, item.tree_digest) for item in matches}
        if len(fingerprints) > 1:
            failures.append(
                VerificationFailure(
                    obligation,
                    "contradictory-owner-approval",
                    f"owner approval evidence for {requirement.action!r} is contradictory",
                )
            )
        for approval in matches:
            failures.extend(_validate_meta(approval.meta, snapshot, _generation_for(snapshot.generations, obligation), obligation, "owner approval"))
            if approval.decision != "approve":
                failures.append(
                    VerificationFailure(
                        obligation,
                        "owner-approval-not-positive",
                        f"owner decision for {requirement.action!r} is {approval.decision!r}, not approve",
                        approval.meta.evidence_id,
                    )
                )
            if approval.tree_digest != snapshot.integration_tree_digest:
                failures.append(
                    VerificationFailure(
                        obligation,
                        "stale-owner-approval-tree",
                        f"owner approval is bound to tree {approval.tree_digest!r}, not {snapshot.integration_tree_digest!r}",
                        approval.meta.evidence_id,
                    )
                )


def _validate_policy_obligation(
    frozen_contract: FrozenContract, snapshot: EvidenceSnapshot, failures: list[VerificationFailure]
) -> None:
    policy = frozen_contract.policy
    if policy is None:
        failures.append(
            VerificationFailure(
                Obligation.POLICY_ACTIVATION,
                "missing-policy-contract",
                "the frozen contract contains no policy activation requirements",
            )
        )
        return

    campaigns = [
        item for item in snapshot.campaigns if item.campaign_id == policy.campaign_id and item.epoch == policy.epoch
    ]
    if not campaigns:
        failures.append(
            VerificationFailure(
                Obligation.POLICY_ACTIVATION,
                "missing-campaign-evidence",
                f"no evidence is present for campaign {policy.campaign_id!r} epoch {policy.epoch}",
            )
        )
    else:
        fingerprints = {
            (
                item.tree_digest,
                item.assignment_manifest_digest,
                item.completion_bitmap_digest,
                item.completed_tasks,
                item.total_tasks,
            )
            for item in campaigns
        }
        if len(fingerprints) > 1:
            failures.append(
                VerificationFailure(
                    Obligation.POLICY_ACTIVATION,
                    "contradictory-campaign-evidence",
                    "multiple campaign records disagree about completion or tree",
                )
            )
        for campaign in campaigns:
            failures.extend(
                _validate_meta(campaign.meta, snapshot, snapshot.generations.policy, Obligation.POLICY_ACTIVATION, "campaign evidence")
            )
            if campaign.tree_digest != snapshot.integration_tree_digest:
                failures.append(
                    VerificationFailure(
                        Obligation.POLICY_ACTIVATION,
                        "stale-campaign-tree",
                        f"campaign evidence is bound to tree {campaign.tree_digest!r}",
                        campaign.meta.evidence_id,
                    )
                )
            if not campaign.assignment_manifest_digest or not campaign.completion_bitmap_digest:
                failures.append(
                    VerificationFailure(
                        Obligation.POLICY_ACTIVATION,
                        "missing-campaign-digest",
                        "campaign evidence requires assignment manifest and completion bitmap digests",
                        campaign.meta.evidence_id,
                    )
                )
            if campaign.total_tasks <= 0 or campaign.completed_tasks != campaign.total_tasks:
                failures.append(
                    VerificationFailure(
                        Obligation.POLICY_ACTIVATION,
                        "incomplete-campaign",
                        f"campaign completion is {campaign.completed_tasks}/{campaign.total_tasks}",
                        campaign.meta.evidence_id,
                    )
                )

    if not snapshot.evaluator_result_digest or not snapshot.evaluator_signature_digest:
        failures.append(
            VerificationFailure(
                Obligation.POLICY_ACTIVATION,
                "missing-current-signed-result",
                "snapshot lacks the current signed evaluator result or signature digest",
            )
        )
    evaluator_items = [
        item for item in snapshot.evaluator_results if item.campaign_id == policy.campaign_id and item.epoch == policy.epoch
    ]
    if not evaluator_items:
        failures.append(
            VerificationFailure(
                Obligation.POLICY_ACTIVATION,
                "missing-evaluator-evidence",
                "policy activation lacks a signed evaluator result for the frozen campaign epoch",
            )
        )
    else:
        fingerprints = {
            (
                item.tree_digest,
                item.evaluator_version,
                item.signer_key_id,
                item.result_digest,
                item.signature_digest,
                item.nonce,
                item.verdict,
            )
            for item in evaluator_items
        }
        if len(fingerprints) > 1:
            failures.append(
                VerificationFailure(
                    Obligation.POLICY_ACTIVATION,
                    "contradictory-evaluator-evidence",
                    "multiple signed evaluator results for this campaign epoch disagree",
                )
            )
        nonces = [item.nonce for item in evaluator_items]
        if len(nonces) != len(set(nonces)):
            failures.append(
                VerificationFailure(
                    Obligation.POLICY_ACTIVATION,
                    "reused-evaluator-nonce",
                    "the same evaluator nonce appears on multiple signed results",
                )
            )
        for item in evaluator_items:
            failures.extend(
                _validate_meta(item.meta, snapshot, snapshot.generations.policy, Obligation.POLICY_ACTIVATION, "evaluator evidence")
            )
            comparisons = (
                ("stale-evaluator-tree", item.tree_digest, snapshot.integration_tree_digest),
                ("stale-evaluator-version", item.evaluator_version, policy.evaluator_version),
                ("untrusted-evaluator-key", item.signer_key_id, policy.signer_key_id),
            )
            for reason, actual, expected in comparisons:
                if actual != expected:
                    failures.append(
                        VerificationFailure(
                            Obligation.POLICY_ACTIVATION,
                            reason,
                            f"evaluator evidence has {actual!r}; expected {expected!r}",
                            item.meta.evidence_id,
                        )
                    )
            if not item.result_digest or not item.signature_digest or not item.nonce:
                failures.append(
                    VerificationFailure(
                        Obligation.POLICY_ACTIVATION,
                        "missing-signed-result-digest",
                        "signed evaluator evidence requires result, signature, and nonce digests",
                        item.meta.evidence_id,
                    )
                )
            if snapshot.evaluator_result_digest and item.result_digest != snapshot.evaluator_result_digest:
                failures.append(
                    VerificationFailure(
                        Obligation.POLICY_ACTIVATION,
                        "stale-evaluator-result",
                        "signed evaluator result digest does not match the current attested result",
                        item.meta.evidence_id,
                    )
                )
            if snapshot.evaluator_signature_digest and item.signature_digest != snapshot.evaluator_signature_digest:
                failures.append(
                    VerificationFailure(
                        Obligation.POLICY_ACTIVATION,
                        "stale-evaluator-signature",
                        "signed evaluator signature digest does not match the current attested signature",
                        item.meta.evidence_id,
                    )
                )
            if not item.verdict:
                failures.append(
                    VerificationFailure(
                        Obligation.POLICY_ACTIVATION,
                        "failed-evaluator-result",
                        "the signed evaluator result is not a passing verdict",
                        item.meta.evidence_id,
                    )
                )

    _validate_owner_approvals(frozen_contract, snapshot, Obligation.POLICY_ACTIVATION, failures)


def _validate_required_checks(
    shard_contract: ShardContract,
    snapshot: EvidenceSnapshot,
    obligation: Obligation,
    shard_tree: str | None,
) -> list[VerificationFailure]:
    failures: list[VerificationFailure] = []
    requirements = [item for item in shard_contract.checks if item.obligation == obligation]
    expected_generation = _generation_for(snapshot.generations, obligation)
    for requirement in requirements:
        matches = [
            item
            for item in snapshot.checks
            if item.check_id == requirement.check_id and item.obligation == requirement.obligation
        ]
        if not matches:
            failures.append(
                VerificationFailure(
                    obligation,
                    "missing-check-evidence",
                    f"required check {requirement.check_id!r} has no completion evidence",
                )
            )
            continue
        fingerprints = {
            (
                item.kind,
                item.tree_digest,
                item.tooling_digest,
                item.completed,
                item.exit_code,
                item.output_digest,
                item.receipt_id,
                item.cached,
                item.original_evidence_id,
                item.original_receipt_id,
            )
            for item in matches
        }
        if len(fingerprints) > 1:
            failures.append(
                VerificationFailure(
                    obligation,
                    "contradictory-check-evidence",
                    f"required check {requirement.check_id!r} has conflicting completion evidence",
                )
            )
        for check in matches:
            failures.extend(_validate_meta(check.meta, snapshot, expected_generation, obligation, "check evidence"))
            if check.kind != requirement.kind:
                failures.append(
                    VerificationFailure(
                        obligation,
                        "contradictory-check-kind",
                        f"check {check.check_id!r} evidence kind {check.kind!r} does not match contract kind {requirement.kind!r}",
                        check.meta.evidence_id,
                    )
                )
            expected_tree = shard_tree if obligation is Obligation.SHARD else snapshot.integration_tree_digest
            if expected_tree and check.tree_digest != expected_tree:
                failures.append(
                    VerificationFailure(
                        obligation,
                        "stale-check-tree",
                        f"check {check.check_id!r} ran on tree {check.tree_digest!r}, not current tree {expected_tree!r}",
                        check.meta.evidence_id,
                    )
                )
            expected_tooling = _tooling_digest_for_gate(snapshot, check.obligation)
            if check.tooling_digest != expected_tooling:
                failures.append(
                    VerificationFailure(
                        obligation,
                        "stale-check-tooling",
                        f"check {check.check_id!r} used tooling {check.tooling_digest!r}, not {expected_tooling!r}",
                        check.meta.evidence_id,
                    )
                )
            if not check.completed:
                failures.append(
                    VerificationFailure(
                        obligation,
                        "incomplete-check",
                        f"check {check.check_id!r} did not complete",
                        check.meta.evidence_id,
                    )
                )
            if check.exit_code != 0:
                failures.append(
                    VerificationFailure(
                        obligation,
                        "failed-check",
                        f"check {check.check_id!r} exited with {check.exit_code!r}",
                        check.meta.evidence_id,
                    )
                )
            if not check.output_digest or not check.receipt_id:
                failures.append(
                    VerificationFailure(
                        obligation,
                        "missing-check-provenance",
                        f"check {check.check_id!r} lacks an output digest or trusted receipt ID",
                        check.meta.evidence_id,
                    )
                )
            if check.cached:
                if check.kind in NEVER_CACHEABLE_CHECK_KINDS:
                    failures.append(
                        VerificationFailure(
                            obligation,
                            "uncacheable-check-evidence",
                            f"{check.kind!r} check {check.check_id!r} can never use cached evidence",
                            check.meta.evidence_id,
                        )
                    )
                if not check.original_evidence_id or not check.original_receipt_id:
                    failures.append(
                        VerificationFailure(
                            obligation,
                            "missing-cache-origin",
                            f"cached check {check.check_id!r} does not point at its original evidence and receipt",
                            check.meta.evidence_id,
                        )
                    )
    return failures


def _validate_meta(
    meta: EvidenceMeta,
    snapshot: EvidenceSnapshot,
    expected_generation: int | None,
    obligation: Obligation,
    label: str,
) -> list[VerificationFailure]:
    failures: list[VerificationFailure] = []
    evidence_id = meta.evidence_id or None
    if not meta.evidence_id:
        failures.append(VerificationFailure(obligation, "missing-evidence-id", f"{label} has no evidence ID"))
    if meta.status == "superseded" or meta.superseded_by:
        failures.append(
            VerificationFailure(obligation, "superseded-evidence", f"{label} has been superseded", evidence_id)
        )
    elif meta.status == "expired":
        failures.append(VerificationFailure(obligation, "expired-evidence", f"{label} is marked expired", evidence_id))
    elif meta.status != "active":
        failures.append(
            VerificationFailure(
                obligation,
                "inactive-evidence",
                f"{label} has non-active status {meta.status!r}",
                evidence_id,
            )
        )
    if meta.expires_at is not None and meta.expires_at <= snapshot.observed_at:
        failures.append(
            VerificationFailure(
                obligation,
                "expired-evidence",
                f"{label} expired at {meta.expires_at}, observed at {snapshot.observed_at}",
                evidence_id,
            )
        )
    if meta.created_at > snapshot.observed_at:
        failures.append(
            VerificationFailure(
                obligation,
                "contradictory-evidence-time",
                f"{label} was created at {meta.created_at}, after observation time {snapshot.observed_at}",
                evidence_id,
            )
        )
    if expected_generation is not None and meta.generation != expected_generation:
        failures.append(
            VerificationFailure(
                obligation,
                "stale-generation",
                f"{label} generation {meta.generation} is not current generation {expected_generation}",
                evidence_id,
            )
        )
    for field_name in _binding_field_names():
        actual = getattr(meta.binding, field_name)
        expected = getattr(snapshot.binding, field_name)
        if actual != expected:
            failures.append(
                VerificationFailure(
                    obligation,
                    f"stale-{_reason_field(field_name)}",
                    f"{label} binding {field_name} is {actual!r}, but current evidence is bound to {expected!r}",
                    evidence_id,
                )
            )
    return failures


def _binding_field_names() -> tuple[str, ...]:
    return (
        "repo",
        "run_id",
        "issue_number",
        "pr_number",
        "contract_revision",
        "contract_digest",
        "base_head",
        "shard_head",
    )


def _reason_field(field_name: str) -> str:
    return {
        "run_id": "run",
        "issue_number": "issue",
        "pr_number": "pr",
        "contract_revision": "contract-revision",
        "contract_digest": "contract-digest",
        "base_head": "base-head",
        "shard_head": "shard-head",
    }.get(field_name, field_name.replace("_", "-"))


def _generation_for(generations: Generations, obligation: Obligation) -> int:
    if obligation is Obligation.SHARD:
        return generations.shard
    if obligation is Obligation.INTEGRATED:
        return generations.integration
    if obligation is Obligation.REVIEW_SUBMISSION:
        return generations.review
    if obligation is Obligation.MERGE_ELIGIBILITY:
        return generations.merge
    return generations.policy
