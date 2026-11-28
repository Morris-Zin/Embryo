# Shadow receipts broker

The broker records T3 delegation and local check evidence for ADR 0005. It runs entirely in
Python’s standard library and makes zero model calls. The coordinator calls it
explicitly. The existing autoloop dispatch, verification, and merge commands
have no broker dependency.

## Coordinator protocol

1. Use `Broker(checkout_repo_id(checkout))` with a shared Git origin. Checkout
   paths, credentials in hosted URLs, and HTTPS versus SSH do not distinguish
   GitHub clones of the same repo. For a checkout without an origin, supply a
   stable repository ID explicitly.
2. Call `reserve` with the frozen metadata and the next generation for this
   run, shard, and role, starting at 1. Use a new T3 request ID for each
   generation. The successful caller owns the dispatch. A duplicate reservation
   raises `ReceiptError`, even for an identical request. Use `by_request` to
   recover an earlier reservation without acquiring dispatch ownership.
3. Call `begin_dispatch(reservation_id)` immediately before making the T3
   request with the persisted request ID as the idempotency key. Call
   `attach(reservation_id, task_id)` once T3 confirms the task. An identical
   attachment is idempotent.
4. Call `settle` with the task result, timing in seconds, Git fingerprints
   (`base`, `head`, `tree`), evidence IDs, and usage. Use `provider`, `estimated`,
   or `not_observed` as the usage source. Omit unavailable counters or set them
   to null. Preserve the reason, currency, and any provider-specific counters
   in the usage object. Use the same currency as the legacy log for the
   optional scalar `cost` projected into routing.
5. Set `publish=True` when the coordinator intends a completed, accepted
   result with evidence to become the current shadow publication. Settlement
   checks the current generation, prior publication pointer, expiry,
   revocation, and before/after Git bindings in one transaction. Call
   `revoke` on cancellation or an external contract, policy, or base change.
   `current_publication` returns the active publication for a slot.
   `settled` includes all outcomes and spend, including superseded work.
   An identical settlement returns the original receipt. Conflicting
   settlement data raises `ReceiptError`.

After a coordinator interruption, call `reconcile` when the previous sender
can no longer submit the request. A reservation that never entered dispatch
settles as `not_run`. A send intent without a confirmed task ID enters
quarantine and blocks replacement. Look up the persisted request ID and attach
the confirmed task, or pass a `no_dispatch_evidence_id` proving T3 did not
start the work. A missing response or timeout alone is insufficient proof.
An attached task stays available for settlement after recovery, including when
its expiry has passed.

## Local check executions

For a coordinator-run subprocess, pass the verification cache's
`cache_key_digest` to `reserve`. Each key has its own generation within the
run, shard, and role. Call `settle_execution` directly with the exit code,
output and evidence digests, evidence ID, integer completion timestamp, elapsed
seconds, and Git fingerprints. The broker computes the digest of the full
canonical outcome. A nonzero exit code can be attested. The check must finish
on the reserved Git fingerprint to publish.

Pass `broker.validate_execution_receipt` to the cache as its receipt
validator. The validator requires the exact key, outcome digest, and outcome
fields. Both validation and `current_publication` check the generation,
revocation, and expiration at read time. A local publication lookup also takes
`cache_key_digest`. The historical receipt remains available through
`settled` after its publication expires. For an interrupted local run,
`reconcile` preserves the reservation so the coordinator can settle a known
outcome or revoke it and reserve a replacement.

## Storage and reporting

The ledger uses the host resource lease authority and the same OS mutex as
the leases. The directory is `%ProgramData%\autoloop\receipts` on Windows
and `/var/lock/autoloop/receipts` on native Linux. Use Windows Python through
interop or PowerShell from WSL. The coordinator principal needs write access
to this directory. Each repository has one atomically replaced JSON ledger.
Settlements are append-only records within it, keyed by the SHA-256 of
canonical JSON for the receipt payload. The reservation state and publication
pointer are committed with the receipt.

Per ADR 0005, **processes running as the same OS user are not a security
boundary**. This shadow ledger coordinates cooperating processes. Enforced
admission and verification require a separate coordinator principal, trusted
T3 and Git observations, and separate evaluator credentials.

`routing-report` reads the legacy JSONL and settled receipts for the current
checkout’s Git origin. Generation supplies the attempt number and the
run/shard/role identifies the assignment. A late accepted result contributes
its cost while acceptance in the report requires a recorded shadow
publication. Estimates appear separately from observed spend. Legacy rows
remain historical observations, read once per digest into the report without
writing to the ledger. Local check receipts are available through the broker
and excluded from delegation routing statistics. For a receipt-inclusive
report from WSL, invoke the existing CLI with Windows Python.
