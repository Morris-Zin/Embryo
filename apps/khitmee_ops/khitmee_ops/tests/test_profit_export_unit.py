import csv
import importlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
	sys.path.insert(0, str(APP_ROOT))

from khitmee_ops.api import profit_export
from khitmee_ops.khitmee_ops.report.fast_gross_profit import fast_gross_profit
from khitmee_ops.profit_summary import export_service


class FakePermissionError(Exception):
	pass


class FakeValidationError(Exception):
	pass


class FakeDoesNotExistError(Exception):
	pass


class FakeDoc(dict):
	def __init__(self, fake, values):
		super().__init__(values)
		self._fake = fake

	def __getattr__(self, name):
		try:
			return self[name]
		except KeyError as error:
			raise AttributeError(name) from error

	def __setattr__(self, name, value):
		if name == "_fake":
			super().__setattr__(name, value)
		else:
			self[name] = value

	def insert(self, ignore_permissions=False):
		self._fake.register_doc(self)
		return self


class FakeAfterCommit:
	def __init__(self):
		self.callbacks = []

	def add(self, callback):
		self.callbacks.append(callback)


class FakeDB:
	def __init__(self, state):
		self.state = state
		self.after_commit = FakeAfterCommit()
		self.sql_calls = []
		self.update_sql = []
		self.commit_count = 0

	def get_value(self, doctype, filters=None, fieldname=None, as_dict=False):
		if doctype == "User":
			return 1 if filters in self.state.active_users else 0
		if doctype == export_service.EXPORT_DOCTYPE and isinstance(filters, dict):
			doc = self.state.existing_by_key.get(filters.get("active_dedupe_key"))
			if doc and as_dict:
				return {
					key: doc.get(key)
					for key in (fieldname if isinstance(fieldname, (list, tuple)) else [fieldname])
				}
			return doc
		return None

	def sql(self, query, params=None, as_dict=False):
		self.sql_calls.append((query, params, as_dict))
		if query.lstrip().upper().startswith("UPDATE"):
			self.update_sql.append((query, params))
			return []
		if "FROM `tabDocShare`" in query:
			return list(self.state.docshares)
		if "FROM `tabSales Invoice`" in query:
			return [{"name": name} for name in self.state.invoice_names]
		return []

	def commit(self):
		self.commit_count += 1


class FakeState:
	def __init__(self):
		self.active_users = {"owner", "other"}
		self.existing_by_key = {}
		self.docshares = []
		self.invoice_names = ["SI-1"]
		self.roles = ["Accounts User"]
		self.can_export = True
		self.user_permissions = {}
		self.match_conditions = ""
		self.now = datetime(2026, 2, 1, 12, 0, 0)
		self.expired_docs = []


class FakeFrappe(types.ModuleType):
	def __init__(self, state, export_dir):
		super().__init__("frappe")
		self.__path__ = []
		self.state = state
		self.export_dir = export_dir
		self.session = types.SimpleNamespace(user="owner")
		self.db = FakeDB(state)
		self.docs = {}
		self.enqueued = []
		self.generated_names = 0
		self.PermissionError = FakePermissionError
		self.ValidationError = FakeValidationError
		self.DoesNotExistError = FakeDoesNotExistError

	def set_user(self, user):
		self.session.user = user

	def get_roles(self, user=None):
		return list(self.state.roles)

	def get_doc(self, doctype_or_dict, name=None):
		if isinstance(doctype_or_dict, dict):
			return FakeDoc(self, dict(doctype_or_dict))
		doc = self.docs.get(name)
		if doc is None:
			raise FakeDoesNotExistError(name)
		return doc

	def register_doc(self, doc):
		if not doc.get("name"):
			self.generated_names += 1
			doc["name"] = f"EXP-{self.generated_names:05d}"
		self.docs[doc["name"]] = doc
		return doc

	def get_all(self, doctype, filters=None, fields=None):
		return list(self.state.expired_docs)

	def enqueue(self, method, **kwargs):
		self.enqueued.append((method, kwargs))

	def get_site_path(self, *parts):
		path = os.path.join(self.export_dir, *parts[:-1]) if parts and parts[-1] == export_service.EXPORT_DIRECTORY else self.export_dir
		if parts and parts[-1] == export_service.EXPORT_DIRECTORY:
			path = os.path.join(self.export_dir, export_service.EXPORT_DIRECTORY)
		return path

	def whitelist(self, *args, **kwargs):
		return lambda function: function


def install_fake_frappe(export_dir):
	state = FakeState()
	frappe_module = FakeFrappe(state, export_dir)

	permissions_module = types.ModuleType("frappe.permissions")
	permissions_module.can_export = lambda *args, **kwargs: state.can_export
	permissions_module.get_user_permissions = lambda user=None: dict(state.user_permissions)

	utils_module = types.ModuleType("frappe.utils")
	utils_module.now_datetime = lambda: state.now
	utils_module.add_days = lambda value, days: value + timedelta(days=days)
	utils_module.cint = lambda value: int(value or 0)
	utils_module.nowdate = lambda: state.now.date().isoformat()

	desk_module = types.ModuleType("frappe.desk")
	desk_module.__path__ = []
	reportview_module = types.ModuleType("frappe.desk.reportview")
	reportview_module.build_match_conditions = lambda doctype: state.match_conditions

	commands_module = types.ModuleType("frappe.commands")
	commands_module.pass_context = lambda function: function

	modules = {
		"frappe": frappe_module,
		"frappe.permissions": permissions_module,
		"frappe.utils": utils_module,
		"frappe.desk": desk_module,
		"frappe.desk.reportview": reportview_module,
		"frappe.commands": commands_module,
	}
	for name, module in modules.items():
		sys.modules[name] = module
	frappe_module.permissions = permissions_module
	frappe_module.utils = utils_module
	return frappe_module, state, modules


class ExportServiceTestCase(unittest.TestCase):
	def setUp(self):
		self.tempdir = tempfile.mkdtemp(prefix="khitmee-export-test-")
		self.export_root = os.path.join(self.tempdir, "private")
		os.makedirs(self.export_root, exist_ok=True)
		self.module_snapshot = {name: sys.modules.get(name) for name in list(sys.modules) if name == "frappe" or name.startswith("frappe.")}
		self.fake, self.state, self.installed_modules = install_fake_frappe(self.export_root)
		self.old_service_frappe = export_service.frappe
		self.old_api_frappe = profit_export.frappe
		export_service.frappe = self.fake
		profit_export.frappe = self.fake

		self.authorize_patcher = patch.object(export_service, "authorize_profit_request", return_value=None)
		self.fingerprint_patcher = patch.object(export_service, "compute_permission_fingerprint", return_value="fp")
		self.restricted_patcher = patch.object(export_service, "has_restricted_match_conditions", return_value=False)
		self.digest_patcher = patch.object(
			export_service,
			"compute_exported_invoice_digest",
			return_value=("digest", ["SI-1"], False),
		)
		self.authorize = self.authorize_patcher.start()
		self.fingerprint = self.fingerprint_patcher.start()
		self.restricted = self.restricted_patcher.start()
		self.digest = self.digest_patcher.start()
		for patcher in (
			self.authorize_patcher,
			self.fingerprint_patcher,
			self.restricted_patcher,
			self.digest_patcher,
		):
			self.addCleanup(patcher.stop)
		self.addCleanup(self.restore_modules)
		self.addCleanup(shutil.rmtree, self.tempdir, True)

	def restore_modules(self):
		export_service.frappe = self.old_service_frappe
		profit_export.frappe = self.old_api_frappe
		for name in list(sys.modules):
			if name == "frappe" or name.startswith("frappe."):
				if name in self.module_snapshot and self.module_snapshot[name] is not None:
					sys.modules[name] = self.module_snapshot[name]
				else:
					sys.modules.pop(name, None)

	def make_log(self, **overrides):
		export_uuid = overrides.pop("export_uuid", "11111111-2222-3333-4444-555555555555")
		values = {
			"name": "EXP-TEST",
			"doctype": export_service.EXPORT_DOCTYPE,
			"requested_by": "owner",
			"filters_json": json.dumps({"company": "Company", "from_date": "2026-01-01", "to_date": "2026-01-31"}),
			"filters_hash": "hash",
			"source": "ui",
			"status": "Completed",
			"active_dedupe_key": None,
			"export_uuid": export_uuid,
			"permission_fingerprint": "fp",
			"artifact_published": 1,
			"exported_invoice_sha256": "digest",
			"row_count": 2,
			"expires_at": self.state.now + timedelta(hours=1),
			"error": None,
		}
		values.update(overrides)
		return self.fake.register_doc(FakeDoc(self.fake, values))

	def artifact_path(self, export_uuid="11111111-2222-3333-4444-555555555555"):
		directory = os.path.join(self.export_root, export_service.EXPORT_DIRECTORY)
		os.makedirs(directory, exist_ok=True)
		return os.path.join(directory, f"{export_uuid}.csv")

	def write_artifact(self, body=b"invoice,amount\nSI-1,10\n"):
		path = self.artifact_path()
		with open(path, "wb") as artifact:
			artifact.write(body)
		return path

	def download(self, name="EXP-TEST"):
		return profit_export.download_export(name)


class TestEnqueueAndWorker(ExportServiceTestCase):
	def test_enqueue_binds_session_user_and_schedules_rq_only_after_commit(self):
		result = export_service.enqueue_profit_csv_export({"company": "Company", "from_date": "2026-01-01"})

		self.assertFalse(result["deduplicated"])
		doc = self.fake.get_doc(export_service.EXPORT_DOCTYPE, result["export_log_name"])
		self.assertEqual(doc["requested_by"], "owner")
		self.assertEqual(doc["source"], "ui")
		self.assertEqual(doc["status"], "Queued")
		self.assertEqual(doc["artifact_published"], 0)
		self.assertIsNone(doc["exported_invoice_sha256"])
		self.assertEqual(doc["active_dedupe_key"], f"owner:{doc['filters_hash']}")
		self.assertEqual(len(self.fake.db.after_commit.callbacks), 1)
		self.assertEqual(self.fake.enqueued, [])

		self.fake.db.after_commit.callbacks[0]()
		self.assertEqual(self.fake.enqueued[0][0], "khitmee_ops.profit_summary.export_service.run_profit_csv_export")
		self.assertEqual(self.fake.enqueued[0][1]["export_log_name"], result["export_log_name"])

	def test_permission_names_accept_frappe_user_permission_doc_shape(self):
		self.assertEqual(
			export_service._permission_names({"Company": [{"doc": "Company B"}, {"doc": "Company A"}]}, "Company"),
			["Company A", "Company B"],
		)

	def test_active_dedupe_key_reuses_queued_or_running_export(self):
		first = export_service.enqueue_profit_csv_export({"company": "Company"})
		doc = self.fake.get_doc(export_service.EXPORT_DOCTYPE, first["export_log_name"])
		self.state.existing_by_key[doc["active_dedupe_key"]] = doc

		second = export_service.enqueue_profit_csv_export({"company": "Company"})

		self.assertTrue(second["deduplicated"])
		self.assertEqual(second["export_log_name"], first["export_log_name"])

	def test_invoice_csv_writer_preserves_phase_one_then_phase_zero_then_total_order(self):
		columns = [{"fieldname": "sales_invoice", "label": "Invoice"}]
		rows = [
			{"sales_invoice": "SI-MAIN-2"},
			{"sales_invoice": "SI-MAIN-1"},
			{"sales_invoice": "SI-UNABSORBED-RETURN"},
			{"sales_invoice": "Total"},
		]
		path = os.path.join(self.tempdir, "profit.csv")
		with patch.object(export_service, "run_detail_gross_profit", return_value=(columns, rows)):
			row_count = export_service.write_profit_csv({"group_by": "Invoice"}, path)

		with open(path, newline="", encoding="utf-8") as csv_file:
			written = list(csv.reader(csv_file))
		self.assertEqual(row_count, 4)
		self.assertEqual([row[0] for row in written], ["Invoice", "SI-MAIN-2", "SI-MAIN-1", "SI-UNABSORBED-RETURN", "Total"])

	def test_worker_renames_before_single_completion_commit_and_preserves_user(self):
		doc = self.make_log(status="Queued", artifact_published=0, exported_invoice_sha256=None)
		self.restricted.return_value = True
		self.fake.set_user("worker-runner")
		columns = [{"fieldname": "sales_invoice", "label": "Invoice"}]
		rows = [{"sales_invoice": "SI-1"}, {"sales_invoice": "Total"}]

		with patch.object(export_service, "run_detail_gross_profit", return_value=(columns, rows)) as runner:
			export_service.run_profit_csv_export(doc["name"])

		self.assertEqual(self.fake.session.user, "worker-runner")
		runner.assert_called_once()
		self.assertEqual(runner.call_args.kwargs["invoice_ids"], ["SI-1"])
		_temporary_path, final_path = export_service.get_export_paths(doc["export_uuid"])
		self.assertTrue(os.path.exists(final_path))
		completion_update, completion_params = self.fake.db.update_sql[-1]
		self.assertIn("`artifact_published` = 1", completion_update)
		self.assertIn("`exported_invoice_sha256` = %s", completion_update)
		self.assertIn("`active_dedupe_key` = NULL", completion_update)
		self.assertIn("`status` = 'Completed'", completion_update)
		self.assertEqual(completion_params[0], "digest")
		self.assertEqual(completion_params[1], 2)

	def test_cleanup_deletes_artifacts_but_preserves_publish_flag_and_digest(self):
		path = self.write_artifact()
		doc = self.make_log(status="Completed", artifact_published=1, exported_invoice_sha256="digest")
		self.state.expired_docs = [doc]

		count = export_service.cleanup_expired_exports()

		self.assertEqual(count, 1)
		self.assertFalse(os.path.exists(path))
		self.assertTrue(self.fake.db.update_sql)
		update_sql, params = self.fake.db.update_sql[-1]
		self.assertIn("`status` = %s", update_sql)
		self.assertIn("`active_dedupe_key` = NULL", update_sql)
		self.assertNotIn("`artifact_published`", update_sql)
		self.assertNotIn("`exported_invoice_sha256`", update_sql)
		self.assertEqual(params[0], "Expired")


class TestDownloadPrecedence(ExportServiceTestCase):
	def test_missing_log_returns_404(self):
		response = self.download("MISSING")
		self.assertEqual(response.status_code, 404)

	def test_non_owner_gets_403_before_other_checks(self):
		self.make_log()
		self.write_artifact()
		self.fake.set_user("other")

		response = self.download()
		self.assertEqual(response.status_code, 403)

	def test_revoked_owner_gets_403(self):
		self.make_log()
		self.write_artifact()
		self.authorize.side_effect = FakePermissionError("revoked")

		response = self.download()
		self.assertEqual(response.status_code, 403)

	def test_restricted_queued_export_gets_409(self):
		self.make_log(status="Queued", artifact_published=0)
		self.restricted.return_value = True

		response = self.download()
		self.assertEqual(response.status_code, 409)

	def test_restricted_unpublished_expired_export_gets_410(self):
		self.make_log(
			status="Completed",
			artifact_published=0,
			expires_at=self.state.now - timedelta(seconds=1),
		)
		self.restricted.return_value = True

		response = self.download()
		self.assertEqual(response.status_code, 410)

	def test_restricted_revoked_published_expired_export_gets_403_before_410(self):
		self.make_log(expires_at=self.state.now - timedelta(seconds=1))
		self.restricted.return_value = True
		self.authorize.side_effect = FakePermissionError("revoked")

		response = self.download()
		self.assertEqual(response.status_code, 403)

	def test_final_file_without_completed_commit_still_gets_409(self):
		# Astra guardrail: a crash after atomic rename but before the publish commit
		# must not make the artifact downloadable.
		self.make_log(status="Running", artifact_published=0)
		self.write_artifact()

		response = self.download()
		self.assertEqual(response.status_code, 409)

	def test_completed_export_returns_werkzeug_stream_with_secure_headers(self):
		self.make_log()
		self.write_artifact()

		response = self.download()

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.headers["Content-Type"], "text/csv; charset=utf-8")
		self.assertEqual(
			response.headers["Content-Disposition"],
			'attachment; filename="fast-gross-profit-2026-01-01-to-2026-01-31.csv"',
		)
		self.assertEqual(response.headers["Cache-Control"], "private, no-store")
		self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
		self.assertTrue(response.direct_passthrough)
		self.assertEqual(response.get_data(), b"invoice,amount\nSI-1,10\n")


class TestNativeExportClosureAndCli(ExportServiceTestCase):
	def test_execute_adapters_return_two_tuple_or_script_report_three_tuple(self):
		import khitmee_ops.profit_summary as profit_summary_package

		self.fake._dict = fast_gross_profit._ReportDict
		self.fake.get_cached_value = lambda *args, **kwargs: "USD"
		result = {"mode": "pending", "columns": ["c"], "data": ["unexpected"], "message": "<b>pending</b>"}
		old_query_profit = getattr(profit_summary_package, "query_profit", None)
		profit_summary_package.query_profit = lambda filters: result
		try:
			with patch.object(fast_gross_profit, "standard_gross_profit", object()), patch.object(
				fast_gross_profit, "frappe", self.fake
			), patch.object(fast_gross_profit, "validate_date_range", lambda filters: None):
				self.assertEqual(
					fast_gross_profit.execute({"company": "Company"}),
					(["c"], [], "<b>pending</b>"),
				)
				result.update({"mode": "detail", "data": ["row"]})
				self.assertEqual(fast_gross_profit.execute({"company": "Company"}), (["c"], ["row"]))
		finally:
			if old_query_profit is None:
				try:
					delattr(profit_summary_package, "query_profit")
				except AttributeError:
					pass
			else:
				profit_summary_package.query_profit = old_query_profit

	def test_native_background_export_query_is_rejected(self):
		self.fake.form_dict = {
			"cmd": "frappe.desk.query_report.export_query",
			"export_in_background": "1",
		}
		with patch.object(fast_gross_profit, "standard_gross_profit", object()), patch.object(
			fast_gross_profit, "frappe", self.fake
		):
			with self.assertRaises(FakePermissionError) as raised:
				fast_gross_profit.execute({"company": "Company"})
		self.assertIn("Background email export is disabled", str(raised.exception))

	def test_cli_registers_export_and_lazy_parity_commands(self):
		class FakeCommand:
			def __init__(self, name, callback):
				self.name = name
				self.callback = callback

		click_module = types.ModuleType("click")
		click_module.command = lambda name: (lambda function: FakeCommand(name, function))
		click_module.option = lambda *args, **kwargs: (lambda function: function)
		click_module.echo = lambda *args, **kwargs: None

		old_commands_module = sys.modules.pop("khitmee_ops.commands", None)
		try:
			with patch.dict(sys.modules, {"click": click_module}):
				commands_module = importlib.import_module("khitmee_ops.commands")

			self.assertEqual(
				[command.name for command in commands_module.commands],
				["khitmee-setup-indexes", "khitmee-export-profit", "khitmee-check-profit-parity"],
			)
			export_source = inspect.getsource(commands_module.khitmee_export_profit.callback)
			parity_source = inspect.getsource(commands_module.khitmee_check_profit_parity.callback)
			self.assertIn("enqueue_profit_csv_export_for_user", export_source)
			self.assertIn('source="cli"', export_source)
			self.assertIn("from khitmee_ops.profit_summary.parity_check import run_profit_parity_check", parity_source)
			self.assertIn('sys.exit(result["exit_code"])', parity_source)
		finally:
			sys.modules.pop("khitmee_ops.commands", None)
			if old_commands_module is not None:
				sys.modules["khitmee_ops.commands"] = old_commands_module


if __name__ == "__main__":
	unittest.main()
