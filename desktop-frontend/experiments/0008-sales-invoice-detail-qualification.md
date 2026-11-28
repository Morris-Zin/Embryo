# 0008 Sales Invoice detail qualification

## Status

Initial live ERP parity passed on 2026-11-27. This is a read-only submitted-invoice detail qualification milestone, not completion of every Sales Invoice form interaction.

## Verified behavior

The differential harness starts web and native on the authoritative Sales Invoice list, filters both to `ACC-SINV-2026-04935`, opens the row through each UI, and compares the resulting detail form. The pass is non-vacuous: both sides expose the same invoice ID, customer title, status, 15 scalar fields, one item row, quantities, rate, amount, warehouse, totals, in-words value, rounding, advance, and outstanding amount.

Evidence:

- `desktop-frontend/evidence/journeys/invoice-detail-initial.json` — passed with zero differences.
- `desktop-frontend/evidence/journeys/invoice-detail-multi-item.json` — passed with zero differences for a three-item submitted invoice (`ACC-SINV-2026-04936`).
- `desktop-frontend/evidence/journeys/invoice-detail-return.json` — passed with zero differences for a submitted return invoice (`ACC-SINV-RET-2026-00114`).
- `desktop-frontend/evidence/journeys/invoice-list-permission-denied.json` — passed non-vacuously: browser and native both expose `not-permitted` for a user without Sales Invoice read permission.
- `desktop-frontend/evidence/journeys/invoice-detail-draft.json` — passed initial draft-state parity for visible actions and editable-field state (`ACC-SINV-2026-04937`). This is initial-state evidence only, not editing/saving/submission behavior evidence.
- `desktop-frontend/evidence/visual/invoice-detail-composite-list.png` — maximized browser/native list comparison after responsive and footer repairs.
- `desktop-frontend/evidence/visual/invoice-detail-composite-detail.png` — browser/native detail comparison after responsive and footer repairs.

Reproduction:

```bash
node desktop-frontend/tools/differential_journey.mjs \
  --screen invoice-detail \
  --out desktop-frontend/evidence/journeys/invoice-detail-initial.json \
  --sequence '[]'
```

The ERP password is read from the isolated environment in the normal qualification shell and is not committed.

## Implementation added

- Idempotent multi-item fixture setup in `desktop-frontend/tools/setup_invoice_detail_fixtures.py`.
- Native invoice-list permission denial surfacing and non-vacuous browser/native permission-state comparison.

- ERP `frappe.client.get` Sales Invoice detail read in `khitmee-erp`.
- Native `InvoiceDetailScreen` using live ERP data, including form fields, status, item grid, totals, and responsive use of window width.
- Invoice-list row activation opens the native detail screen.
- Differential invoice-detail oracle extracts the browser's semantic form controls and compares them with native accessibility evidence.

## Honest remaining gaps

- Full editable draft Sales Invoice interaction behavior is not implemented or qualified; current evidence covers initial draft action/editability state plus submitted read-only details, not editing, validation, saving, submission, or item-grid mutation.
- Tab interactions (`Payments`, `Address & Contact`, `Terms`, `More Info`, `Connections`) are visually represented but not behaviorally qualified.
- Cancel, email, assignment, attachment, tag, share, print, and connection actions are not implemented.
- Paid/overdue statuses, direct detail-route permission denial, keyboard/form-focus behavior, editing/saving/submission journeys, item-grid mutation, comprehensive visual state coverage, and performance evidence remain open.
- Cross-process memory/frame/network-byte measurements and hardware qualification remain open.
## Editable draft save/submit milestone

`evidence/journeys/invoice-detail-editable-draft.json` passed with zero differential failures. Independent web and native draft fixtures were edited with the same Payment Due Date, exposed the same `Not Saved` dirty state and Save/Submit action transitions, saved through ERPNext, submitted through ERPNext, and matched authoritative readback for docstatus, customer, due date, update-stock flag, warehouse, quantities, totals, outstanding amount and first-line item outcome. Posting Time is intentionally excluded from exact cross-document equality because each implementation has an independent invoice and ERPNext advances posting time on save/submit; the oracle checks monotonic progression instead. This milestone does not yet prove item-grid editing, validation errors, keyboard/focus behavior, tabs/actions, direct-detail permission denial, paid/overdue states, comprehensive visual states, or performance.
## Item-grid quantity edit milestone

`evidence/journeys/invoice-detail-item-grid-edit.json` passed with zero differential failures. Web and native independently opened the first item row into editable controls, exposed matching item code/qty/rate/amount/warehouse editor values, changed quantity from 1.000 to 2.000 with amount recalculated from 18.46 to 36.92, entered matching dirty Save states, saved, submitted, and matched authoritative ERP readback for the item and financial totals. This proves first-row quantity mutation only; row add/remove, item-code changes, rate editing and validation-error behavior remain open.
## Direct detail permission milestone

`evidence/journeys/invoice-detail-permission-denied.json` passed with zero differential failures for `noperm.counter@example.com` opening `ACC-SINV-2026-04935` directly. Browser and native both exposed a non-vacuous `not-permitted` state, no invoice fields, no item rows, no document actions, and no editable controls. This is direct-detail permission evidence in addition to the earlier invoice-list denial evidence.
## Mandatory validation milestone

`evidence/journeys/invoice-detail-mandatory-validation.json` passed with zero differential failures. Browser and native both allowed Payment Due Date to be cleared into the same `Not Saved` state, exposed Save rather than Submit, rejected save with the same missing-fields state and validation message (`Payment Due Date` plus payment-schedule due date), and preserved matching draft document outcomes because neither invalid document was persisted. This is mandatory-field validation evidence only; server-side validation and numeric/item validation remain open.
## Paid and overdue state milestone

`evidence/journeys/invoice-detail-paid.json` and `evidence/journeys/invoice-detail-overdue.json` passed with zero differential failures. Browser and native both exposed `Paid` with zero outstanding amount and `Overdue` with `$ 18.46` outstanding, with matching read-only actions (`Create`, `Cancel`) and matching detail fields/items. The paid invoice is a genuine POS cash-payment submission (`ACC-SINV-2026-04961`). The overdue invoice (`ACC-SINV-2026-04964`) is display-state evidence only: because ERPNext rejects backdated due dates at creation, the isolated fixture seed updates the due date/payment schedule/status after a genuine submission; it is not evidence of an overdue aging workflow.
