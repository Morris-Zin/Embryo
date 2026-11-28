# Seasonal benchmark scorecard

The tested cashier guest had 2 vCPUs and 2 GiB RAM. On the seeded year fixture, e208 offered one mixed minute at the original shared numbering policy. It confirmed **69 of 100 by second 60** and all 100 by second 81.753. Searches and manager reads ran concurrently. The manager served a verified external snapshot. Manager timings below predate the per-request oracle session check. Cache-hit latency under mixed load with that check enabled remains unmeasured.

| Voucher | Required by 60 s | Confirmed by 60 s | Confirmed after 60 s |
| --- | ---: | ---: | ---: |
| Sales | 80 | 56 | 24 |
| Transfers | 10 | 5 | 5 |
| Purchase receipts | 5 | 5 | 0 |
| Purchase invoices | 5 | 3 | 2 |

| Objective | Result |
| --- | --- |
| Correct posting and stock valuation | Independent database verification found every voucher exactly once. Immediate FIFO and opening-to-closing stock conservation passed. |
| Cashier confirmation p95 below 1.5 s | Missed for all four types, from 14.78 s for sales to 24.73 s for purchase invoices. |
| Search p95 below 300 ms | 12.12 s. |
| Manager summary p95 below 500 ms and first 50 detail rows below 2 s | 12 summary reads and two detail workflows succeeded. Summary p95 was 2 ms. Both first pages took 1 ms. |
| Sustained minimum hardware | Open. Qualification requires the 60-minute baseline, 15 minutes at 125% load, and overload and recovery phases. |

The [measurement extract](benchmark/e208-results.json) includes the digest of the retained raw minute summary. A previous mixed minute lost a receipt to a database deadlock and its dependent invoice could not proceed. The [e208 experimental module](benchmark/experimental_stock_bin_serialization_purchase_hook.py) acquired Item locks in order before purchase submission and Bin locks for receipts. All five receipt/invoice pairs completed in the corrected run. The module remains a lab specimen. It rejects missing Bin rows that ERPNext can normally create.

## Report parity and export

The [comparator](manager/candidate_stock_oracle_compare.py) can reuse an independently verified, hash-pinned native report reference for the same snapshot, actors, report source, filters and precision. The candidate executes again. Ordered detail comparison streams the rows and checks their exact types. Reuse avoids about 74 minutes of archived full-year stock report execution or 67 minutes year to date. End-to-end repeat timing remains unmeasured. Reuse requires the original archived manifest bytes. Rebuilding the manifest changes its timestamp and digest.

The full-year Warehouse and Monthly summaries matched stock. Fast invoice detail failed ordered parity, so verified detail pages came from native Gross Profit. In a separate disposable export environment the RPC aliases and content corrections produced the native CSV exactly over 1,350,437 rows and 211,904,767 bytes (SHA256 `8cfe4b836ccf7bd733515ea0a2eb72ef0c1978d0c6d713059080199ea3f9224c`). This export resumed the original log after an interruption under an Administrator worker identity. The evidence applies to that corrected implementation and recovery path. The original export implementation remains unqualified. The RPC alias patch is for the disposable lab copy only. Applying it to the application triggers the repository's hook-override guardrail.

To exercise the comparator checks on Linux, run the following from the manager directory.

```sh
python3 -B -m unittest test_stock_oracle_reference test_candidate_detail_streaming test_ordered_detail_contract
```
