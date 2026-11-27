# Khitmee Ops

Khitmee Ops is a Frappe v16 application for fast counter sales, warehouse transfers, local receipt printing, and a bounded-scan Gross Profit variant.

## Contents

- `khitmee_ops.api.search` — permission-checked counter item search and counter context APIs.
- `khitmee_ops.api.vouchers` — retry-safe Sales Invoice and Stock Entry submission APIs.
- `Fast Gross Profit` — a Script Report that preserves ERPNext Gross Profit semantics while fetching only matched Stock Ledger Entry pairs and scoped Packed Item rows.
- `/khitmee_sales` and `/khitmee_transfer` — standalone keyboard-first counter pages that do not load the Desk bundle.
- `bench khitmee-setup-indexes` — idempotent composite-index and item-search-index setup helper.

## Install

From the bench directory:

```bash
bench get-app /path/to/khitmee_ops
bench --site <site> install-app khitmee_ops
bench --site <site> khitmee-setup-indexes --rebuild-item-search
bench build --app khitmee_ops
```

The app uses flit through `pyproject.toml`, matching Frappe v16 packaging conventions; no legacy `setup.py` is required.

## Local print companion

The counter pages first post receipt JSON to `http://127.0.0.1:18421/print` with the `X-Khitmee-Print-Token` header when a token is configured. If the companion is unavailable, the browser print dialog opens with a receipt-only fallback view.
