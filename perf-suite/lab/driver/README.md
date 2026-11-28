# Trusted arm driver

`seasonal_driver.py` runs on the external Linux supervisor. Install it as the executable in the [environment adapter](../environment/khitmee_lab_adapter.py) configuration, with its relative `perf-suite` and `tools` dependencies in the pinned harness tree. The adapter supplies a restored namespace, guest identity, fixture manifest and supervisor request on stdin.

The driver reads the trusted Git mirror recorded in the supervisor's `BASELINE` object and archives the requested commit's application directory. For a candidate it recalculates the binary diff digest against the reference commit. The isolated guest verifies the transferred bytes, the restored Docker volume labels and the running namespace. All Python, worker and frontend services mount the arm's own app directory read-only. The restored guest must be available for exclusive use.

## Gross Profit screen

The fixture workload is `smoke-gross-profit-shop1-last-seed-day-by-item-v1`. Pin the controller with `--screen-latency summary_p95_ms`. The measured operation is the Fast Gross Profit report module's in-process execution for Shop 1 on the final bulk seed day, grouped by Item Code. The latency excludes SSH, Python startup, authentication, HTTP and rendering.

The external driver issues one upstream reference invocation and 20 successive measured invocations in independent sessions. It compares each result against the stock result outside the guest. A pinned hash also checks the stock rows, columns, filters, fixture identity, currency and precision against the earlier independent stock run. The p95 is the 19th sorted duration of the 20 measurements. The driver saves every raw row and duration, SQL templates and digest changes, CPU ticks, guest disk bytes and periodic guest memory samples. CPU and I/O include observer overhead within the measured windows. SQL time comes from the report session's query instrumentation.

The [verified report baseline](verified-report-screen.json) records the operator receipt, raw artifact hashes and metrics for the protected smoke-screen-v1 run. It completed all 20 report calls with 43.98 ms p95 in-process latency. The operational driver also ranks the observed SQL templates across its per-invocation traces for the next proposal.

Each report runs in a read-only Frappe database session. The driver compares persistent posting counts and container identities before and after the screen. It requires the financial verifier's three mutation controls and introduces a one-unit difference into a captured report cell to check its report comparator. These checks cover this specific read-only report. Cashier confirmations and committed voucher retries require separate workload evidence.

## Cashier minute

The `cashier-sales-minute-v1` screen invokes `seasonal-smoke/supervisor_arm.py` after deploying the requested commit. The operator installs its pinned external k6 runtime. Each run returns the five resource signals and four sale latency metrics, an independent audit of the committed postings, and the financial signature used in the paired comparison. [Cashier result labels](../README.md#one-minute-cashier-screen)

### FIFO stock trace

The development release `cashier-fifo-v1` derives from the protected `cashier-screen-v3` release. It adds a read-only query to the cashier audit transactions immediately before and after the measured minute. Both transactions return the complete SLE histories and current Bin for KM-00101 and KM-00039 at Dist Hub 1. The archive retains each persisted queue, posting and creation time, valuation fields, voucher detail and cashier request ID. A future new pin can be installed with `sudo python3 -B perf-suite/lab/driver/pin_cashier_fifo.py cashier-fifo-v2` in KhitmeeERPNext WSL.

Use two fresh restores with the same application, fixture, seed, requests and observer pin. With exclusive use of the 2 GiB guest, run the pinned supervisor's `baseline` command twice. Each run gets a separate namespace and archive. The following command compares both opening histories and reconstructs how each outgoing entry consumed FIFO layers. Supply the two `0-baseline` directories under the operator store.

    sudo -u km-lab-operator python3 -B perf-suite/lab/driver/replay_cashier_fifo.py "$first_arm" "$second_arm"

To reproduce the observed result, compare the two archived arms in chronological order:

    d=/srv/khitmee-lab/operator/stores/cashier-fifo-v1/runs
    sudo -u km-lab-operator python3 -B perf-suite/lab/driver/replay_cashier_fifo.py \
        "$d/1795763159847033357/0-baseline" "$d/1795763300795107752/0-baseline"

| Dist Hub 1 item | First run Bin / queue units | First run closing Bin minus opening and SLE changes | Second run Bin / queue units | Second run value gap |
| --- | ---: | ---: | ---: | ---: |
| KM-00101 | 487 / 494 | USD 25.664 | 487 / 487 | USD 0 |
| KM-00039 | 479 / 486 | USD 284.978 | 479 / 479 | USD 0 |

The two invocations used identical opening SLE histories. The first run's seven extra queue units agree with the first large invoice's issue quantity for each SKU. Both that invoice and the first medium invoice have a valuation queue and value change consistent with consuming the opening layers independently. Their accumulated SLE quantities account for both deductions. The persisted posting-time and creation-time orderings differ; the captured columns do not expose the original transactions' read views. The first run committed 79 sales with two scheduler drops. The second committed 80 and passed the existing strict cashier screen. Both traces include all three KM-00101 issues and four KM-00039 issues. [Archived observations](cashier-fifo-observations.json)

Future cashier arms require a separate hashed FIFO conservation verdict covering both bins. Their quantities must match layer consumption, the SLE chain and the closing Bin. The closing Bin value must match the opening value plus all posted value changes and the recomputed positive FIFO layers to within half a US cent. The gate also checks the queue and value progression at each outgoing entry. The retained evidence and mutations of closing quantity and value exercise this gate offline.

Two follow-up trials on unmodified ERPNext v16.60.0 observed different on-hand and FIFO quantities immediately after overlapping sales. Both reconciled after pending valuation work completed, with zero remaining queue-quantity or stock-value discrepancy on the two traced items. The earlier cashier receipts capture the immediate state only. Record an immediate snapshot and a second snapshot after a verified valuation-job drain when investigating a persistent accounting error. [Clean stock trial C6](../fifo-upstream-repro/evidence/c6.json) · [Clean stock trial C7](../fifo-upstream-repro/evidence/c7.json)

## Dev-only process/kernel OOM attribution

`process_kernel_telemetry.py` is a diagnostic companion to the normal cgroup window; it is not part of release qualification. It uses the same pinned SSH target, deployed namespace, fixture digest and boot identity. The default scope is only `backend` because thread enumeration is observer overhead. Keep the interval at or below ten seconds and use it for a short diagnostic rerun, not a production or hour-long qualification measurement.

Integrate it around the workload with explicit start/stop hooks:

```python
import process_kernel_telemetry

handle = process_kernel_telemetry.start(target, message, folder, interval=1.0)
try:
    run_measured_workload()
finally:
    result = process_kernel_telemetry.stop(handle)
```

`start` performs a guest identity/service boundary, records a kernel journal cursor, captures the container command and init PID, then samples procfs. Each sample maps every thread ID in scope to its owning process/TGID, parent, command line, cgroup and RSS. `stop` ends sampling, reads kernel journal records after the start cursor, writes `process-kernel-telemetry.json`, parses memcg OOM reports and resolves the allocator TID and victim PID against the retained process samples. Missing samples or an unmapped allocator make the artifact incomplete rather than substituting a guess.

The collector proves process/thread attribution only. Procfs and kernel OOM records do not contain HTTP routes, request IDs or application session state. Route statements require separate high-precision application/client events and remain temporal correlation unless an application instrumentation marker binds a route to the same thread or process. Command lines and journal messages are redacted for common credential shapes before retention; actor credentials must stay outside the artifact folder.

## Seasonal readiness

For a seasonal release the operator installs `seasonal.json` beside `seasonal_acceptance.py` in the hashed harness. Its fields are `fixture_sha256`, `as_of` (fixture business date), `k6`, `seed_manifest`, `actors`, `references` and `manager_oracles`. Every file reference contains its absolute `path` and `sha256`. Both oracle maps use the decimal trial seed as a key. Keep actor credentials outside source control. The actor file contains the cashier's `PERF_USER` and `PERF_PASSWORD`, and `SEASONAL_MANAGER_{WAREHOUSE,MONTHLY,DETAIL,EXPORT}_{USER,PASSWORD}` for the manager sessions. The install must include the k6 scripts and scorecard, the standalone seasonal DB verifier, and the driver modules for committed comparisons and telemetry.

Use stock ERPNext on the same seeded financial state to produce the manager observations for each paired seed. Record `fixture_sha256`, `seed` and `observations`. For each checkpoint record `observation_id` (scenario/iteration), `event`, `group`, the independent `snapshot` identifier and expected `sha256`. Capture Warehouse, Monthly, two-page invoice detail and downloaded CSV. Match the account, query dates and source state when a concurrent sale changes a report. Summary hashes cover ordered columns and rows, detail hashes cover the ordered page hashes, and CSV hashes cover downloaded content. The continuous load runs on the measured server while the stock oracle is prepared separately. The full seasonal gate requires live actor credentials, committed oracle evidence and implemented report/export features.

The seasonal driver passes a qualifying restored fixture to `seasonal_acceptance.run`. To inspect the existing diagnostic clone without submitting transactions, run `python3 -B seasonal_driver.py --readiness --ssh-config /path/to/operator/ssh_config` from the installed driver directory. The [recorded smoke readiness result](smoke-readiness.json) reports the fixture history, catalog and discovered export methods. It covers installed source presence. The full seasonal executor must exercise the APIs and validate their actual behavior.
