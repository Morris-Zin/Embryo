"""Receipt validation interface used by the verification check cache.

The receipts broker is implemented by the parallel receipts shard.  This module
is intentionally only an interface stub: the cache accepts a caller-supplied
validator and never guesses receipt validity itself.
"""

from __future__ import annotations

from typing import Protocol


class ReceiptValidator(Protocol):
    """Return True only when ``receipt_id`` is present, current, and unrevoked."""

    def __call__(self, receipt_id: str) -> bool:
        ...


def missing_receipts_shard_validator(receipt_id: str) -> bool:
    """Fail-closed placeholder used when no receipts broker has been wired."""

    return False
