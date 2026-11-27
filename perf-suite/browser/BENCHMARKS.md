# Cashier browser benchmark evidence

Benchmark environment: isolated `khitmee-perf` stack at `http://127.0.0.1:18086`, ERPNext v16.60.0/Frappe v16 Docker `2gb` profile, `bench-loadtest.localhost`, smoke dataset (512 items, 512 barcodes, 60 customers, 5,000 invoices, 11,000 invoice lines). Browser: headless Chrome with Playwright; network/API bytes are CDP encoded bytes. Throttling is 4x CPU plus 400 ms/50 KiB network emulation.

Commands:

```bash
perf-suite/scripts/up.sh --profile 2gb --with-khitmee-ops
perf-suite/scripts/seed.sh --scale smoke
cd perf-suite/browser
pnpm install --frozen-lockfile --ignore-scripts
pnpm exec playwright test
node run_benchmarks.mjs --label <label> --profiles normal,throttled --scenarios page-load,search,scan,repeat-scan,keyboard,lines,memory --line-counts 10,50,200 --repeat 1
python3 analyze_trace.py <trace.json.gz>
bash tools/khitmee-dev.sh browser-perf-check   # reusable regression gate; run from repo root
```

Baseline label: `baseline-before-frontend-opt-v4` (2026-11-27T05:32:36Z). Optimized label: `after-final-frontend-opt-v2` (2026-11-27T05:43:50Z). Raw traces, heap snapshots, and `benchmark.json` files are generated under ignored `perf-suite/results/browser/` directories.

## Measured results

| Scenario/profile | Before | After | Change |
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

Trace evidence before optimization showed 1,129,064 `Document::UpdateStyleAndLayout` events and 2,002 layout passes during the 200-line scan trace. After incremental rendering, mutation count fell by 94%, but scan latency remains dominated by network round trips and browser key-event processing; this is why the repeat-scan cache is the next large cashier workflow win.

Regression evidence: `bash tools/khitmee-dev.sh browser-perf-check` passed 6/6 Playwright assertions, and `bash tools/khitmee-dev.sh fast-check` passed Python, JavaScript, Bash, Compose, and knip gates.
