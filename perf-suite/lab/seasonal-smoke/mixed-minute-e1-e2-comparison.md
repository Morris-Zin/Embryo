# Mixed-minute baseline comparison: e1-s42 vs e2-s42 (corrected recovery)

Both runs use the same protected year fixture `8cc3e00c…`, seed 42, run id `mixed-year-minute-20261127`, so client voucher series are identical. e1 used harness `year-mixed-minute-v2` (resubmit-on-ambiguous); e2 used `year-mixed-minute-recovery-final` manifest `ed0d01e3674ee5f6` (read-only purchase recovery).

| class | metric | e1 (v2) | e2 (recovery-final) |
|---|---|---|---|
| sale | started | 41 | 23 |
| sale | confirmed | 41 | 9 |
| sale | confirmed_in_first_60s | 20 | 8 |
| sale | rejected | 0 | 2 |
| sale | confirmed_p95_ms | 88621 | 81778 |
| sale_10 | started | 32 | 18 |
| sale_10 | confirmed | 32 | 8 |
| sale_10 | confirmed_in_first_60s | 15 | 7 |
| sale_10 | rejected | 0 | 1 |
| sale_10 | confirmed_p95_ms | 89449 | 81778 |
| sale_50 | started | 9 | 4 |
| sale_50 | confirmed | 9 | 0 |
| sale_50 | confirmed_in_first_60s | 5 | 0 |
| sale_50 | rejected | 0 | 1 |
| sale_50 | confirmed_p95_ms | 57401 | None |
| sale_200 | started | 0 | 1 |
| sale_200 | confirmed | 0 | 1 |
| sale_200 | confirmed_in_first_60s | 0 | 1 |
| sale_200 | rejected | 0 | 0 |
| sale_200 | confirmed_p95_ms | None | 9892 |
| transfer | started | 4 | 3 |
| transfer | confirmed | 4 | 0 |
| transfer | confirmed_in_first_60s | 2 | 0 |
| transfer | rejected | 0 | 0 |
| transfer | confirmed_p95_ms | 90329 | None |
| purchase_receipt | started | 2 | 1 |
| purchase_receipt | confirmed | 1 | 0 |
| purchase_receipt | confirmed_in_first_60s | 0 | 0 |
| purchase_receipt | rejected | 1 | 0 |
| purchase_receipt | confirmed_p95_ms | 36684 | None |
| purchase_invoice | started | 2 | 1 |
| purchase_invoice | confirmed | 1 | 0 |
| purchase_invoice | confirmed_in_first_60s | 0 | 0 |
| purchase_invoice | rejected | 1 | 1 |
| purchase_invoice | confirmed_p95_ms | 2480 | None |

| run-level | e1 | e2 |
|---|---|---|
| confirmed_in_minute | 22 | 8 |
| last_confirmation_s | 92.508 | 103.25 |
| dropped_iterations | 42 | None |
| k6_exit | 0 | None |

## Critical caveats

- **e2 load generator timed out after 370s** (`errors.load_generator`): k6 was killed mid-flight, so `k6_exit`/`dropped_iterations` are null, 15 started vouchers have no terminal event, and several committed documents never received a client confirmation. e2 latency/completion numbers are therefore not a like-for-like performance comparison with e1; the guest was materially more degraded (host WSL itself was intermittently unresponsive during the window).
- e1 created **two** submitted Purchase Receipt rows for one client identity after a withheld confirmation was blindly resubmitted; e2 created **zero** duplicates — the ambiguous receipt (HTTP 502) was not resubmitted, read-only recovery correctly found no committed document, and the dependent invoice correctly refused to cascade.
- e2 OOM evidence comes from the integrated `process_kernel_telemetry` hook (`complete: True`, 2 OOM event(s) with direct thread/process attribution); e1 OOM evidence was reconstructed post-hoc from kernel journals.

## Purchase recovery verdict

```json
{
 "both_proven": false,
 "database_proven": [],
 "purchase_invoice": {
  "proven": false
 },
 "purchase_receipt": {
  "proven": false
 }
}
```

## Duplicate / reconciliation evidence

```json
{
 "committed_financial_failures": 0,
 "purchase_invoice_rows": 0,
 "purchase_receipt_rows": 0,
 "unexplained_count_changes": {}
}
```

## Process/kernel evidence

- complete: `True`
- oom_event_count: `2`
- detailed evidence: `mixed-minute-e2-process-evidence.json.gz`

Both runs are diagnostic smoke evidence only; `full_season_qualified=false`.
