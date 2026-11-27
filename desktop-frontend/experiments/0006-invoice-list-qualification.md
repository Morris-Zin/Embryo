# 0006 Invoice-list differential qualification

## Passing evidence

- `evidence/journeys/invoice-list-initial.json` — first page parity against `/app/sales-invoice`: all 20 visible ERP rows match exactly for customer, status, posting date, due date, grand total and invoice ID; count text matches `20 of 1,000+`.
- `evidence/journeys/invoice-list-filter.json` — ID filter `04921` matches exactly: one row, same financial values, count `1 of 1`.

## Implementation changes

- Native invoice rows are authoritative ERPNext readbacks through `frappe.desk.reportview.get`, not fixture rows.
- Native row counts use `frappe.desk.reportview.get_count` with the same capped count semantics as ERPNext (`1,000+`).
- Invoice ID filtering uses the same server-side `name like` filter as the ERPNext list column.
- Native rows expose one machine-readable `list-item` accessibility row and visible text for every compared column.

## Defects caught by the harness and fixed

1. Native initially displayed fixture invoices rather than authoritative ERP rows.
2. Native rows omitted visible text/status even though placeholder accessibility rectangles existed.
3. Native had no count text.
4. Accessibility row extraction initially mis-grouped off-screen rows and the `Load More` control; fixed by exposing one semantic row per invoice.
5. Browser retained a stale saved ID filter; the harness now clears it before invoice-list comparison.

## Regression

The sales-voucher default differential journey was rerun after these changes and passed.

## Remaining honest gaps

- Pagination/load-more, sorting, column customization, bulk actions, opening an invoice, keyboard traversal and permission-denied behavior are not yet qualified.
- Visual styling is responsive/native but has not been separately video-qualified.
- Query latency and large-list scrolling/frame performance are not yet benchmarked.
- Invoice-list reads are synchronous in the pilot UI callback; correctness is proven, but asynchronous responsiveness under slow networks remains open.
