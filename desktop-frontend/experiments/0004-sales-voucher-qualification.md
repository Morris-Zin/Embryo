# Sales voucher qualification evidence

This is the reproducible qualification record for the first migrated screen. It is not a mass-migration approval record.

## Environment

- Isolated ERPNext: `http://127.0.0.1:18086`, Docker project `khitmee-perf`, site `bench-loadtest.localhost`.
- The live `khitmee-erpnext` stack and port `18085` are explicitly out of scope.
- Print companion: local dry-run mode on `127.0.0.1:18421`; real hardware printing is still unproven.
- Fixture item: `KM-00001` / `Mekong Fresh Beverage 00001`, barcode `8850000000010`, rate `18.46`, warehouse `Dist Hub 1 - KMD`.
- Customer: `KM-CUST-0001` / `Khit Customer 0001`; price list `Standard Selling`.

## Passed evidence

| Gate | Evidence | Result |
|---|---|---|
| Fixed corpus differential journey | `desktop-frontend/evidence/journeys/sales-voucher-interactions.json` | Passed with zero behavioral differences |
| Validation journey | `desktop-frontend/evidence/journeys/sales-voucher-validation.json` | Passed |
| Live authoritative submission and ERP readback | `desktop-frontend/evidence/journeys/sales-voucher-submission.json` | Passed |
| Permission-denied journey | `desktop-frontend/evidence/journeys/sales-voucher-permission-denied.json` | Passed; both clients preserve the line and deny submission |
| Concurrent idempotent replay | `desktop-frontend/evidence/journeys/sales-voucher-idempotency.json` | Passed; concurrent attempts resolved to one invoice, `ACC-SINV-2026-04934` |
| Item Price deletion cache correctness | `desktop-frontend/evidence/cache/item-price-delete-correctness.json` | Passed without catalog reset |
| Identical interaction benchmark | `desktop-frontend/evidence/benchmarks/sales-voucher-comparison.json` | Measured, with caveats below |
| Native startup/memory scalability | `desktop-frontend/evidence/benchmarks/native-startup-memory.json` | Measured gate passed; required dimensions still pending |

Fresh UI evidence captured after the responsive redesign showed independent live submissions `ACC-SINV-2026-04932` (native) and `ACC-SINV-2026-04933` (browser), with matching customer, qty, rate, amount, warehouse, outstanding amount, and submitted docstatus. The capture initially exposed a native line-item text overlap; that defect was fixed and focused visual verification then passed.

## Benchmark summary

Identical warm interaction workload: text search for `Mekong` and rotating uncached fixture barcode scans. Values are wall-clock UI latency from input action to observable result/line and include rendering/harness polling overhead.

| Implementation | Scan p50 | Scan p95 | Search p50 | Search p95 |
|---|---:|---:|---:|---:|
| Original web server catalog | 30.3 ms | 61.4 ms | 149.7 ms | 261.5 ms |
| PR #8 loopback catalog in browser | 29.1 ms | 30.9 ms | 122.9 ms | 132.9 ms |
| Native Slint integrated catalog | 22.6 ms | 27.9 ms | 30.5 ms | 39.1 ms |

Native release fixture scalability measured separately:

| Fixture rows | Cold startup | Idle working set |
|---:|---:|---:|
| 10,000 | 591.1 ms | 49.3 MiB |
| 100,000 | 848.6 ms | 146.2 MiB |

These results do not yet prove production performance qualification. Required missing measurements are cross-process browser/Tauri memory comparison, frame latency under scrolling, allocation counts, network byte totals, barcode hardware latency, real printer latency, and low-end Windows hardware validation.

## Reproduction commands

Run the Python and Rust checks:

```bash
python -m unittest apps.khitmee_ops.khitmee_ops.tests.test_catalog_unit
cd desktop-frontend && cargo fmt --check && cargo check -p khitmee-desktop
```

Run the live differential interaction corpus with the isolated ERP administrator credential supplied through `ERPNEXT_PASSWORD`:

```bash
node desktop-frontend/tools/differential_journey.mjs \
  --out desktop-frontend/evidence/journeys/sales-voucher-interactions.json
```

Run the permission-denied journey with the test-only isolated user `noperm.counter@example.com`:

```bash
node desktop-frontend/tools/differential_journey.mjs \
  --username noperm.counter@example.com \
  --scenario permission-denied \
  --out desktop-frontend/evidence/journeys/sales-voucher-permission-denied.json \
  --sequence '[{"op":"scan","value":"8850000000010"},{"op":"customer","value":"Khit Customer 0001"},{"op":"f9"},{"op":"wait","ms":1800}]'
```

Run live cache and idempotency proofs:

```bash
python desktop-frontend/tools/cache_correctness_check.py --password "$ADMIN_PASSWORD" \
  --output desktop-frontend/evidence/cache/item-price-delete-correctness.json
python desktop-frontend/tools/idempotency_check.py --password "$ADMIN_PASSWORD" \
  --output desktop-frontend/evidence/journeys/sales-voucher-idempotency.json
```

Comparative interaction benchmark evidence is retained at `desktop-frontend/evidence/benchmarks/sales-voucher-comparison.json`. The PR #8 loopback implementation is intentionally not merged into this branch; reproducing that comparison requires a separate PR #8 benchmark checkout/environment, while the native-versus-original-web measurements remain reproducible with the current native harness.

## Security and cache findings resolved

- The legacy PR #8 loopback catalog service and Tauri shell are intentionally absent from this branch, eliminating the tokenless cross-account loopback attack surface from the native implementation.
- Browser and native counter error surfaces now use ERP's concise `_error_message`, making permission failures understandable and comparable.
- Item Price deletion correctness is handled through `Deleted Document` recovery because modified-delta queries cannot see deleted rows. The live proof shows a temporary price appears in delta sync and deletion returns the item to its baseline price without a reset.

## Journey-generation investigation

`desktop-frontend/experiments/0003-journey-generation-investigation.md` records the GraphWalker/Hypothesis assessment. They are useful sequence generators, not authoritative oracles. The Playwright/Slint differential harness remains the oracle. Automated generated journeys are still future work and must preserve deterministic seeds and fixed coverage for scan, ambiguous search, validation, selection, keyboard, quantity, scroll, submit success/failure, permission failure, network failure, and idempotent replay.

## Remaining blockers before calling the sales voucher fully production-qualified

1. Automated GraphWalker/Hypothesis-generated differential journeys are investigated but not implemented.
2. Full performance qualification is incomplete as listed above.
3. Real barcode scanner hardware and real printer hardware have not been tested; printing is currently dry-run companion evidence.
4. Browser/PR #8/native cross-process memory comparison is not yet implemented.
5. Low-end Windows hardware validation is impossible on the current VM alone.
