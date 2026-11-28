# ADR 0002: Cashier write path under 2 GiB / 2 vCPU

Status: accepted (autoloop run `backend-2gb-proof-20261128`, decided by GPT-6.2 Astra, 2026-11-28)

## Context

A one-minute 80-sale probe on 2 vCPU / 2 GiB attributed 97.3 s of overlapping SQL wait to the locking read of `tabSeries.current`. That was 87.5% of SQL time, with 67 InnoDB row-lock waits. These are waiter durations, not the owner's hold time, and they predate PR #17, which cut the in-process `sale_10_lines` time from 2.5 s to 0.96 s. A mixed year-fixture probe also recorded two Gunicorn OOM kills, nine manager timeouts, and a purchase receipt posted twice after a lost confirmation.

## Decision

Keep Frappe's transactional `getseries` allocator and the single global invoice series. Shorten the work done while the series lock is held. Do it in this order:

1. **Measure the current branch first.** Split pre-lock wait, lock hold, CPU and response time by basket size. At 80 or 100 sale allocations per minute, the weighted mean hold must stay under 750 or 600 ms just to keep the queue stable.
2. **Reduce post-naming work inside the one atomic voucher transaction.** Move pure preparation ahead of `doc.insert()`. Extend the transaction read cache only for identical, permission-equivalent and visibility-equivalent reads. Never call `frappe.db.commit()` mid-request.
3. **Bound manager memory and prove purchase recovery under mixed load** before raising concurrency.
4. **Tune Gunicorn, the buffer pool and hot-stock locks** one variable at a time. Start with 2 workers × 1 thread, and keep the Item-before-Bin lock order.

## Alternatives rejected

- **Naming after posting:** `Document.insert()` assigns the name before child rows, SLE and GL are written.
- **Per-worker name blocks:** these strand numbers and reorder commits.
- **SEQUENCE or AUTO_INCREMENT side tables:** these run a second allocator beside Frappe's series administration.
- **An in-process voucher queue:** this means multiple queues per process, or a serial bottleneck with only 480 ms of mean service time at 125/min.
- **Per-invoice independent reservation on `tabSeries`:** a preferred experiment *only if* the numbering contract is relaxed. That needs a Myanmar accountant's determination on gaps and out-of-order issuance.

## Consequences

- Rust has no demonstrated role on the write path. PyO3 cannot release an InnoDB lock sooner. Consider it only for a measured, CPU-bound, pure batch step such as export encoding.
- Trial 2 GiB budget (MiB): MariaDB 512 (InnoDB pool 320), Gunicorn master + 2 workers 544, queue worker, job and scheduler 320, Redis 96, nginx 24, websocket 72, OS and headroom 480. Unproven until both full-hour runs pass.
- If the measured hold still exceeds capacity after trimming, record a failed capacity proof and escalate the numbering-contract decision to the owner.

## Verification

- Paired restores of the stock-ERPNext oracle and the candidate.
- Fault injection after naming and before commit: no partial voucher, and the next serial matches the oracle.
- Lost-confirmation replay for all four voucher types, including retries while the first transaction is still in flight.
- Per-voucher exact comparison of rows, GL, SLE and Bins.
- 60-minute open-loop runs at 6,000 and 7,500 scheduled vouchers with zero drops, duplicates, unresolved outcomes, OOM kills or parity differences, and cashier p95 ≤ 1.5 s per basket class.
