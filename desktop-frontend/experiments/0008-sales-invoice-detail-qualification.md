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
## Item rate and multi-row editing milestone

`evidence/journeys/invoice-detail-item-rate-edit.json` passed with zero differential failures for editing the first row rate to `20`, recalculating amount/grand total, saving, submitting, and matching authoritative item outcomes in independent invoices (`ACC-SINV-2026-04967` web, `ACC-SINV-2026-04968` native).

`evidence/journeys/invoice-detail-multi-row-edit.json` passed with zero differential failures for opening the second row of a three-row draft, changing quantity `2` to `4`, saving, submitting, and matching the complete authoritative three-item arrays in independent invoices (`ACC-SINV-2026-04974` web, `ACC-SINV-2026-04975` native). This extends item-grid evidence beyond the first row, but row add/remove, item-code substitution, and invalid numeric validation remain open.
## Zero-quantity validation milestone

`evidence/journeys/invoice-detail-zero-qty-validation.json` passed with zero differential failures. Browser and native both accepted a zero item quantity into the dirty editor, recalculated amount/totals to zero, exposed Save rather than Submit, rejected save with the same invalid-quantity state and message (`Row #1: Quantity for Item KM-00001 cannot be zero.`), closed the row editor, and did not persist the invalid document. This proves representative numeric item validation; it does not exhaust every ERPNext validation rule.
## Item-row add/delete milestone

`evidence/journeys/invoice-detail-row-add-delete.json` passed with zero differential failures. Browser and native both started with one row and draft Add row/Add multiple/Upload grid actions; Add row created a second expanded blank row with empty item/qty/rate/amount, default warehouse, `Not Saved` status and Save action; selecting row 2 exposed Delete row/Edit row/Duplicate row/Upload; Delete row removed it and returned to one row while preserving the dirty state. This proves row add/select/delete UI mutation. It does not yet prove saving a newly populated row or item-code substitution.
## Item-code row population milestone

`evidence/journeys/invoice-detail-item-code-add.json` passed with zero differential failures. Browser and native both added a blank second row, selected `KM-00002`, populated quantity `1.000`, rate `0.53`, amount `0.53` and warehouse, recalculated total quantity `2` and grand total `$ 18.99`, saved, submitted, and matched complete authoritative two-item arrays in independent invoices (`ACC-SINV-2026-04974` style outcome pair: web `ACC-SINV-2026-05016`, native `ACC-SINV-2026-05017`). This proves newly populated row addition and item-code substitution for catalog-backed exact item selection; advanced autocomplete search/create-item behavior remains outside this milestone.
## Keyboard item-editing milestone

`evidence/journeys/invoice-detail-keyboard-edit.json` passed with zero differential failures using actual keyboard events rather than direct value injection. After adding/selecting `KM-00002`, browser and native both replaced the selected quantity with keyboard input `2` (`2.000`), then replaced rate with `3` (`3.00`), recalculated amount `6.00`, saved, submitted, and matched complete authoritative outcomes in independent invoices (`ACC-SINV-2026-05036` web, `ACC-SINV-2026-05037` native): second row qty `2`, rate `3`, amount `6`, grand total `24.46`. The verifier first exposed a real native defect where typed input was inserted into the existing text (`1.002`); native grid inputs now select their contents on click like the browser. Focus-trap/order coverage remains open.
## Cancel document-action milestone

`evidence/journeys/invoice-detail-cancel.json` passed with zero differential failures. Browser and native both opened the same cancellation confirmation (`Permanently Cancel <invoice>?`) with No/Yes actions; after Yes both reached `Cancelled`, hid document actions, closed the confirmation, and matched authoritative ERP readback with `docstatus: 2` in independent invoices. The first verifier run exposed a real native post-cancel action-state defect (Cancel/Create remained visible); it was fixed before the passing run. Create/Preview/Get Items From behavior and tabs remain open.
## Initial performance benchmark milestone

`evidence/benchmarks/invoice-detail-comparison.json` records three matched cold-open and item-grid interaction samples for draft `ACC-SINV-2026-04937` with the 10,000-row catalog/invoice workload. Median direct-detail cold open was 1654 ms web versus 1916 ms native. Median first-row expansion was 103 ms web versus 125 ms native. Median quantity-edit-to-total-update latency was 195 ms web versus 56 ms native. Native resident memory was about 47.6 MB; cross-process browser memory, frame scrolling, network bytes, hardware input/print latency, and low-end Windows remain pending, so this is useful evidence but not full performance qualification.
## Sectioned responsive form milestone

The native detail form no longer uses a flat field grid or overlapping header buttons. It now follows the browser document structure more closely: primary details, collapsed currency section, item scan/warehouse controls, item grid, grid actions, collapsed taxes section, and totals below the grid. `evidence/journeys/invoice-detail-layout-regression.json` reran the authoritative editable-draft save/submit journey after this layout change and passed with zero differential failures. Current visual evidence is `evidence/visual/invoice-detail-composite-current.png`. Remaining visual gaps are real checkbox/link control styling, tab-dependent content, the right document sidebar, dropdown actions, and comprehensive focus/video evidence.
## Tab navigation milestone

`evidence/journeys/invoice-detail-tabs.json` passes with zero differential failures. Web and native both move through Details, Payments, Address & Contact, Terms, More Info, Connections, and back to Details with matching active-tab state and visible section headings. The verifier initially exposed an oracle defect: hidden browser tab content was being read from the DOM even though users could not see it; extraction now filters hidden controls/rows. Remaining tab work is authoritative field values and editing inside those tabs, especially payment schedule, address/contact, terms, tax, accounting, print, subscription, and connection data.
## Server-validation defect currently under repair

The new due-date validation harness caught real native/server-integration defects: ERP validation errors were not surfaced as validation UI, native validation text initially could not be generically extracted, and the browser clears `In Words` after the failed save while native state handling diverged. The current `invoice-detail-server-validation.json` remains failing and must not be counted as passed evidence. Repair is in progress; comprehensive server-side validation remains pending.
