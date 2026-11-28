# One-minute cashier sales experiment

This diagnostic offers the sales portion of the seasonal workload for one minute through the installed `khitmee_ops.api.vouchers.submit_sale` endpoint. External k6 runs three independently scheduled groups with the same basket generator as the seasonal scenario.

| Group | Offered per minute | Lines per voucher | Maximum concurrent users |
| --- | ---: | ---: | ---: |
| Small | 60 | 10 | 10 |
| Medium | 16 | 50 | 5 |
| Distribution hubs | 4 | 200 | 3 |

The harness uses an authenticated bench session, a run-specific idempotency key for each sale, server-side pricing and 50 ms simulated network delay. Requests may finish during a 130-second drain. The receipt distinguishes confirmations within the arrival minute from confirmations during the drain. The auditor resolves the actual idempotency rows and compares each submitted invoice and its stock ledger to the requested stock quantities by SKU and warehouse. It also checks per-invoice GL debit/credit balance and the change in invoice and idempotency row counts.

## Measured on 2026-11-27

**All four 200-line sales posted, but their p95 confirmation time was 8.841 seconds.** That exceeds the 1.5-second cashier budget. Each distribution hub handled two sales with 200 distinct SKUs. For four observations, the nearest-rank p95 equals the slowest observation.

| Basket | Scheduled per minute | Actual starts | Confirmed within 60 s | Final committed | Confirmation p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10 lines | 60 | 61 | 60 | 61 | 7.025 s |
| 50 lines | 16 | 17 | 16 | 17 | 7.511 s |
| 200 lines | 4 | 4 | 4 | 4 | 8.841 s |

k6 started one extra 10-line sale and one extra 50-line sale at 60.001 seconds. The first 80 sales started and confirmed within the arrival minute. All 82 had confirmed by 61.632 seconds. k6 dropped zero iterations. The virtual users shared the fixture's Administrator session. This exercises the real submission and posting paths with Administrator permissions.

The independent read-only audit found 82 submitted sales invoices, 82 idempotency records, 2,260 new invoice items, 2,260 stock ledger entries and 492 GL entries. Every voucher's SKU, quantity and warehouse matched its submitted basket and corresponding stock ledger movements. Each voucher balanced its debits and credits. All confirmations matched persisted invoices one-to-one. The database, backend and frontend container identities and restart counts remained stable.

The receipt includes the versioned k6 source, request baskets, per-arrival timestamps, confirmations, fixture and image identities, before/after database counts and per-voucher ledger observations. SHA-256 is `0b9a061fb175dbbf18f1c5579c259e87608503d00e942e2f86ca40a2fd2c2114`. [Measured cashier receipt](cashier-sales-2026-11-27.json.gz)

Client HTTP request p95 was 6.973 seconds for small, 7.460 seconds for medium and 8.789 seconds for large sales. These timings include HTTP send, wait and receive. The receipt lacks the HTTP waiting subphase, server-only latency, CPU, peak memory, SQL timing, block I/O and lock-wait measurements for the busy minute. The observations identify a slow submission path. Attribution requires another instrumented run.

The public profile contains aggregate timings, sample counts, coverage and an evidence-linked hypothesis without credentials, session data, individual requests or voucher identifiers. Supply it to the supervisor with `loop --profile perf-suite/lab/seasonal-smoke/ranked-profile.json` for proposal input. The next measurement should cover the sale path if that is the proposed optimization target. [Redacted ranked profile](ranked-profile.json)

```bash
python3 -B perf-suite/lab/seasonal-smoke/cashier_profile.py \
  perf-suite/lab/seasonal-smoke/cashier-sales-2026-11-27.json.gz
```

## Instrumented repeat on a fresh restore

**Invoice naming-series contention is the first bottleneck to investigate.** The locking read of `tabSeries.current` ran 82 times, examined 82 rows, and accumulated 97.316 seconds, or 87.5% of the 111.179 seconds attributed to 104,683 site SQL statements. Concurrent database sessions accumulate overlapping durations. InnoDB recorded 67 row-lock waits totaling 97.268 seconds and zero deadlocks. These independent observations point at the naming-series lock. The counters do not identify the blocking transaction for each waiter.

| Measured signal | Observation |
| --- | ---: |
| Sampled guest window, including login, arrivals and drain | 66.930 s |
| Guest CPU / two-vCPU capacity in window | 65.34 / 133.86 core-seconds |
| Backend / database CPU | 44.71 / 15.75 core-seconds |
| Guest used-memory peak, 135 samples | 1,355,345,920 bytes |
| Database / backend sampled memory peaks | 436,678,656 / 299,593,728 bytes |
| Guest virtual-disk reads / writes | 13,705,216 / 33,939,968 bytes |
| Container CPU throttling / OOM kills | 0 seconds / 0 kills |

Sale confirmation p95 reached **6.081, 6.821 and 7.806 seconds** for 10-, 50- and 200-line baskets. Their HTTP waiting p95s were 6.029, 6.770 and 7.754 seconds. All 80 scheduled sales confirmed within the minute. An additional medium and large sale started at 60.001 seconds and confirmed during drain. Across both runs, 81 common request IDs had identical warehouses, SKUs and quantities. The extra large boundary sale replaced the first run's extra small sale. The repeat persisted 82 invoices, 2,450 matching invoice and stock ledger lines, 492 balanced GL entries and 82 idempotency records. Every confirmation matched a posting.

SQL and cgroup snapshots bracketed the load through a readiness and stop handshake. Between boundaries, a guest process sampled memory every 500 ms. Performance Schema was enabled in both restored clones. The repeat also incurred the boundary queries and sampling overhead. The two runs provide descriptive observations with different final baskets. The next paired experiment should trace the interval from naming-series acquisition to commit for each basket size, then test whether moving independent work before that lock shortens the hold without changing transaction or posting semantics.

The read-only sampler reported complete coverage with no counter resets or identity changes. The protected operator receipt was copied with SHA-256 `895165d6a2e771597363ccd5241a71414edb2bdd51e069755e107992890a2b26`. Supply the aggregate profile to the proposer using `--profile perf-suite/lab/seasonal-smoke/ranked-instrumented-profile.json`. [Instrumented profile](ranked-instrumented-profile.json) · [Measured receipt](cashier-sales-instrumented-2026-11-27.json.gz)

## Reproduce on a fresh disposable arm

If all sales use the same counter row, its exclusive lock can occupy at most 60 seconds in a minute. At 80 sales/minute the sustainable mean hold time must therefore be at most 0.75 seconds per sale. At the margin of 100 sales/minute it must be at most 0.60 seconds. These ceilings assume a single acquisition per sale and no other users of that row. The digest aggregates keys and measures elapsed query time. Identifying the key and tracing acquisition through commit are necessary before applying this bound to the installation.

Use a fresh adapter-provisioned, deployed writable arm on the isolated guest. Run in the KhitmeeERPNext WSL distribution with the guest operator's pinned SSH identity. The external k6 binary is taken from the locally installed Grafana k6 image and checked by SHA-256 before execution.

```bash
python3 -B perf-suite/lab/seasonal-smoke/run.py "$NAMESPACE" \
  --output /var/lib/khitmee-experiment-guest/seasonal-cashier/run.json.gz
```

For the measured basket sequence with telemetry, add `--telemetry --run-id 4756cc5002a4` to the command on another fresh restore.

This experiment measures sales submissions on the synthetic smoke fixture. The full seasonal requirement includes 100 vouchers per minute across four voucher types for one hour plus searches, concurrent manager work, retry, margin and recovery checks.

## Paired supervisor workload

`cashier-sales-minute-v1` runs the same three sale classes on each adapter-restored arm. The harness uses the operator's SSH config and a pinned external k6 binary. Install `cashier.json` alongside `supervisor_arm.py` with a `k6` object containing the binary's absolute `path` and its `sha256`. The driver obtains the synthetic Administrator credential from the private namespace and keeps it out of receipts.

Each pair uses its supervisor seed for both the request IDs and generated baskets. The driver submits exactly 60 small, 16 medium and 4 large sales. A k6 iteration at the minute boundary beyond a group's cap records a no-op. The verifier enumerates the 80 expected IDs and checks scheduler counts, emitted requests, confirmations and committed postings. It deliberately discards the first small sale's confirmation and retries the same idempotency key. The measured duration includes the retry, which must return the original invoice.

Per-arm evidence includes p95 for every basket class and all sales combined, five resource metrics, the measured profile, ledger rows, a comparison against fixture prices, and four verifier corruption tests. The supervisor compares the financial signature across each sales-subgroup pair. The v3 signature included each requested basket and selling totals, whole-run stock quantity/value by warehouse and item, and whole-run debits/credits by account and party. The next harness revision also compares these stock and GL allocations within each client voucher. The regression test exchanges balanced posting costs across two invoices and verifies that both per-voucher signatures change while the whole-run totals stay equal.

## Mixed minute on the year fixture

The protected 2 GiB guest confirmed **22 vouchers within 60 seconds** on November 27, 2026. The generator scheduled 100 vouchers, emitted 49 voucher starts and received 47 final confirmations by 92.508 seconds. k6 dropped 42 iterations across the voucher, search and manager scenarios. Thirty-three regular voucher worker iterations failed at login or CSRF setup. Each latency below includes retries for the confirmed sample. [Measured summary and resource counters](mixed-minute-year-summary.json)

| Voucher | Scheduled | Started | Confirmed by 60 s | Confirmed after drain | P95 including retries |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10-line sale | 60 | 32 | 15 | 32 | 89.449 s |
| 50-line sale | 16 | 9 | 5 | 9 | 57.401 s |
| 200-line sale | 4 | 0 | 0 | 0 | — |
| Transfer | 10 | 4 | 2 | 4 | 90.329 s |
| Purchase receipt | 5 | 2 | 0 | 1 | 36.684 s |
| Linked purchase invoice | 5 | 2 | 0 | 1 | 2.480 s |

MariaDB contained 49 new financial documents for 48 distinct client IDs. All 47 confirmed baskets matched their stored items and the audited ledger and purchase linkage checks. Withholding one purchase receipt confirmation led to two submitted receipts for the same client ID. Each receipt added 226 units across 50 stock rows and posted USD 3,355.92 in balanced GL entries. The dependent purchase invoice failed before submission. The transfer retry returned its original document. [Portable external events and MariaDB rows](mixed-minute-year-2026-11-27.json.gz)

Of 80 scheduled searches, 22 succeeded with p95 of 7.702 s and 12 emitted rejection events. The other 46 did not reach a search result event. All nine manager report requests timed out without an HTTP response. The three Warehouse and five Monthly requests timed out near 10 s. The detail request's first page timed out near 30 s. Other manager iterations hit HTTP 0 or 502 at login. The detail fallback also timed out during export enqueue.

The 113.541 s resource window included setup, 60 s of arrivals and drain. The guest used 117.24 CPU core-seconds, reached 1,877,962,752 bytes of memory (1.749 GiB), recorded 4,545,022,464 bytes of block I/O, accumulated 256.686 SQL seconds and 24.081 s of row-lock waits. The backend cgroup recorded two OOM kills and every other service recorded zero. The provisioner released all three temporary volumes and restored the original eight-service reference stack to a successful HTTP ping. The receipt retains the fixture, harness, raw artifact and cleanup identities. This was a single mixed-minute diagnostic. [Measured summary](mixed-minute-year-summary.json)

The kernel attribution review established both victims exactly as backend gunicorn processes: PID 11408 at 07:13:56.505394 UTC with 325,341,184 bytes RSS and PID 11409 at 07:14:29.716928 UTC with 469,823,488 bytes RSS. Both allocating tasks were `ThreadPoolExecu` kernel task IDs 12646 and 12648, but the prior evidence does not map those thread IDs to their owning worker process or any HTTP route. [Redacted verified OOM attribution](mixed-minute-oom-attribution-2026-11-27.json) · [Rerun collector contract](../driver/README.md#dev-only-processkernel-oom-attribution)
