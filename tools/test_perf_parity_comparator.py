import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    def test_live_gross_profit_percent_detects_third_decimal_at_currency_precision(self):
        columns = [{"fieldname": "gross_profit_%", "fieldtype": "Percent"}]
        mismatches = perf_parity.compare_live_gross_profit_reports(
            columns,
            [[30.001]],
            columns,
            [[30.000]],
            {
                "currency_precision": 3,
                "float_precision": 2,
                "rounding_method": "Banker's Rounding (legacy)",
            },
        )
        self.assertEqual(
            [(m["kind"], m["field"], m["precision"]) for m in mismatches],
            [("numeric_mismatch", "gross_profit_%", 3)],
        )

    def test_live_gp_parity_uses_precision_aware_fast_verify_comparator(self):
        columns = [{"fieldname": "selling_amount", "label": "Selling Amount", "fieldtype": "Currency"}]
        precision = {
            "currency_precision": 3,
            "float_precision": 2,
            "rounding_method": "Banker's Rounding (legacy)",
        }
        equal = perf_parity.compare_live_gross_profit_reports(
            columns,
            [[1.0001]],
            columns,
            [[1.0002]],
            precision,
        )
        self.assertEqual(equal, [])

        mismatch = perf_parity.compare_live_gross_profit_reports(
            columns,
            [[1.001]],
            columns,
            [[1.0]],
            precision,
        )
        self.assertTrue(any(item["kind"] == "monetary_mismatch" for item in mismatch), mismatch)

        with self.assertRaises(perf_parity.ParityFailure):
            perf_parity.compare_live_gross_profit_reports(
                columns,
                [[1.0]],
                columns,
                [[1.0]],
                {**precision, "rounding_method": "Commercial Rounding"},
            )

    def test_inner_payload_embeds_fast_verify_and_compiles(self):
        payload = perf_parity.build_inner_python_source()
        self.assertIn(b"_fast_verify_module", payload[:5000])
        self.assertIn(b"compare_financial_reports", payload)
        compile(payload, "<embedded-parity-test>", "exec")

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

    def test_sidecar_mounts_oracle_cache_dir_when_provided(self):
        command = perf_parity.build_sidecar_command(
            "khitmee-perf-backend-1",
            "frappe/erpnext:v16",
            "khitmee-perf_default",
            "1536m",
            ["--oracle-cache-dir", "/tmp/khitmee-oracle-cache"],
            "p2",
            oracle_cache_mount=("/host/oracle-cache", "/tmp/khitmee-oracle-cache"),
        )
        self.assertIn("-v", command)
        self.assertEqual(command[command.index("-v") + 1], "/host/oracle-cache:/tmp/khitmee-oracle-cache")


class OracleCacheAndTimingTests(unittest.TestCase):
    def _sample_key(self, **overrides):
        key = {
            "db_fingerprint": "dbfp-abc123",
            "erpnext_source_sha256": "srcsha-def456",
            "report": "Gross Profit",
            "filters": {"company": "Khit Me Distribution", "from_date": "2026-10-27", "to_date": "2026-11-26", "group_by": "Warehouse"},
            "user": "Administrator",
            "precision_settings": {"currency_precision": 3, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)"},
        }
        key.update(overrides)
        return key

    def test_cli_parser_default_oracle_cache_dir_is_none(self):
        args_default = perf_parity.build_parser().parse_args([])
        self.assertIsNone(args_default.oracle_cache_dir)
        args_set = perf_parity.build_parser().parse_args(["--oracle-cache-dir", "/tmp/oc"])
        self.assertEqual(args_set.oracle_cache_dir, "/tmp/oc")

    def test_oracle_cache_roundtrip_hit_and_clean_miss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = perf_parity.ParityOracleCache(tmpdir)
            key = self._sample_key()
            self.assertIsNone(cache.get(key))
            cols = WAREHOUSE_COLUMNS
            rows = warehouse_group_rows()
            cache.put(key, cols, rows)

            # Reordered filter keys still hit the same canonical entry.
            reordered = self._sample_key(
                filters={"group_by": "Warehouse", "to_date": "2026-11-26", "from_date": "2026-10-27", "company": "Khit Me Distribution"}
            )
            hit = cache.get(
                reordered,
                expected_db_fingerprint="dbfp-abc123",
                expected_source_sha256="srcsha-def456",
                expected_precision=key["precision_settings"],
            )
            self.assertIsNotNone(hit)
            cached_cols, cached_rows = hit
            self.assertEqual(cached_cols, cols)
            self.assertEqual(cached_rows, rows)

    def test_oracle_cache_fails_closed_on_drift_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = perf_parity.ParityOracleCache(tmpdir)
            key = self._sample_key()
            entry = cache.put(key, WAREHOUSE_COLUMNS, warehouse_group_rows())

            # DB fingerprint drift fails closed.
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                cache.get(key, expected_db_fingerprint="dbfp-different")

            # ERPNext source SHA-256 drift fails closed.
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                cache.get(key, expected_source_sha256="srcsha-modified")

            # Precision drift fails closed.
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                cache.get(key, expected_precision={"currency_precision": 2, "float_precision": 2, "rounding_method": "Banker's Rounding (legacy)"})

            # Payload byte tampering fails closed.
            payload_path = Path(tmpdir) / entry["payload_path"]
            original = payload_path.read_text(encoding="utf-8")
            payload_path.write_text(original.replace("100.0", "100.1", 1), encoding="utf-8")
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                cache.get(key)

            # Restore payload and tamper manifest root hash -> fails closed.
            payload_path.write_text(original, encoding="utf-8")
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["entries_root_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                cache.get(key)

    def test_format_section_timings(self):
        formatted = perf_parity.format_section_timings({"voucher": 1.2345, "gp": 0.4567}, 1.7001)
        self.assertEqual(formatted, {"sections": {"gp": 0.457, "voucher": 1.234}, "total_seconds": 1.7})

    def test_host_exec_fails_closed_for_oracle_cache_with_exec_runner(self):
        import io
        from unittest import mock

        err_buf = io.StringIO()
        with mock.patch("sys.stderr", err_buf), mock.patch.object(perf_parity, "_inspect_backend") as inspect_mock:
            rc = perf_parity.host_exec(["--oracle-cache-dir", "/tmp/oc", "--runner", "exec"], runner="exec")
        self.assertEqual(rc, 3)
        self.assertIn("--runner sidecar", err_buf.getvalue())
        inspect_mock.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "requires POSIX cache descriptors")
    def test_prepare_sidecar_oracle_cache_dir_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "oracle-cache"
            cache_dir.mkdir()
            manifest = cache_dir / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            chowned = []

            def record_chown(fd, uid, gid):
                chowned.append((os.fstat(fd).st_ino, uid, gid))
                real_fchown(fd, uid, gid)

            real_fchown = os.fchown
            uid, gid = os.getuid() or 1000, os.getegid()
            with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
                os, "fchown", side_effect=record_chown
            ):
                perf_parity.prepare_sidecar_oracle_cache_dir(cache_dir, uid, gid)

            target_modes = {cache_dir: 0o750, cache_dir / "entries": 0o750, manifest: 0o640}
            self.assertEqual(chowned, [(path.stat().st_ino, uid, gid) for path in target_modes])
            for path, mode in target_modes.items():
                info = path.stat()
                self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (uid, gid, mode))

            # A non-root caller with a different UID cannot prepare the mount.
            with mock.patch.object(os, "geteuid", return_value=uid + 1):
                with self.assertRaisesRegex(perf_parity.OracleCacheDriftError, "requires that UID or root"):
                    perf_parity.prepare_sidecar_oracle_cache_dir(cache_dir, uid, gid)

            with mock.patch.object(os, "geteuid", return_value=uid), mock.patch.object(
                os, "fchown"
            ) as fchown, mock.patch.object(os, "fchmod") as fchmod:
                perf_parity.prepare_sidecar_oracle_cache_dir(cache_dir, uid, gid)
                fchown.assert_not_called()
                fchmod.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "requires POSIX cache descriptors")
    def test_prepare_sidecar_oracle_cache_dir_rejects_unsafe_paths_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            os, "geteuid", return_value=0
        ), mock.patch.object(os, "fchown") as fchown, mock.patch.object(os, "fchmod") as fchmod:
            for unsafe in ("/tmp", "/var/tmp", "/"):
                with self.subTest(unsafe=unsafe):
                    with self.assertRaisesRegex(perf_parity.OracleCacheDriftError, "shared/system directory"):
                        perf_parity.prepare_sidecar_oracle_cache_dir(unsafe)

            for mode in (0o1755, 0o777):
                with self.subTest(mode=oct(mode)):
                    unsafe = Path(tmpdir) / f"cache-{mode}"
                    unsafe.mkdir()
                    unsafe.chmod(mode)
                    with self.assertRaises(perf_parity.OracleCacheDriftError):
                        perf_parity.prepare_sidecar_oracle_cache_dir(unsafe)
                    self.assertFalse((unsafe / "entries").exists())
            real_cache = Path(tmpdir) / "cache"
            real_cache.mkdir()
            cache_link = Path(tmpdir) / "cache-link"
            cache_link.symlink_to(real_cache, target_is_directory=True)
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                perf_parity.prepare_sidecar_oracle_cache_dir(cache_link)
            self.assertFalse((real_cache / "entries").exists())
            fchown.assert_not_called()
            fchmod.assert_not_called()

    def test_root_cache_preparation_requires_no_follow_support(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            os, "geteuid", return_value=0, create=True
        ), mock.patch.object(os, "O_NOFOLLOW", 0, create=True):
            cache_dir = Path(tmpdir) / "oracle-cache"
            with self.assertRaisesRegex(perf_parity.OracleCacheDriftError, "POSIX descriptor support"):
                perf_parity.prepare_sidecar_oracle_cache_dir(cache_dir)
            self.assertFalse(cache_dir.exists())

    @unittest.skipUnless(os.name == "nt", "exercises unprivileged Windows cache preparation")
    def test_windows_cache_preparation_creates_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "oracle-cache"
            self.assertEqual(perf_parity.prepare_sidecar_oracle_cache_dir(cache_dir), str(cache_dir.resolve()))
            self.assertTrue((cache_dir / "entries").is_dir())

    def _assert_linked_cache_child_rejected(self, child, link_kind):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "oracle-cache"
            cache_dir.mkdir()
            outside = Path(tmpdir) / "outside"
            if child == "entries":
                outside.mkdir(mode=0o755)
            else:
                outside.write_text("outside cache", encoding="utf-8")
                outside.chmod(0o644)

            link = cache_dir / child
            if link_kind == "symlink":
                link.symlink_to(outside, target_is_directory=child == "entries")
            else:
                os.link(outside, link)

            before = outside.stat()
            with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
                os, "fchown", wraps=os.fchown
            ) as fchown, mock.patch.object(os, "fchmod", wraps=os.fchmod) as fchmod:
                with self.assertRaises(perf_parity.OracleCacheDriftError):
                    perf_parity.prepare_sidecar_oracle_cache_dir(cache_dir, os.getuid(), os.getgid())
                fchown.assert_not_called()
                fchmod.assert_not_called()
            self.assertEqual(outside.stat(), before)
            if child == "manifest.json":
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside cache")

    @unittest.skipUnless(os.name == "posix", "requires POSIX cache descriptors")
    def test_root_cache_preparation_rejects_entries_symlink(self):
        self._assert_linked_cache_child_rejected("entries", "symlink")

    @unittest.skipUnless(os.name == "posix", "requires POSIX cache descriptors")
    def test_root_cache_preparation_rejects_manifest_symlink(self):
        self._assert_linked_cache_child_rejected("manifest.json", "symlink")

    @unittest.skipUnless(os.name == "posix", "requires POSIX cache descriptors")
    def test_root_cache_preparation_rejects_manifest_hardlink(self):
        self._assert_linked_cache_child_rejected("manifest.json", "hardlink")

    def test_external_oracle_manifest_and_fail_closed_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            external_manifest = Path(tmpdir) / "trust-root" / "manifest.json"
            cache = perf_parity.ParityOracleCache(cache_dir, manifest_path=external_manifest)
            key = self._sample_key()
            cache.put(key, WAREHOUSE_COLUMNS, warehouse_group_rows())
            self.assertTrue(external_manifest.is_file())
            self.assertFalse((cache_dir / "manifest.json").exists())

            hit = cache.get(key)
            self.assertIsNotNone(hit)

            # Manifest rooted inside cache_dir must fail closed.
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                perf_parity.ParityOracleCache(cache_dir, manifest_path=cache_dir / "manifest.json")

            # Tampered external manifest digest fails closed.
            manifest = json.loads(external_manifest.read_text(encoding="utf-8"))
            kh = next(iter(manifest["entries"]))
            manifest["entries"][kh]["payload_sha256"] = "0" * 64
            external_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(perf_parity.OracleCacheDriftError):
                cache.get(key)

    def test_capture_db_fingerprint_tracks_repost_sle_sums_and_table_watermarks(self):
        class FakeDB:
            def __init__(self, sle_val_sum=1000.0, repost_count=0):
                self.sle_val_sum = sle_val_sum
                self.repost_count = repost_count
                self.queries = []

            def sql(self, query, *args, **kwargs):
                self.queries.append(query)
                if "SUM(stock_value_difference)" in query:
                    return [(500, "2026-11-26 10:00:00", "2026-11-26", "2026-11-26 10:00:00", self.sle_val_sum, -250.0)]
                if "MAX(posting_date)" in query:
                    return [(100, "2026-11-26 10:00:00", "2026-11-26")]
                if "`tabRepost Item Valuation`" in query:
                    return [(self.repost_count, "2026-11-26 10:00:00" if self.repost_count else None)]
                if "MAX(modified)" in query:
                    return [(42, "2026-11-26 09:00:00")]
                return [(10,)]

        class FakeFrappe:
            def __init__(self, db):
                self.db = db

        fp_base = perf_parity.capture_db_fingerprint(FakeFrappe(FakeDB(1000.0, 0)), site="bench.localhost")
        fp_repost = perf_parity.capture_db_fingerprint(FakeFrappe(FakeDB(1000.0, 1)), site="bench.localhost")
        fp_val_drift = perf_parity.capture_db_fingerprint(FakeFrappe(FakeDB(1000.5, 0)), site="bench.localhost")

        self.assertNotEqual(fp_base, fp_repost)
        self.assertNotEqual(fp_base, fp_val_drift)
        db = FakeDB()
        perf_parity.capture_db_fingerprint(FakeFrappe(db), site="bench.localhost")
        joined = "\n".join(db.queries)
        for tbl in perf_parity.FINGERPRINT_EXTRA_TABLES:
            self.assertIn(tbl, joined)
        self.assertIn("SUM(stock_value_difference)", joined)
        self.assertIn("SUM(actual_qty)", joined)
        self.assertIn("MAX(creation)", joined)

    def test_capture_with_rollback_tolerates_inner_db_rollback_without_savepoint(self):
        class FakeDB:
            def __init__(self):
                self.rollbacks = 0

            def sql(self, query, *args, **kwargs):
                if "SAVEPOINT" in str(query).upper():
                    raise AssertionError("SAVEPOINT must not be used in capture_with_rollback")
                return []

            def get_value(self, *args, **kwargs):
                return {}

            def rollback(self):
                self.rollbacks += 1

        class FakeLocal:
            def __init__(self):
                self.document_cache = {"k": "v"}

        class FakeFrappe:
            def __init__(self):
                self.db = FakeDB()
                self.local = FakeLocal()

        frappe = FakeFrappe()

        def build_doc():
            frappe.db.rollback()
            return {"voucher_no": "SINV-0001"}

        name, snap = perf_parity.capture_with_rollback(
            frappe,
            "test-sale",
            [("KM-001", "Shop 1")],
            build_doc,
            lambda f, n, before: {"voucher": n},
        )
        self.assertEqual(name, "SINV-0001")
        self.assertEqual(snap, {"voucher": "SINV-0001"})
        self.assertEqual(frappe.db.rollbacks, 2)
        self.assertEqual(frappe.local.document_cache, {})

    def test_prepare_sidecar_oracle_manifest_dir_rejects_shared_and_sticky_dirs(self):
        import stat as stat_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            mutated = []

            # Shared system directories (/tmp, /var/tmp) must be rejected without mutation.
            for shared in ("/tmp/manifest.json", "/var/tmp/manifest.json"):
                with self.subTest(shared=shared):
                    with self.assertRaises(perf_parity.OracleCacheDriftError):
                        perf_parity.prepare_sidecar_oracle_manifest_dir(
                            shared,
                            cache_dir,
                            is_posix=True,
                            geteuid=lambda: 0,
                            chown_fn=lambda *a: mutated.append(a),
                            chmod_fn=lambda *a: mutated.append(a),
                        )
            self.assertEqual(mutated, [])

            # Sticky directory (S_ISVTX) must be rejected without mutation.
            sticky_dir = Path(tmpdir) / "sticky-parent"
            sticky_dir.mkdir()

            class StickyStat:
                st_mode = stat_mod.S_IFDIR | stat_mod.S_ISVTX | 0o777

            with self.assertRaises(perf_parity.OracleCacheDriftError):
                perf_parity.prepare_sidecar_oracle_manifest_dir(
                    sticky_dir / "manifest.json",
                    cache_dir,
                    is_posix=True,
                    geteuid=lambda: 0,
                    chown_fn=lambda *a: mutated.append(a),
                    chmod_fn=lambda *a: mutated.append(a),
                    stat_fn=lambda _p: StickyStat(),
                )
            self.assertEqual(mutated, [])

            # Dedicated non-sticky subdirectory succeeds.
            dedicated = Path(tmpdir) / "dedicated-trust-root" / "manifest.json"
            class NormalStat:
                st_mode = stat_mod.S_IFDIR | 0o755

            mdir, mname = perf_parity.prepare_sidecar_oracle_manifest_dir(
                dedicated,
                cache_dir,
                target_uid=1000,
                target_gid=1000,
                is_posix=True,
                geteuid=lambda: 0,
                chown_fn=lambda *a: mutated.append(a),
                chmod_fn=lambda *a: mutated.append(a),
                stat_fn=lambda _p: NormalStat(),
            )
            self.assertEqual(mname, "manifest.json")
            self.assertTrue(Path(mdir).is_dir())
            self.assertEqual(len(mutated), 2)
            self.assertEqual(mutated[1][1], 0o750)


if __name__ == "__main__":
    unittest.main()
