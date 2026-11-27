# 0005 Item-search differential qualification

## Scope

Item-search behavior inside the Khitmee sales counter was qualified against the original web frontend in the isolated ERPNext environment (`http://127.0.0.1:18086`). This is behavioral/accessibility evidence only; it does not close full-screen visual or hardware qualification.

## Passing differential evidence

- `evidence/journeys/item-search-results.json` — complete ten-result list parity: item order, codes, names, stock, rates, expanded state, and count.
- `evidence/journeys/item-search-keyboard.json` — ArrowDown/ArrowDown/ArrowUp/Enter parity, including `aria-selected`/native selected-item index after every key and the selected item amount.
- `evidence/journeys/item-search-wrap-up.json` — ArrowUp wrap-around parity from the first result to the last result.
- `evidence/journeys/item-search-wrap-down.json` — ArrowDown wrap-around parity past the last result.
- `evidence/journeys/item-search-escape.json` — Escape clears/hides results like the browser search input; following Enter is a no-op in both implementations.
- `evidence/journeys/item-search-click.json` — clicking result index 2 selects the same item and clears the search input in both implementations.
- `evidence/journeys/item-search-repeat-scan.json` — repeated exact barcode scans increment the same draft line identically.
- `evidence/journeys/item-search-no-match.json` — unmatched Enter leaves status/draft state unchanged in both implementations.
- `evidence/journeys/item-search-warehouse.json` — switching to Shop 1 refreshes the encrypted catalog scope and matches browser stock exactly.
- `evidence/journeys/item-search-customer-price.json` — selecting Khit Customer 0002 refreshes customer-sensitive pricing and matches the customer-specific 11.11 rate exactly.
- `evidence/journeys/item-search-zero-stock.json` — Dist Hub 2 zero-stock rows match exactly.
- `evidence/journeys/item-search-burmese.json` — Burmese search text/result rendering matches exactly.

## Defects caught by the harness and fixed

1. Slint arrow keys initially inserted text or were consumed by `LineEdit`; the harness caught disappearing results. Fixed by mapping Slint private-use key codes and handling keys on the focused input.
2. Native selection initially clamped instead of wrapping; fixed to browser-compatible modulo navigation.
3. Native Escape retained the typed query and Enter incorrectly added an item; fixed by matching browser search-input clearing behavior.
4. Native mouse selection did not clear the query; fixed.
5. Native unmatched Enter cleared the ready status; fixed to preserve browser behavior.
6. Native result rows did not expose selected state accessibly; added `accessible-item-selected` and added it to the oracle.
7. Native warehouse changes retained stale stock from the startup scope; fixed by refreshing the encrypted catalog scope on warehouse/customer changes.

## Regression evidence

The sales-voucher default and validation differential journeys were rerun after these changes and passed. See `evidence/journeys/sales-voucher-differential.json` and `evidence/journeys/sales-voucher-validation.json` where present.

## Reproduction

Prepare the isolated edge fixtures first:

```bash
ERPNEXT_PASSWORD="$PW" python desktop-frontend/tools/setup_item_search_edge_fixtures.py
```

Then run any differential journey, for example:

```bash
PW=$(grep '^ADMIN_PASSWORD=' perf-suite/docker/.env | cut -d= -f2-)
export ERPNEXT_PASSWORD="$PW"
node desktop-frontend/tools/differential_journey.mjs \
  --out desktop-frontend/evidence/journeys/item-search-keyboard.json \
  --sequence '[{"op":"search","value":"Mekong"},{"op":"key","key":"ArrowDown"},{"op":"key","key":"ArrowDown"},{"op":"key","key":"ArrowUp"},{"op":"key","key":"Enter"}]'
```

## Remaining honest gaps

- Item-search visual styling has not been separately video-qualified after these behavior changes.
- Search latency is covered only in the sales-voucher benchmark, not as a dedicated cold-cache item-search matrix.
- Warehouse/customer catalog refresh is synchronous in the pilot UI thread; correctness is proven, but responsiveness under a slow network still needs asynchronous hardening and measurement.
