# 0006 Invoice-list differential qualification

## Passing evidence

- `evidence/journeys/invoice-list-initial.json` — first page parity against `/app/sales-invoice`: all 20 visible ERP rows match exactly for customer, status, posting date, due date, grand total and invoice ID; count text matches `20 of 1,000+`.
- `evidence/journeys/invoice-list-filter.json` — ID filter `04921` matches exactly: one row, same financial values, count `1 of 1`.
- `evidence/journeys/invoice-list-load-more.json` — Load More parity matches exactly: both sides expose the same 40 rows in the same order and count `40 of 1,000+`.
- `evidence/journeys/invoice-list-sort-asc.json` — sort-order toggle parity matches exactly: both sides sort ascending by invoice ID with the same first page and row order.

## Implementation changes

- Native invoice rows are authoritative ERPNext readbacks through `frappe.desk.reportview.get`, not fixture rows.
- Native row counts use `frappe.desk.reportview.get_count` with the same capped count semantics as ERPNext (`1,000+`).
- Invoice ID filtering uses the same server-side `name like` filter as the ERPNext list column.
- Native rows expose one machine-readable `list-item` accessibility row and visible text for every compared column.
- Native Load More increases the authoritative ERP page length by 20 and the row list is scrollable through `Flickable`.
- Native sorting uses the authoritative ERP reportview `name asc`/`name desc` ordering and the visible sort-direction control toggles it.
- The harness normalizes the browser sort direction to descending before each invoice journey so user-specific ERP sort state cannot leak between runs.
- Invoice MCP extraction uses targeted `query_element_descendants` because `get_element_tree` is hard-capped at 1,000 elements.
- Because native rows outside the scroll viewport are clipped from accessibility extraction, invoice observation now scrolls through the native list, deduplicates semantic rows by invoice ID, and restores the viewport before comparison. This verifies loaded-row parity without requiring off-screen rows to be visually rendered.

## Defects caught by the harness and fixed

1. Native initially displayed fixture invoices rather than authoritative ERP rows.
2. Native rows omitted visible text/status even though placeholder accessibility rectangles existed.
3. Native had no count text.
4. Accessibility row extraction initially mis-grouped off-screen rows and the `Load More` control; fixed by exposing one semantic row per invoice.
5. Browser retained a stale saved ID filter; the harness now clears it before invoice-list comparison.
6. Native `invoice_model` silently capped every ERP result set at 20 rows even after Load More; the harness caught this and the cap was removed.
7. Slint MCP tree extraction truncated large lists at 1,000 total elements; the verifier now uses targeted descendant queries rather than weakening row expectations.
8. Naive descendant queries duplicated results when an extra `matchDescendants` stage was added; corrected to use the query root's built-in descendant traversal.
9. Adding the visible `ID` sort field exposed an oracle ambiguity: extraction initially read the sort control instead of the `TextInput` filter. The verifier now targets the input by role rather than weakening value comparison.

## Regression

The sales-voucher default differential journey was rerun after these changes and passed.

## Remaining honest gaps

- Sort field selection, column customization, bulk actions, opening an invoice, keyboard traversal and permission-denied behavior are not yet qualified.
- Visual styling is responsive/native but has not been separately video-qualified.
- Query latency and large-list scrolling/frame performance are not yet benchmarked.
- Invoice-list reads are synchronous in the pilot UI callback; correctness is proven, but asynchronous responsiveness under slow networks remains open.
