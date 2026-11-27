import copy
import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import perf_parity


WAREHOUSE_COLUMNS = [
    {"fieldname": "warehouse", "label": "Warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 140},
    {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "options": "", "width": 90},
    {"fieldname": "selling_amount", "label": "Selling Amount", "fieldtype": "Currency", "options": "currency", "width": 120},
    {"fieldname": "buying_amount", "label": "Buying Amount", "fieldtype": "Currency", "options": "currency", "width": 120},
    {"fieldname": "gross_profit", "label": "Gross Profit", "fieldtype": "Currency", "options": "currency", "width": 120},
]

MONTHLY_COLUMNS = [
    {"fieldname": "monthly", "label": "Monthly", "fieldtype": "Data", "options": "", "width": 110},
    *WAREHOUSE_COLUMNS[1:],
]


def numeric_body(value=100.0):
    return {
        "qty": 1.0,
        "avg._selling_rate": value,
        "valuation_rate": 70.0,
        "selling_amount": value,
        "base_amount": value,
        "buying_amount": 70.0,
        "gross_profit": 30.0,
        "gross_profit_%": 30.0,
        "currency": "USD",
    }


def total_row(group_field):
    return {
        group_field: "Total",
        "qty": None,
        "avg._selling_rate": None,
        "valuation_rate": None,
        "selling_amount": 200.0,
        "base_amount": 200.0,
        "buying_amount": 140.0,
        "gross_profit": 60.0,
        "gross_profit_%": 30.0,
        "currency": "USD",
    }


def warehouse_group_rows(order=("B", "A")):
    rows = []
    metadata = {
        "A": {"query_phase": 1, "posting_date": "2026-01-01", "posting_time": "10:00:00"},
        "B": {"query_phase": 0, "posting_date": "2026-01-02", "posting_time": "09:00:00"},
    }
    amounts = {"A": 100.0, "B": 100.0}
    for warehouse in order:
        row = {"warehouse": warehouse, **numeric_body(amounts[warehouse]), **metadata[warehouse]}
        rows.append(row)
    rows.append(total_row("warehouse"))
    return rows


def monthly_group_rows(order=("Feb 2026", "Jan 2026")):
    rows = []
    metadata = {
        "Jan 2026": {"query_phase": 1, "posting_date": "2026-01-01", "posting_time": "10:00:00"},
        "Feb 2026": {"query_phase": 0, "posting_date": "2026-02-02", "posting_time": "09:00:00"},
    }
    for monthly in order:
        row = {"monthly": monthly, **numeric_body(100.0), **metadata[monthly]}
        rows.append(row)
    rows.append(total_row("monthly"))
    return rows


def tied_warehouse_rows(order=("A", "B")):
    rows = []
    tie = {"query_phase": 1, "posting_date": "2026-01-01", "posting_time": "10:00:00"}
    for warehouse in order:
        rows.append({"warehouse": warehouse, **numeric_body(100.0), **tie})
    rows.append(total_row("warehouse"))
    return rows


def invoice_rows(order=("A", "B"), distinct=True):
    rows = []
    metadata = {
        "A": {"query_phase": 1, "posting_date": "2026-01-01", "posting_time": "10:00:00"},
        "B": {
            "query_phase": 0 if distinct else 1,
            "posting_date": "2026-01-02" if distinct else "2026-01-01",
            "posting_time": "09:00:00" if distinct else "10:00:00",
        },
    }
    for invoice in order:
        rows.append({
            "sales_invoice": invoice,
            "indent": 0,
            "qty": 1.0,
            "selling_amount": 100.0,
            "buying_amount": 70.0,
            "gross_profit": 30.0,
            "gross_profit_%": 30.0,
            "currency": "USD",
            **metadata[invoice],
        })
    rows.append({
        "sales_invoice": "Total",
        "qty": 2.0,
        "selling_amount": 200.0,
        "buying_amount": 140.0,
        "gross_profit": 60.0,
        "gross_profit_%": 30.0,
        "currency": "USD",
    })
    return rows


def compare(rows, upstream_rows, columns=WAREHOUSE_COLUMNS, **kwargs):
    return perf_parity.compare_gross_profit_rows(columns, rows, columns, upstream_rows, **kwargs)


class GrossProfitComparatorNegativeControlTests(unittest.TestCase):
    def test_exact_warehouse_baseline_passes_at_oracle_tolerance(self):
        rows = warehouse_group_rows()
        self.assertEqual(compare(rows, copy.deepcopy(rows), tolerance=1e-7, compare_group_order=True), [])

    def test_one_unit_at_sixth_decimal_fails_under_strict_tolerance(self):
        upstream = warehouse_group_rows()
        fast = warehouse_group_rows()
        fast[0]["selling_amount"] += 0.000001

        mismatches = compare(fast, upstream, tolerance=1e-7, compare_group_order=True)

        self.assertTrue(
            any(mismatch["kind"] == "numeric_field" and mismatch["field"] == "selling_amount" for mismatch in mismatches),
            mismatches,
        )

    def test_total_detection_covers_warehouse_monthly_and_generic_first_group_key(self):
        self.assertTrue(perf_parity.is_total_row({"warehouse": "Total", "qty": 1}))
        self.assertTrue(perf_parity.is_total_row({"monthly": "Total", "qty": 1}))
        self.assertTrue(perf_parity.is_total_row({"custom_group": "Total", "qty": 1}))
        self.assertFalse(perf_parity.is_total_row({"warehouse": "Shop 1", "qty": 1}))

    def test_mutated_total_amount_fails_for_warehouse_and_monthly(self):
        fixtures = (
            ("Warehouse", warehouse_group_rows, WAREHOUSE_COLUMNS),
            ("Monthly", monthly_group_rows, MONTHLY_COLUMNS),
        )
        for label, factory, columns in fixtures:
            with self.subTest(group_by=label, mutation="amount"):
                upstream = factory()
                fast = factory()
                fast[-1]["selling_amount"] += 1.0
                mismatches = compare(fast, upstream, columns=columns, tolerance=1e-7, compare_group_order=True)
                self.assertTrue(
                    any(mismatch["kind"] == "numeric_field" and mismatch["location"] == "total" for mismatch in mismatches),
                    mismatches,
                )

    def test_total_row_not_final_fails_for_warehouse_and_monthly(self):
        fixtures = (
            ("Warehouse", warehouse_group_rows, WAREHOUSE_COLUMNS),
            ("Monthly", monthly_group_rows, MONTHLY_COLUMNS),
        )
        for label, factory, columns in fixtures:
            with self.subTest(group_by=label, mutation="position"):
                upstream = factory()
                fast = factory()
                total = fast.pop()
                fast.insert(0, total)
                mismatches = compare(fast, upstream, columns=columns, tolerance=1e-7, compare_group_order=True)
                self.assertTrue(any(mismatch["kind"] == "total_row_not_final" for mismatch in mismatches), mismatches)

    def test_swapped_distinct_cross_phase_warehouse_order_fails(self):
        correct = warehouse_group_rows(("B", "A"))
        swapped = warehouse_group_rows(("A", "B"))

        mismatches = compare(swapped, correct, tolerance=1e-7, compare_group_order=True)

        self.assertTrue(
            any(mismatch["kind"] in {"group_bucket_order", "group_bucket_order_at_position"} for mismatch in mismatches),
            mismatches,
        )

    def test_cross_month_monthly_order_requires_february_before_january(self):
        correct = monthly_group_rows(("Feb 2026", "Jan 2026"))
        swapped = monthly_group_rows(("Jan 2026", "Feb 2026"))

        self.assertEqual(compare(correct, copy.deepcopy(correct), columns=MONTHLY_COLUMNS, tolerance=1e-7, compare_group_order=True), [])
        mismatches = compare(swapped, correct, columns=MONTHLY_COLUMNS, tolerance=1e-7, compare_group_order=True)
        self.assertTrue(
            any(mismatch["kind"] in {"group_bucket_order", "group_bucket_order_at_position"} for mismatch in mismatches),
            mismatches,
        )

    def test_permutation_within_equal_tie_bucket_passes_but_cross_bucket_swap_fails(self):
        upstream = tied_warehouse_rows(("A", "B"))
        permuted_within_tie = tied_warehouse_rows(("B", "A"))
        self.assertEqual(
            compare(permuted_within_tie, upstream, tolerance=1e-7, compare_group_order=True),
            [],
        )

        distinct_upstream = warehouse_group_rows(("B", "A"))
        distinct_swapped = warehouse_group_rows(("A", "B"))
        self.assertNotEqual(
            compare(distinct_swapped, distinct_upstream, tolerance=1e-7, compare_group_order=True),
            [],
        )

    def test_invoice_order_is_exact_across_distinct_buckets_and_tie_permissive_within_bucket(self):
        correct = invoice_rows(("A", "B"), distinct=True)
        self.assertEqual(compare(correct, copy.deepcopy(correct), columns=[], tolerance=1e-7, compare_group_order=True), [])
        swapped = invoice_rows(("B", "A"), distinct=True)
        mismatches = compare(swapped, correct, columns=[], tolerance=1e-7, compare_group_order=True)
        self.assertTrue(
            any(mismatch["kind"] in {"invoice_bucket_order", "invoice_bucket_order_at_position", "invoice_order"} for mismatch in mismatches),
            mismatches,
        )

        tied = invoice_rows(("A", "B"), distinct=False)
        tied_permutation = invoice_rows(("B", "A"), distinct=False)
        self.assertEqual(
            compare(tied_permutation, tied, columns=[], tolerance=1e-7, compare_group_order=True),
            [],
        )

    def test_none_replaced_by_zero_fails_in_total_qty_and_rates(self):
        for field in ("qty", "avg._selling_rate", "valuation_rate"):
            with self.subTest(field=field):
                upstream = warehouse_group_rows()
                fast = warehouse_group_rows()
                fast[-1][field] = 0
                mismatches = compare(fast, upstream, tolerance=1e-7, compare_group_order=True)
                self.assertTrue(
                    any(mismatch["kind"] == "null_numeric" and mismatch["field"] == field for mismatch in mismatches),
                    mismatches,
                )

    def test_mutated_column_fieldtype_or_width_fails(self):
        for field, mutated in (("fieldtype", "Data"), ("width", 99)):
            with self.subTest(field=field):
                upstream_columns = copy.deepcopy(WAREHOUSE_COLUMNS)
                fast_columns = copy.deepcopy(WAREHOUSE_COLUMNS)
                fast_columns[0][field] = mutated
                rows = warehouse_group_rows()
                mismatches = perf_parity.compare_gross_profit_rows(
                    fast_columns,
                    rows,
                    upstream_columns,
                    copy.deepcopy(rows),
                    tolerance=1e-7,
                    compare_group_order=True,
                )
                self.assertTrue(
                    any(mismatch["kind"] == "column_metadata" and mismatch["field"] == field for mismatch in mismatches),
                    mismatches,
                )

    def test_explicit_order_keys_can_annotate_an_equal_tie_bucket(self):
        def rows(order):
            result = [
                {"warehouse": warehouse, "qty": 1.0, "selling_amount": 100.0, "buying_amount": 70.0, "gross_profit": 30.0}
                for warehouse in order
            ]
            result.append({"warehouse": "Total", "qty": 2.0, "selling_amount": 200.0, "buying_amount": 140.0, "gross_profit": 60.0})
            return result

        canonical = rows(("A", "B"))
        permuted = rows(("B", "A"))
        tie = (1, "2026-01-01", "10:00:00")
        order_keys = {"fast": [tie, tie], "upstream": [tie, tie]}
        self.assertEqual(
            perf_parity.compare_gross_profit_rows(
                [], permuted, [], canonical,
                tolerance=1e-7,
                compare_group_order=True,
                order_keys=order_keys,
            ),
            [],
        )

    def test_legacy_columns_with_width_and_fieldtype_omitted_remain_compatible(self):
        columns = [{"fieldname": "warehouse", "label": "Warehouse"}, {"fieldname": "qty", "label": "Qty"}]
        rows = tied_warehouse_rows()
        self.assertEqual(
            perf_parity.compare_gross_profit_rows(columns, rows, columns, copy.deepcopy(rows), tolerance=1e-7),
            [],
        )

    def test_invoice_order_keys_allow_equal_timestamp_swap_and_reject_distinct_timestamp_swap(self):
        def make_rows(first_inv, second_inv):
            metrics = {
                "ACC-SINV-2026-317777": (100.0, 70.0, 30.0),
                "ACC-SINV-2026-317776": (50.0, 30.0, 20.0),
            }
            rows = []
            for inv in (first_inv, second_inv):
                selling, buying, profit = metrics[inv]
                rows.append({"sales_invoice": inv, "indent": 0, "posting_date": "2026-11-25", "selling_amount": selling, "buying_amount": buying, "gross_profit": profit})
                rows.append({"sales_invoice": "ITEM-1", "parent_invoice": inv, "indent": 1, "selling_amount": selling, "buying_amount": buying, "gross_profit": profit})
            rows.append({"sales_invoice": "Total", "selling_amount": 150.0, "buying_amount": 100.0, "gross_profit": 50.0})
            return rows

        fast_data = make_rows("ACC-SINV-2026-317777", "ACC-SINV-2026-317776")
        upstream_data = make_rows("ACC-SINV-2026-317776", "ACC-SINV-2026-317777")
        same_ts = (1, "2026-11-25", "20:49:27.000000")
        equal_keys = {
            "fast": [same_ts, None, same_ts, None, None],
            "upstream": [same_ts, None, same_ts, None, None],
        }
        self.assertEqual(
            perf_parity.compare_gross_profit_rows(
                [], fast_data, [], upstream_data, tolerance=1e-7, compare_group_order=True, order_keys=equal_keys
            ),
            [],
        )
        newer_ts = (1, "2026-11-25", "20:49:28.000000")
        distinct_keys = {
            "fast": [same_ts, None, newer_ts, None, None],
            "upstream": [newer_ts, None, same_ts, None, None],
        }
        mismatches = perf_parity.compare_gross_profit_rows(
            [], fast_data, [], upstream_data, tolerance=1e-7, compare_group_order=True, order_keys=distinct_keys
        )
        self.assertTrue(any(m["kind"] in {"invoice_bucket_order", "invoice_bucket_order_at_position"} for m in mismatches))


class OracleSidecarRunnerTests(unittest.TestCase):
    def test_host_only_flags_are_not_forwarded(self):
        argv = ["--require-db", "--runner", "sidecar", "--sidecar-memory=2g", "--only", "gp", "--runner=exec"]
        self.assertEqual(perf_parity._strip_host_only_args(argv), ["--require-db", "--only", "gp"])

    def test_sidecar_is_bounded_without_swap_and_reuses_backend_mounts(self):
        command = perf_parity.build_sidecar_command(
            "khitmee-perf-backend-1",
            "frappe/erpnext:v16",
            "khitmee-perf_default",
            "1536m",
            ["--require-db"],
            "p1",
            env=["PYTHONPATH=/home/frappe/frappe-bench/apps/khitmee_ops", "HOSTNAME=abc"],
        )
        self.assertIn("PYTHONPATH=/home/frappe/frappe-bench/apps/khitmee_ops", command)
        self.assertNotIn("HOSTNAME=abc", command)
        self.assertEqual(command[:4], ["docker", "run", "--rm", "-i"])
        self.assertEqual(command[command.index("--memory") + 1], "1536m")
        self.assertEqual(command[command.index("--memory-swap") + 1], "1536m")
        self.assertEqual(command[command.index("--volumes-from") + 1], "khitmee-perf-backend-1")
        self.assertEqual(command[command.index("--network") + 1], "khitmee-perf_default")
        self.assertEqual(command[-3:], ["-", "--inside-backend", "--require-db"])
        self.assertNotIn("khitmee-erpnext", " ".join(command))


if __name__ == "__main__":
    unittest.main()
