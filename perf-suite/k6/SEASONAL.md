# Seasonal acceptance benchmark

`scenarios/seasonal.js` is plugged into the normal runner as the `seasonal` scenario. It uses the seeded manifest as trusted offline input, real counter endpoints for sales/transfers/searches, stock Frappe `savedocs` submit flows for purchase receipts and linked purchase invoices, and an independent read-only MariaDB verifier after k6 exits.

## Standalone workload run

The external [experiment supervisor](../lab/README.md) owns the 2 GiB qualification. It verifies whole-machine limits and the pinned fixture, compares stock financial results and retains the evidence. The standalone command below produces a workload scorecard on the selected development stack.

Run from the repository root in the KhitmeeERPNext WSL distribution, against the isolated `khitmee-perf` stack only:

```bash
SEASONAL_PROFILE=acceptance bash perf-suite/k6/run_k6.sh seasonal
```

The runner writes:

- `perf-suite/results/<run>-k6.log` — structured `KHITMEE_SEASONAL_EVENT` evidence;
- `perf-suite/results/<run>-k6-summary.json` — k6 metrics and dropped iterations;
- `perf-suite/results/<run>-container-stats.csv` — memory/restart evidence;
- `perf-suite/results/<run>-seasonal-verification.json` — independent database verdict;
- `perf-suite/results/<run>-scorecard.md` — feature-separated seasonal gates, including login count/cost and endpoint-class request attribution;
- `perf-suite/results/<run>-attribution/` — nginx JSON access rows, Gunicorn rows joined by `X-K6-Request-Id`, Redis command/latency snapshots, and InnoDB row-lock deltas.

A full qualification requires cashier, manager summary, issue-#2 detail pagination, full-year export, overload/recovery, exact accounting, container evidence, and database correctness gates. Missing endpoints or evidence fail closed; they are not treated as success.

Useful smoke discovery before spending an hour:

```bash
SEASONAL_PROFILE=smoke bash perf-suite/k6/run_k6.sh seasonal
```

The smoke profile uses the same workflow logic with short durations. It is not seasonal qualification.

## Cashier-only discovery

In the first baseline minute, the harness withholds one successful response for each of the four voucher types. The observer records the first committed voucher name only as benchmark evidence. Sale and transfer retries resubmit the original idempotent custom-endpoint request and must return that same voucher. Purchase receipts and linked purchase invoices are stock Frappe `savedocs` creates and are never resubmitted after an ambiguous outcome or transport timeout: the cashier performs bounded read-only polling through the permission-aware resource API, using only the original client voucher identity (`bill_no` for invoices, the exact marker-bearing `remarks` for receipts) and the already-built document. Exactly one submitted, semantically identical purchase document (same original scalar fields and ordered item rows, including receipt row links for invoices) may be confirmed as the original voucher. Missing, unreadable, ambiguous, or non-identical lookup results emit `voucher_unknown` and fail safely without another create. The database must contain a single posting. Use a disposable restored fixture for this test.

Set `SEASONAL_ENABLE_MANAGERS=0` to run only voucher, contention and search traffic:

```bash
SEASONAL_PROFILE=smoke SEASONAL_ENABLE_MANAGERS=0 bash perf-suite/k6/run_k6.sh seasonal
```

In this mode the runner passes `--cashier-only` to `evaluate_seasonal.py`. The scorecard reports a cashier-only verdict and marks overall seasonal qualification as not run. This mode is useful for real cashier endpoint discovery, but manager and export skips must never be represented as full seasonal acceptance.


## Driver integration evaluator

The reusable evaluator is:

```python
sys.path.insert(0, "perf-suite/k6")
import evaluate_seasonal
result = evaluate_seasonal.evaluate_events(events, k6_summary, cashier_only=False)
```

`events` may be either the raw ordered list of parsed `KHITMEE_SEASONAL_EVENT` objects or the indexed mapping returned by `perf-suite/seasonal/verify_run.py::parse_events`. Terminal voucher states are exactly one of `voucher_complete`, `voucher_reject`, or `voucher_unknown`; unknown is a fail-safe ambiguous purchase outcome, not a successful retry or a clean application rejection. `k6_summary` is the parsed `--summary-export` JSON. The callable uses only load evidence and the k6 summary; it does not require or invent a production reconciliation endpoint. It returns a JSON-serializable dictionary with `passed`, `verdict`, gate rows, explicit `report_windows`, public actor metadata, offered-work rows, and `manager_outputs`.

The evaluator covers exact offered accounting, deterministic per-minute mix, 1x/1.25x/recovery offered rates, phase-specific cashier latency, hot-stock contention, retry identity, baseline/margin/recovery workflow completion, overload and recovery evidence, manager summary/detail latency, manager core-phase failure evidence, and export evidence. Manager summary/detail gates require at least one successful terminal operation in each baseline, margin and recovery phase and allow a 0% terminal reject rate there; manager iterator exceptions are emitted as endpoint-specific rejects and tagged in the k6 `errors` counter, whose core-phase manager thresholds are `count==0`. Overload cashier rejections/admission 429s are the intended overload evidence and are not counted as core manager failures. Export cycles are long-running, so their per-cycle evidence requires zero terminal rejects in core phases plus at least one verified export overall; overload-phase export failures do not satisfy or invalidate that core evidence. The evaluator intentionally does not attest database correctness, guest machine size, or trusted stock replay; those remain separate supervisor gates. Trusted replay code may use `evaluate_seasonal.canonical_json_text(value)` / `canonical_json_sha256(value)` for the report-row canonicalization instead of reimplementing it.

### Trusted integer arrival schedule

Do not compare the run against `duration * rate` or a rounded voucher aggregate. k6 constant-arrival-rate starts iteration zero immediately; when the nominal stream count is integral, the final iteration can fall exactly on the duration endpoint and may or may not be executed. The public config therefore carries explicit `arrival_bounds` for each stream:

- integral nominal `N`: accepted stream iterations are `N` or `N + 1` (`endpoint_aligned: true`);
- non-integral nominal `x`: accepted stream iterations are exactly `ceil(x)` (`endpoint_aligned: false`).

For the acceptance 15-minute margin phase, regular voucher iterations are bounded as `1650..1651`; contention bursts are exactly `19` for nominal `18.75`. There is no fractional “expected 18.75” assertion and no endpoint+1 contention burst because no contention arrival is aligned with the 900-second endpoint. The same integer rules apply independently to each segment and stream.

`voucher_template` is compact trusted schedule data (`[logical_minute, mix_key]` entries). The generator apportions the scaled mix cumulatively: at each logical-minute boundary every voucher class is an integer floor/ceil of its exact scaled cumulative count, integral targets remain exact, and each minute's regular-iteration row total is integer-consistent. The evaluator expands the exact integer candidate matrices, including endpoint candidates, and matches all `voucher_start` events by logical minute, voucher type, line count, and stream iteration count. It uses integer per-class vectors only; for example, a margin candidate may legitimately total an integer other than the nominal `15 * 125` voucher aggregate because atomic contention bursts and receipt/invoice pairs cannot be fractional. `evaluate_events(...)` exposes the machine-readable interpretation as `arrival_schedule`; protected drivers should consume that field (or call `evaluate_seasonal.match_integer_arrival_schedule` / the gates directly) rather than duplicating the interpreter.

There is intentionally no public `expected_vouchers` aggregate. Use `result["offered_totals"]["minimum"]` / `["maximum"]` for bounded aggregate reporting, `result["offered_work"]` for per-segment rows, and `result["arrival_schedule"]` for the authoritative matched stream iteration counts and integer shape totals.

Individual sale/transfer start events may be resubmitted after submission using the same client voucher id; purchase recovery reuses that identity only in read-only lookups. Neither case emits an additional `voucher_start` event. A duplicate `voucher_start` client id is therefore a schedule defect, not retry evidence.

`manager_outputs` is hash-only evidence in load order. Summary and first-detail-page hashes use `canonical-json-v1([columns,result])`: object keys are sorted recursively, array/row order is preserved, numbers use at most 15 fractional digits with trailing zeroes trimmed, and JSON is serialized without whitespace before SHA-256. For replay implementations, the canonical text `{"a":1,"b":[2,{"a":"x"}]}` must hash to `2c77e8e42231c5a2b199e37b8c1225614c241a3b2e7623a7ebd6f17815a4a738`. Export hashes are SHA-256 over the response body text exactly as received by k6. Each output carries actor, actor user, explicit report window, observation id, hash algorithm, and canonicalization; customer row values and export contents are not included.

## Login/session lifecycle and request attribution

The harness follows the normal client lifecycle: one authenticated SID/CSRF pair per active k6 VU actor, cached across iterations with `noCookiesReset`. It does not use a privileged prebuilt session or share one session across independent active clients. k6 VUs are isolated JavaScript runtimes, so a replaced or newly allocated client must authenticate again; failed/interrupted initialization can therefore produce additional real login attempts. The scorecard reports login begins/completions and total, p50 and p95 login HTTP, Desk bootstrap, and combined cost so shift-start authentication remains visible rather than hidden.

Every HTTP helper emits a unique `X-K6-Request-Id` plus `X-K6-Endpoint-Class`. The seasonal nginx template logs request, upstream connect/header/response times, status, upstream address, request id and endpoint class as JSON. The seasonal Gunicorn command logs the same request id and service duration. `perf-suite/seasonal/attribution.py` joins those rows and reports p50/p95 server time and `estimated_dispatch_to_entry_ms` per endpoint class (`cashier_submit`, `search`, `login/bootstrap`, `manager_summary`, `manager_detail`, `manager_export`). The estimate is nginx upstream response time minus Gunicorn service duration; it includes proxy dispatch/transport/admission wait and is not a true nginx `upstream_queue_time` unless nginx is built with that upstream-queue feature.

## Idle preflight

`qualify_2gb.sh --preflight` restores the pinned fixture, deploys the exact candidate app tree, migrates it, grants the manager fixture actors the ERPNext Accounts roles required by the real report, then executes `scenarios/seasonal_preflight.js`. The preflight is a single idle client pass through nginx, before any load: login/boot/discovery, sale, transfer, purchase receipt plus linked invoice, search, both warm summaries, detail page 1 through a deep page, full-year export enqueue/status/download, and a negative check that the cashier actor is denied the manager detail report. Any missing or failed step increments k6 `errors` and fails closed.

## Important environment overrides

- `PERF_USER` / `PERF_PASSWORD` (or `FRAPPE_USER` / `FRAPPE_PASSWORD`) authenticate the cashier actor.
- `SEASONAL_MANAGER_<ROLE>_USER` and `SEASONAL_MANAGER_<ROLE>_PASSWORD` are required for `WAREHOUSE`, `MONTHLY`, `DETAIL` and `EXPORT` whenever managers are enabled. The cashier and these four manager actors must be exact distinct non-`Administrator` users; the run fails during setup rather than silently falling back to a shared login.
- `REPORT_TO_DATE` / `REPORT_FROM_DATE` define the explicit year-to-date summary/detail window. `EXPORT_FROM_DATE` / `REPORT_TO_DATE` define the explicit rolling full-year export window; it must span at least 365 inclusive dates.
- `SEASONAL_SEED` fixes basket generation; `RUN_ID` fixes the artifact prefix.
- `SEASONAL_PURCHASE_RECOVERY_POLLS` / `SEASONAL_PURCHASE_RECOVERY_POLL_INTERVAL_MS` bound purchase recovery reads (default 3 polls at 350 ms). Recovery never issues another purchase create.
- `SEASONAL_EXPORT_ENQUEUE_METHOD` and `SEASONAL_EXPORT_DOWNLOAD_METHOD` select the real full-year export API. There is no fallback fabrication if it is absent.
- `SEASONAL_VERIFY=0` intentionally disables verification but writes a failing verifier artifact, so the scorecard remains NO-GO.

The runner refuses the live `khitmee-erpnext` development stack. Use `http://127.0.0.1:18086` on the isolated perf environment.
