# 0008 Sales Invoice detail qualification

## Status

Initial live ERP parity passed on 2026-11-27. This is a read-only submitted-invoice detail qualification milestone, not completion of every Sales Invoice form interaction.

## Verified behavior

The differential harness starts web and native on the authoritative Sales Invoice list, filters both to `ACC-SINV-2026-04935`, opens the row through each UI, and compares the resulting detail form. The pass is non-vacuous: both sides expose the same invoice ID, customer title, status, 15 scalar fields, one item row, quantities, rate, amount, warehouse, totals, in-words value, rounding, advance, and outstanding amount.

Evidence:

- `desktop-frontend/evidence/journeys/invoice-detail-initial.json` — passed with zero differences.

Reproduction:

```bash
node desktop-frontend/tools/differential_journey.mjs \
  --screen invoice-detail \
  --out desktop-frontend/evidence/journeys/invoice-detail-initial.json \
  --sequence '[]'
```

The ERP password is read from the isolated environment in the normal qualification shell and is not committed.

## Implementation added

- ERP `frappe.client.get` Sales Invoice detail read in `khitmee-erp`.
- Native `InvoiceDetailScreen` using live ERP data, including form fields, status, item grid, totals, and responsive use of window width.
- Invoice-list row activation opens the native detail screen.
- Differential invoice-detail oracle extracts the browser's semantic form controls and compares them with native accessibility evidence.

## Honest remaining gaps

- Full editable draft Sales Invoice form behavior is not implemented or qualified; current evidence covers a submitted read-only invoice.
- Tab interactions (`Payments`, `Address & Contact`, `Terms`, `More Info`, `Connections`) are visually represented but not behaviorally qualified.
- Cancel, email, assignment, attachment, tag, share, print, and connection actions are not implemented.
- Multi-item invoices, return invoices, paid/overdue statuses, permission denial, keyboard/form-focus behavior, visual video evidence, and performance evidence remain open.
- Cross-process memory/frame/network-byte measurements and hardware qualification remain open.
