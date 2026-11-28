# Cashier browser benchmark evidence

Benchmark environment: isolated `khitmee-perf` stack at `http://127.0.0.1:18086`, ERPNext v16.60.0/Frappe v16 Docker `2gb` profile, `bench-loadtest.localhost`, smoke dataset (512 items, 512 barcodes, 60 customers, 5,000 invoices, 11,000 invoice lines). Browser: headless Chrome with Playwright; network/API bytes are CDP encoded bytes. Throttling is 4x CPU plus 400 ms/50 KiB network emulation.

Commands:

```bash
perf-suite/scripts/up.sh --profile 2gb --with-khitmee-ops
perf-suite/scripts/seed.sh --scale smoke
cd perf-suite/browser
pnpm install --frozen-lockfile --ignore-scripts
pnpm exec playwright test
node run_benchmarks.mjs --label <label> --profiles normal,throttled --scenarios page-load,search,scan,repeat-scan,inflight-scan,keyboard,lines,memory,print-memory --line-counts 10,50,200 --memory-cycles 40 --repeat 1
python3 analyze_trace.py <trace.json.gz>
bash tools/khitmee-dev.sh browser-perf-check   # reusable regression gate; run from repo root
```

Baseline label: `baseline-before-frontend-opt-v4` (2026-11-27T05:32:36Z). First optimized label: `after-final-frontend-opt-v2` (2026-11-27T05:43:50Z). Scan-path iteration labels: `before-inflight-dedupe-v1` (2026-11-27T06:16:44Z) and `after-scan-dedupe-final-v1` (2026-11-27T06:19:05Z). Raw traces, heap snapshots, and `benchmark.json` files are generated under ignored `perf-suite/results/browser/` directories.

## Measured results

| Scenario/profile | Before | First optimization | Change |
|---|---:|---:|---:|
| Search render, normal | 231.9 ms | 160.8 ms | -30.7% |
| Search render, 4x CPU + slow network | 664.6 ms | 572.4 ms | -13.9% |
| First barcode scan, normal | 60.6 ms | 52.3 ms | -13.7% |
| First barcode scan, throttled | 453.3 ms | 470.8 ms | +3.9% (network-bound noise) |
| Repeat barcode scan, normal | not cached | 37.0 ms, 1 API request for 4 scans | new cache path |
| Repeat barcode scan, throttled | not cached | 28.4 ms, 1 API request for 4 scans | avoids three 400+ ms round trips |
| 200-line DOM mutation records | 23,307 | 1,408 | -94.0% |
| 200-line layout duration | 2,040 ms | 532 ms | -73.9% |
| 200-line quantity increment | 103.8 ms | 71.8 ms | -30.8% |
| 50-line DOM mutation records | 2,082 | 358 | -82.8% |
| 10-line DOM mutation records | 218 | 78 | -64.2% |
| Memory scan/clear DOM mutation records | 1,258 | 454 | -63.9% |
| Memory heap growth after forced GC | 76,940 B | 80,928 B | +5.2% (flat; no invoice-row retention) |
| Heap snapshot class diff, scan/clear | +194 KiB | +91.2 KiB | -53.0% |

Trace evidence before optimization showed 1,129,064 `Document::UpdateStyleAndLayout` events and 2,002 layout passes during the 200-line scan trace. After incremental rendering, mutation count fell by 94%.

## Second iteration: scan-path rendering and duplicate requests

Profiling the first optimized implementation showed that Enter scans still rendered autocomplete rows and immediately discarded them, and that pressing Enter while the debounced autocomplete request was in flight duplicated that same request under slow-network conditions. The second iteration makes scan Enter resolution silent (no transient autocomplete render) and joins identical in-flight searches instead of issuing another request.

| Scenario/profile | First optimization | Current | Additional change |
|---|---:|---:|---:|
| Barcode scan mutation records, normal | 26 | 14 | -46.2% |
| Barcode scan layout duration, normal | 19.9 ms | 8.0 ms | -60.1% |
| Repeat-scan mutation records, normal | 29 | 13 | -55.2% |
| 10-line DOM mutation records | 78 | 34 | -56.4% |
| 200-line DOM mutation records | 1,408 | 608 | -56.8% (-97.4% vs original baseline) |
| 200-line layout duration | 532 ms | 425 ms | -20.1% (-79.2% vs original baseline) |
| 200-line script duration | 151.9 ms | 124.6 ms | -18.0% |
| Enter during in-flight search, throttled | 2 API requests / 1,524 encoded B | 1 API request / 762 encoded B | -50.0% requests and bytes |
| Enter during in-flight search duration, throttled | ~1.95 s | ~1.35 s | about -31% |

The current first-scan network path is already thin: exact-scan responses are compressed, permission-filtered, and only contain public counter fields. Remaining first-time distinct-scan latency is dominated by the network round trip itself (about 410-470 ms in the 400 ms latency profile) rather than response size or autocomplete rendering. A further step would require a coordinated freshness design for local price/stock snapshots; that should not be improvised in the browser because financial correctness and permission filtering matter more than latency.

## Third iteration: sustained scan memory and scanner input cost

Live heap and CDP probing at repeated scan/clear load found three browser-frontend inefficiencies distinct from the already optimized rendering path: the persistent print-fallback dialog accumulated one `close` listener per invocation, repeat line lookup scanned the invoice array linearly, and Chromium retained editor undo/text commands for scanner keystrokes. The print dialog now installs the persistent close listener once and replaces per-invocation button handlers rather than stacking them. Line membership is indexed by item code, keeping repeat scans O(1) while removal/reset maintain the index.

Scanner input is now treated as a burst protocol for the usual uppercase/numeric barcode alphabet (`A-Z`, `0-9`, `-`): keydowns are batched to at most one input mutation per 16 ms burst, then a single `input` event is emitted. This keeps lowercase/manual typing on the browser's normal editing path, preserves visible scan/selection behavior, and prevents Chromium from constructing per-scan editor undo commands. In a one-scan heap isolate, `blink::UndoStep`, `blink::InsertIntoTextNodeCommand`, retained `Text`, and `blink::AppendNodeCommand` growth all went to zero; the 400-scan final heap diff also contains none of those classes.

Same-day isolated-stack comparison (normal profile):

| measurement | before | current | change |
|---|---:|---:|---:|
| Print fallback repeated 20 times | +20 DOM event listeners | 0 listener growth, 0 node growth | bounded |
| Scan main-thread script+layout | 11.32 ms | 9.53 ms | -15.8% |
| Repeat-scan main-thread script+layout | 10.86 ms | 6.45 ms | -40.6% |
| 200-line scan script+layout | 516.60 ms | 362.29 ms | -29.9% |
| 200-line scan median | 59.25 ms | 56.35 ms | -4.9% |
| Repeat-scan median | 41.0 ms | 31.2 ms | -23.9% |
| Scan/clear heap growth, forced GC, 60 scans | 75,756 B | 56,536 B | -25.4% |
| Sustained scan/clear DOM nodes, 400 scans | +203 nodes over prior 200-scan probe | +3 nodes | bounded |
| Sustained scan/clear JS heap, 400 scans | — | 2.92 MB end; last 200 scans +6,464 B | flat after bounded scan-cache warm-up |

The remaining sustained heap growth is dominated by bounded/intentional or browser-owned objects: the scan cache is capped at 100 entries, resource-timing/performance entries are browser-capped, and compiled-code/network internals fluctuate independently. The final 400-scan heap diff contains 100 cached item objects (the configured cap), not unbounded row or editor objects.

Profiler-guided experiments not retained: cloning a static row template did not improve script/layout time; `content-visibility` on table rows or cells did not reduce layout because internal table boxes still participate in table layout; CSS-grid row emulation only reduced 100-row layout by about 6% and was not worth table-semantics risk; replacing the search input flattened node counts but did not remove editor undo retention.

Regression evidence: `bash tools/khitmee-dev.sh browser-perf-check` passed 10/10 Playwright assertions, and `bash tools/khitmee-dev.sh fast-check` passed Python, JavaScript, Bash, Compose, and knip gates.
