"""Acceptance tests for the Phase 2 verification predicate and check cache."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from autoloop.verification import cache as cache_module
from autoloop.verification.digests import canonical_json_bytes
from autoloop.verification import (
    CACHE_SCHEMA_VERSION,
    ApprovalRequirement,
    Binding,
    CampaignEvidence,
    CheckCache,
    CheckCacheKey,
    CheckEvidence,
    CheckOutcome,
    CheckRequirement,
    EvaluatorEvidence,
    EvidenceMeta,
    EvidenceSnapshot,
    FindingEvidence,
    FreshCheckResult,
    FrozenContract,
    Generations,
    InputDigest,
    IntegrationEvidence,
    Obligation,
    OwnerApprovalEvidence,
    PolicyContract,
    ReceiptAttestation,
    ReviewEvidence,
    SafeguardEvidence,
    ShardContract,
    ShardEvidence,
    Target,
    ToolingEvidence,
    ToolingSource,
    tooling_sources_digest,
    env_allowlist_digest,
    receipt_attestation,
    verify,
)

NOW = 1_000
REPO = "owner/repo"
RUN = "autoloop-recursive-20261128"
ISSUE = 42
PR = 43
REVISION = 7
CONTRACT_DIGEST = "c" * 64
BASE_HEAD = "b" * 40
SHARD_HEAD = "5" * 40
INTEGRATION_COMMIT = "6" * 40
SHARD_TREE = "a" * 64
INTEGRATED_TREE = "d" * 64
TOOLING_SOURCES = (ToolingSource("tools/autoloop/guard.py", "2" * 64),)
TOOLING_DIGEST = tooling_sources_digest(TOOLING_SOURCES)
EVALUATOR_RESULT_DIGEST = "f" * 64
EVALUATOR_SIGNATURE_DIGEST = "1" * 64
SHARD_ID = "verification"


def binding() -> Binding:
    return Binding(
        repo=REPO,
        run_id=RUN,
        issue_number=ISSUE,
        pr_number=PR,
        contract_revision=REVISION,
        contract_digest=CONTRACT_DIGEST,
        base_head=BASE_HEAD,
        shard_head=SHARD_HEAD,
    )


def meta(evidence_id: str, generation: int, **kwargs) -> EvidenceMeta:
    values = {
        "evidence_id": evidence_id,
        "binding": binding(),
        "generation": generation,
        "created_at": NOW - 10,
    }
    values.update(kwargs)
    return EvidenceMeta(**values)


def contract() -> FrozenContract:
    return FrozenContract(
        repo=REPO,
        run_id=RUN,
        issue_number=ISSUE,
        pr_number=PR,
        revision=REVISION,
        digest=CONTRACT_DIGEST,
        shards=(
            ShardContract(
                SHARD_ID,
                checks=(
                    CheckRequirement("unit", Obligation.SHARD),
                    CheckRequirement("integrated-suite", Obligation.INTEGRATED),
                    CheckRequirement("merge-gate", Obligation.MERGE_ELIGIBILITY),
                    CheckRequirement("private-evaluator", Obligation.POLICY_ACTIVATION, kind="evaluator"),
                ),
            ),
        ),
        approvals=(
            ApprovalRequirement(Obligation.MERGE_ELIGIBILITY, "merge"),
            ApprovalRequirement(Obligation.POLICY_ACTIVATION, "activate-policy"),
        ),
        review_required=True,
        policy=PolicyContract(
            campaign_id="campaign-1",
            epoch=3,
            evaluator_version="evaluator-v1",
            signer_key_id="owner-key-v1",
        ),
    )


def check_evidence(
    check_id: str,
    obligation: Obligation,
    generation: int,
    tree_digest: str,
    *,
    kind: str = "deterministic",
) -> CheckEvidence:
    return CheckEvidence(
        meta=meta(f"check-{check_id}", generation),
        check_id=check_id,
        obligation=obligation,
        kind=kind,
        tree_digest=tree_digest,
        tooling_digest=TOOLING_DIGEST,
        completed=True,
        exit_code=0,
        output_digest=f"output-{check_id}",
        receipt_id=f"receipt-{check_id}",
    )


def valid_snapshot() -> EvidenceSnapshot:
    generations = Generations(shard=1, integration=2, review=3, merge=4, policy=5)
    finding = FindingEvidence(
        meta=meta("finding-1-evidence", generations.review),
        finding_id="finding-1",
        reviewed_tree=INTEGRATED_TREE,
        artifact_digest="finding-artifact",
        blocking=True,
        resolved=True,
        resolution_digest="resolution-artifact",
        safeguard_digests=("safeguard-artifact",),
    )
    return EvidenceSnapshot(
        observed_at=NOW,
        binding=binding(),
        generations=generations,
        integration_commit=INTEGRATION_COMMIT,
        integration_tree_digest=INTEGRATED_TREE,
        shard_tree_digest=SHARD_TREE,
        tooling_digest=TOOLING_DIGEST,
        evaluator_result_digest=EVALUATOR_RESULT_DIGEST,
        evaluator_signature_digest=EVALUATOR_SIGNATURE_DIGEST,
        tooling=(
            ToolingEvidence(
                meta=meta("tooling-evidence", 0),
                tooling_digest=TOOLING_DIGEST,
                sources=TOOLING_SOURCES,
            ),
        ),
        shard_results=(
            ShardEvidence(
                meta=meta("shard-evidence", generations.shard),
                shard_id=SHARD_ID,
                tree_digest=SHARD_TREE,
                implementation_receipt_id="implementation-receipt",
            ),
        ),
        integrations=(
            IntegrationEvidence(
                meta=meta("integration-evidence", generations.integration),
                integration_commit=INTEGRATION_COMMIT,
                integration_tree_digest=INTEGRATED_TREE,
                integration_receipt_id="integration-receipt",
            ),
        ),
        reviews=(
            ReviewEvidence(
                meta=meta("review-evidence", generations.review),
                reviewed_tree=INTEGRATED_TREE,
                review_digest="review-digest",
                finding_digests=("finding-artifact",),
                reviewer_identity="reviewer-a",
            ),
        ),
        checks=(
            check_evidence("unit", Obligation.SHARD, generations.shard, SHARD_TREE),
            check_evidence("integrated-suite", Obligation.INTEGRATED, generations.integration, INTEGRATED_TREE),
            check_evidence("merge-gate", Obligation.MERGE_ELIGIBILITY, generations.merge, INTEGRATED_TREE),
            check_evidence(
                "private-evaluator",
                Obligation.POLICY_ACTIVATION,
                generations.policy,
                INTEGRATED_TREE,
                kind="evaluator",
            ),
        ),
        findings=(finding,),
        safeguards=(
            SafeguardEvidence(
                meta=meta("safeguard-evidence", generations.merge),
                finding_id="finding-1",
                tree_digest=INTEGRATED_TREE,
                artifact_digest="safeguard-artifact",
            ),
        ),
        approvals=(
            OwnerApprovalEvidence(
                meta=meta("merge-approval", generations.merge),
                obligation=Obligation.MERGE_ELIGIBILITY,
                action="merge",
                tree_digest=INTEGRATED_TREE,
            ),
            OwnerApprovalEvidence(
                meta=meta("policy-approval", generations.policy),
                obligation=Obligation.POLICY_ACTIVATION,
                action="activate-policy",
                tree_digest=INTEGRATED_TREE,
            ),
        ),
        campaigns=(
            CampaignEvidence(
                meta=meta("campaign-evidence", generations.policy),
                campaign_id="campaign-1",
                epoch=3,
                tree_digest=INTEGRATED_TREE,
                assignment_manifest_digest="assignment-manifest",
                completion_bitmap_digest="completion-bitmap",
                completed_tasks=4,
                total_tasks=4,
            ),
        ),
        evaluator_results=(
            EvaluatorEvidence(
                meta=meta("evaluator-evidence", generations.policy, expires_at=NOW + 100),
                campaign_id="campaign-1",
                epoch=3,
                tree_digest=INTEGRATED_TREE,
                evaluator_version="evaluator-v1",
                signer_key_id="owner-key-v1",
                result_digest=EVALUATOR_RESULT_DIGEST,
                signature_digest=EVALUATOR_SIGNATURE_DIGEST,
                nonce="nonce-1",
                verdict=True,
            ),
        ),
    )


def target(obligation: Obligation = Obligation.POLICY_ACTIVATION) -> Target:
    return Target(obligation, SHARD_ID)


def replace_item(snapshot: EvidenceSnapshot, collection: str, index: int, **changes) -> EvidenceSnapshot:
    items = list(getattr(snapshot, collection))
    items[index] = replace(items[index], **changes)
    return replace(snapshot, **{collection: tuple(items)})


def replace_item_meta(snapshot: EvidenceSnapshot, collection: str, index: int, **changes) -> EvidenceSnapshot:
    items = list(getattr(snapshot, collection))
    items[index] = replace(items[index], meta=replace(items[index].meta, **changes))
    return replace(snapshot, **{collection: tuple(items)})


class VerificationPredicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = contract()
        self.snapshot = valid_snapshot()

    def assert_rejected(self, snapshot: EvidenceSnapshot, obligation: Obligation = Obligation.POLICY_ACTIVATION) -> None:
        decision = verify(target(obligation), self.contract, snapshot)
        self.assertFalse(decision.ok)
        self.assertTrue(decision.failures)
        self.assertNotIn(obligation, decision.obligations_met)

    def test_valid_snapshot_satisfies_each_ordered_target(self) -> None:
        expected: list[Obligation] = []
        for obligation in (
            Obligation.SHARD,
            Obligation.INTEGRATED,
            Obligation.REVIEW_SUBMISSION,
            Obligation.MERGE_ELIGIBILITY,
            Obligation.POLICY_ACTIVATION,
        ):
            with self.subTest(obligation=obligation.value):
                expected.append(obligation)
                decision = verify(target(obligation), self.contract, self.snapshot)
                self.assertTrue(decision.ok, decision.failures)
                self.assertEqual(tuple(expected), decision.obligations_met)
                self.assertEqual((), decision.failures)

    def test_verify_is_deterministic_for_identical_inputs(self) -> None:
        first = verify(target(), self.contract, self.snapshot)
        second = verify(target(), self.contract, self.snapshot)
        self.assertEqual(first, second)

    def test_obligation_ordering_and_additive_review_requirement(self) -> None:
        without_review = replace(self.snapshot, reviews=(), findings=(), safeguards=())
        integrated = verify(target(Obligation.INTEGRATED), self.contract, without_review)
        self.assertTrue(integrated.ok, integrated.failures)
        self.assertEqual((Obligation.SHARD, Obligation.INTEGRATED), integrated.obligations_met)

        # ADR 0005: a run qualifies for review submission before any review exists.
        review = verify(target(Obligation.REVIEW_SUBMISSION), self.contract, without_review)
        self.assertTrue(review.ok, review.failures)
        self.assertEqual(
            (Obligation.SHARD, Obligation.INTEGRATED, Obligation.REVIEW_SUBMISSION), review.obligations_met
        )

        # The exact-tree review is required additively at merge eligibility.
        merge = verify(target(Obligation.MERGE_ELIGIBILITY), self.contract, without_review)
        self.assertFalse(merge.ok)
        self.assertEqual(
            (Obligation.SHARD, Obligation.INTEGRATED, Obligation.REVIEW_SUBMISSION), merge.obligations_met
        )
        self.assertIn("missing-review-evidence", {failure.reason for failure in merge.failures})
        self.assertTrue(
            all(failure.obligation is Obligation.MERGE_ELIGIBILITY for failure in merge.failures), merge.failures
        )

        stale_review = replace_item(self.snapshot, "reviews", 0, reviewed_tree="0" * 64)
        stale_submission = verify(target(Obligation.REVIEW_SUBMISSION), self.contract, stale_review)
        self.assertTrue(stale_submission.ok, stale_submission.failures)
        stale_merge = verify(target(Obligation.MERGE_ELIGIBILITY), self.contract, stale_review)
        self.assertFalse(stale_merge.ok)
        self.assertIn("stale-reviewed-tree", {failure.reason for failure in stale_merge.failures})

        no_shard = replace(self.snapshot, shard_results=())
        shard_blocked = verify(target(Obligation.INTEGRATED), self.contract, no_shard)
        self.assertFalse(shard_blocked.ok)
        self.assertEqual((), shard_blocked.obligations_met)
        self.assertIn("missing-shard-evidence", {failure.reason for failure in shard_blocked.failures})

    def _per_gate_tooling_snapshot(self) -> tuple[EvidenceSnapshot, str]:
        """Shard check ran with tooling that excludes the shard; integrated checks with overlay tooling."""
        shard_sources = (ToolingSource("tools/autoloop/state.py", "3" * 64),)
        shard_digest = tooling_sources_digest(shard_sources)
        self.assertNotEqual(shard_digest, TOOLING_DIGEST)
        snapshot = replace(
            self.snapshot,
            tooling=self.snapshot.tooling
            + (
                ToolingEvidence(
                    meta=meta("shard-tooling-evidence", 0),
                    tooling_digest=shard_digest,
                    sources=shard_sources,
                    gate=Obligation.SHARD,
                ),
            ),
        )
        return replace_item(snapshot, "checks", 0, tooling_digest=shard_digest), shard_digest

    def test_distinct_valid_tooling_per_gate_is_accepted(self) -> None:
        snapshot, _ = self._per_gate_tooling_snapshot()
        decision = verify(target(), self.contract, snapshot)
        self.assertTrue(decision.ok, decision.failures)

    def test_swapped_gate_tooling_digests_are_rejected(self) -> None:
        snapshot, shard_digest = self._per_gate_tooling_snapshot()
        # shard check reports the integrated tooling instead of its own gate's tooling
        shard_swapped = replace_item(snapshot, "checks", 0, tooling_digest=TOOLING_DIGEST)
        decision = verify(target(Obligation.SHARD), self.contract, shard_swapped)
        self.assertFalse(decision.ok)
        self.assertIn("stale-check-tooling", {failure.reason for failure in decision.failures})
        # integrated check reports the shard gate's tooling
        integrated_swapped = replace_item(snapshot, "checks", 1, tooling_digest=shard_digest)
        decision = verify(target(Obligation.INTEGRATED), self.contract, integrated_swapped)
        self.assertFalse(decision.ok)
        self.assertEqual((Obligation.SHARD,), decision.obligations_met)
        self.assertIn("stale-check-tooling", {failure.reason for failure in decision.failures})

    def test_conflicting_tooling_records_for_one_gate_are_contradictory(self) -> None:
        snapshot, _ = self._per_gate_tooling_snapshot()
        other_sources = (ToolingSource("tools/autoloop/cli.py", "4" * 64),)
        conflicting = replace(
            snapshot,
            tooling=snapshot.tooling
            + (
                ToolingEvidence(
                    meta=meta("shard-tooling-evidence-2", 0),
                    tooling_digest=tooling_sources_digest(other_sources),
                    sources=other_sources,
                    gate=Obligation.SHARD,
                ),
            ),
        )
        decision = verify(target(Obligation.SHARD), self.contract, conflicting)
        self.assertFalse(decision.ok)
        self.assertIn("contradictory-tooling-evidence", {failure.reason for failure in decision.failures})

    def test_unbound_tooling_must_still_match_snapshot_digest(self) -> None:
        stale = replace(self.snapshot, tooling_digest=tooling_sources_digest((ToolingSource("x.py", "5" * 64),)))
        decision = verify(target(Obligation.SHARD), self.contract, stale)
        self.assertFalse(decision.ok)
        self.assertIn("stale-tooling-digest", {failure.reason for failure in decision.failures})

    def test_reviewer_identity_is_never_part_of_the_predicate(self) -> None:
        mutated = replace_item(self.snapshot, "reviews", 0, reviewer_identity="completely-different-reviewer")
        decision = verify(target(Obligation.REVIEW_SUBMISSION), self.contract, mutated)
        self.assertTrue(decision.ok, decision.failures)

    def test_each_bound_field_mutation_fails_closed(self) -> None:
        def mutate_binding(**changes):
            return replace(self.snapshot, binding=replace(self.snapshot.binding, **changes))

        def mutate_generations(**changes):
            return replace(self.snapshot, generations=replace(self.snapshot.generations, **changes))

        def mutate_shard_binding(field, value):
            shard = self.snapshot.shard_results[0]
            rebound = replace(shard.meta.binding, **{field: value})
            return replace_item_meta(self.snapshot, "shard_results", 0, binding=rebound)

        cases = [
            ("binding repo", lambda: mutate_binding(repo="other/repo")),
            ("binding run", lambda: mutate_binding(run_id="other-run")),
            ("binding issue", lambda: mutate_binding(issue_number=ISSUE + 1)),
            ("binding pr", lambda: mutate_binding(pr_number=PR + 1)),
            ("binding contract revision", lambda: mutate_binding(contract_revision=REVISION + 1)),
            ("binding contract digest", lambda: mutate_binding(contract_digest="0" * 64)),
            ("binding base head", lambda: mutate_binding(base_head="0" * 40)),
            ("binding shard head", lambda: mutate_binding(shard_head="0" * 40)),
            ("integration commit", lambda: replace(self.snapshot, integration_commit="0" * 40)),
            ("integration tree", lambda: replace(self.snapshot, integration_tree_digest="0" * 64)),
            ("shard tree", lambda: replace(self.snapshot, shard_tree_digest="0" * 64)),
            ("tooling digest", lambda: replace(self.snapshot, tooling_digest="0" * 64)),
            ("evaluator result digest", lambda: replace(self.snapshot, evaluator_result_digest="0" * 64)),
            ("evaluator signature digest", lambda: replace(self.snapshot, evaluator_signature_digest="0" * 64)),
            ("shard generation", lambda: mutate_generations(shard=99)),
            ("integration generation", lambda: mutate_generations(integration=99)),
            ("review generation", lambda: mutate_generations(review=99)),
            ("merge generation", lambda: mutate_generations(merge=99)),
            ("policy generation", lambda: mutate_generations(policy=99)),
            ("evidence repo binding", lambda: mutate_shard_binding("repo", "other/repo")),
            ("evidence run binding", lambda: mutate_shard_binding("run_id", "other-run")),
            ("evidence issue binding", lambda: mutate_shard_binding("issue_number", ISSUE + 1)),
            ("evidence pr binding", lambda: mutate_shard_binding("pr_number", PR + 1)),
            ("evidence revision binding", lambda: mutate_shard_binding("contract_revision", REVISION + 1)),
            ("evidence contract digest binding", lambda: mutate_shard_binding("contract_digest", "0" * 64)),
            ("evidence base binding", lambda: mutate_shard_binding("base_head", "0" * 40)),
            ("evidence shard head binding", lambda: mutate_shard_binding("shard_head", "0" * 40)),
            ("tooling evidence digest", lambda: replace_item(self.snapshot, "tooling", 0, tooling_digest="0" * 64)),
            (
                "tooling source blob",
                lambda: replace_item(
                    self.snapshot,
                    "tooling",
                    0,
                    sources=(replace(TOOLING_SOURCES[0], blob_sha256="0" * 64),),
                ),
            ),
            (
                "tooling source path",
                lambda: replace_item(
                    self.snapshot,
                    "tooling",
                    0,
                    sources=(replace(TOOLING_SOURCES[0], path="tools/other.py"),),
                ),
            ),
            ("shard evidence tree", lambda: replace_item(self.snapshot, "shard_results", 0, tree_digest="0" * 64)),
            ("integration evidence commit", lambda: replace_item(self.snapshot, "integrations", 0, integration_commit="0" * 40)),
            ("integration evidence tree", lambda: replace_item(self.snapshot, "integrations", 0, integration_tree_digest="0" * 64)),
            ("integration receipt missing", lambda: replace_item(self.snapshot, "integrations", 0, integration_receipt_id="")),
            ("shard check tree", lambda: replace_item(self.snapshot, "checks", 0, tree_digest="0" * 64)),
            ("check tooling", lambda: replace_item(self.snapshot, "checks", 0, tooling_digest="0" * 64)),
            ("check incomplete", lambda: replace_item(self.snapshot, "checks", 0, completed=False)),
            ("check failed", lambda: replace_item(self.snapshot, "checks", 0, exit_code=1)),
            ("check receipt missing", lambda: replace_item(self.snapshot, "checks", 0, receipt_id="")),
            ("reviewed tree", lambda: replace_item(self.snapshot, "reviews", 0, reviewed_tree="0" * 64)),
            ("review digest", lambda: replace_item(self.snapshot, "reviews", 0, review_digest="")),
            ("review finding binding", lambda: replace_item(self.snapshot, "reviews", 0, finding_digests=("other",))),
            ("finding tree", lambda: replace_item(self.snapshot, "findings", 0, reviewed_tree="0" * 64)),
            ("finding artifact", lambda: replace_item(self.snapshot, "findings", 0, artifact_digest="other")),
            ("finding unresolved", lambda: replace_item(self.snapshot, "findings", 0, resolved=False)),
            ("finding resolution missing", lambda: replace_item(self.snapshot, "findings", 0, resolution_digest=None)),
            ("finding safeguard binding", lambda: replace_item(self.snapshot, "findings", 0, safeguard_digests=("other",))),
            ("safeguard tree", lambda: replace_item(self.snapshot, "safeguards", 0, tree_digest="0" * 64)),
            ("safeguard artifact", lambda: replace_item(self.snapshot, "safeguards", 0, artifact_digest="other")),
            ("merge approval tree", lambda: replace_item(self.snapshot, "approvals", 0, tree_digest="0" * 64)),
            ("merge approval decision", lambda: replace_item(self.snapshot, "approvals", 0, decision="reject")),
            ("merge approval action", lambda: replace_item(self.snapshot, "approvals", 0, action="other")),
            ("campaign tree", lambda: replace_item(self.snapshot, "campaigns", 0, tree_digest="0" * 64)),
            ("campaign epoch", lambda: replace_item(self.snapshot, "campaigns", 0, epoch=4)),
            ("campaign incomplete", lambda: replace_item(self.snapshot, "campaigns", 0, completed_tasks=3)),
            ("campaign manifest digest", lambda: replace_item(self.snapshot, "campaigns", 0, assignment_manifest_digest="")),
            ("evaluator tree", lambda: replace_item(self.snapshot, "evaluator_results", 0, tree_digest="0" * 64)),
            ("evaluator version", lambda: replace_item(self.snapshot, "evaluator_results", 0, evaluator_version="other")),
            ("evaluator key", lambda: replace_item(self.snapshot, "evaluator_results", 0, signer_key_id="other")),
            ("evaluator result", lambda: replace_item(self.snapshot, "evaluator_results", 0, result_digest="0" * 64)),
            ("evaluator signature", lambda: replace_item(self.snapshot, "evaluator_results", 0, signature_digest="0" * 64)),
            ("evaluator verdict", lambda: replace_item(self.snapshot, "evaluator_results", 0, verdict=False)),
            ("expired evidence", lambda: replace_item_meta(self.snapshot, "shard_results", 0, expires_at=NOW)),
            ("superseded evidence", lambda: replace_item_meta(self.snapshot, "integrations", 0, status="superseded")),
            ("future evidence", lambda: replace_item_meta(self.snapshot, "reviews", 0, created_at=NOW + 1)),
            ("stale evidence generation", lambda: replace_item_meta(self.snapshot, "shard_results", 0, generation=99)),
        ]
        for name, mutate in cases:
            with self.subTest(mutation=name):
                self.assert_rejected(mutate())

    def test_missing_evidence_classes_fail_closed_with_specific_reasons(self) -> None:
        cases = [
            ("tooling", Obligation.SHARD, replace(self.snapshot, tooling=()), "missing-tooling-evidence"),
            ("integration", Obligation.INTEGRATED, replace(self.snapshot, integrations=()), "missing-integration-evidence"),
            (
                "required check",
                Obligation.SHARD,
                replace(self.snapshot, checks=self.snapshot.checks[1:]),
                "missing-check-evidence",
            ),
            (
                "merge approval",
                Obligation.MERGE_ELIGIBILITY,
                replace(self.snapshot, approvals=self.snapshot.approvals[1:]),
                "missing-owner-approval",
            ),
            ("campaign", Obligation.POLICY_ACTIVATION, replace(self.snapshot, campaigns=()), "missing-campaign-evidence"),
            ("evaluator", Obligation.POLICY_ACTIVATION, replace(self.snapshot, evaluator_results=()), "missing-evaluator-evidence"),
        ]
        for name, obligation, mutated, expected_reason in cases:
            with self.subTest(missing=name):
                decision = verify(target(obligation), self.contract, mutated)
                self.assertFalse(decision.ok)
                self.assertIn(expected_reason, {failure.reason for failure in decision.failures})

    def test_contradictory_check_evidence_fails_closed(self) -> None:
        first = self.snapshot.checks[0]
        duplicate = replace(
            first,
            meta=replace(first.meta, evidence_id="conflicting-check"),
            output_digest="different-output",
        )
        mutated = replace(self.snapshot, checks=(first, duplicate, *self.snapshot.checks[1:]))
        decision = verify(target(Obligation.SHARD), self.contract, mutated)
        self.assertFalse(decision.ok)
        self.assertIn("contradictory-check-evidence", {failure.reason for failure in decision.failures})

    def test_cached_live_timing_or_evaluator_evidence_is_rejected_by_predicate(self) -> None:
        index = 3
        mutated = replace_item(
            self.snapshot,
            "checks",
            index,
            cached=True,
            original_evidence_id="origin",
            original_receipt_id="origin-receipt",
        )
        decision = verify(target(), self.contract, mutated)
        self.assertFalse(decision.ok)
        self.assertIn("uncacheable-check-evidence", {failure.reason for failure in decision.failures})


class CheckCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.cache = CheckCache(Path(self.tempdir.name))
        self.key = CheckCacheKey(
            code_tree_sha256="a" * 64,
            tooling_digest="b" * 64,
            argv=("python", "-B", "tools/fast_check.py"),
            interpreter_dependency_lock_digest="c" * 64,
            env_allowlist_digest="d" * 64,
            platform="windows-x86_64",
            working_directory=".",
        )
        self.outcome = CheckOutcome(
            evidence_id="original-evidence",
            evidence_digest="e" * 64,
            output_digest="f" * 64,
            exit_code=0,
            completed_at=NOW,
        )

        # Receipts ledger: receipt id -> the execution it attests (None once revoked).
        self.ledger: dict[str, ReceiptAttestation | None] = {
            "receipt-1": receipt_attestation(self.cache.key_digest(self.key), self.outcome)
        }

    def valid_receipt(self, receipt_id: str, attestation: ReceiptAttestation) -> bool:
        return self.ledger.get(receipt_id) == attestation

    def store_valid(self) -> None:
        stored = self.cache.store(
            self.key,
            self.outcome,
            "receipt-1",
            self.valid_receipt,
            cacheable=True,
        )
        self.assertTrue(stored)

    def test_cache_hit_points_at_original_evidence(self) -> None:
        self.store_valid()
        found = self.cache.lookup(self.key, self.valid_receipt, cacheable=True)
        self.assertTrue(found.hit, found.reason)
        self.assertIsNotNone(found.evidence)
        assert found.evidence is not None
        self.assertTrue(found.evidence.cached)
        self.assertEqual("original-evidence", found.evidence.original_evidence_id)
        self.assertEqual("receipt-1", found.evidence.original_receipt_id)
        self.assertEqual(self.outcome.output_digest, found.evidence.output_digest)
        self.assertEqual(self.cache.key_digest(self.key), found.evidence.cache_key_digest)
        evidence_path = self.cache._cached_evidence_path(found.evidence.evidence_id)
        self.assertTrue(evidence_path.is_file())
        self.assertIn("original-evidence", evidence_path.read_text(encoding="utf-8"))

    def test_cache_misses_on_any_key_component_change(self) -> None:
        self.store_valid()
        mutations = {
            "code tree": {"code_tree_sha256": "0" * 64},
            "tooling": {"tooling_digest": "0" * 64},
            "argv": {"argv": ("python", "-B", "other.py")},
            "interpreter dependency lock": {"interpreter_dependency_lock_digest": "0" * 64},
            "environment allowlist": {"env_allowlist_digest": "0" * 64},
            "platform": {"platform": "linux-x86_64"},
            "working directory": {"working_directory": "subdir"},
            "additional input": {"input_digests": (InputDigest("fixture.json", "1" * 64),)},
        }
        for name, changes in mutations.items():
            with self.subTest(component=name):
                mutated_key = replace(self.key, **changes)
                found = self.cache.lookup(mutated_key, self.valid_receipt, cacheable=True)
                self.assertFalse(found.hit)
                self.assertEqual("miss", found.reason)

    def test_hit_without_valid_receipt_is_refused(self) -> None:
        self.store_valid()
        found = self.cache.lookup(self.key, lambda receipt_id, attestation: False, cacheable=True)
        self.assertFalse(found.hit)
        self.assertEqual("invalid-receipt", found.reason)
        self.assertFalse(
            self.cache.store(self.key, self.outcome, "", self.valid_receipt, cacheable=True)
        )

    def test_receipt_for_a_different_outcome_cannot_vouch(self) -> None:
        """Integration-shard repro: a current receipt that recorded exit 1 must not vouch for exit 0."""
        failed = replace(self.outcome, evidence_id="failed-evidence", exit_code=1)
        key_digest = self.cache.key_digest(self.key)
        self.ledger = {"receipt-failed": receipt_attestation(key_digest, failed)}
        self.assertFalse(self.cache.store(self.key, self.outcome, "receipt-failed", self.valid_receipt, cacheable=True))
        # Even a planted entry claiming exit 0 under that receipt is refused on lookup.
        entry = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "key_digest": key_digest,
            "key": cache_module._canonical_key(self.key),
            "receipt_id": "receipt-failed",
            "outcome": cache_module._canonical_outcome(self.outcome),
        }
        path = self.cache._entry_path(key_digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(entry) + b"\n")
        found = self.cache.lookup(self.key, self.valid_receipt, cacheable=True)
        self.assertFalse(found.hit)
        self.assertEqual("invalid-receipt", found.reason)
        self.assertFalse((self.cache.root / "cached-evidence").exists())
        # A receipt for the same outcome under another key cannot vouch either.
        other_key = replace(self.key, argv=("python", "-B", "other.py"))
        self.ledger = {"receipt-other": receipt_attestation(self.cache.key_digest(other_key), self.outcome)}
        self.assertFalse(self.cache.store(self.key, self.outcome, "receipt-other", self.valid_receipt, cacheable=True))

    def test_revoked_entry_is_replaced_by_fresh_validated_result(self) -> None:
        self.store_valid()
        self.ledger["receipt-1"] = None  # revoked
        self.assertEqual("invalid-receipt", self.cache.lookup(self.key, self.valid_receipt, cacheable=True).reason)
        fresh = replace(self.outcome, evidence_id="fresh-evidence", completed_at=NOW + 5)
        self.ledger["receipt-2"] = receipt_attestation(self.cache.key_digest(self.key), fresh)
        self.assertTrue(self.cache.store(self.key, fresh, "receipt-2", self.valid_receipt, cacheable=True))
        found = self.cache.lookup(self.key, self.valid_receipt, cacheable=True)
        self.assertTrue(found.hit, found.reason)
        assert found.evidence is not None
        self.assertEqual("receipt-2", found.evidence.original_receipt_id)
        self.assertEqual("fresh-evidence", found.evidence.original_evidence_id)

    def test_still_valid_entry_is_not_overwritten_by_a_different_result(self) -> None:
        self.store_valid()
        other = replace(self.outcome, evidence_id="other-evidence", output_digest="9" * 64)
        self.ledger["receipt-3"] = receipt_attestation(self.cache.key_digest(self.key), other)
        self.assertFalse(self.cache.store(self.key, other, "receipt-3", self.valid_receipt, cacheable=True))
        found = self.cache.lookup(self.key, self.valid_receipt, cacheable=True)
        assert found.evidence is not None
        self.assertEqual("receipt-1", found.evidence.original_receipt_id)

    def test_non_cacheable_check_never_hits(self) -> None:
        self.store_valid()
        implicit = self.cache.lookup(self.key, self.valid_receipt)
        self.assertFalse(implicit.hit)
        self.assertEqual("check-not-cacheable", implicit.reason)
        for kind in ("live", "timing", "evaluator"):
            with self.subTest(kind=kind):
                found = self.cache.lookup(self.key, self.valid_receipt, cacheable=True, kind=kind)
                self.assertFalse(found.hit)
                self.assertEqual("never-cacheable-kind", found.reason)
                self.assertFalse(
                    self.cache.store(
                        self.key,
                        self.outcome,
                        "receipt-1",
                        self.valid_receipt,
                        cacheable=True,
                        kind=kind,
                    )
                )

    def test_run_executes_fresh_when_not_explicitly_cacheable(self) -> None:
        calls = []

        def runner() -> FreshCheckResult:
            calls.append("run")
            return FreshCheckResult(self.outcome, "receipt-1")

        first = self.cache.run(self.key, runner, self.valid_receipt)
        second = self.cache.run(self.key, runner, self.valid_receipt)
        self.assertFalse(first.cached)
        self.assertFalse(second.cached)
        self.assertFalse(first.stored)
        self.assertFalse(second.stored)
        self.assertEqual(["run", "run"], calls)

    def test_corrupt_or_conflicting_entry_is_not_used(self) -> None:
        self.store_valid()
        path = self.cache._entry_path(self.cache.key_digest(self.key))
        path.write_text("not json", encoding="utf-8")
        found = self.cache.lookup(self.key, self.valid_receipt, cacheable=True)
        self.assertFalse(found.hit)
        self.assertEqual("invalid-entry", found.reason)

    def test_environment_allowlist_digest_is_canonical_and_closed(self) -> None:
        env = {"A": "1", "B": "2", "UNLISTED": "3"}
        self.assertEqual(
            env_allowlist_digest(env, ("B", "A")),
            env_allowlist_digest(env, ("A", "B")),
        )
        self.assertNotEqual(
            env_allowlist_digest(env, ("A", "B")),
            env_allowlist_digest(env, ("A", "B", "MISSING")),
        )
        self.assertNotEqual(
            env_allowlist_digest(env, ("A",)),
            env_allowlist_digest({"A": "9"}, ("A",)),
        )

    def test_synthetic_one_second_check_hit_takes_milliseconds(self) -> None:
        calls = []

        def runner() -> FreshCheckResult:
            calls.append("run")
            time.sleep(1.0)
            return FreshCheckResult(self.outcome, "receipt-1")

        first = self.cache.run(self.key, runner, self.valid_receipt, cacheable=True)
        second = self.cache.run(self.key, runner, self.valid_receipt, cacheable=True)
        self.assertFalse(first.cached)
        self.assertTrue(first.stored)
        self.assertGreaterEqual(first.elapsed_seconds, 0.9)
        self.assertTrue(second.cached)
        self.assertLess(second.elapsed_seconds, 0.5)
        self.assertEqual(["run"], calls)
        self.assertIsNotNone(second.evidence)


if __name__ == "__main__":
    unittest.main()
