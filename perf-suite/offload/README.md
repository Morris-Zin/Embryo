# Database workload offloading proof of concept

This directory contains the reproducible isolated benchmark harness used to compare database workload-offloading architectures for Khitmee's ERPNext/MariaDB server.

The proof of concept deliberately does **not** modify ERPNext financial, accounting, inventory, FIFO, or invoice-submission logic. MariaDB remains authoritative. All database work runs against the disposable `khitmee-perf` Compose project; the scripts refuse the live project and keep credentials inside the backend container.

## Candidate architectures under test

| Candidate | Hypothesis | Main correctness risk | Operational cost |
|---|---|---|---|
| Incrementally maintained MariaDB summary/read model | Eliminates repeated full scans while preserving same-database consistency and security | Incremental maintenance must exactly handle returns, cancellations, amendments, reposts, bundles and backdating | Small write amplification; no new service |
| DuckDB snapshot/read model on manager computers | Moves scan/aggregation CPU and RAM completely off the server for heavy historical reports | Snapshot freshness and exact ERPNext Gross Profit semantics; sensitive data leaves the server | No recurring hosting cost, but reader-side packaging/key management is required |
| Incremental synchronization | Avoids repeated full-dataset transfers after the first snapshot | Modified-time CDC can miss unusually long transactions; needs overlap, idempotent replacement and reconciliation | Low steady bandwidth after initial transfer |
| External MariaDB-compatible read database | Keeps SQL close to source while moving reads offsite | Replication lag, consistency, free-tier capacity, network exposure | Potential recurring cost after free quotas |
| Native MariaDB replica + MaxScale/read-write split | Established exact replication and read offload | Replica consistency lag and another always-on database machine | Another database service to secure, patch and monitor |
| Debezium/Kafka, Airbyte or PeerDB/ClickHouse | Established CDC ecosystems | Correct event handling is possible, but system complexity is high | Too heavy for a six-shop 2 GB deployment unless analytics becomes a larger independent project |
| Flexviews | Incremental materialized views for MySQL | Effectively obsolete/MySQL-5.1-era tooling | Rejected before implementation |
| ColumnStore on the ERPNext server | Columnar analytics beside InnoDB | Violates the constraint unless another host is used; same-server RAM/CPU/disk cost is material | Rejected for this server |

Research anchors: [DuckDB memory/streaming execution](https://duckdb.org/2024/07/09/memory-management), [DuckDB MySQL extension](https://duckdb.org/docs/2.0/core_extensions/mysql), [MariaDB replication](https://mariadb.com/docs/server/ha-and-performance/standard-replication/replication-overview), [MaxScale readwritesplit](https://mariadb.com/docs/maxscale/reference/maxscale-routers/maxscale-readwritesplit), [Debezium MariaDB](https://debezium.io/documentation/reference/stable/connectors/mariadb.html), [Airbyte MySQL CDC limitations](https://docs.airbyte.com/integrations/sources/mysql/mysql-troubleshooting), [PeerDB architecture](https://github.com/PeerDB-io/peerdb/blob/main/docs/peerdb-architecture.md), [TiDB Starter quota](https://docs.pingcap.com/tidbcloud/select-cluster-tier/), [Supabase pricing](https://supabase.com/pricing), and [FlexCDC/Flexviews](https://flexviews.sourceforge.net/flexcdc.html).

## What the harness measures

`run_benchmark.sh` compares MariaDB central execution with a DuckDB copy of the same committed snapshot:

1. Export selected invoice, invoice-line, stock-ledger, packed-item, item and warehouse columns inside one MariaDB `REPEATABLE READ` consistent snapshot.
2. Copy the gzip CSV evidence out of the backend container.
3. Execute canonical financial and inventory aggregate queries twice inside MariaDB, recording latency and session status deltas such as rows read, pages read, temporary tables and bytes sent.
4. Load the same snapshot into DuckDB with explicit decimal/date types.
5. Execute the same aggregate semantics in DuckDB with bounded threads and memory.
6. Compare every result row exactly after numeric normalization. A mismatch fails the run.

`run_summary_benchmark.sh` separately measures a lightweight MariaDB daily summary table:

- one-time build cost and resulting table size;
- raw-range query cost versus summary-range query cost;
- exact parity for the additive canonical aggregate;
- trigger-free line-upsert maintenance primitive cost.

`compose.binlog.yaml` plus `perf-suite/scripts/up.sh --with-binlog` creates an optional MariaDB ROW-binlog variant so write/write-amplification experiments can measure the price of binlog-based CDC instead of assuming it is free.

The container stats collector now records cgroup CPU counters and cumulative cgroup `io.stat` read/write bytes and operations in addition to memory, OOM and restart evidence.

## Correctness boundary

The canonical `gross_profit_estimate` is an explicit transport/read-model parity measure. It is **not** claimed to be ERPNext Gross Profit. ERPNext Gross Profit also depends on FIFO matching, stock reposts, returns, product bundles, delivery-note interactions, permissions, precision and ordering semantics. A production summary table must reuse or exactly reproduce those semantics and must fall back to live authoritative evaluation for dirty/current days; that is the design already captured in issue #2 and must not be replaced by this lightweight benchmark.

Inventory parity in this harness is exact for the canonical stock-ledger aggregate at the exported committed snapshot. It is not a substitute for the full Stock Balance report contract.

Incremental `--mode delta` uses a modified-time lower bound and idempotent primary-key replacement in DuckDB. This is sufficient for measuring transfer size and merge cost, but production use requires overlap windows plus periodic full reconciliation because a transaction can theoretically commit late with an older `modified` value. Deletes of draft/master rows also require explicit tombstones or reconciliation.

## Reproduction

Install the reader-side dependency on the machine representing the manager computer:

```bash
python3 -m pip install --user -r perf-suite/offload/requirements-duckdb.txt
```

Start and seed the isolated year dataset (never the live stack). `seed.sh --scale year` temporarily raises only the backend container cap for one-time dataset construction and restores the configured 2 GB-profile cap before exiting; measured workloads still run under the unchanged Compose memory profile.

```bash
perf-suite/scripts/up.sh --profile 2gb --with-khitmee-ops
perf-suite/scripts/seed.sh --scale year
```

Run the central/offloaded comparison:

```bash
bash perf-suite/offload/run_benchmark.sh \
  --run-id year-full-baseline \
  --company 'Khit Me Distribution' \
  --from-date 2025-11-27 \
  --to-date 2026-11-26 \
  --threads 2 --memory-limit 512MB
```

Run the MariaDB summary proof of concept:

```bash
bash perf-suite/offload/run_summary_benchmark.sh \
  --run-id year-summary-poc \
  --company 'Khit Me Distribution' \
  --from-date 2025-11-27 \
  --to-date 2026-11-26 \
  --maintenance-rows 1000
```

Measure an incremental transfer after new writes by passing the lower bound captured before those writes. Copy the original DuckDB database first if it must remain unchanged, then merge into that copy with `--database`:

```bash
cp perf-suite/results/offload/year-full-baseline/offload.duckdb /tmp/offload-incremental.duckdb
bash perf-suite/offload/run_benchmark.sh \
  --run-id year-incremental-after-load \
  --mode delta \
  --modified-after '2026-11-27 08:00:00' \
  --database /tmp/offload-incremental.duckdb \
  --company 'Khit Me Distribution' \
  --from-date 2025-11-27 \
  --to-date 2026-11-26
```

Outputs stay in ignored `perf-suite/results/offload/<run-id>/` directories:

- `mariadb-results.json` — central query evidence;
- `duckdb-report.json` — load, memory, latency and parity evidence;
- `offload-report.md` — compact comparison table;
- `summary-report.json` — summary build/query/maintenance evidence.

## Measurements from the current full-year experiment

Dataset: 360,000 sales invoices, 950,000 invoice lines, 1,208,300 stock ledger entries, 14,728 stock items and six warehouses on the 2 GB profile. The immutable MariaDB volume snapshot was captured before any k6 writes.

### Canonical full-year scan/offload comparison

The consistent full export contained 2,601,283 rows in 108,327,914 gzip bytes and took 135.8s to produce. DuckDB loaded it in 28.2s using 53.4 CPU-seconds on two threads; the resulting database was 144,191,488 bytes. Exact row parity passed for every canonical query.

| Query | MariaDB cold/warm | DuckDB cold/warm | Cold speedup | Rows | MariaDB pages read, cold |
|---|---:|---:|---:|---:|---:|
| invoice fact scan | 7.877s / 8.693s | 0.412s / 0.189s | 19.1x | 1 | 140,657 |
| daily invoice summary | 10.165s / 10.256s | 0.220s / 0.214s | 46.3x | 2,190 | 142,568 |
| monthly item margin | 21.498s / 18.135s | 0.674s / 0.663s | 31.9x | 89,940 | 129,524 |
| inventory balance | 12.686s / 12.597s | 0.224s / 0.218s | 56.7x | 64,818 | 151,396 |

Across the probe suite MariaDB used 82.9 cgroup CPU-seconds and read 17.9GB from disk in 136s. Warm execution did not materially improve latency because the 320MiB InnoDB buffer pool cannot hold the scanned working set. DuckDB peak process memory stayed below 500MiB with a 512MB memory limit.

### Incrementally maintained MariaDB daily summary PoC

The 2,190-row date/warehouse summary table was only 245,760 bytes. Exact numeric parity passed for the additive canonical aggregate.

| Operation | Cost | Evidence |
|---|---:|---|
| one-time summary build | 10.871s | 155,763 InnoDB pages read |
| raw full-year range query | 10.843s | 143,665 InnoDB pages read, one disk temp table |
| summary full-year range query | 0.029s | 39 buffer-pool requests, zero InnoDB disk reads |
| line-upsert maintenance primitive | 0.087ms/line | hot-row trigger-free `INSERT ... ON DUPLICATE KEY UPDATE` microbenchmark |

This is strong evidence for the read-model architecture, but it is not yet production Gross Profit logic. Correct incremental maintenance still needs exact ERPNext valuation/return/bundle semantics, dirty-window invalidation and live fallback.

### Concurrent full-year reporting baseline before offload

`report_stress` used six shop workers, two report managers, the full seeded year and a configured peak of 100 vouchers/minute for three minutes. The baseline failed every strict latency/reliability gate:

- sale/transfer submit p95: 2.652s end-to-end (2.601s server-side), target <1.5s;
- report p95: 10.463s, target <5s;
- Fast Gross Profit p95: 10.709s with only two successful samples;
- Stock Balance p95: 8.299s;
- HTTP failure rate: 5.25%, mostly 502s during backend OOM recycling;
- backend cgroup peak: 576.4MiB of its 576MiB limit with 6 OOM kills;
- db cgroup disk reads: 35.6GB in about three minutes;
- fleet sum of container memory peaks: 1,483.8MiB; sum of averages: 875.6MiB.

Only 152 sales and 63 transfers succeeded because errors and backend recycling reduced the achieved rate below the configured peak. This is precisely the interference the offload architecture must remove.

### Incremental synchronization after the write load

After that write load, the modified-time delta contained 4,648 rows in 191,575 gzip bytes and exported in 4.1s, versus 2,601,283 rows / 108,327,914 bytes / 135.8s for the initial full extract. That is a 565x reduction in transfer bytes for this workload interval. Canonical-query parity for the incrementally merged DuckDB copy is recorded in `year-incremental-after-load` when the run completes.

### Binlog and external-service comparison

Pending final binlog run; the Compose override is present but measurements must be recorded here before any CDC architecture is ranked above simpler incremental extraction.
