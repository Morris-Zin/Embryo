# Experiment 0002 — pilot qualification ledger

Date: 2026-11-27

This ledger is append-only evidence for sales voucher, item search and invoice-list qualification. A blank or pending row is a failure to complete qualification, not a pass.

## Required gates

| Gate | Sales voucher | Item search | Invoice list | Evidence |
|---|---:|---:|---:|---|
| Static discovery recorded | passed | passed | passed | `desktop-frontend/registry/screens.json` |
| Native implementation exists | fixture-pilot passed | fixture-pilot passed | fixture-pilot passed | `desktop-frontend/crates/khitmee-desktop` |
| Live web reference screenshot captured | blocked | blocked | blocked | isolated ERP Docker/WSL stack unavailable |
| Native screenshot captured through Slint MCP | fixture-only passed | fixture-only passed | fixture-only passed | `desktop-frontend/evidence/native-test-run` |
| Visual comparison passes strict criteria | failed — reference missing | failed — reference missing | failed — reference missing | `desktop-frontend/evidence/verification/verification-report.json` |
| Text/accessibility/keyboard comparison passes | failed — reference missing | failed — reference missing | failed — reference missing | `desktop-frontend/evidence/verification/verification-report.json` |
| ERPNext authoritative business outcome comparison passes | not run — no live ERP fixture | not applicable to read-only search | not applicable to read-only list | business evidence reports absent |
| 10,000-row performance qualification passes | partial smoke only — no formal benchmark | partial smoke only — no formal benchmark | partial smoke only — no formal benchmark | UI launched with 10,000 rows; `evidence/benchmarks` absent |
| 100,000-row performance qualification passes | not run | not run | not run | `desktop-frontend/evidence/benchmarks` absent |
| Burmese text rendering verified | fixture-only passed | fixture-only passed | fixture-only passed | fresh desktop screenshots and Slint MCP tree |
| Barcode/printing/low-end Windows validation | not validated | barcode search logic unit-tested only | not applicable | hardware evidence absent |

## 2026-11-27 runtime smoke evidence

- `cargo check -p khitmee-desktop --no-default-features` passed on Windows MSVC.
- `cargo test -p migration-core -p khitmee-catalog --no-default-features` passed: 7 tests.
- `python tools/architecture_lint.py` passed.
- `python tools/verify.py --screen sales-voucher` correctly failed because live reference, business and performance evidence are missing.
- The real desktop UI was driven for a 10,000-item/10,000-invoice fixture run. Empty totals show `0`/`0.00`; Burmese query `ဆန်` returned 3 visible matches; selecting `ITEM-000000` produced qty `1` and subtotal `250.00`; submit/print was blocked without fabricating a sale; exact invoice filtering narrowed to one row.
- SQLCipher encrypted-catalog validation is now proven on this Windows VM after installing OpenSSL through Chocolatey: default-feature `cargo check` and `cargo test -p migration-core -p khitmee-catalog --locked` passed, including encrypted roundtrip and wrong-key rejection. Earlier `--no-default-features` UI evidence remains fixture-only and is not encrypted-catalog evidence.
- The migration verifier was hardened into a state-based one-to-one harness: exact pixel comparison diagnostics, visible-control identity/geometry/accessibility comparison, state-directory matching, deterministic reference/native action capture, native keyboard probe recording, and synthetic self-tests (`python desktop-frontend/tools/test_verify.py`, 3 passed). Live ERP visual/business/performance parity remains unproven until capture completes.
- Default-feature SQLCipher desktop UI evidence now exists: the differential journey harness launches the normal `khitmee-desktop` binary (not `--no-default-features`) with the encrypted live catalog store, syncs `khitmee_ops.api.catalog.sync_catalog` from the isolated ERP, and drives the real UI through Slint MCP. The 2026-11-27 journey correctly fails on the footer/scroll quantity defect; it is evidence of encrypted startup and verifier detection, not sales-voucher qualification.

## Rules

- Fixture-only results must remain labelled fixture-only.
- A native draft submission must never be presented as a posted ERPNext voucher.
- ERPNext remains authoritative for financial transactions, inventory mutations, permissions and pricing.
- Acceptance criteria cannot be weakened to make a failing comparison pass.
- Once a screen is marked `verified` or `regression-locked`, later verification failures are regressions and block migration of new screens.
