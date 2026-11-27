# Khitmee native desktop migration workspace

`desktop-frontend/` is the isolated native Rust migration workspace. It preserves the existing web frontend and does not use Tauri, Electron, WebView, or an embedded browser.

## Layout

- `crates/khitmee-catalog/` — in-process SQLCipher catalog storage, DPAPI key protection and search indexing extracted from PR #8. The PR #8 loopback HTTP service and Tauri shell are not dependencies.
- `crates/migration-core/` — machine-readable registry, evidence and acceptance-criteria types.
- `crates/khitmee-desktop/` — Slint native pilot for sales voucher, item search and invoice-list workloads.
- `registry/` — generated migration registry plus explicit screen overrides and ownership scopes.
- `tools/` — discovery, deterministic web capture, Slint MCP native capture, visual comparison, architectural lint, verification and performance harnesses.
- `experiments/` — persistent framework and qualification records.
- `evidence/` — generated screenshots, traces, comparisons and benchmark reports (not source of truth until regenerated and verified).

## Commands

```bash
python desktop-frontend/tools/discover.py
python desktop-frontend/tools/architecture_lint.py
cd desktop-frontend && cargo test --locked
cd desktop-frontend && cargo build -p khitmee-desktop
```

Run the native pilot with MCP inspection enabled. On this Windows VM use the MSVC wrapper; `--no-default-features` bypasses SQLCipher only for fixture UI smoke testing and must not be reported as encrypted-catalog evidence:

```bash
cd desktop-frontend
SLINT_MCP_PORT=49272 SLINT_EMIT_DEBUG_INFO=1 KHITMEE_CATALOG_ROWS=10000 KHITMEE_INVOICE_ROWS=10000 ./cargo-msvc.cmd cargo run -p khitmee-desktop --no-default-features
```

Encrypted-catalog validation uses the Chocolatey OpenSSL installation with `OPENSSL_DIR=C:\Program Files\OpenSSL-Win64`, `OPENSSL_LIB_DIR=C:\Program Files\OpenSSL-Win64\lib\VCd\MD`, and `OPENSSL_INCLUDE_DIR=C:\Program Files\OpenSSL-Win64\include`; default-feature SQLCipher tests pass on this VM.

Capture deterministic web references (requires the isolated `khitmee-perf` stack or another approved test site):

```bash
cd perf-suite/browser && pnpm install --frozen-lockfile --ignore-scripts
node desktop-frontend/tools/capture_reference.mjs \
  --base-url http://127.0.0.1:18086 \
  --routes /khitmee_sales,/khitmee_transfer
```

Capture native evidence and compare it:

```bash
node desktop-frontend/tools/capture_native_mcp.mjs --port 49272 --screen sales-voucher
python desktop-frontend/tools/verify.py --screen sales-voucher --screen item-search --screen invoice-list
```

`verify.py` exits nonzero when evidence is missing or incomparable. It must never be edited to treat missing ERPNext, visual, accessibility, keyboard, business or performance evidence as success.

### One-to-one state verification contract

Evidence is state-based. The initial capture lives directly under `evidence/web-reference/<route-or-screen>/` and `evidence/native/<screen>/`; interaction states live in lowercase child directories such as `search-results/` or `draft-line-added/`. Both sides must contain the same state directories.

Reference captures accept deterministic replay actions. Replaying from a fresh page is intentional: actions must include the full path from the initial state.

```bash
node desktop-frontend/tools/capture_reference.mjs   --base-url http://127.0.0.1:18086   --routes /khitmee_sales   --state search-results   --actions '[{"type":"fill","selector":"#item-search","value":"paracetamol"},{"type":"wait","ms":300}]'
```

Native captures use the same state name and semantic action sequence, driven through Slint MCP rather than browser automation:

```bash
node desktop-frontend/tools/capture_native_mcp.mjs   --port 49272 --screen sales-voucher --state search-results   --actions '[{"type":"fill","label":"Barcode / item search F2","value":"paracetamol"},{"type":"wait","ms":300}]'
```

The verifier compares exact PNG bytes/pixels, dimensions, differing-pixel ratio and bounds; visible-control identity, role, label/text, accessibility state, geometry, and disabled state; behavioral keyboard outcomes; ERPNext business outcomes; and performance evidence. Exact screenshot matching is the default visual gate. Diagnostic metrics never soften a configured acceptance threshold. Missing evidence, empty structured evidence, unmatched states, mismatched dimensions, or missing behavioral keyboard results are failures.

Harness self-tests use synthetic evidence and must pass after every verifier change:

```bash
python desktop-frontend/tools/test_verify.py
```

## Concurrent-agent ownership model

Screen agents own only their screen slice under `owner_scope`; shared edits belong to the narrower shared-component scopes recorded in `registry/screens.json`:

- catalog storage/search: `desktop-frontend/crates/khitmee-catalog`
- migration registry primitives: `desktop-frontend/crates/migration-core`
- verification harnesses: `desktop-frontend/tools`
- design tokens/widgets: the shared section of `crates/khitmee-desktop/ui/app.slint`

A screen may move to `verified` only after all evidence kinds exist. Shared-component changes require rerun of every `regression-locked` screen.

## Current honesty boundary

The first pilot implementation uses deterministic fixture data. Fixture-only rendering and local search evidence do not prove ERPNext parity. Authoritative submission/printing are deliberately blocked in the UI until the ERPNext adapters and security review exist.
