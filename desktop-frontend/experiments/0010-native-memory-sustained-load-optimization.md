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

## Additional profiling-driven iteration

Live `/proc/<pid>/smaps` probing at 100,000 rows showed that catalog-owned allocations dominated anonymous memory: row metadata was about 5.5 MiB, inline original text about 4.0 MiB after the fixes below, and inline folded search text about 3.9 MiB. Before this iteration the same probe showed separate 8.0 MiB original and 5.4 MiB folded blobs plus wider row metadata. The concrete inefficiencies and fixes were:

| hypothesis | fix | behavior guard |
| --- | --- | --- |
| `u32` per-field length arrays wasted bytes on ERP-length fields | store field lengths as `u16` in compact row metadata | catalog unit/property tests; 56-byte maximum row metadata test |
| unit/group/brand text was repeated in every original and folded row | intern those low-cardinality dimension values once, with original casing and folded search text side by side | mixed-dimension roundtrip/search unit test |
| `modified` sync metadata was duplicated into the UI search index although no cashier lookup/rendering path uses it | keep it only in encrypted storage, not in the derived search index | streamed-index and exact-lookup tests |
| a primary barcode duplicated as the only barcode-text token had two exact-index entries | retain the primary entry and skip the identical token entry | exact barcode precedence/property tests |
| invoice rows deep-copied immutable display strings into the Slint row model | store invoice text as `SharedString` so model rows share immutable string storage | invoice filtering/paging unit tests and sustained row-count verification |
| test instrumentation and unused tray support shipped in every optimized binary | remove `system-tray`; make Slint MCP an explicit `ui-mcp` feature while default production builds omit it | MCP benchmarks build with `--features ui-mcp` |

## Startup and idle memory result

PR #18 release and the MCP-instrumented candidate release executables were alternated for seven warm repeats each:

| binary | p50 startup | mean startup | p50 idle RSS |
| --- | ---: | ---: | ---: |
| PR #18 release | 378.399 ms | 379.856 ms | 241,840,128 B |
| compact candidate release | 280.190 ms | 280.632 ms | 52,391,936 B |

Measured warm p50 startup improvement: **25.95%**. Measured p50 idle memory reduction: **78.34%** (230.63 MiB to 49.96 MiB). The startup comparison harness still exits nonzero because its fixed aspirational gate is 30%, but the complete measurements above are valid.

A default production build without `ui-mcp` is smaller on disk (24.7 MiB text versus 25.5 MiB for the MCP-instrumented build) and omitting unused tray support removed another about 1.2 MiB of text. The externally observed production idle RSS was approximately 51.7 MB in fixed one-second probes; sustained interaction measurements below use the MCP-instrumented build because Slint MCP is required to drive and verify the UI.

## Sustained-load result

The committed `benchmark_sustained.py` harness drove 1,000 timed iterations in each workload; the invoice workload adds 100 verification filter edits and 100 verified Load More clicks. Gates were p95 below 30 ms, end RSS below 128 MiB, memory growth below 4 MiB, and CPU below 10 ms/interaction. All gates passed. Every invoice Load More click was verified by the displayed loaded-row count (100 of 100 appended 20 rows, split into 50 filtered and 50 unfiltered pages). Filtered paging used the selective invoice-name term `fixture-sinv-a`, scrolled to the appended end of the virtualized list, and verified that visible row content matched the active filter, so an implementation ignoring the filter or appending unfiltered rows below the viewport could not pass. The expected append length is derived from the displayed filtered total, so smaller fixtures validate partial pages or skip impossible paging instead of failing correct behavior. The scroll workload first verified a 220-row warm-up (10 clicks) so scrolling exercised virtualization rather than only the initial 20 rows.

| workload | end RSS | memory behavior | p95 interaction latency | CPU/interaction |
| --- | ---: | --- | ---: | ---: |
| 1,000 catalog search edits | 55,824,384 B | flat at 55,824,384 B at every checkpoint | 6.786 ms | 2.860 ms |
| 1,100 invoice filter edits + 100 verified Load More clicks | 54,415,360 B | ended 581,632 B below start; flat at 54,415,360 B at every checkpoint | 3.052 ms | 1.492 ms |
| 1,000 invoice scroll events over a 220-row loaded list | 53,956,608 B | ended 1,454,080 B below start; checkpoints stayed within 4,096 B | 0.412 ms | 0.180 ms |

Slint MCP `click_element` has an approximately 51 ms fixed wait even when clicking non-interactive text. The invoice report therefore separates raw click latency (51.588 ms p95) from latency above the same-run control-click floor (0.545 ms p95). CPU time is measured independently; invoice work averaged 1.492 ms per interaction including clicks and row-count verification.

After exact-barcode compaction, a 1,000-search-edit MCP run passed the same gates: end RSS 56,414,208 B, checkpoints flat after warm-up within 4,096 B, p95 7.798 ms, mean 3.488 ms, CPU 3.240 ms/interaction. Exact-entry compaction halves the entry struct from the prior padded 24-byte layout to 12 bytes; RSS comparisons are noisier than that structural saving because renderer/driver allocations vary by several MiB across process launches.

Release-profile size optimization was tested rather than assumed. Across five quiet alternating startup probes at 100,000 rows, `opt-level = "s"` lowered median idle RSS from 52.18 MB (`opt-level = 3`) to 48.98 MB but increased 300-edit search p95 from 7.51 ms to 11.77 ms and CPU from 3.00 ms to 5.20 ms per interaction. `opt-level = 2` was performance-neutral but did not meaningfully reduce RSS. The release profile therefore remains `opt-level = 3`.

## Behavior-preservation evidence

- `CatalogIndex::search` is property-tested against a naive reference oracle across generated ASCII/Burmese terms and limits.
- Interned stock-UOM/item-group/brand values are tested for independent roundtrip and search behavior with mixed dimensions.
- Streamed encrypted-store indexing is equality-tested against materializing rows across a decode-chunk boundary.
- Exact item-code, exact-barcode precedence, exact-token boundary, long-barcode-text indexing, prefix-before-substring ordering, deleted-row filtering, Burmese search, quantity editing, invoice filtering, invoice paging order, migration strictness, and sales-invoice sort-order unit tests pass.
- Native invoice-list capture comparison against the PR #18 binary preserved the same visible rows, labels, values, positions, and sizes. Intentional semantic deltas are the added virtualized `List` container, the newly populated `20 of 10000` fixture count text, and omission of the one tail row that PR #18 rendered underneath the fixed footer (15 accessibility elements belonging to that hidden row). Initial row order, scrolling row replacement, and opening an invoice detail were smoke-tested through Slint MCP.
- Independent fixture UI validation of the pre-interning native runtime passed catalog search ordering/selection/add-line, exact barcode, Burmese search after installing a Myanmar font, invoice Load More, filtering, virtualized scrolling, and detail-shell navigation. That validation does not by itself cover the later interned-index revision; rerun it before treating the compact revision as UI-validated.
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

Build the MCP-instrumented candidate used by the harnesses and copy it aside:

```bash
cd desktop-frontend
cargo build -p khitmee-desktop --locked --release --features ui-mcp
cp target/release/khitmee-desktop /tmp/khitmee-desktop-mcpcompact-release
```

Build the production candidate without the inspection server:

```bash
cargo build -p khitmee-desktop --locked --release
```

Compare startup/memory with the saved PR #18 release binary:

```bash
KHITMEE_NATIVE_DATA_DIR=/tmp/khitmee-native-memory-bench \
python tools/benchmark_startup_compare.py \
  --base-executable /tmp/khitmee-desktop-pr18-release \
  --candidate-executable /tmp/khitmee-desktop-mcpcompact-release \
  --output evidence/startup-compare-pr18-compact.json \
  --rows 100000 --invoice-rows 10000 --repeats 7 --hold-seconds 0.2
```

Run sustained search, invoice, and scroll workloads:

```bash
KHITMEE_NATIVE_DATA_DIR=/tmp/khitmee-native-memory-bench \
python tools/benchmark_sustained.py \
  --executable /tmp/khitmee-desktop-mcpcompact-release \
  --output evidence/sustained-compact-release.json \
  --rows 100000 --invoice-rows 10000 --iterations 1000
```

## Remaining honest gaps

- This is Linux/host evidence, not low-end Windows hardware validation.
- Scroll/input CPU and state latency are measured, but true display frame presentation latency is still not measured.
- Network-byte totals, barcode-device latency, printer latency, and cross-process browser memory comparison remain separate explicit gates.
- The isolated live ERPNext oracle suite should be rerun on the perf runner before treating this as production migration evidence.
- Fixture invoice-detail fields/items remain untested because fixture detail requires live ERP configuration; the shell/back navigation was validated but the detail body is intentionally blank without ERP.
- Invoice rows still duplicate lightweight row structs/handles between canonical state and the Slint `VecModel` even though immutable string bytes are now shared. A backing custom Slint model is the next profiling hypothesis, but it is not yet implemented or claimed as a saving.
