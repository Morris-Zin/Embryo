"""Receipt validation interface used by the verification check cache.

The receipts broker is implemented by the receipts shard.  This module only
defines the interface: the cache accepts a caller-supplied validator and never
guesses receipt validity itself.

A receipt is not a bare token.  The validator receives the exact execution the
cache wants to vouch for (cache key digest plus a digest of the full outcome)
and must return True only when the receipt is present, current, unrevoked AND
attests precisely that execution.  A valid receipt for a different exit code,
output or key must be refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ReceiptAttestation:
    """The execution a receipt must attest for a cache store or hit."""

    cache_key_digest: str
    outcome_digest: str
    evidence_id: str
    evidence_digest: str
    output_digest: str
    exit_code: int
    completed_at: int


class ReceiptValidator(Protocol):
    """Return True only when ``receipt_id`` is current, unrevoked and attests ``attestation`` exactly."""

    def __call__(self, receipt_id: str, attestation: ReceiptAttestation) -> bool:
        ...


def missing_receipts_shard_validator(receipt_id: str, attestation: ReceiptAttestation) -> bool:
    """Fail-closed placeholder used when no receipts broker has been wired."""

    return False
