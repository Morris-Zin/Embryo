# 0010 Native memory and sustained-load optimization

> Evidence artifact note: raw benchmark JSON, screenshots, and captured element trees belong in gitignored local evidence. This file is the committed concise experiment record; rerun the commands below to regenerate exact machine-local numbers.

## Scope

This experiment stacks on the PR #18 native-speed head (`c19d7bc` locally) and keeps the same contract: preserve ERPNext-oracle-visible behavior while reducing native frontend memory and proving sustained interaction performance. Measurements used 100,000 encrypted catalog rows and 10,000 fixture invoice rows on the same Linux host. The SQLCipher fixture store was warmed before startup comparison so the measurement covers loading/decoding/indexing/UI startup rather than one-time fixture generation.

## Implementation retained

- Replace per-row catalog index strings and cloned rows with compact row metadata plus concatenated original/folded text blobs. Exact barcode lookup uses sorted hashes with full value verification, and search clones only returned rows.
- Stream SQLCipher payload reads in 2,048-row chunks directly into index construction instead of materializing every `CatalogItem` first.
- Use Slint's software renderer rather than the femtovg/OpenGL renderer, removing the large graphics-driver/GL working-set cost.
- Render invoice rows through a virtualized `ListView`; retain a persistent row model and append only new rows for Load More instead of rebuilding the complete model. The viewport ends above the fixed footer so the final row cannot remain trapped underneath it.
- Load additional live ERP invoice rows with offset pagination (`start = raw ERP rows already requested`) rather than refetching the whole expanded prefix. This matches Frappe `BaseList` Load More behavior: increment `start`, request one page, and concatenate unique rows. Raw response-row count is tracked independently of display parsing, so a skipped malformed/null row cannot rewind the offset and duplicate invoices; appended rows are deduplicated by `name`, again matching Frappe's `uniqBy` behavior.
- Mark the live invoice list stale after a successful save, submit, or cancel and refresh it when returning from detail to the list, so edited amounts/statuses and modified-time ordering cannot remain stale during subsequent paging.
- Return unused glibc allocation arenas after startup initialization on GNU/Linux.

## Startup and idle memory result

PR #18 release and candidate release executables were alternated for seven warm repeats each:

| binary | p50 startup | mean startup | p50 idle RSS |
| --- | ---: | ---: | ---: |
| PR #18 release | 367.029 ms | 368.552 ms | 241,524,736 B |
| candidate release | 280.706 ms | 280.709 ms | 61,804,544 B |

Measured warm p50 startup improvement: **23.52%**. Measured p50 idle memory reduction: **74.41%** (230.34 MiB to 58.94 MiB).

## Sustained-load result

The committed `benchmark_sustained.py` harness drove 1,000 timed iterations in each workload; the invoice workload adds 100 verification filter edits and 100 verified Load More clicks. Gates were p95 below 30 ms, end RSS below 128 MiB, memory growth below 4 MiB, and CPU below 10 ms/interaction. All gates passed. Every invoice Load More click was verified by the displayed loaded-row count (100 of 100 appended 20 rows, split into 50 filtered and 50 unfiltered pages). Filtered paging used the selective invoice-name term `fixture-sinv-a`, scrolled to the appended end of the virtualized list, and verified that visible row content matched the active filter, so an implementation ignoring the filter or appending unfiltered rows below the viewport could not pass. The expected append length is derived from the displayed filtered total, so smaller fixtures validate partial pages or skip impossible paging instead of failing correct behavior. The scroll workload first verified a 220-row warm-up (10 clicks) so scrolling exercised virtualization rather than only the initial 20 rows.

| workload | end RSS | memory behavior | p95 interaction latency | CPU/interaction |
| --- | ---: | --- | ---: | ---: |
| 1,000 catalog search edits | 63,295,488 B | flat at 63,295,488 B at every checkpoint | 6.515 ms | 2.930 ms |
| 1,100 invoice filter edits + 100 verified Load More clicks | 64,319,488 B | ended 1,048,576 B below start; flat at 64,319,488 B at every checkpoint | 3.165 ms | 1.508 ms |
| 1,000 invoice scroll events over a 220-row loaded list | 63,803,392 B | ended 1,449,984 B below start; checkpoints stayed within 4,096 B | 2.003 ms | 0.280 ms |

Slint MCP `click_element` has an approximately 51 ms fixed wait even when clicking non-interactive text. The invoice report therefore separates raw click latency (51.550 ms p95) from latency above the same-run control-click floor (0.662 ms p95). CPU time is measured independently; invoice work averaged 1.508 ms per interaction including clicks and row-count verification.

## Behavior-preservation evidence

- `CatalogIndex::search` is property-tested against a naive reference oracle across generated ASCII/Burmese terms and limits.
- Streamed encrypted-store indexing is equality-tested against materializing rows across a decode-chunk boundary.
- Exact item-code, exact-barcode precedence, prefix-before-substring ordering, deleted-row filtering, Burmese search, quantity editing, invoice filtering, invoice paging order, migration strictness, and sales-invoice sort-order unit tests pass.
- Native invoice-list capture comparison against the PR #18 binary preserved the same visible rows, labels, values, positions, and sizes. Intentional semantic deltas are the added virtualized `List` container, the newly populated `20 of 10000` fixture count text, and omission of the one tail row that PR #18 rendered underneath the fixed footer (15 accessibility elements belonging to that hidden row). Initial row order, scrolling row replacement, and opening an invoice detail were smoke-tested through Slint MCP.
- Screenshots remained visually equivalent in layout and content. Renderer rasterization is not pixel-identical (5.19% of pixels differed; mean channel absolute difference 1.52/255), so exact pixel parity is not claimed.
- No ERPNext/Frappe upstream core, financial calculation, inventory calculation, or authoritative server-side behavior was modified. PR #18's live ERPNext differential journeys remain the direct oracle evidence for the unchanged screen behavior; the live stack was not rebuilt in this Linux session.

## Verification run locally

```bash
cd desktop-frontend
cargo fmt --all -- --check
cargo test --locked
cargo clippy --locked --workspace --all-targets
python tools/test_verify.py
python tools/architecture_lint.py
```

All passed. Repository `bash tools/khitmee-dev.sh fast-check` could not run on this VM because only Python 3.10 is installed while the repo requires Python 3.14 and pinned dev tools; the CI workflow provisions Python 3.14.

## Reproduction

Build the candidate and copy it aside:

```bash
cd desktop-frontend
cargo build -p khitmee-desktop --locked --release
cp target/release/khitmee-desktop /tmp/khitmee-desktop-final-release
```

Compare startup/memory with the saved PR #18 release binary:

```bash
KHITMEE_NATIVE_DATA_DIR=/tmp/khitmee-native-memory-bench \
python tools/benchmark_startup_compare.py \
  --base-executable /tmp/khitmee-desktop-pr18-release \
  --candidate-executable /tmp/khitmee-desktop-final-release \
  --output evidence/startup-compare-pr18-optimized.json \
  --rows 100000 --invoice-rows 10000 --repeats 7 --hold-seconds 0.2
```

Run sustained search, invoice, and scroll workloads:

```bash
KHITMEE_NATIVE_DATA_DIR=/tmp/khitmee-native-memory-bench \
python tools/benchmark_sustained.py \
  --executable /tmp/khitmee-desktop-final-release \
  --output evidence/sustained-optimized-release.json \
  --rows 100000 --invoice-rows 10000 --iterations 1000
```

## Remaining honest gaps

- This is Linux/host evidence, not low-end Windows hardware validation.
- Scroll/input CPU and state latency are measured, but true display frame presentation latency is still not measured.
- Network-byte totals, barcode-device latency, printer latency, and cross-process browser memory comparison remain separate explicit gates.
- The isolated live ERPNext oracle suite should be rerun on the perf runner before treating this as production migration evidence.
