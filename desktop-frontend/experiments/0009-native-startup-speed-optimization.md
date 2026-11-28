# 0009 Native startup speed optimization

> Evidence artifact note: raw benchmark JSON belongs in gitignored local evidence. This file is the committed concise experiment record; rerun the commands below to regenerate exact machine-local numbers.

## Scope

Native startup was measured from process spawn to Slint MCP readiness with 100,000 catalog rows and 10,000 invoice rows. The SQLCipher fixture store was warmed before measurement so the comparison measures loading/decoding/indexing/UI startup rather than one-time fixture generation. Baseline and candidate executables were alternated for five repeats each on the same Linux host.

## Result

| binary | p50 startup | mean startup | p50 idle RSS |
| --- | ---: | ---: | ---: |
| baseline `c7b1e0c` | 1225.413 ms | 1201.920 ms | 301,981,696 B |
| optimized `211e7c7` | 648.201 ms | 656.503 ms | 272,044,032 B |

Measured p50 startup improvement: **47.10%**. Measured p50 idle memory reduction: **9.91%**. A smaller 10,000-catalog-row control improved p50 startup by **19.57%** (269.009 ms to 216.366 ms), so the 100,000-row workload is the dominant measured hot path and the claimed >30% win.

## Implementation retained

- Decode encrypted catalog JSON payloads in parallel while preserving row order.
- Build normalized catalog index rows in parallel and sort them with Rayon's stable parallel sort.
- Replace the duplicated `by_code` catalog clone map with logarithmic lookup in the already item-code-sorted `CatalogIndex`.
- Materialize only the currently visible invoice page (`invoice_limit`) instead of all 10,000 fixture invoice rows; Load More still expands the same deterministic prefix.

## Hypotheses rejected or not retained

- Fixture invoice generation was not the dominant startup cost; row materialization was the expensive part worth changing.
- Lazy/asynchronous catalog loading was rejected without retention because it changes first-search readiness and error lifecycles rather than proving identical behavior.
- Removing normalized search text or changing exact-barcode/search ordering was rejected because it risks ERPNext-visible behavior for marginal additional startup gain.
- Returning to Tauri/Electron/WebView remained a contract non-goal, not a performance hypothesis.

## Correctness coverage added

- A property test compares `CatalogIndex::search` against a naive reference oracle across generated ASCII/Burmese terms and limits.
- Exact item-code lookup is regression-tested, including case-sensitive misses.
- Invoice filtering tests prove the page cap is applied before row materialization and preserves first-match ordering for Load More.
- The existing Playwright-vs-Slint differential harness remains the authoritative ERPNext behavioral oracle.


## Live ERPNext differential regression

The isolated `khitmee-perf` ERPNext stack was rebuilt, seeded with the smoke dataset, and used to regenerate live Playwright-vs-Slint parity evidence after the optimization. All of these passed with zero mismatches:

- `evidence/journeys/sales-voucher-speed-regression-final.json`
- `evidence/journeys/item-search-speed-keyboard.json`
- `evidence/journeys/item-search-speed-repeat-scan.json`
- `evidence/journeys/invoice-list-speed-initial.json`
- `evidence/journeys/invoice-list-speed-load-more.json`
- `evidence/journeys/invoice-list-speed-filter.json`
- `evidence/journeys/invoice-list-speed-sort-asc.json`

That oracle caught one pre-existing parity defect on the fresh seed: ERPNext's invoice list defaults to `Created On` (`creation`) ordering while native had hard-coded invoice-name ordering. Native now uses the same authoritative reportview `creation` ordering; the invoice-list evidence above is post-fix.

## Reproduction

Build the two executables, copying them aside before rebuilding the other revision:

```bash
# baseline
git worktree add --detach /tmp/embryo-baseline-main c7b1e0c592b7a4f97e1c6463de48e7cf839871b2
cd /tmp/embryo-baseline-main/desktop-frontend
cargo build -p khitmee-desktop --locked
cp target/debug/khitmee-desktop /tmp/khitmee-desktop-base

# candidate
cd /path/to/worktree/desktop-frontend
git checkout 211e7c72eccfd4a6be32fc1dcc8836cb37555c49
cargo build -p khitmee-desktop --locked
cp target/debug/khitmee-desktop /tmp/khitmee-desktop-opt
```

Run the committed comparison harness:

```bash
KHITMEE_NATIVE_DATA_DIR=/tmp/khitmee-native-baseline \
python desktop-frontend/tools/benchmark_startup_compare.py \
  --base-executable /tmp/khitmee-desktop-base \
  --candidate-executable /tmp/khitmee-desktop-opt \
  --output desktop-frontend/evidence/startup-comparison.json \
  --rows 100000 --invoice-rows 10000 --repeats 5 --min-improvement-pct 30
```

## Remaining honest gaps

- This is Linux/host evidence, not low-end Windows hardware validation.
- Frame latency, p95 input latency, allocation rates, network traffic, barcode-device latency, and printer latency remain separate explicit performance gates in `benchmark_native.py`.
- The committed benchmark harness compares startup and idle memory only; interaction latency remains a separate gate.
