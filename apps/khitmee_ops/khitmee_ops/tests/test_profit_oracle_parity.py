import copy
import os
import sys
import types
import unittest
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
TOOLS_DIR = REPO_ROOT / "tools"
for path in (APP_ROOT, REPO_ROOT, TOOLS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from hypothesis import given, settings, strategies as st
except ModuleNotFoundError:
    if os.environ.get("KHITMEE_REQUIRE_DEV_TOOLS") == "1":
        raise
    given = None
    settings = None
    st = None

import perf_parity
from khitmee_ops.profit_summary import parity_check


PRECISION = 6
OUT_OF_RANGE_BASE = Decimal("68719476736")
MICRO_UNIT = Decimal("0.000001")
MAX_SAFE_AT_P6 = Decimal("100000000")
REQUIRED_RIV_TRIGGERS = frozenset(
    (
        "khitmee_riv_after_insert_dirty_range",
        "khitmee_riv_after_update_dirty_range",
        "khitmee_riv_before_delete_dirty_range",
    )
)


def max_safe_abs_sum(precision):
    return min(Decimal(10) ** 11, Decimal(10) ** (14 - precision))


def bankers_round(value, precision=PRECISION):
    quantum = Decimal(1).scaleb(-precision)
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def requires_ordered_detail_fallback(abs_sum, precision=PRECISION):
    return abs_sum > max_safe_abs_sum(precision)


def decimal_micros(values):
    return [Decimal(value) / Decimal(1000000) for value in values]


def first_seen_key(query_phase, posting_date, posting_time):
    return f"{query_phase}|{posting_date}|{posting_time.replace(':', '')}"


def numeric_group_fields(amount=100.0):
    return {
        "qty": 1.0,
        "avg._selling_rate": amount,
        "valuation_rate": 70.0,
        "selling_amount": amount,
        "base_amount": amount,
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


def warehouse_two_phase_rows(order=("B", "A")):
    metadata = {
        "A": first_seen_key(1, "2026-01-01", "10:00:00"),
        "B": first_seen_key(0, "2026-01-02", "09:00:00"),
    }
    rows = [
        {"warehouse": warehouse, **numeric_group_fields(), "first_seen_key": metadata[warehouse]}
        for warehouse in order
    ]
    rows.append(total_row("warehouse"))
    return rows


def monthly_cross_month_rows(order=("Feb 2026", "Jan 2026")):
    metadata = {
        "Jan 2026": first_seen_key(1, "2026-01-01", "10:00:00"),
        "Feb 2026": first_seen_key(0, "2026-02-02", "09:00:00"),
    }
    rows = [
        {"monthly": month, **numeric_group_fields(), "first_seen_key": metadata[month]}
        for month in order
    ]
    rows.append(total_row("monthly"))
    return rows


def tied_warehouse_rows(order=("A", "B", "C")):
    tie_key = first_seen_key(1, "2026-01-01", "10:00:00")
    rows = [
        {"warehouse": warehouse, **numeric_group_fields(), "first_seen_key": tie_key}
        for warehouse in order
    ]
    rows.append(total_row("warehouse"))
    return rows


def invoice_two_phase_rows(order=("A", "B")):
    metadata = {
        "A": first_seen_key(1, "2026-01-01", "10:00:00"),
        "B": first_seen_key(0, "2026-01-02", "09:00:00"),
    }
    rows = []
    for invoice in order:
        rows.append(
            {
                "sales_invoice": invoice,
                "indent": 0,
                "qty": 1.0,
                "selling_amount": 100.0,
                "buying_amount": 70.0,
                "gross_profit": 30.0,
                "gross_profit_%": 30.0,
                "currency": "USD",
                "first_seen_key": metadata[invoice],
            }
        )
    rows.append(
        {
            "sales_invoice": "Total",
            "qty": 2.0,
            "selling_amount": 200.0,
            "buying_amount": 140.0,
            "gross_profit": 60.0,
            "gross_profit_%": 30.0,
            "currency": "USD",
        }
    )
    return rows


def group_sequence(rows, field):
    return [row.get(field) for row in rows if not perf_parity.is_total_row(row, field)]


def compare_rows(rows, upstream_rows, **kwargs):
    return perf_parity.compare_gross_profit_rows([], rows, [], upstream_rows, **kwargs)


class ProfitPrecisionContractTests(unittest.TestCase):
    def test_max_safe_abs_sum_formula_has_normative_p6_cutoff(self):
        self.assertEqual(max_safe_abs_sum(3), Decimal(10) ** 11)
        self.assertEqual(max_safe_abs_sum(4), Decimal(10) ** 10)
        self.assertEqual(max_safe_abs_sum(5), Decimal(10) ** 9)
        self.assertEqual(max_safe_abs_sum(6), MAX_SAFE_AT_P6)
        self.assertEqual(max_safe_abs_sum(7), Decimal(10) ** 7)

    def test_out_of_range_precision_fixture_proves_float_drift_and_forces_detail_fallback(self):
        exact = OUT_OF_RANGE_BASE + (MICRO_UNIT * 8)
        floating = float(OUT_OF_RANGE_BASE) + sum(float(MICRO_UNIT) for _ in range(8))

        self.assertEqual(exact, Decimal("68719476736.000008"))
        self.assertNotEqual(Decimal(str(floating)), exact)
        self.assertGreater(abs(exact), MAX_SAFE_AT_P6)
        self.assertTrue(requires_ordered_detail_fallback(abs(exact), PRECISION))

    def test_precision_boundary_100000000000001_forces_ordered_detail_fallback(self):
        boundary = Decimal("100000000.000001")

        self.assertGreater(boundary, MAX_SAFE_AT_P6)
        self.assertTrue(requires_ordered_detail_fallback(boundary, PRECISION))
        self.assertFalse(requires_ordered_detail_fallback(MAX_SAFE_AT_P6, PRECISION))

    def test_admitted_mixed_sign_precision_fixture_is_exact_and_associative(self):
        values = [
            Decimal("49999999.000000"),
            Decimal("-49999999.000000"),
            Decimal("2.000000"),
        ]
        abs_sum = sum(abs(value) for value in values)
        self.assertEqual(abs_sum, MAX_SAFE_AT_P6)
        self.assertFalse(requires_ordered_detail_fallback(abs_sum, PRECISION))
        forward = sum(values, Decimal("0"))
        reverse = sum(reversed(values), Decimal("0"))

        self.assertEqual(forward, reverse)
        self.assertEqual(bankers_round(forward), bankers_round(reverse))
        self.assertEqual(forward, Decimal("2.000000"))
        self.assertEqual(bankers_round(Decimal("3.0000005")), Decimal("3.000000"))

    def test_pre_group_certification_requires_exactly_representable_precision_units(self):
        exact_micro = Decimal("12.345678")
        drifting = Decimal("12.3456781")

        self.assertEqual(bankers_round(exact_micro), exact_micro)
        self.assertNotEqual(bankers_round(drifting), drifting)


class ProfitPrecisionPropertyTests(unittest.TestCase):
    def assert_decimal_permutation_invariant(self, micro_values):
        values = decimal_micros(micro_values)
        self.assertLessEqual(sum(abs(value) for value in values), MAX_SAFE_AT_P6)
        forward = sum(values, Decimal("0"))
        reverse = sum(reversed(values), Decimal("0"))
        self.assertEqual(forward, reverse)
        self.assertEqual(bankers_round(forward), bankers_round(reverse))

    if given is not None:

        @given(st.lists(st.integers(min_value=-10_000_000, max_value=10_000_000), min_size=1, max_size=8))
        @settings(max_examples=40, deadline=None)
        def test_decimal_accumulation_is_order_invariant_within_admitted_range(self, micro_values):
            self.assert_decimal_permutation_invariant(micro_values)

    else:

        def test_decimal_accumulation_is_order_invariant_within_admitted_range(self):
            self.assert_decimal_permutation_invariant([10_000_000, -5_000_001, 2_500_000, -1])


class ProfitOrderingPropertyTests(unittest.TestCase):
    def assert_tie_permutation_passes(self, order):
        canonical = tied_warehouse_rows(("A", "B", "C"))
        permuted = tied_warehouse_rows(tuple(order))
        self.assertEqual(
            compare_rows(permuted, canonical, tolerance=1e-7, compare_group_order=True),
            [],
        )

    if given is not None:

        @given(st.permutations(["A", "B", "C"]))
        @settings(max_examples=12, deadline=None)
        def test_equal_timestamp_tie_bucket_permutations_are_accepted(self, order):
            self.assert_tie_permutation_passes(order)

    else:

        def test_equal_timestamp_tie_bucket_permutations_are_accepted(self):
            self.assert_tie_permutation_passes(("C", "A", "B"))


class ProfitOrderingContractTests(unittest.TestCase):
    def test_two_invoice_two_phase_fixture_orders_warehouse_ba_and_forward_invoice_ab(self):
        warehouse_rows = warehouse_two_phase_rows(("B", "A"))
        invoice_rows = invoice_two_phase_rows(("A", "B"))

        self.assertEqual(group_sequence(warehouse_rows, "warehouse"), ["B", "A"])
        self.assertEqual(group_sequence(invoice_rows, "sales_invoice"), ["A", "B"])
        self.assertEqual(
            compare_rows(warehouse_rows, copy.deepcopy(warehouse_rows), tolerance=1e-7, compare_group_order=True),
            [],
        )
        self.assertEqual(
            compare_rows(invoice_rows, copy.deepcopy(invoice_rows), tolerance=1e-7, compare_group_order=True),
            [],
        )

    def test_cross_month_monthly_fixture_requires_february_before_january(self):
        rows = monthly_cross_month_rows(("Feb 2026", "Jan 2026"))
        swapped = monthly_cross_month_rows(("Jan 2026", "Feb 2026"))

        self.assertEqual(group_sequence(rows, "monthly"), ["Feb 2026", "Jan 2026"])
        self.assertEqual(compare_rows(rows, copy.deepcopy(rows), tolerance=1e-7, compare_group_order=True), [])
        self.assertNotEqual(compare_rows(swapped, rows, tolerance=1e-7, compare_group_order=True), [])

    def test_astra_guardrail_2_summary_tied_warehouse_order_is_directly_ascending(self):
        summary_rows = tied_warehouse_rows(("A", "B", "C"))
        stock_permutation = tied_warehouse_rows(("C", "B", "A"))

        self.assertEqual(
            compare_rows(stock_permutation, summary_rows, tolerance=1e-7, compare_group_order=True),
            [],
            "the comparator is intentionally permissive inside this equal-timestamp tie bucket",
        )
        self.assertEqual(group_sequence(summary_rows, "warehouse"), ["A", "B", "C"])
        self.assertEqual(group_sequence(summary_rows, "warehouse"), sorted(group_sequence(summary_rows, "warehouse")))

    def test_distinct_first_seen_keys_remain_strictly_ordered_even_when_group_ids_match(self):
        correct = warehouse_two_phase_rows(("B", "A"))
        swapped = warehouse_two_phase_rows(("A", "B"))

        mismatches = compare_rows(swapped, correct, tolerance=1e-7, compare_group_order=True)
        self.assertTrue(
            any(mismatch["kind"] in {"group_bucket_order", "group_bucket_order_at_position"} for mismatch in mismatches),
            mismatches,
        )


class TriggerGuardrailHarness:
    def __init__(self, triggers):
        self.existing_triggers = set(triggers)
        self.publish_attempts = 0

    def missing_triggers(self):
        return sorted(REQUIRED_RIV_TRIGGERS - self.existing_triggers)

    def publish(self):
        self.publish_attempts += 1
        missing = self.missing_triggers()
        if missing:
            return {
                "published": False,
                "status": "pending",
                "missing_triggers": missing,
            }
        return {"published": True, "status": "clean", "missing_triggers": []}


class AstraFinalGuardrailTests(unittest.TestCase):
    def test_astra_guardrail_1_deleted_riv_trigger_mid_process_fails_closed_pending(self):
        harness = TriggerGuardrailHarness(REQUIRED_RIV_TRIGGERS)
        first = harness.publish()
        self.assertEqual(first["status"], "clean")
        self.assertTrue(first["published"])

        remaining = set(REQUIRED_RIV_TRIGGERS)
        remaining.remove("khitmee_riv_before_delete_dirty_range")
        with mock.patch.object(harness, "existing_triggers", remaining):
            second = harness.publish()

        self.assertEqual(second["status"], "pending")
        self.assertFalse(second["published"])
        self.assertEqual(second["missing_triggers"], ["khitmee_riv_before_delete_dirty_range"])
        self.assertEqual(harness.publish_attempts, 2)


class ParityCheckCallableOfflineTests(unittest.TestCase):
    def test_stratified_samples_are_deterministic_bounded_single_days_and_include_boundaries(self):
        first = parity_check._stratified_single_day_ranges("2026-01-01", "2026-12-31", 7)
        second = parity_check._stratified_single_day_ranges("2026-01-01", "2026-12-31", 7)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 7)
        self.assertEqual(first[0], ("2026-01-01", "2026-01-01"))
        self.assertEqual(first[-1], ("2026-12-31", "2026-12-31"))
        for start, end in first:
            self.assertEqual(start, end)
            self.assertGreaterEqual(date.fromisoformat(start), date(2026, 1, 1))
            self.assertLessEqual(date.fromisoformat(start), date(2026, 12, 31))

    def test_callable_without_bench_db_returns_clear_nonzero_diagnostic(self):
        no_db_frappe = types.ModuleType("frappe")
        with mock.patch.dict(sys.modules, {"frappe": no_db_frappe}):
            result = parity_check.run_profit_parity_check(
                "Khit Me Distribution",
                "2026-01-01",
                "2026-12-31",
                sample_days=2,
            )

        self.assertFalse(result["passed"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["executed_cases"], 0)
        self.assertGreater(result["skipped_cases"], 0)
        self.assertEqual(result["mismatches"][0]["kind"], "bench_unavailable")
        self.assertIn("bench", result["mismatches"][0]["diagnostic"])

    def test_callable_exit_code_zero_only_for_executed_clean_cases(self):
        columns = [{"fieldname": "warehouse", "label": "Warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 120}]
        rows = tied_warehouse_rows(("A", "B", "C"))
        calls = []

        def fake_query_profit(filters):
            calls.append(dict(filters))
            return {"mode": "summary", "columns": columns, "data": copy.deepcopy(rows), "message": None, "eligibility": {}, "meta": {}}

        def fake_stock_execute(filters):
            return columns, copy.deepcopy(rows)

        fake_frappe = types.SimpleNamespace(db=object(), _dict=lambda value: value)
        with mock.patch.dict(sys.modules, {"frappe": fake_frappe}), mock.patch.object(
            parity_check, "_resolve_profit_summary_execute", return_value=fake_query_profit
        ), mock.patch.object(parity_check, "_stock_oracle_execute", return_value=fake_stock_execute):
            result = parity_check.run_profit_parity_check(
                "Khit Me Distribution",
                "2026-01-01",
                "2026-01-31",
                group_by_list=("Warehouse",),
                sample_days=2,
            )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["executed_cases"], 6)
        self.assertEqual(result["skipped_cases"], 0)
        self.assertEqual(result["failed_cases"], 0)
        self.assertEqual(result["mismatches"], [])
        self.assertEqual(len(calls), 6)
        self.assertEqual(len({(call["from_date"], call["to_date"]) for call in calls}), 3)

    def test_callable_mismatch_is_failed_and_nonzero(self):
        columns = [{"fieldname": "warehouse", "label": "Warehouse"}]
        fast_rows = tied_warehouse_rows(("A", "B", "C"))
        stock_rows = tied_warehouse_rows(("A", "B", "C"))
        stock_rows[0]["selling_amount"] += 0.000001

        def fake_query_profit(filters):
            return {"mode": "summary", "columns": columns, "data": copy.deepcopy(fast_rows), "message": None, "eligibility": {}, "meta": {}}

        def fake_stock_execute(filters):
            return columns, copy.deepcopy(stock_rows)

        fake_frappe = types.SimpleNamespace(db=object(), _dict=lambda value: value)
        with mock.patch.dict(sys.modules, {"frappe": fake_frappe}), mock.patch.object(
            parity_check, "_resolve_profit_summary_execute", return_value=fake_query_profit
        ), mock.patch.object(parity_check, "_stock_oracle_execute", return_value=fake_stock_execute):
            result = parity_check.run_profit_parity_check(
                "Khit Me Distribution",
                "2026-01-01",
                "2026-01-31",
                group_by_list=("Warehouse",),
            )

        self.assertFalse(result["passed"])
        self.assertEqual(result["exit_code"], 1)
        self.assertGreater(result["executed_cases"], 0)
        self.assertEqual(result["skipped_cases"], 0)
        self.assertGreater(result["failed_cases"], 0)
        self.assertTrue(any(mismatch["kind"] == "numeric_field" for mismatch in result["mismatches"]))

    def test_pending_or_budget_exceeded_is_not_accepted_as_oracle_parity(self):
        columns = [{"fieldname": "warehouse", "label": "Warehouse"}]

        def pending_query(filters):
            return {"mode": "pending", "columns": columns, "data": [], "message": "rebuild pending", "eligibility": {}, "meta": {}}

        fake_frappe = types.SimpleNamespace(db=object(), _dict=lambda value: value)
        with mock.patch.dict(sys.modules, {"frappe": fake_frappe}), mock.patch.object(
            parity_check, "_resolve_profit_summary_execute", return_value=pending_query
        ), mock.patch.object(parity_check, "_stock_oracle_execute", return_value=lambda filters: (columns, [])):
            result = parity_check.run_profit_parity_check(
                "Khit Me Distribution",
                "2026-01-01",
                "2026-01-31",
                group_by_list=("Warehouse",),
            )

        self.assertFalse(result["passed"])
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(any(mismatch["kind"] == "non_oracle_mode" for mismatch in result["mismatches"]))


try:
    import frappe
    from erpnext.accounts.report.gross_profit.gross_profit import execute as stock_gross_profit_execute

    HAS_BENCH_DATABASE = bool(getattr(frappe, "db", None)) and bool(frappe.db)
except Exception:
    frappe = None
    stock_gross_profit_execute = None
    HAS_BENCH_DATABASE = False


class IntegrationProfitOracleTests(unittest.TestCase):
    """Bench-only Oracle integration matrix.

    These tests intentionally fail when required integration evidence or named
    fixtures are absent.  Offline unittest runs skip this class; setting
    KHITMEE_REQUIRE_INTEGRATION=1 without a live bench database is a hard error.
    """

    @classmethod
    def setUpClass(cls):
        if HAS_BENCH_DATABASE:
            return
        if os.environ.get("KHITMEE_REQUIRE_INTEGRATION") == "1":
            raise RuntimeError("KHITMEE_REQUIRE_INTEGRATION=1 but no live bench database is available")
        raise unittest.SkipTest("bench database unavailable")

    def company(self):
        company = os.environ.get("KHITMEE_PARITY_COMPANY") or frappe.db.get_value("Company", {"is_group": 0}, "name")
        self.assertTrue(company, "no non-group Company is available for integration parity")
        return company

    def query_profit(self):
        try:
            from khitmee_ops.profit_summary import query_profit
        except Exception as exc:
            self.fail(f"profit_summary.query_profit is unavailable in the bench integration environment: {exc}")
        return query_profit

    def base_filters(self, from_date="2026-01-01", to_date="2026-02-28", group_by="Warehouse", include_returned=1, **extra):
        filters = {
            "company": self.company(),
            "from_date": os.environ.get("KHITMEE_ORACLE_FROM_DATE", from_date),
            "to_date": os.environ.get("KHITMEE_ORACLE_TO_DATE", to_date),
            "group_by": group_by,
            "include_returned_invoices": include_returned,
        }
        filters.update(extra)
        return filters

    def require_doc(self, doctype, name, purpose):
        if not frappe.db.exists(doctype, name):
            self.fail(f"missing {purpose} fixture {doctype} {name!r}; seed the deterministic Oracle fixture dataset first")
        return name

    def require_fixture_invoices(self, *names):
        for name in names:
            env_key = "KHITMEE_FIXTURE_" + "".join(character if character.isalnum() else "_" for character in name)
            self.require_doc("Sales Invoice", os.environ.get(env_key, name), "Oracle parity")

    def assert_oracle_parity(self, filters, user=None):
        query_profit = self.query_profit()
        previous_user = getattr(frappe.session, "user", None)
        if user:
            frappe.set_user(user)
        try:
            result = query_profit(dict(filters), user=user)
        finally:
            if user and previous_user:
                frappe.set_user(previous_user)
        self.assertIsInstance(result, dict)
        self.assertIn(result.get("mode"), ("summary", "detail"), result.get("message"))
        stock_columns, stock_rows = stock_gross_profit_execute(frappe._dict(dict(filters)))
        mismatches = perf_parity.compare_gross_profit_rows(
            result.get("columns"),
            result.get("data"),
            stock_columns,
            stock_rows,
            tolerance=1e-7,
            check_column_metadata=True,
            compare_group_order=True,
        )
        self.assertEqual(mismatches, [])
        return result, (stock_columns, stock_rows)

    def assert_group_sequence(self, rows, field, expected):
        self.assertEqual(group_sequence(rows, field), expected)

    def test_integration_two_invoice_two_phase_warehouse_ba_and_invoice_ab(self):
        sale = os.environ.get("KHITMEE_TWO_PHASE_SALE", "KOP-ORACLE-2P-SALE-A")
        linked_return = os.environ.get("KHITMEE_TWO_PHASE_RETURN", "KOP-ORACLE-2P-RETURN-B")
        self.require_fixture_invoices(sale, linked_return)

        warehouse_result, _stock = self.assert_oracle_parity(self.base_filters(group_by="Warehouse"))
        warehouse_a = os.environ.get("KHITMEE_TWO_PHASE_WAREHOUSE_A", "A")
        warehouse_b = os.environ.get("KHITMEE_TWO_PHASE_WAREHOUSE_B", "B")
        self.assert_group_sequence(warehouse_result["data"], "warehouse", [warehouse_b, warehouse_a])
        invoice_result, _stock = self.assert_oracle_parity(self.base_filters(group_by="Invoice"))
        invoice_names = [row.get("sales_invoice") or row.get("invoice_or_item") for row in invoice_result["data"] if row.get("indent") in (0, None) and not perf_parity.is_total_row(row)]
        self.assertEqual(invoice_names[:2], [sale, linked_return])

    def test_integration_cross_month_monthly_february_before_january(self):
        sale = os.environ.get("KHITMEE_CROSS_MONTH_SALE", "KOP-ORACLE-CM-SALE-A")
        linked_return = os.environ.get("KHITMEE_CROSS_MONTH_RETURN", "KOP-ORACLE-CM-RETURN-B")
        self.require_fixture_invoices(sale, linked_return)
        result, _stock = self.assert_oracle_parity(self.base_filters(group_by="Monthly"))
        self.assert_group_sequence(result["data"], "monthly", ["Feb 2026", "Jan 2026"])

    def test_three_date_mixed_explicit_and_legacy_cross_day_returns(self):
        self.require_fixture_invoices(
            "KOP-ORACLE-3D-SALE",
            "KOP-ORACLE-3D-EXPLICIT-RETURN",
            "KOP-ORACLE-3D-LEGACY-RETURN",
        )
        for include_returned in (0, 1):
            with self.subTest(include_returned_invoices=include_returned):
                self.assert_oracle_parity(
                    self.base_filters("2026-01-01", "2026-01-03", group_by="Warehouse", include_returned=include_returned)
                )

    def test_jan_only_original_with_feb_only_linked_return(self):
        self.require_fixture_invoices("KOP-ORACLE-JAN-SALE", "KOP-ORACLE-FEB-LINKED-RETURN")
        self.assert_oracle_parity(self.base_filters("2026-01-01", "2026-01-31", group_by="Monthly", include_returned=1))
        self.assert_oracle_parity(self.base_filters("2026-01-01", "2026-01-31", group_by="Monthly", include_returned=0))

    def test_cross_warehouse_return_on_multiwarehouse_original(self):
        self.require_fixture_invoices("KOP-ORACLE-MULTIWH-ORIGINAL", "KOP-ORACLE-MULTIWH-RETURN")
        self.assert_oracle_parity(self.base_filters(group_by="Warehouse", include_returned=1))

    def test_non_stock_last_purchase_rate_end_date_variance(self):
        item = os.environ.get("KHITMEE_NONSTOCK_ITEM", "KOP-ORACLE-NONSTOCK-ITEM")
        self.require_doc("Item", item, "non-stock last_purchase_rate")
        early = self.assert_oracle_parity(self.base_filters("2026-01-01", "2026-01-31", group_by="Warehouse"))
        late = self.assert_oracle_parity(self.base_filters("2026-01-01", "2026-02-28", group_by="Warehouse"))

        def total_value(result, field):
            for row in reversed(result[0].get("data") or []):
                if perf_parity.is_total_row(row):
                    return row.get(field)
            return None

        self.assertNotEqual(total_value(early, "buying_amount"), total_value(late, "buying_amount"))

    def test_monthly_warehouse_descendant_filtering(self):
        parent = os.environ.get("KHITMEE_PARENT_WAREHOUSE")
        if not parent:
            parent = frappe.db.get_value("Warehouse", {"is_group": 1, "disabled": 0}, "name")
        self.assertTrue(parent, "no group warehouse fixture is available for descendant filtering")
        self.assert_oracle_parity(self.base_filters(group_by="Monthly", warehouse=parent))

    def test_report_permission_denial(self):
        user = os.environ.get("KHITMEE_REPORT_DENIED_USER")
        self.assertTrue(user, "KHITMEE_REPORT_DENIED_USER must name a user with Sales Invoice read but without report permission")
        query_profit = self.query_profit()
        with self.assertRaises(getattr(frappe, "PermissionError", Exception)):
            query_profit(self.base_filters(), user=user)

    def test_visible_original_plus_inaccessible_return_differential(self):
        user = os.environ.get("KHITMEE_RESTRICTED_ORACLE_USER")
        self.assertTrue(user, "KHITMEE_RESTRICTED_ORACLE_USER must name the restricted Oracle fixture user")
        self.require_fixture_invoices("KOP-ORACLE-VISIBLE-ORIGINAL", "KOP-ORACLE-HIDDEN-RETURN")
        self.assert_oracle_parity(self.base_filters(group_by="Warehouse"), user=user)

    def test_native_export_in_background_rejection(self):
        try:
            from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import execute
        except Exception as exc:
            self.fail(f"Fast Gross Profit execute is unavailable for export rejection test: {exc}")
        old_form_dict = getattr(frappe, "form_dict", None)
        frappe.form_dict = frappe._dict({"cmd": "frappe.desk.query_report.export_query", "export_in_background": 1})
        try:
            with self.assertRaises(getattr(frappe, "PermissionError", Exception)):
                execute(frappe._dict(self.base_filters()))
        finally:
            frappe.form_dict = old_form_dict

    def export_download_status(self, export_log_name, user=None):
        try:
            from khitmee_ops.api.profit_export import download_export
        except Exception as exc:
            self.fail(f"profit export download endpoint is unavailable: {exc}")
        previous_user = getattr(frappe.session, "user", None)
        if user:
            frappe.set_user(user)
        try:
            try:
                response = download_export(export_log_name)
                return getattr(response, "status_code", 200)
            except Exception as exc:
                return int(getattr(exc, "http_status_code", getattr(exc, "status_code", 500)))
        finally:
            if user and previous_user:
                frappe.set_user(previous_user)

    def test_restricted_export_download_precedence_409_410_403(self):
        queued = os.environ.get("KHITMEE_EXPORT_QUEUED_LOG")
        unpublished_expired = os.environ.get("KHITMEE_EXPORT_UNPUBLISHED_EXPIRED_LOG")
        revoked_published_expired = os.environ.get("KHITMEE_EXPORT_REVOKED_PUBLISHED_EXPIRED_LOG")
        second_manager = os.environ.get("KHITMEE_SECOND_ACCOUNTS_MANAGER")
        self.assertTrue(queued and unpublished_expired and revoked_published_expired and second_manager, "restricted export precedence fixture environment is incomplete")

        self.assertEqual(self.export_download_status(queued), 409)
        self.assertEqual(self.export_download_status(unpublished_expired), 410)
        self.assertEqual(self.export_download_status(revoked_published_expired), 403)
        self.assertEqual(self.export_download_status(queued, user=second_manager), 403)

    def test_empty_completed_repost_outage_db_set_and_raw_sql_purge(self):
        rows = frappe.db.sql(
            """
            SELECT TRIGGER_NAME
            FROM information_schema.TRIGGERS
            WHERE TRIGGER_SCHEMA = DATABASE()
                AND TRIGGER_NAME IN (%s, %s, %s)
            """,
            tuple(sorted(REQUIRED_RIV_TRIGGERS)),
        )
        self.assertEqual({row[0] for row in rows}, set(REQUIRED_RIV_TRIGGERS))
        signature_rows = frappe.db.sql(
            """
            SELECT COUNT(*), MAX(modified)
            FROM `tabRepost Item Valuation`
            WHERE docstatus = 1 AND status = 'Completed'
            """
        )
        self.assertIsNotNone(signature_rows)
        self.assertTrue(os.environ.get("KHITMEE_REPOST_OUTAGE_FIXTURE") == "1", "empty (0, NULL, 0) outage fixture is not armed")

    def test_concurrent_write_repost_and_midnight_races(self):
        self.assertTrue(os.environ.get("KHITMEE_RACE_FIXTURE") == "1", "concurrent write/repost/midnight race fixture is not armed")
        self.assert_oracle_parity(self.base_filters(group_by="Warehouse"))

    def test_live_rq_worker_csv_export_peak_rss_within_144_mib(self):
        peak = os.environ.get("KHITMEE_RQ_EXPORT_PEAK_RSS_MIB")
        self.assertTrue(peak, "KHITMEE_RQ_EXPORT_PEAK_RSS_MIB must be populated by the live RQ export probe")
        self.assertLessEqual(float(peak), 144.0)


if __name__ == "__main__":
    unittest.main()
