# Development performance experiments

The experiment supervisor runs on Linux outside the measured server. The proposer sees measurements and prior outcomes, edits a disposable code checkout, and submits one hypothesis. A separately owned evaluator restores the baseline fixture for each arm and runs the workload and verifier. Only the evaluator has credentials for the lab. The production deployment has no dependency on this directory.

For the year-fixture seasonal workload, the offline index assessment is available to the proposer through `--profile perf-suite/lab/offload-followup/benchmark/sle-index-candidate-analysis.json`. It records the rejection before a timed trial. ERPNext already declares the four-column ledger index and the measured prior-entry lookups averaged one row examined per call. Revisit this candidate if a new workload or a changed query shows substantial scan work. [Decision and supporting measurements](offload-followup/benchmark/sle-index-candidate-assessment.md)

The [operator installation](guest/operator.md) records the real two-core guest and its protected reference. The [SQL screening loop](diagnostics/README.md) recorded two rejected index changes and replays their decisions offline. The [cashier experiment](seasonal-smoke/README.md) exercises the sales portion of a busy minute. The [seasonal readiness receipt](driver/smoke-readiness.json) identifies the installed report/export capabilities and the actual dates covered by the smoke fixture.

For the next cashier experiment, supply `--profile perf-suite/lab/seasonal-smoke/ranked-instrumented-profile.json` to the supervisor loop. The instrumented run found 97.316 seconds of aggregate SQL time on the document-number locking read, 67 InnoDB row-lock waits and 7.806 seconds p95 for 200-line sales. Trace the interval between acquiring the numbering lock and committing each voucher. A trial that shortens that interval must preserve rollback, number allocation, stock and ledger results under concurrent sales. The [retained measurements](seasonal-smoke/ranked-instrumented-profile.json) include the fixture hash and sampling scope.

## Review and trusted boundary

Astra's pre-implementation review identified four issues. Compose shares fixed volume names and writable application paths. The old constant-user scenario slows arrivals when the app slows and its basket builder caps rows at 25. The old parity switch can be supplied by the caller. The old sampler lacks CPU, I/O and database lock evidence. The experiment interface requires distinct verified guest/fixture identity, complete workload and finance checks, and all five telemetry dimensions. Only a measured run in the dedicated guest can qualify for the [seasonal target](../../docs/research/financial-correctness-and-agents.md).

Use separate OS identities or machines for the model worker and the supervisor. The worker must have no write access to the trusted source mirror, evaluator, baseline store or guest; it submits a diff. Only the supervisor account may write the experiment archive. A hash detects altered evidence when the expected digest is trusted. Keep the printed baseline digest separately and retain the operator-owned fixture and runner versions. Local file permissions do not protect against the same account or root.

## Running the supervisor

The normal entry point is `bash tools/khitmee-dev.sh lab` or `tools\khitmee-dev.ps1 lab`. Paths below are supervisor-side Linux paths. Install a trusted evaluator executable, with its driver and configuration in a read-only harness directory. An arm receives one JSON request on stdin, writes its raw files to `artifacts_dir`, then prints its measured result as JSON on stdout. A nonzero exit, missing evidence, or changed adapter invalidates the experiment. The store must be outside the source checkout.

    bash tools/khitmee-dev.sh lab --store "$LAB_STORE" pin \
      --repo "$REFERENCE_REPO" --commit "$REFERENCE_COMMIT" \
      --adapter "$ARM_RUNNER" --harness "$TRUSTED_HARNESS" \
      --fixture "$FIXTURE_SHA256" --workload seasonal-v1 --pairs 4

The pin is created once with exclusive creation. It records the exact Git commit, archived source, fixture digest, workload identifier, runner digest, and thresholds. The operator should include the load driver, oracle, VM/image manifest, dependency versions and profile config in the hashed harness installation. The adapter separately verifies these inputs at execution. Use a new store for a changed baseline.

For fast discovery, pin a separate short, fixed, repeatable workload with `--screen-latency summary_p95_ms` (or another measured route). Its trusted driver still needs the financial, workload, resource and telemetry checks for that scope. Such a win receives `screened`; run the surviving patch through the full seasonal store to qualify it. This keeps a one-hour load and reserve/recovery trial off the inner loop. A one-off diagnostic without repeated matched trials has no win status.

### One-minute cashier screen

Pin `--workload cashier-sales-minute-v1` and the verified smoke fixture in a separate store. The workload records submit-to-confirmation p95 for all sales together and separately for 10-, 50- and 200-line baskets. The external driver runs against freshly restored candidate or baseline code. It audits committed postings, and the supervisor compares the financial signature between arms of each pair. It requires 60 small, 16 medium and four large committed sales, stock movements by warehouse and SKU, and GL totals by account and party. Every arm must satisfy the resource, telemetry, mutation-control, lifecycle and offered-work checks.

The standard pin uses four alternating baseline/candidate pairs. Its default minimum improvement is 2%. Every pair must improve the proposed objective, and the median improvement must reach the greater of 2% and twice the baseline median absolute deviation as a percentage. All four median submission latencies and peak memory are gated against regression. A previously on-budget class must stay within its 1,500 ms budget in its matched candidate arm.

A qualifying paired win receives `screened` when all four p95s are within 1,500 ms in every candidate arm. A repeatable gain with an existing budget miss receives `interim_progress`. The result records each class's worst candidate p95 and the classes still above budget. Both outcomes remain local to this cashier workload. The full seasonal candidate gate continues to enforce every absolute latency budget, including manager routes, beside its financial and full-load checks.

In the KhitmeeERPNext WSL distribution, the operator can install a new protected cashier release from the repository root. Set `new_release` to an unused name. The installer preserves existing pins.

    new_release=cashier-screen-next
    fixture=ccbecd846f6fe9bba96a83a6815859c3385bb795cf8a69f3e62135191ecfb61c
    sudo bash perf-suite/lab/guest/pin-supervisor.sh cashier "$new_release" "$fixture"

With the dedicated guest available exclusively, run the pinned supervisor. The proposal JSON must be readable by the operator account. It can name `sale_200_p95_ms` to target large baskets or `sql_seconds` to target database work. Each command uses the supervisor and store from the same release.

    op=/srv/khitmee-lab/operator
    release=cashier-screen-v4
    supervisor=(sudo -u km-lab-operator python3 -B "$op/releases/$release/experiments.py" --store "$op/stores/$release")
    "${supervisor[@]}" baseline
    "${supervisor[@]}" try /absolute/operator-readable/proposal.json
    "${supervisor[@]}" rank
    "${supervisor[@]}" verify

The two November 27 diagnostic receipts each include two additional boundary arrivals. The paired screen caps each arrival stream before its HTTP request so that both arms commit the same 80 seeded baskets. [Measured one-minute workload and limitations](seasonal-smoke/README.md)

The accepted v3 baseline confirmed and persisted all 80 sales within one minute. Submit-to-confirmation p95 was 4.598 s for 10 lines, 5.459 s for 50 lines and 7.088 s for 200 lines. The independent audit counted 2,200 stock ledger rows and 480 GL entries. These baseline values exceed the 1.5 s cashier target. [Cashier baseline receipt and resource measurements](driver/verified-cashier-screen.json)

Two fresh restores running the same application and seed produced identical sales and stock quantities, but different stock valuation on 30 warehouse/item groups. The v3 run debited COGS and credited Dist Hub 1 stock by an additional USD 19.24. The recorded client events do not establish database commit order or explain the difference. The paired supervisor requires byte-identical financial signatures and would reject this comparison. The smallest observed affected group has three contributing sales. [Valuation witness](driver/cashier-valuation-witness.json) · [Three reconstructed request bodies](driver/cashier-valuation-requests.jsonl)

Two subsequent instrumented restores captured the persisted FIFO queues. In one run, the first two overlapping invoices both consumed the opening valuation layers for two Dist Hub 1 items. Their SLE posting-time order differed from their creation-time order. Both queues finished seven units above the Bin quantity. The other run's quantities and values reconciled. The first committed 79 of 80 scheduled sales and failed the load gate. The second committed all 80. The snapshots establish the inconsistent stored valuation state; they do not identify the internal database read or lock that produced it. [Per-bin quantities, values, timestamps and FIFO queues](driver/cashier-fifo-observations.json)

The operator's new `cashier-screen-v4` pin enforces these bins' positive FIFO quantities exactly and requires each stock-value discrepancy to be less than half a US cent. Its verifier rejects the first trace and passes the second in offline replay. The historical v3 baseline keeps its original checks. The two observed minutes used the separately pinned observer on v3; v4 has no live baseline yet. [FIFO replay and observation commands](driver/README.md#fifo-stock-trace)

From the repository root in KhitmeeERPNext WSL, recheck the original fixture, regenerate both financial signatures from the archived postings and recompute the discrepancy. The replay reads the operator archives locally and prints each signature SHA, the three request IDs and the stock/GL differences.

    sudo -u km-lab-operator python3 -B perf-suite/lab/driver/replay_cashier_valuation.py

    bash tools/khitmee-dev.sh lab --store "$LAB_STORE" try proposal.json
    bash tools/khitmee-dev.sh lab --store "$LAB_STORE" baseline
    bash tools/khitmee-dev.sh lab --store "$LAB_STORE" history
    bash tools/khitmee-dev.sh lab --store "$LAB_STORE" rank
    bash tools/khitmee-dev.sh lab --store "$LAB_STORE" verify

`rank` compares observed intervention effects within the same objective and baseline. `verify` rehashes archived objects, arm receipts and retained raw evidence. The separate telemetry profiler identifies expensive operations to investigate before an intervention has been measured.

A proposal identifies the mechanism, expected effect, measured objective, producing model and full commit SHA. For example, measure `sql_seconds` for a bounded return lookup before proposing to batch it. Each candidate descends from the pinned commit, touches application code only, and is retained as a binary Git patch. A hypothesis retains its financial constraints even when the metric is SQL time.

## Autonomous operation

At pin time add `--proposer /trusted/bin/launch-worker`. This trusted executable accepts one JSON assignment and returns either a proposal, a proposal with a base64 Git diff instead of a commit, or a stop reason. The provided `proposal_worker.py` is a Codex implementation for a separate worker account with a local source mirror. Install it there as a forced SSH command, e.g. `python3 /opt/khitmee/proposal_worker.py --mirror /srv/worker-mirror --model MODEL`. On the supervisor the launcher can simply `exec ssh -o BatchMode=yes -T khitmee-proposer` using a dedicated SSH configuration with a pinned host key. T3/Devin workers can instead submit the same proposal contract. For provider choices see the [routing policy](../../docs/agents/routing.md).

    bash tools/khitmee-dev.sh lab --store "$LAB_STORE" loop \
      --profile ranked-profile.json --budget 3

Omit `--profile` to measure the baseline with the pinned evaluator before launching the worker. The first successful baseline receipt for each seed is preserved separately from subsequent paired runs. A missing workload or financial capability stops a qualifying baseline measurement. The default allowable median latency and peak-memory regression is zero.

The loop hands each proposal worker the original measured profile, the selected objective names and previous decisions. A new worktree starts from the same baseline on every attempt. The supervisor imports submitted diffs through a temporary Git index. Proposed code never runs inside the supervisor process. The budget caps candidate count. Stop or repeat with a new measured profile when the bottleneck changes. Missing metrics must remain missing; synthetic fixtures exercise the controller, not the target server.

## How decisions are made

The supervisor holds an exclusive file lock. Each pair uses the same seed in both arms; the order alternates AB/BA. Four pairs are the default. The comparison requires all pairs to improve the named metric and the median improvement to exceed both the configured threshold (default 5%) and twice the baseline median absolute deviation as a percentage. This is a screening rule, not a statistical confidence bound. The candidate must satisfy the absolute cashier/manager latency budgets. Median latency in any class and peak memory may regress no more than the pinned regression allowance. Identity, finance, mutation, offered-work, telemetry, CPU/RAM limits, restart, OOM and document-loss checks apply independently. Use the same declared scope and complete measurements for both arms.

SQLite records every admitted hypothesis before starting its first trial. The archive contains content-addressed proposals, patches, reference source and receipts with raw stdout/stderr digests. Full per-arm artifacts stay in the corresponding run directory. Reusing a patch or mechanism on the same baseline is refused. An interrupted supervisor marks an abandoned running experiment on the next submission. A retained candidate is evidence for review; deploying it requires the normal project release process.

## Quick controller check

    python3 -B -m unittest discover -s perf-suite/lab -p test_experiments.py

Tests use a temporary Git repository and synthetic evaluator. They check alternating paired runs, archive retention, duplicate detection, missing financial evidence, runner tampering, independent latency regressions and safe patch import. The [research notes](../../docs/research/autonomous-experiments.md) cover mutation testing, failure replay, architecture checks, model indexes and the case for modeling a stale snapshot publish.
