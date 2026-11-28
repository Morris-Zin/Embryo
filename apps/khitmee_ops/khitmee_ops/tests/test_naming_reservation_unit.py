import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Reuse the voucher suite's offline Frappe and ERPNext stubs.
from khitmee_ops.tests import test_vouchers_unit
from khitmee_ops.api import invoice_series


class TestInvoiceReservation(unittest.TestCase):
	def test_independent_allocation_writes_both_rows_and_commits_only_its_connection(self):
		connection = Mock()
		connection.sql.side_effect = [None, [(38,)], None]
		caller_db = Mock()
		with (
			patch.object(invoice_series, "_connect_site_db", return_value=connection),
			patch.object(invoice_series.frappe, "db", caller_db),
		):
			self.assertEqual(invoice_series._reserve("INV-2026-", 5, "request-a"), "00038")
		self.assertIn("ON DUPLICATE KEY UPDATE", connection.sql.call_args_list[0].args[0])
		self.assertEqual(connection.sql.call_args.args[1][:3], ("INV-2026-00038", "INV-2026-", 38))
		connection.commit.assert_called_once_with()
		connection.close.assert_called_once_with()
		caller_db.commit.assert_not_called()

	def test_audit_failure_rolls_back_independent_counter(self):
		connection = Mock()
		connection.sql.side_effect = [None, [(1,)], RuntimeError("audit failed")]
		with patch.object(invoice_series, "_connect_site_db", return_value=connection):
			with self.assertRaisesRegex(RuntimeError, "audit failed"):
				invoice_series._reserve("NEW-", 4, "request-b")
		connection.commit.assert_not_called()
		connection.rollback.assert_called_once_with()
		connection.close.assert_called_once_with()

	def test_stock_parser_controls_prefix_width_and_fallback(self):
		doc = {}
		native = Mock(return_value="stock")
		parse = Mock(side_effect=lambda parts, doc, number_generator: number_generator("INV-2026-", 4))
		generate = invoice_series._make_generate_dispatcher(native, parse)
		series = SimpleNamespace(series="INV-.YYYY.-.####", validate=Mock())
		with (
			patch.object(invoice_series._SCOPE, "reservation", (doc, "request-c"), create=True),
			patch.object(invoice_series, "_reserve", return_value="0009") as reserve,
		):
			self.assertEqual(generate(series, doc), "0009")
			reserve.assert_called_once_with("INV-2026-", 4, "request-c")
			series.validate.assert_called_once_with()
			self.assertEqual(generate(series, {}), "stock")
			doc["amended_from"] = "earlier"
			self.assertEqual(generate(series, doc), "stock")
			doc.clear()
			series.series += ".suffix"
			self.assertEqual(generate(series, doc), "stock")
		self.assertEqual(generate(series, doc), "stock")

	def test_audited_number_is_not_reverted_but_native_number_is(self):
		native = Mock()
		db = Mock(db_type="mariadb")
		db.sql.side_effect = [[(1,)], []]
		doc = SimpleNamespace(doctype="Sales Invoice")
		revert = invoice_series._make_revert_dispatcher(native)
		with patch.object(invoice_series.frappe, "db", db):
			self.assertIsNone(revert("INV-.####", "INV-0042", doc))
			revert("INV-.####", "INV-0043", doc)
		native.assert_called_once_with("INV-.####", "INV-0043", doc)

	def test_disabled_flag_never_installs_reservation_scope(self):
		with (
			patch.object(invoice_series.frappe, "conf", {}, create=True),
			patch.object(invoice_series, "install_series_dispatchers") as install,
			invoice_series.counter_series_reservation({}, "request-d", fresh_snapshot=True),
		):
			self.assertIsNone(getattr(invoice_series._SCOPE, "reservation", None))
		install.assert_not_called()
