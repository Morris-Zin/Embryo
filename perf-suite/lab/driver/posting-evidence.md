# Capturing committed seasonal postings

The protected runner calls `capture_before(req, target, message)` once after deploying an arm and before opening the timed telemetry window. After the external load stops, it calls `capture(req, target, message, events, reference_path)`. Both reads check the guest boot identity, disposable namespace, database volume and exclusive running project. Each boundary uses a consistent, read-only MariaDB transaction through the pinned SSH connection. The operator holds the evidence and the candidate application cannot generate the comparison result.

The opening scan warms database pages. Perform the same scan at the same point for every baseline, candidate and independent stock replay. Exclude both inventory scans and the raw database export from the timed load and resource metrics.

### Generating a reference

Use the protected driver installation and operator-owned files. The envelope is the ordinary harness envelope with its validated request, isolated provision receipt and pinned SSH/guest identity. The actor password JSON maps each recorded cashier email to the password for that synthetic user. Restrict the file to the operator. `stock-source.json` records the upstream Git revision, backend Docker image ID, and `controllers` map of relative paths and SHA-256 values from that pinned ERPNext revision. All four paths are listed in `posting_evidence.CONTROLLERS`.

```sh
python3 native_replay.py pin-stock --upstream-checkout "$TRUSTED_ERPNEXT_CHECKOUT" \
  --upstream-revision "$FIXTURE_ERPNEXT_COMMIT" --backend-image-id "$FIXTURE_BACKEND_IMAGE_ID" \
  --output stock-source.json
```

1. On the original disposable arm, capture its opening before issuing any vouchers. The normal seasonal driver already does this. For an externally controlled original run use the following command. After its load finishes, point the closing observation at its original parsed events JSON. Both observation commands perform read-only queries and exclusively create their receipts.

   ```sh
   python3 native_replay.py source-before --envelope original-envelope.json
   python3 native_replay.py source-after --envelope original-envelope.json --events seasonal-events.json
   ```

2. Extract the submitted inputs. k6 emits an allowlisted input object in each start event including unrounded requested quantities, UOM, native purchasing rates and the selected sale or purchasing fields. It logs the rows once. Missing input rows, duplicate client IDs, invalid purchase order, mismatched basket hashes, or unexplained rejections stop extraction. The optional source close provides independent per-item stock ledger order and receipt-to-invoice dependencies. Extraction follows a topological order compatible with those observations. A cycle rejects a serial reconstruction. `--ordered-ids FILE` accepts an explicit JSON array containing each committed client ID once. Without a source close the tool can produce a diagnostic from this list or recorded start-event order.

   ```sh
   python3 native_replay.py extract --events seasonal-events.json \
     --source-before original-arm/posting-before.json --source-after original-arm/posting-after.json \
     --posting-date 2026-11-27 --output replay-plan.json
   ```

3. Provision a fresh baseline namespace on the same protected fixture, separate from the original measured arm. Stop other projects on the dedicated guest. The replay checks the entire opening financial inventory, Bin fingerprint, fixture, seed, independent namespace, source controller hashes and image before posting. The plan uses distinct chronological posting seconds within the declared business date. It authenticates the original actors using their fixture passwords, applies normal stock document validation and permissions, and submits the maintained classic parity sale/transfer builders plus standard ERPNext purchase receipt/invoice documents. Invoice rows bind to the new receipt's item names. Each operation commits before the next starts. A failed partial replay requires a fresh arm. Keep the workload generator and its scans outside the measured timing window.

   ```sh
   python3 native_replay.py run --envelope native-arm-envelope.json \
     --plan replay-plan.json --plan-sha256 "$PLAN_SHA256" \
     --stock-source stock-source.json --stock-source-sha256 "$UPSTREAM_CONTRACT_SHA256" \
     --actor-passwords /protected/native-actors.json --output /protected/stock-seed-42.json.gz
   ```

4. The writer reads the committed native rows back from MariaDB. It binds the actual per-item stock ledger sequence to the source close when supplied. Each qualifying candidate must independently match that same observed financial sequence, the complete closing Bin fingerprint and the pinned voucher results. Disjoint items can have different wall-clock ordering. A serial plan lacking a source close is explicitly diagnostic, and the full seasonal runner refuses it. The CLI returns the reference path and SHA-256 for `seasonal.json.references[str(trial_seed)]`. Install the reviewed pair in a fresh protected release. Existing seasonal receipts created before replay input logging cannot establish the original rows.

The native stock reference uses this shape. The examples below show the structure. The actual file contains all four controller pins and all completed operations.

```text
schema = "khitmee-stock-posting-capture-v1"
fixture_sha256, seed, baseline_commit, namespace, upstream_revision, order_scope
order = {method, streams, movements, sha256}
stock_source = {backend_image_id, controllers: {ERPNext-relative path: SHA-256}}
bin = {scope: "whole-run", before: {count, sha256}, after: {count, sha256}}
operations[client_voucher_id] = {kind, actor_user, basket_hash, snapshot}
snapshot = {doc, items, gl, gl_summary, sle, sle_summary, bin}
snapshot.payments = ordered tender rows for Sales Invoice
snapshot.doc.payment_ledger = sorted party/account/amount and linked-voucher rows
```

`before` and `after` Bin fingerprints cover every item/warehouse's actual, projected and reserved quantity, valuation rate and stock value. Concurrent changes to a shared Bin cannot be attributed to one invoice from two boundary reads. Each snapshot explicitly identifies the whole-run Bin comparison. Per-voucher SLE quantities, values, valuation rates and child links remain independently compared. Concurrent permutations can produce a genuine per-voucher difference, and the finance verdict retains it. A reference with extra possible completions still has to match the actual whole-run closing stock valuation.

The 13-table inventory includes every parent and item table for the four voucher types, POS payments, GL, SLE, Payment Ledger, and client reservations. The ending scan removes only the rows attributed to confirmed voucher names. Each remaining table must preserve its opening row count and the four summed 64-bit limbs of SHA-256 over its captured fields, including row identities. This catches an extra committed document under a rejected client ID, orphan child/ledger rows, historical changes to captured fields, duplicate allocations, and missing rows. Exact comparison to the native stock capture checks the contents within each confirmed voucher. Posting captures preserve decimal values as strings and validate payable, tender, transfer and purchase-detail linkage. A confirmed lost-reply retry must independently have the original voucher and final voucher names, one submitted database row, valid client reservation for sales/transfers, and the matched GL/stock effects.

`posting-before.json` and `posting-after.json` retain the inventory and Bin evidence in the arm folder. The financial verifier consumes the returned operations and the seasonal driver retains its verdict. A full native reference trace for the requested seed is a prerequisite for running the comparison.

For a retried voucher, the observer records the two confirmations and verifies the final committed rows for its client ID. The reported per-voucher row deltas describe those final rows. Physical table inventory is sampled at the beginning and end of the whole workload.
