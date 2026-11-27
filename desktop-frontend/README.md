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

Run the native pilot with MCP inspection enabled. On this Windows VM use the MSVC wrapper; `--no-default-features` bypasses the unresolved OpenSSL/SQLCipher toolchain only for fixture UI smoke testing and must not be reported as encrypted-catalog evidence:

```bash
cd desktop-frontend
SLINT_MCP_PORT=49272 SLINT_EMIT_DEBUG_INFO=1 KHITMEE_CATALOG_ROWS=10000 KHITMEE_INVOICE_ROWS=10000 ./cargo-msvc.cmd cargo run -p khitmee-desktop --no-default-features
```

Encrypted-catalog validation requires a working Windows OpenSSL toolchain for the `bundled-sqlcipher` feature, then the default `encrypted-catalog` feature must be rebuilt and tested end-to-end.

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

## Concurrent-agent ownership model

Screen agents own only their screen slice under `owner_scope`; shared edits belong to the narrower shared-component scopes recorded in `registry/screens.json`:

- catalog storage/search: `desktop-frontend/crates/khitmee-catalog`
- migration registry primitives: `desktop-frontend/crates/migration-core`
- verification harnesses: `desktop-frontend/tools`
- design tokens/widgets: the shared section of `crates/khitmee-desktop/ui/app.slint`

A screen may move to `verified` only after all evidence kinds exist. Shared-component changes require rerun of every `regression-locked` screen.

## Current honesty boundary

The first pilot implementation uses deterministic fixture data. Fixture-only rendering and local search evidence do not prove ERPNext parity. Authoritative submission/printing are deliberately blocked in the UI until the ERPNext adapters and security review exist.
