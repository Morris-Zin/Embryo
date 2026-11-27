# 0007 Stock-transfer differential qualification

## Passing evidence

- `evidence/journeys/stock-transfer-initial.json` — initial native/web state parity.
- `evidence/journeys/stock-transfer-validation.json` — missing-target validation parity.
- `evidence/journeys/stock-transfer-same-warehouse.json` — identical source/target validation parity.
- `evidence/journeys/stock-transfer-interactions.json` — search, row selection, quantity increase/decrease/edit, scrolling and clear parity.
- `evidence/journeys/stock-transfer-keyboard.json` — search keyboard traversal parity.
- `evidence/journeys/stock-transfer-warehouse-search.json` — source-warehouse-sensitive search result parity.
- `evidence/journeys/stock-transfer-submission.json` — authoritative direct transfer parity. Browser and native independently submitted distinct Stock Entries with matching docstatus, warehouses, item, quantity, UOM and transfer rows.
- `evidence/journeys/stock-transfer-dispatch-validation.json` — ERP validation error surface parity for a non-transit dispatch target.
- `evidence/journeys/stock-transfer-dispatch-success.json` — authoritative dispatch-to-transit parity, including `add_to_transit` in ERP readback.
- `evidence/journeys/stock-transfer-receive-validation.json` — receive-mode outgoing-entry requirement and enabled/disabled state parity.
- `evidence/journeys/stock-transfer-transit-receive.json` — full dispatch then receive chain. Each side used its own dynamically captured outgoing Stock Entry and ERP readback matched exactly.
- `evidence/journeys/stock-transfer-permission-denied.json` — browser and native both surface `No permission for Stock Entry` for the isolated no-permission counter user.
- `evidence/journeys/stock-transfer-idempotency.json` — two concurrent submissions with one `client_voucher_id` resolved to the same Stock Entry (`MAT-STE-2026-00245`).
- `evidence/benchmarks/stock-transfer-comparison.json` — identical 10,000-row search/scan workload: native Slint p50 search 27.4 ms versus 123.9 ms original web and 126.5 ms PR #8 loopback; native p50 scan 20.8 ms versus 29.6 ms original web and 30.4 ms PR #8 loopback.
- `evidence/visual/stock-transfer-composite-initial.png`, `evidence/visual/stock-transfer-composite-scan.png` and `evidence/visual/stock-transfer-composite-submitted.png` — maximized native/browser visual comparison after responsive repair.
- `evidence/visual/stock-transfer-visual-erp-outcomes.json` — visual-flow ERP readback for native `MAT-STE-2026-00250` and browser `MAT-STE-2026-00251`, both submitted Material Transfers with matching warehouses, item, quantity, UOM and valuation.

## Reproduction commands

Run the principal direct-transfer proof with the isolated ERP administrator credential supplied through `ERPNEXT_PASSWORD`:

```bash
node desktop-frontend/tools/differential_journey.mjs \n  --screen stock-transfer \n  --out desktop-frontend/evidence/journeys/stock-transfer-submission.json \n  --sequence '[{"op":"target-warehouse","value":"Shop 1"},{"op":"scan","value":"8850000000010"},{"op":"f9"}]'
```

Run the full dispatch/receive chain:

```bash
node desktop-frontend/tools/differential_journey.mjs \n  --screen stock-transfer \n  --out desktop-frontend/evidence/journeys/stock-transfer-transit-receive.json \n  --sequence '[{"op":"target-warehouse","value":"Goods In Transit"},{"op":"transfer-mode","value":"Dispatch to transit"},{"op":"scan","value":"8850000000010"},{"op":"f9"},{"op":"source-warehouse","value":"Goods In Transit"},{"op":"target-warehouse","value":"Shop 1"},{"op":"transfer-mode","value":"Receive from transit"},{"op":"scan","value":"8850000000010"},{"op":"outgoing-entry","value":"own-last-voucher"},{"op":"f9"}]'
```

Run permission and idempotency proofs:

```bash
node desktop-frontend/tools/differential_journey.mjs \n  --screen stock-transfer --scenario permission-denied \n  --username noperm.counter@example.com --password 'NoPermPass123!' \n  --out desktop-frontend/evidence/journeys/stock-transfer-permission-denied.json \n  --sequence '[{"op":"target-warehouse","value":"Shop 1"},{"op":"scan","value":"8850000000010"},{"op":"f9"},{"op":"wait","ms":1500}]'

python desktop-frontend/tools/idempotency_check.py --transfer \n  --password "$ADMIN_PASSWORD" \n  --output desktop-frontend/evidence/journeys/stock-transfer-idempotency.json
```

Run the comparative interaction benchmark:

```bash
KHITMEE_DESKTOP_TOKEN="$ISOLATED_DESKTOP_TOKEN" ERPNEXT_PASSWORD="$ADMIN_PASSWORD" \n  node desktop-frontend/tools/benchmark_compare.mjs --screen stock-transfer --iterations 10 \n  --out desktop-frontend/evidence/benchmarks/stock-transfer-comparison.json
```

## Implementation changes

- Native stock transfer now uses real Slint `ComboBox` controls for source/target warehouse and transfer mode instead of static browser-shaped placeholders.
- Target warehouse and transfer mode callbacks update migration state and authoritative submission payload fields.
- Native validation mirrors browser validation for missing source, missing target, identical source/target and missing outgoing entry in receive mode.
- Receive-mode outgoing entry is functionally and accessibly enabled only for `Receive from transit`.
- Direct transfer totals display quantity rather than money because browser stock transfer uses `showRates: false`.
- Native ERP error formatting now follows the browser's concise `_error_message`/`exception`/`message` ordering instead of exposing raw `_server_messages` JSON.
- The invoice-list query no longer runs when the native app starts on sales or stock-transfer screens; this avoids irrelevant permission failures and unnecessary startup reads.
- Stock-transfer business readback compares `Stock Entry` docstatus, source/target warehouses, `add_to_transit`, total quantity and item transfer rows.
- The differential harness supports stock-transfer source/target/mode/outgoing-entry actions and can dynamically use each side's prior submitted dispatch voucher in the receive leg.
- The generic idempotency proof now supports `submit_transfer` as well as `submit_sale`.

## Defects caught by the harness and fixed

1. The native combo-selection harness matched the source warehouse combo when the requested target label equaled the current source value; exact accessible-label matching now takes priority.
2. The outgoing-entry oracle initially read the static text label instead of the text input; extraction now requires `TextInput` role.
3. The receive-mode input's accessibility metadata did not expose its enabled state even though visual state changed; it now exposes the semantic enabled state explicitly.
4. Native ERP validation failures displayed raw response JSON while the browser displayed the concise ERP exception; native formatting now matches.
5. Native startup refreshed Sales Invoice rows even when launching directly into stock transfer, causing an irrelevant invoice-permission status for the no-permission user.
6. The oracle incorrectly treated independently generated browser/native outgoing voucher IDs as a mismatch; voucher-shaped values are normalized while the authoritative ERP readback still verifies each actual voucher.
7. The source-warehouse oracle inferred state by scanning all combo values and could confuse target/source values; it now reads the `Source warehouse` combo directly.
8. Visual testing found the transfer instance still fixed at 1180×760 inside a maximized window, leaving large unused areas; it now uses the parent window size and expands the lines table vertically.
9. Visual testing found the first scanned row hidden behind the fixed footer; transfer details now use a compact responsive two-column layout and the lines panel starts high enough for the first row to be visible.
10. Visual testing found native transfer rows displaying sales rate/amount `18.46`; transfer rows now display non-monetary dashes like the browser while authoritative ERP valuation remains server-calculated.

## Regression

After the strengthened baseline oracle was introduced, invoice-list initial/filter/load-more/sort, sales-voucher differential and item-search keyboard/result regressions were rerun and passed.

## Remaining honest gaps

- Cross-process memory, frame scrolling, network-byte totals, hardware barcode/printer latency and low-end Windows performance remain unmeasured.
- Real printer hardware remains unproven; evidence is the same print-companion dry-run path used by the browser. The dry-run receipt labels the target warehouse as `Customer` in both implementations; this is an upstream print-template semantic concern, not a native/browser parity difference.
- Real barcode scanner hardware remains unproven; barcode input is simulated through the same keyboard/text path as prior screen evidence.
- Exact browser geometry is intentionally not the oracle. Styling, responsive use of space, state, keyboard behavior, validation and business outcome are the parity criteria.
- Visual/video review for stock transfer is still required before closing the screen's final evidence package.
