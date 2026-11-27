# Khitmee desktop-client experiment

This directory contains a proof-of-concept resource-efficient desktop architecture for the existing Khitmee counter. It is deliberately additive: ERPNext remains the financial and inventory authority, and the normal browser counter continues to work when the experiment is disabled.

## Components

- `khitmee-catalog-service` — a small Rust loopback HTTP service. It stores the permission-filtered counter catalog in SQLCipher, rebuilds an in-memory exact-barcode/prefix/substring index, and answers autocomplete requests.
- `tauri-shell/` — a minimal Tauri v2 shell that opens the existing `/khitmee_sales` interface. The remote web content receives **no Tauri native capabilities**. Local processing is reached through the bounded loopback service rather than exposing Rust IPC to ERP pages.
- `apps/khitmee_ops/public/js/catalog_bridge.js` — opt-in browser bridge. It synchronizes the server catalog, forwards it to the Rust service (`?khitmee_catalog=rust`), or stores it in IndexedDB for the browser-side control (`?khitmee_catalog=browser`).
- `khitmee_ops.api.catalog.sync_catalog` — server-side permission-checked reset/incremental-sync endpoint.

The default mode is off. Nothing syncs unless the URL or `localStorage["khitmee-catalog-mode"]` selects `rust` or `browser`.

## Build and test on Linux

```bash
cd desktop-client
cargo fmt --check
cargo test
cargo build --release
./target/release/khitmee-catalog-service benchmark --rows 5000 --iterations 1000
```

Run the local service for the isolated benchmark stack:

```bash
./target/release/khitmee-catalog-service serve \
  --db /tmp/khitmee-catalog.sqlcipher.db \
  --key /tmp/khitmee-catalog.key \
  --port 18422 \
  --allowed-origin http://127.0.0.1:18086
```

Optional token hardening: start with `--token <random>` or `KHITMEE_DESKTOP_TOKEN`, and set the same value as `khitmee_desktop_token` in the bench site config. This token is not a privileged server credential; it only reduces casual cross-site/local abuse of the loopback catalog API.

## Tauri shell

`tauri-shell` is a separate crate because Tauri needs platform WebView dependencies. Ubuntu 22.04+ development packages are listed in <https://v2.tauri.app/start/prerequisites/>. After installing them:

```bash
cd desktop-client/tauri-shell
cargo check
cargo build --release
```

Runtime environment:

```bash
KHITMEE_SERVER_URL=http://127.0.0.1:18086/khitmee_sales
KHITMEE_DESKTOP_MODE=rust   # rust local catalog; basic means plain wrapper
KHITMEE_ALLOWED_ORIGINS=http://127.0.0.1:18086
```

Windows builds should be produced and measured on a real low-end Windows cashier machine. Do not treat Linux WebKitGTK results as representative of Windows WebView2.

## Sync and correctness model

The first sync is a bounded catalog reset. Later syncs pass the previous server-issued state and receive rows changed in `Item`, `Item Barcode`, `Item Price`, warehouse `Bin`, or the delete/rename tombstone log during an overlapping 10-second window. Overlap makes updates idempotent; clients upsert by item code. A schema/scope/permission revision mismatch, a state older than 24 hours, or crossing a date-based price-validity boundary forces a reset. Permission-aware deployments always use full replacement resets so hidden item codes are not disclosed through deletion markers. Catalog scopes are also bound to the ERP origin.

Rates and quantities are advisory display snapshots only. `submit_sale` already resolves authoritative ERPNext prices server-side unless explicit override permission is used, validates item/warehouse/customer permissions, and validates inventory/financial posting through ERPNext. The client must treat local rows as disposable cache.

Known PoC limits:

- Direct SQL edits that delete rows without touching `modified` can be missed until the 24-hour reset. Normal Frappe writes update the participating rows.
- Local display prices use the selected price list but not customer-specific pricing in the default bridge scope. The server remains authoritative at submission.
- Stock and price display can be up to `stale_after_seconds` old, plus at most one sync interval after a midnight price-validity boundary; stale sync failure falls back to server search rather than enabling offline checkout.
- The sync endpoint scans `modified` columns. This is intentionally measured before proposing any index or trigger because new write amplification may outweigh the savings.

## Security summary

See `../docs/desktop-client-experiment.md` for the full threat model. The important constraints are:

- No shared encryption key or privileged credential is embedded in either binary.
- SQLCipher uses a random per-device key. On Windows the key blob is protected with user-scope DPAPI; Linux uses a `0600` key file only as a development convenience.
- SQLCipher protects the powered-off/at-rest file. It cannot stop an authorized cashier, malware running as that user, or someone reverse-engineering the unlocked client from extracting the catalog.
- Only public counter fields are synchronized: item identity/name/UOM/group/brand/barcodes, selling-rate display snapshot, and quantity display snapshot. Cost, supplier, valuation, purchase, credential, and financial posting data are excluded.
- The Tauri remote page receives an empty capability set. The loopback service uses exact-origin CORS, JSON content-type enforcement, request-size limits, optional token authentication, and binds only to `127.0.0.1`.
