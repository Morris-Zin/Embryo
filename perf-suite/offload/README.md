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

Pending final full-year measurements in this session; this section must be updated before review with actual immutable-baseline numbers rather than estimates.
