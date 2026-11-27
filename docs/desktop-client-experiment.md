# Khitmee resource-efficient desktop client experiment

Status: proof-of-concept branch. Measurements are being collected in `perf-suite/results/desktop-experiment/`; this document distinguishes actual measurements from simulation and clearly marks unmeasured hardware.

## Objective and evaluation question

Test whether moving catalog indexing, barcode lookup, autocomplete, and limited display computations onto existing shop computers materially reduces central ERPNext CPU/RAM/network use and cashier latency, without weakening ERPNext's authoritative role. The target context is six shops, 10-20 concurrent users, and 5,000+ daily invoices on a 2-core/2 GiB server.

The experiment does **not** assume a desktop shell is inherently superior. It includes a browser-local IndexedDB control because browser-side processing may capture most savings without installer/update complexity.

**Current recommendation:** the evidence favors the browser-local IndexedDB control for catalog search/offload. It eliminated measured search API traffic with the least new complexity and without adding browser-engine memory. The Tauri/Rust shell is a workable proof of concept, but this experiment found no client-latency or client-memory advantage large enough to justify deployment by itself. Rust/native components remain justified only if a required native integration (for example printing or hardware control) cannot be safely handled by the existing narrower print-companion/browser path. Central-server CPU savings remain plausible but unproven at seasonal scale; do not deploy based on this smoke benchmark alone.

## Prior art reviewed before building

| Project/evidence | Relevant finding | Consequence for Khitmee |
|---|---|---|
| `kodlyft/xpos` | ERPNext POS with IndexedDB/service-worker browser cache. Its v3 documentation says the unfinished Electron app was removed and v3 is browser/PWA-only, while retaining the IndexedDB cache and sale queue. | Strong evidence that browser-local processing is a credible lower-complexity alternative; desktop must prove incremental value. |
| `defendicon/POS-Awesome-V15` | ERPNext POS with an Electron shell that stores a server URL and wraps the web UI. | Validates thin-shell wrapping, but Electron memory/update cost conflicts with the lightweight target. |
| `tw93/Pake` and Tauri loopback-wrapper patterns | Tauri can point a native window at an external/loopback web app with little frontend rebuild work. | Reuse the existing Khitmee interface rather than reimplementing it. |
| `YaoApp/cui-desktop` | Uses a Rust local proxy/cookie jar to make a remote web app same-origin. | Considered but rejected for this PoC: Khitmee only needs a small loopback catalog API, not a full proxy or credential-bearing cookie store. |
| Tauri capabilities/CSP docs | Capabilities can be empty and remote URLs should not receive local system access unless explicitly justified. | The shell exposes no Tauri permissions to ERP web content. Local behavior is isolated in the loopback service. |
| SQLCipher/rusqlite docs | `bundled-sqlcipher` transparently encrypts database pages; key management is the application's responsibility. | Use SQLCipher for at-rest storage, with explicit limitations. |
| Microsoft DPAPI docs | User-scope DPAPI normally decrypts only for the same user on the same machine. | Protect the random SQLCipher key on Windows; do not embed or sync key material. |

## Architecture

```text
ERPNext / khitmee_ops
  ├─ existing search_items API        (unchanged fallback)
  ├─ sync_catalog API                 (permission-checked reset/delta snapshot)
  └─ submit_sale / submit_transfer    (unchanged authoritative validation)

Browser or Tauri WebView running existing /khitmee_sales
  └─ catalog_bridge.js (opt-in)
       ├─ browser mode: IndexedDB + JavaScript ranker (control)
       └─ rust mode: loopback sync/search API

Rust catalog service (127.0.0.1:18422)
  ├─ SQLCipher catalog_rows/sync_states
  ├─ device-generated key, DPAPI-protected on Windows
  └─ HashMap exact barcode + normalized prefix/substring index

Tauri shell
  └─ external WebView URL only; empty native capability set
```

The full reverse-proxy design was not built. It would have increased cookie/session attack surface and implementation complexity just to make remote pages same-origin. The chosen bridge works from the ERP origin using CORS and never handles ERP session cookies.

## Security threat model

### Assets

- Item names/codes/groups/brands/barcodes.
- Selling-price and stock display snapshots.
- ERP session cookies and user credentials (never stored by the desktop service).
- SQLCipher key material.
- Financial/inventory correctness and ERP permission enforcement.

### Threats and controls

| Threat | Control | Residual risk |
|---|---|---|
| Powered-off laptop/backup theft exposes catalog DB | SQLCipher at-rest encryption; random per-device key; DPAPI user-scope wrapping on Windows | DPAPI is not a hardware guarantee. Same-user malware or credential compromise can decrypt. Linux dev key file is only `0600`. |
| Shared key extracted from binary | No shared key exists; keys are generated per device | None for embedded-key threat; local reverse engineering remains possible. |
| ERP session theft | Service never stores/proxies ERP cookies or passwords; browser/Tauri uses normal session storage | Compromised ERP origin or malicious browser extension can still misuse the session. |
| Malicious web content calls loopback service | Loopback-only bind, exact-origin CORS, JSON content-type enforcement, size limits, optional token | Browser CORS is not a complete local-machine security boundary. Local malware can call the service directly. |
| Tauri remote content escalates to native APIs | Empty capability set; no custom Rust commands exposed to web content | OS/WebView vulnerabilities are outside this design. |
| Catalog scraping by authorized cashier | Minimize synchronized fields; server-side permissions; no cost/supplier/credential fields | An authorized cashier can still enumerate the data they are allowed to see. Encryption does not prevent this realistic extraction risk. |
| Permission revocation | Permission revision participates in sync state; permission-aware deployments receive full replacement resets instead of tombstones, avoiding hidden item-code disclosure. Submit/search server APIs enforce permissions | Permission-aware users trade incremental traffic for confidentiality and correctness. Role-rule changes not represented in the simple revision may still require operational testing; financial writes always remain server-side. |
| Sync endpoint abuse / forced resets | 20,000-item bound, permission filtering, overlap deltas, incomplete-catalog fallback | An authenticated user can intentionally omit state and repeatedly request resets. Production rollout should rate-limit/monitor this endpoint just like other search APIs. |
| Stale/deleted item used in sale | Delete/rename tombstones plus daily validity-boundary resets, reset fallback, stale sync failure falls back to server; ERPNext submit validates existence/permission/inventory/price | UI can briefly show stale display data. This is deliberate and labeled; no offline checkout is enabled. |
| Malicious client alters rate/qty | Server ignores client rates by default and resolves authoritative ERPNext pricing; ERPNext validates inventory/financial logic | Users with legitimate price-override permission can override as before; this is not a new desktop privilege. |

### AI-assisted reverse-engineering assumption

Assume a malicious user can inspect the binary, JavaScript, network protocol, SQLCipher schema, and memory of a running process. Therefore the design does not rely on obfuscation. The only meaningful local-control boundary is minimization plus OS/account isolation. Client-side encryption is an at-rest control, not a DRM/control-the-user mechanism.

## Correctness constraints preserved

- ERPNext remains the only financial/inventory authority.
- No upstream ERPNext/Frappe core files are modified.
- Offline checkout is not implemented.
- Submission payloads are validated by the existing server APIs.
- Search falls back to the existing permission-checked server endpoint when sync is unavailable or stale.
- Local catalog rows are disposable cache, never accounting facts.

## Benchmark protocol

Configurations:

1. `off` — existing optimized browser application.
2. `browser` — browser-local IndexedDB processing control.
3. `rust` — Rust encrypted local catalog accessed from the same web interface.
4. `tauri-basic` and `tauri-rust` — shell measurements. These require actual Windows cashier hardware; this Linux VM cannot produce representative WebView2/installer measurements.

Reproducible isolated-stack commands:

```bash
perf-suite/scripts/up.sh --profile 2gb --with-khitmee-ops
perf-suite/scripts/seed.sh --scale smoke
cd desktop-client && cargo build --release && cd ..
bash perf-suite/desktop/run_experiment.sh --modes off,browser,rust --profiles normal,throttled
python3 perf-suite/desktop/summarize_experiment.py perf-suite/results/desktop-experiment/<run>
```

The browser harness measures interaction latency, mutation/layout work, API request count, and encoded network bytes. The collector samples isolated Docker cgroup memory/CPU once per second. `desktop-client ... benchmark` measures synthetic Rust indexing locally and marks itself `simulated: true` because it is not a hardware/ERP workload.

## Experiment log

- **Reverse proxy/cookie-jar shell: rejected before implementation.** Prior art shows it is feasible, but it stores or forwards authentication state and duplicates ERP routing for little benefit. The loopback catalog API is narrower.
- **XPOS-style desktop parity: not copied.** XPOS itself removed its unfinished Electron path in v3 while retaining browser IndexedDB caching. This is treated as evidence against assuming desktop superiority, not as a reason to skip measurement.
- **Customer-specific local pricing: deferred.** Including customer-specific rows would enlarge scope-sensitive local data and force resyncs. Display-price mismatch is possible; server submission remains correct.
- **Full local masters/offline checkout: rejected.** Out of scope and financially unsafe for this experiment.
- **Indexes on every `modified` column: deferred.** They could reduce sync scans but add write amplification to high-volume stock rows. Measure first.
- **Linux SQLCipher key protection: development-only.** Raw `0600` key files are not the intended shop protection; Windows DPAPI is.

## Measurements and evidence

### Actual isolated Linux VM measurements

Environment: `khitmee-perf` Docker stack, 2 GiB profile, smoke dataset (512 sellable items plus bundles), headless Chromium benchmark, two repetitions in normal and 4x-CPU/slow-3G profiles. These are real measurements on this Linux VM, but they are **not** low-end Windows cashier measurements. Server memory uses cgroup current bytes because container process smaps were not readable without elevated privileges.

Interactive workload result (`poc-final`; search/scan/repeat-scan/keyboard):

| mode | scan p95 ms | search p95 ms | measured scenario API requests | measured scenario API KiB | initial sync KiB | backend memory peak MiB | backend CPU avg % | fleet memory peak MiB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| optimized browser (`off`) | 478.7 | 591.6 | 36 | 31.03 | — | 116.8 | 1.74 | 706.43 |
| browser IndexedDB (`browser`) | 67.1 | 156.9 | 0 | 0 | 28.91 | 118.78 | 1.79 | 710.92 |
| Rust SQLCipher loopback (`rust`) | 468.3 | 667.9 | 0 | 0 | 28.91 | 119.45 | 1.08 | 716.5 |

Interpreting those rows requires the profile split:

| mode | normal scan median/p95 ms | throttled scan median/p95 ms |
|---|---:|---:|
| optimized browser | 58.7 / 62.4 | 459.5 / 478.7 |
| browser IndexedDB | 58.0 / 67.1 | 55.0 / 60.1 |
| Rust loopback | 58.0 / 63.2 | 461.6 / 468.3 |

Findings:

- On an unthrottled local network, all three modes have essentially the same scan latency because DOM update and input handling dominate.
- Under simulated slow networking, browser-local processing remains fast while server search becomes slow. This is a real benefit of eliminating search network round trips.
- The Rust loopback throttled row is **not valid evidence of real loopback latency**: Chromium/CDP network throttling also throttles `127.0.0.1:18422`, so the benchmark cannot distinguish WAN latency from loopback latency. Normal-network parity and the synthetic index microbenchmark are the usable Rust evidence here.
- Both local modes eliminated all measured scenario `search_items` API calls after synchronization. In this harness the optimized web mode emitted 36 API requests / 31.03 KiB across the measured scenarios.
- Initial synchronization was 28.91 KiB because the benchmark first prepares the default warehouse and then the selected Shop 1 scope (two compressed 512-row resets). The selected-scope sync completed in about 43-45 ms browser-side. Incremental sync traffic was validated but not benchmarked as a separate long-running workload.
- Backend CPU differences are small and not statistically meaningful in this short smoke run; queue/scheduler noise can exceed the search savings. RAM correctly shows little difference because removing search requests primarily reduces CPU/network work, not persistent backend memory.
- Do not extrapolate these CPU values to 5,000 invoices/day. A longer month/year workload is required before making that claim.

Browser heap/page-load measurement (`poc-memory`, normal profile, 60 scanned items in cycles):

| mode | page DOMContentLoaded ms | initial JS heap MiB | heap growth after cycles KiB | heap growth ratio |
|---|---:|---:|---:|---:|
| optimized browser | 51.7 | 1.58 | 319.7 | 1.198 |
| browser IndexedDB | 24.8 | 1.60 | 362.6 | 1.222 |
| Rust loopback | 29.5 | 1.58 | 341.8 | 1.211 |

These page-load timings are local-cache reload timings, not application installer startup timings. The browser IndexedDB row uses the final implementation, which avoids keeping a duplicate in-memory catalog when IndexedDB persistence succeeds. Heap growth is dominated by invoice-row DOM/probe work and remains similar across modes.

### Synthetic client-index microbenchmark

Measured on the same Linux VM with synthetic rows and marked simulated in the artifact. It is useful only for client indexing cost, not ERP workload proof:

| rows | iterations | index build ms | exact barcode p50/p95 ms | generic search p50/p95 ms | process RSS MiB | release binary bytes |
|---:|---:|---:|---:|---:|---:|---:|
| 20,000 | 2,000 | 18.63 | 0.000228 / 0.000404 | 0.010907 / 0.012478 | 27.06 | 1,946,104 |

The exact-barcode fast path intentionally returns the unique barcode row immediately. Generic text autocomplete still scans normalized rows using `memchr`, which is acceptable for a 20k-row proof of concept but should be re-evaluated for larger catalogs.

### Tauri shell measurements and gaps

- Linux `cargo check` and release build pass for the Tauri shell.
- The Linux release binary is 4,549,368 bytes before installer packaging. This is a binary-size measurement, **not** a Windows installer-size measurement.
- Single-run exploratory Linux WebKitGTK measurements: basic shell window appeared in 123 ms with 287.87 MiB process-tree PSS and 1.01 CPU seconds after two idle seconds; rust shell window appeared in 187 ms with 307.43 MiB PSS and 1.62 CPU seconds. Headless Chromium showing the same counter used 273.31 MiB PSS across nine processes. These are single-run Linux development measurements, not WebView2 or low-end Windows results; they do show that a native shell does not remove browser-engine memory cost.
- No valid Windows WebView2 startup/RAM/CPU measurement exists yet.
- No valid basic-Tauri-wrapper versus browser startup comparison exists yet.
- Windows cross-checking from this Linux VM is blocked because the bundled SQLCipher C build requires Microsoft `lib.exe`/MSVC for `x86_64-pc-windows-msvc`. The Windows DPAPI crate API was verified against its source, but the Windows binary itself remains unbuilt.
- A low-end Windows cashier computer remains required for the mandated hardware comparison.

## Correctness validation performed

- Live reset sync returned 512 rows with only public counter fields; a follow-up delta returned a small overlapping update set rather than another reset.
- A live sale submitted with a manipulated client rate of `0.01` was posted at the authoritative server rate `18.46`; the invoice response used `18.46`, proving client rates are ignored without override permission.
- Unauthenticated sale submission returned HTTP 403.
- Submission of a nonexistent item returned HTTP 417 and no voucher.
- Browser regression coverage verifies that synchronized local scans do not call `search_items`, and that an unknown barcode falls back to authoritative server search.
- Rust unit tests cover ranking/deletion, SQLCipher wrong-key rejection, encrypted round trips, scope validation, and constant-time token comparison.
- Python unit tests cover sync state parsing, overlap deltas, reset causes, tombstones, permission-aware resets, date-boundary resets, and public-field filtering.

## Remaining limitations

- SQLCipher protects powered-off files only. It does not protect the unlocked in-memory index or prevent an authorized cashier/local malware from extracting the visible catalog.
- Browser-mode IndexedDB is unencrypted by design; it relies on browser profile and OS account protection.
- Customer-specific display pricing is outside the default local scope. Server-side pricing remains authoritative.
- Hard deletions that bypass Frappe modification timestamps may persist in cache until reset. Server submission still rejects invalid items.
- Role-permission rule changes that do not alter relevant `User Permission` rows may not immediately force reset. Financial writes remain server-side.
- Incremental sync scanning cost, month/year-scale behavior, multi-user concurrency, long-running server CPU savings, Windows installer size/startup/RAM, and real receipt-printer integration remain unproven.
- Native receipt printing was investigated through the existing print-companion architecture but not integrated into this catalog PoC; it should remain a separate loopback service with its own token/CORS boundary.
