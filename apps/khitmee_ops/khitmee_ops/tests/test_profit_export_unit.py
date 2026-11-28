import csv
import importlib
import inspect
import json
import os
import shutil
import sqlite3
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
from khitmee_ops.khitmee_ops.report.fast_gross_profit import fast_gross_profit, invoice_batches
from khitmee_ops.profit_summary import budget, export_service


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

	def run(self):
		while self.callbacks:
			self.callbacks.pop(0)()


class FakeDB:
	def __init__(self, state):
		self.state = state
		self.after_commit = FakeAfterCommit()
		self.after_rollback = FakeAfterCommit()
		self.sql_calls = []
		self.update_sql = []
		self.commit_count = 0
		self.external_admission_lock = False
		self.admission_locks = 0

	def get_value(self, doctype, filters=None, fieldname=None, as_dict=False):
		if doctype == "User":
			return 1 if filters in self.state.active_users else 0
		if doctype == export_service.EXPORT_DOCTYPE and isinstance(filters, str):
			return self.state.docs.get(filters, {}).get(fieldname)
		return None

	def sql(self, query, params=None, as_dict=False):
		self.sql_calls.append((query, params, as_dict))
		if "GET_LOCK(" in query:
			if self.external_admission_lock:
				return [(0,)]
			self.admission_locks += 1
			return [(1,)]
		if "RELEASE_LOCK(" in query:
			self.admission_locks -= 1
			return [(1,)]
		if query.lstrip().upper().startswith("UPDATE"):
			self.update_sql.append((query, params))
			doc = self.state.docs[params[-1]]
			status = "Completed" if "`status` = 'Completed'" in query else params[0]
			doc.update(status=status, modified=self.state.now)
			if "`active_dedupe_key` = NULL" in query:
				doc["active_dedupe_key"] = None
			if status == "Completed":
				doc.update(artifact_published=1, exported_invoice_sha256=params[0], row_count=params[1])
			return []
		if "FOR UPDATE" in query and "Khitmee Profit Export Log" in query:
			if "WHERE `name`" in query:
				doc = self.state.docs.get(params[0])
				return [doc] if doc else []
			active = [
				doc for doc in self.state.docs.values()
				if doc.get("active_dedupe_key") and doc["status"] in {"Queued", "Running"}
			]
			return sorted(active, key=lambda doc: doc["active_dedupe_key"] != params[0])[:1]
		if "FROM `tabDocShare`" in query:
			return list(self.state.docshares)
		if "FROM `tabSales Invoice`" in query:
			names = self.state.invoice_names
			if "`name` IN (" in query:
				placeholders = query.split("`name` IN (", 1)[1].split(")", 1)[0]
				bound_names = set(params[-placeholders.count("%s"):])
				names = sorted(bound_names.intersection(names))
			return [{"name": name} for name in names]
		return []

	def commit(self):
		self.commit_count += 1
		self.after_rollback.callbacks.clear()
		self.after_commit.run()

	def rollback(self):
		self.rollback_count = getattr(self, "rollback_count", 0) + 1
		self.after_commit.callbacks.clear()
		self.after_rollback.run()


class FakeState:
	def __init__(self):
		self.active_users = {"owner", "other"}
		self.docs = {}
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
		self.conf = {"encryption_key": "test-export-signing-secret"}
		self.session = types.SimpleNamespace(user="owner")
		self.db = FakeDB(state)
		self.docs = state.docs
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
		doc.setdefault("modified", self.state.now)
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
		self.old_budget_frappe = budget.frappe
		export_service.frappe = self.fake
		profit_export.frappe = self.fake
		budget.frappe = self.fake

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
		budget.frappe = self.old_budget_frappe
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
		self.assertEqual(self.fake.enqueued, [])
		self.assertEqual(self.fake.db.admission_locks, 1)

		self.fake.db.commit()
		self.assertEqual(self.fake.db.admission_locks, 0)
		self.assertEqual(self.fake.enqueued[0][0], "khitmee_ops.profit_summary.export_service.run_profit_csv_export")
		self.assertEqual(self.fake.enqueued[0][1]["export_log_name"], result["export_log_name"])

	def test_permission_names_accept_frappe_user_permission_doc_shape(self):
		self.assertEqual(
			export_service._permission_names({"Company": [{"doc": "Company B"}, {"doc": "Company A"}]}, "Company"),
			["Company A", "Company B"],
		)

	def test_active_dedupe_key_reuses_queued_or_running_export(self):
		first = export_service.enqueue_profit_csv_export({"company": "Company"})
		self.fake.db.commit()

		second = export_service.enqueue_profit_csv_export({"company": "Company"})

		self.assertTrue(second["deduplicated"])
		self.assertEqual(second["export_log_name"], first["export_log_name"])
		self.fake.db.commit()
		self.assertEqual(len(self.fake.enqueued), 1)

	def test_one_slot_bounds_queued_and_running_exports_across_requesters(self):
		first = export_service.enqueue_profit_csv_export({"company": "Company"})
		self.fake.db.commit()
		doc = self.fake.docs[first["export_log_name"]]

		for status in ("Queued", "Running"):
			with self.subTest(status=status):
				doc["status"] = status
				self.fake.set_user("other")
				with self.assertRaises(export_service.ProfitExportBusyError) as denied:
					export_service.enqueue_profit_csv_export({"company": "Company"})
				self.assertEqual(denied.exception.http_status_code, 429)
				self.assertEqual(len(self.fake.docs), 1)
				self.fake.db.rollback()
				self.assertEqual(self.fake.db.admission_locks, 0)

		doc.update(status="Completed", active_dedupe_key=None)
		second = export_service.enqueue_profit_csv_export({"company": "Company"})
		self.assertNotEqual(second["export_log_name"], first["export_log_name"])
		self.assertEqual(self.fake.docs[second["export_log_name"]]["requested_by"], "other")

	def test_concurrent_admission_fails_without_queueing_or_waiting(self):
		self.fake.db.external_admission_lock = True
		with self.assertRaises(export_service.ProfitExportBusyError):
			export_service.enqueue_profit_csv_export({"company": "Company"})
		self.assertEqual(self.fake.docs, {})
		self.assertEqual(self.fake.enqueued, [])

	def test_dispatch_failure_releases_slot_for_retry(self):
		first = export_service.enqueue_profit_csv_export({"company": "Company"})
		with patch.object(self.fake, "enqueue", side_effect=OSError("RQ unavailable")), self.assertRaises(OSError):
			self.fake.db.commit()
		doc = self.fake.docs[first["export_log_name"]]
		self.assertEqual(doc["status"], "Failed")
		self.assertIsNone(doc["active_dedupe_key"])
		self.assertEqual(self.fake.db.admission_locks, 0)

		second = export_service.enqueue_profit_csv_export({"company": "Company"})
		self.assertNotEqual(second["export_log_name"], first["export_log_name"])

	def test_cli_inline_claim_does_not_dispatch_a_second_copy(self):
		result = export_service.enqueue_profit_csv_export_for_user({"company": "Company"}, "owner")
		with patch.object(export_service, "write_profit_csv", side_effect=OSError("stop before publish")), self.assertRaises(OSError):
			export_service.run_profit_csv_export(result["export_log_name"])
		self.assertEqual(self.fake.enqueued, [])
		self.assertEqual(self.fake.docs[result["export_log_name"]]["status"], "Failed")

	def test_status_is_small_and_bound_to_requester_and_scope(self):
		self.make_log()
		result = export_service.get_profit_csv_export_status("EXP-TEST")
		self.assertEqual(result, {
			"export_log_name": "EXP-TEST", "status": "Completed", "deduplicated": False,
			"expires_at": "2026-02-01 13:00:00", "artifact_published": 1,
		})
		self.fake.set_user("other")
		with self.assertRaises(FakePermissionError):
			export_service.get_profit_csv_export_status("EXP-TEST")
		self.fake.set_user("owner")
		self.fingerprint.return_value = "changed"
		with self.assertRaises(FakePermissionError):
			export_service.get_profit_csv_export_status("EXP-TEST")

	def test_invoice_csv_writer_preserves_phase_one_then_phase_zero_then_total_order(self):
		columns = [
			{"fieldname": "sales_invoice", "label": "Invoice"},
			{"fieldname": "selling_amount", "label": "Selling"},
		]
		rows = [
			{"sales_invoice": "SI-MAIN-2", "selling_amount": 100, "indent": 1},
			{"sales_invoice": "SI-MAIN-1", "selling_amount": 2.5, "indent": 1},
			{"sales_invoice": "SI-UNABSORBED-RETURN", "selling_amount": -7.5, "indent": 1},
		]
		path = os.path.join(self.tempdir, "profit.csv")
		with patch.object(invoice_batches, "iter_invoice_detail_batches", return_value=[(columns, rows[:2]), (columns, rows[2:])]), patch.object(
			fast_gross_profit, "invoice_total_row",
			side_effect=lambda filters, selling, buying: {"sales_invoice": "Total", "selling_amount": selling},
		):
			row_count = export_service.write_profit_csv({"group_by": "Invoice", "currency": "MMK"}, path)

		with open(path, newline="", encoding="utf-8") as csv_file:
			written = list(csv.reader(csv_file))
		self.assertEqual(row_count, 4)
		self.assertEqual([row[0] for row in written], ["Invoice", "SI-MAIN-2", "SI-MAIN-1", "SI-UNABSORBED-RETURN", "Total"])
		self.assertEqual(written[-1], ["Total", "95.0"])

	def test_invoice_csv_writer_uses_query_report_fieldname_fallback(self):
		columns = [
			{"fieldname": "sales_invoice", "label": "Sales Invoice"},
			{"label": "Brand", "fieldtype": "Link", "options": "Brand", "width": 100},
			{"label": "Net Unit-Price", "fieldtype": "Currency"},
			{},
		]
		rows = [{"sales_invoice": "ACC-SINV-2026-317799", "brand": "Phsar Select", "net_unit_price": 7, "3": "x"}]
		path = os.path.join(self.tempdir, "profit-brand.csv")
		with patch.object(invoice_batches, "iter_invoice_detail_batches", return_value=[(columns, rows)]), patch.object(
			fast_gross_profit, "invoice_total_row", return_value={"sales_invoice": "Total"},
		):
			row_count = export_service.write_profit_csv({"group_by": "Invoice", "currency": "MMK"}, path)

		with open(path, newline="", encoding="utf-8") as csv_file:
			written = list(csv.reader(csv_file))
		self.assertEqual(row_count, 2)
		self.assertEqual(
			written,
			[["Sales Invoice", "Brand", "Net Unit-Price", ""], ["ACC-SINV-2026-317799", "Phsar Select", "7", "x"], ["Total", "", "", ""]],
		)

	def test_invoice_csv_fetches_and_flushes_bounded_batches(self):
		batch_size = invoice_batches.INVOICE_KEY_BATCH_SIZE
		invoice_count = 3 * batch_size + 1
		columns = [{"fieldname": "sales_invoice"}]
		path = Path(self.tempdir) / "bounded.csv"
		keys_read = 0
		batch_sizes = []

		def keys(*args, **kwargs):
			nonlocal keys_read
			for index in range(invoice_count):
				keys_read += 1
				yield 1, f"SI-{index:04d}", 0

		def fetch(filters, *, invoice_ids, invoice_phase, invoice_row_limit):
			self.assertEqual(invoice_phase, 1)
			self.assertLessEqual(len(invoice_ids), batch_size)
			self.assertLessEqual(keys_read - sum(batch_sizes), batch_size + 1)
			if batch_sizes:
				self.assertGreater(path.stat().st_size, 0)
			self.assertEqual(
				invoice_row_limit,
				invoice_batches.MAX_SINGLE_FAMILY_ROWS if len(invoice_ids) == 1 else invoice_batches.INVOICE_BATCH_ROWS,
			)
			batch_sizes.append(len(invoice_ids))
			return columns, [{"sales_invoice": name} for name in invoice_ids] + [{"sales_invoice": "Total"}]

		with patch.object(invoice_batches, "_invoice_keys", side_effect=keys), patch.object(
			fast_gross_profit, "run_detail_gross_profit", side_effect=fetch
		), patch.object(fast_gross_profit, "validate_date_range"), patch.object(
			fast_gross_profit, "invoice_total_row", return_value={"sales_invoice": "Total"}
		):
			rows = export_service.write_profit_csv({"group_by": "Invoice", "currency": "MMK"}, path)

		self.assertEqual(batch_sizes, [batch_size, batch_size, batch_size, 1])
		self.assertEqual(rows, invoice_count + 1)
		with path.open(newline="", encoding="utf-8") as output:
			self.assertEqual(list(csv.reader(output)), [
				["sales_invoice"], *([f"SI-{index:04d}"] for index in range(invoice_count)), ["Total"],
			])

	def test_invoice_sort_uses_ascending_invoice_tie_break(self):
		rows = [
			{"parent": "ACC-SINV-2026-317777", "posting_date": "2026-11-25", "posting_time": "10:00:00"},
			{"parent": "ACC-SINV-2026-317776", "posting_date": "2026-11-25", "posting_time": "10:00:00"},
		]

		ordered = fast_gross_profit.sort_invoice_rows_descending(rows)

		self.assertEqual(
			[row["parent"] for row in ordered],
			["ACC-SINV-2026-317776", "ACC-SINV-2026-317777"],
		)

	def test_export_service_exposes_whitelisted_enqueue_json_and_download_delegation(self):
		result = export_service.enqueue_profit_csv_export(json.dumps({"company": "Company", "from_date": "2026-01-01"}))
		self.assertFalse(result["deduplicated"])
		self.assertEqual(result["status"], "Queued")

		self.make_log()
		self.write_artifact(b"Sales Invoice,Brand\nSI-1,Phsar Select\n")
		response = export_service.download_profit_csv_export("EXP-TEST")
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.get_data(), b"Sales Invoice,Brand\nSI-1,Phsar Select\n")

		self.fake.set_user("other")
		forbidden = export_service.download_profit_csv_export("EXP-TEST")
		self.assertEqual(forbidden.status_code, 403)

		whitelisted = []

		def record_whitelist(*args, **kwargs):
			def decorator(function):
				whitelisted.append(function.__name__)
				return function

			return decorator

		self.fake.whitelist = record_whitelist
		importlib.reload(export_service)
		try:
			self.assertIn("enqueue_profit_csv_export", whitelisted)
			self.assertIn("download_profit_csv_export", whitelisted)
			self.assertIn("get_profit_csv_export_status", whitelisted)
		finally:
			export_service.frappe = self.fake

	def test_worker_renames_before_single_completion_commit_and_preserves_user(self):
		doc = self.make_log(status="Queued", artifact_published=0, exported_invoice_sha256=None)
		self.restricted.return_value = True
		self.fake.set_user("worker-runner")
		columns = [{"fieldname": "sales_invoice", "label": "Invoice"}]
		rows = [{"sales_invoice": "SI-1", "indent": 0}]
		self.digest.return_value = ("digest", ["SI-1", "SI-ELSEWHERE"], False)
		doc["filters_hash"] = export_service.compute_filters_hash(json.loads(doc["filters_json"]))
		temporary_path, final_path = export_service.get_export_paths(doc["export_uuid"])
		scope_tmp, scope_path = export_service._export_scope_paths(doc["export_uuid"])

		def batch(*args, **kwargs):
			self.assertEqual(self.fake.session.user, "owner")
			self.assertEqual(self.fake.db.commit_count, 1)
			self.assertIn("START TRANSACTION WITH CONSISTENT SNAPSHOT", [call[0] for call in self.fake.db.sql_calls])
			self.assertFalse(os.path.exists(final_path))
			kwargs["export_scope"].include(["SI-1"])
			yield columns, rows

		with patch.object(invoice_batches, "iter_invoice_detail_batches", side_effect=batch) as runner, patch.object(
			fast_gross_profit, "invoice_total_row", return_value={"sales_invoice": "Total"}
		), patch.object(self.fake, "get_cached_value", return_value="MMK", create=True):
			export_service.run_profit_csv_export(doc["name"])

		self.assertEqual(self.fake.session.user, "worker-runner")
		runner.assert_called_once()
		self.assertEqual(runner.call_args.kwargs["invoice_ids"], ["SI-1", "SI-ELSEWHERE"])
		self.assertTrue(os.path.exists(final_path))
		self.assertFalse(os.path.exists(temporary_path))
		self.assertFalse(os.path.exists(scope_tmp))
		manifest = json.loads(Path(scope_path).read_bytes())
		self.assertEqual(manifest["scope"]["invoice_names"], ["SI-1"])
		self.assertEqual(manifest["scope"]["requested_by"], "owner")
		self.assertEqual(self.fake.db.commit_count, 2)
		completion_update, completion_params = self.fake.db.update_sql[-1]
		self.assertIn("`artifact_published` = 1", completion_update)
		self.assertIn("`exported_invoice_sha256` = %s", completion_update)
		self.assertIn("`active_dedupe_key` = NULL", completion_update)
		self.assertIn("`status` = 'Completed'", completion_update)
		self.assertEqual(completion_params[0], "digest")
		self.assertEqual(completion_params[1], 2)
		plain_sql = [call[0] for call in self.fake.db.sql_calls]
		self.assertIn("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ", plain_sql)
		self.assertIn("START TRANSACTION WITH CONSISTENT SNAPSHOT", plain_sql)
		self.fake.set_user("other")
		self.assertEqual(export_service.download_profit_csv_export(doc["name"]).status_code, 403)

	def test_worker_manifest_requires_visibility_of_absorbed_return_without_its_own_header(self):
		class Row(dict):
			__getattr__ = dict.get

		doc = self.make_log(status="Queued", artifact_published=0, exported_invoice_sha256=None)
		filters = {**json.loads(doc["filters_json"]), "sales_invoice": "SI-1", "currency": "MMK"}
		doc["filters_json"] = json.dumps(filters)
		doc["filters_hash"] = export_service.compute_filters_hash(filters)
		self.state.match_conditions = "`tabSales Invoice`.`customer` = 'allowed'"
		self.state.invoice_names = ["SI-1", "RET-1"]
		self.restricted.return_value = True

		def build(generator, filters, *, invoice_ids, invoice_phase, invoice_row_limit, export_scope):
			self.assertEqual(invoice_ids, ("SI-1",))
			self.assertEqual(export_scope.match_conditions, self.state.match_conditions)
			generator.si_list = [Row(parent="SI-1")]
			generator._export_scope = export_scope
			generator._build_returned_invoice_query = lambda parents: types.SimpleNamespace(limit=lambda size: parents)
			generator._run_invoice_query = lambda query: [Row(
				name="RET-1", return_against="SI-1", sales_invoice_item="LINE-1", item_code="ITEM", base_amount=-25,
			)]
			generator._invoice_row_limit = invoice_row_limit
			generator.get_returned_invoice_items()

		def render(generator, report_filters):
			sale = 100 + generator.returned_invoices["SI-1"]["LINE-1"][0].base_amount
			columns = [{"fieldname": "sales_invoice"}, {"fieldname": "selling_amount"}]
			return columns, [
				{"sales_invoice": "SI-1", "indent": 0, "selling_amount": sale},
				{"sales_invoice": "ITEM", "indent": 1, "selling_amount": sale},
				{"sales_invoice": "Total", "selling_amount": sale},
			]

		with patch.object(self.fake, "_dict", Row, create=True), patch.object(fast_gross_profit, "frappe", self.fake), patch.object(
			fast_gross_profit, "standard_gross_profit", object()
		), patch.object(fast_gross_profit.FastGrossProfitGenerator, "__init__", build), patch.object(
			fast_gross_profit, "_render_detail_result", side_effect=render
		), patch.object(fast_gross_profit, "validate_date_range"), patch.object(
			invoice_batches, "_invoice_keys", return_value=[(1, "SI-1", 0)]
		), patch.object(fast_gross_profit, "invoice_total_row", return_value={"sales_invoice": "Total", "selling_amount": 75}):
			export_service.run_profit_csv_export(doc["name"])

		with Path(self.artifact_path()).open(newline="", encoding="utf-8") as artifact:
			self.assertEqual(list(csv.reader(artifact)), [
				["sales_invoice", "selling_amount"], ["SI-1", "75"], ["ITEM", "75"], ["Total", "75"],
			])
		_, scope_path = export_service._export_scope_paths(doc["export_uuid"])
		self.assertEqual(json.loads(Path(scope_path).read_bytes())["scope"]["invoice_names"], ["RET-1", "SI-1"])
		self.state.invoice_names.append("SI-NEW")
		self.assertTrue(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))
		self.state.invoice_names.remove("RET-1")
		self.assertEqual(export_service.compute_permission_fingerprint(filters, "owner"), doc["permission_fingerprint"])
		self.assertFalse(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))

	def test_worker_rechecks_requester_scope_before_writing(self):
		doc = self.make_log(status="Queued", artifact_published=0)
		self.fingerprint.return_value = "revoked"
		self.fake.set_user("worker-runner")
		with patch.object(export_service, "write_profit_csv") as write, self.assertRaises(FakePermissionError):
			export_service.run_profit_csv_export(doc["name"])
		write.assert_not_called()
		self.assertEqual(doc["status"], "Failed")
		self.assertEqual(self.fake.session.user, "worker-runner")
		self.authorize.assert_called_once_with(json.loads(doc["filters_json"]), "owner", require_export=True)

	def test_posting_between_batches_keeps_the_worker_snapshot_rows_and_values(self):
		doc = self.make_log(status="Queued", artifact_published=0)
		doc["filters_json"] = json.dumps({**json.loads(doc["filters_json"]), "currency": "MMK"})
		ledger_path = Path(self.tempdir) / "snapshot.db"
		reader = sqlite3.connect(ledger_path)
		poster = sqlite3.connect(ledger_path)
		self.addCleanup(reader.close)
		self.addCleanup(poster.close)
		poster.executescript("""
			PRAGMA journal_mode=WAL;
			CREATE TABLE ledger (invoice TEXT PRIMARY KEY, amount INTEGER);
			INSERT INTO ledger VALUES ('SI-A', 7), ('SI-C', 11);
		""")
		columns = [{"fieldname": name} for name in ("sales_invoice", "selling_amount")]
		original_sql = self.fake.db.sql

		def sql(query, *args, **kwargs):
			if query == "START TRANSACTION WITH CONSISTENT SNAPSHOT":
				reader.execute("BEGIN")
				reader.execute("SELECT COUNT(*) FROM ledger").fetchone()
			return original_sql(query, *args, **kwargs)

		def keys(*args, **kwargs):
			cursor = ""
			while row := reader.execute(
				"SELECT invoice FROM ledger WHERE invoice > ? ORDER BY invoice LIMIT 1", (cursor,)
			).fetchone():
				cursor = row[0]
				yield 1, cursor, 0

		def fetch(filters, *, invoice_ids, invoice_phase, invoice_row_limit):
			if invoice_ids == ["SI-A"]:
				poster.executescript("UPDATE ledger SET amount = 1100 WHERE invoice = 'SI-C'; INSERT INTO ledger VALUES ('SI-D', 999);")
			name, amount = reader.execute("SELECT * FROM ledger WHERE invoice = ?", invoice_ids).fetchone()
			return columns, [{"sales_invoice": name, "selling_amount": amount, "indent": 1}, {"sales_invoice": "Total"}]

		with patch.object(self.fake.db, "sql", side_effect=sql), patch.object(
			invoice_batches, "_invoice_keys", side_effect=keys
		), patch.object(invoice_batches, "INVOICE_KEY_BATCH_SIZE", 1), patch.object(
			fast_gross_profit, "run_detail_gross_profit", side_effect=fetch
		), patch.object(fast_gross_profit, "validate_date_range"), patch.object(
			fast_gross_profit, "invoice_total_row",
			side_effect=lambda filters, selling, buying: {"sales_invoice": "Total", "selling_amount": selling},
		):
			export_service.run_profit_csv_export(doc["name"])

		with Path(self.artifact_path()).open(newline="", encoding="utf-8") as output:
			self.assertEqual(list(csv.reader(output)), [
				["sales_invoice", "selling_amount"], ["SI-A", "7"], ["SI-C", "11"], ["Total", "18"],
			])
		self.assertEqual(poster.execute("SELECT SUM(amount) FROM ledger").fetchone(), (2106,))

	def test_failed_completion_removes_unpublished_csv(self):
		doc = self.make_log(status="Queued", artifact_published=0)
		self.restricted.return_value = True

		def write(filters, path, **kwargs):
			Path(path).write_text("Invoice\nSI-1\n", encoding="utf-8")
			return 1

		with patch.object(export_service, "write_profit_csv", side_effect=write), patch.object(
			export_service, "_commit_completed_export", side_effect=OSError("cannot publish")
		), self.assertRaisesRegex(OSError, "cannot publish"):
			export_service.run_profit_csv_export(doc["name"])

		export_uuid = doc["export_uuid"]
		paths = (*export_service.get_export_paths(export_uuid), *export_service._export_scope_paths(export_uuid))
		self.assertFalse(any(os.path.exists(path) for path in paths))
		self.assertEqual(doc["status"], "Failed")
		self.assertEqual(doc["artifact_published"], 0)

	def test_invoice_pages_slice_ordered_rows_across_family_and_return_phase_boundaries(self):
		filters = {"group_by": "Invoice"}
		columns = [{"fieldname": "sales_invoice"}]
		families = {
			"A": [{"sales_invoice": "A"}, {"sales_invoice": "A.1"}, {"sales_invoice": "A.2"}],
			"B": [{"sales_invoice": "B"}, {"sales_invoice": "B.1"}],
			"R": [{"sales_invoice": "R"}, {"sales_invoice": "R.1"}],
		}
		keys = [(1, "A", 3), (1, "B", 2), (0, "R", 2)]
		calls = []

		def render(report_filters, *, invoice_ids, invoice_phase, invoice_row_limit):
			calls.append((invoice_phase, tuple(invoice_ids)))
			return columns, [row for name in invoice_ids for row in families[name]] + [{"sales_invoice": "Total"}]

		def names(page):
			return [
				row["sales_invoice"]
				for _, data in invoice_batches.iter_invoice_detail_batches(filters, page=page)
				for row in data
			]

		with patch.object(invoice_batches, "INVOICE_PAGE_SIZE", 4), patch.object(
			invoice_batches, "_invoice_keys", return_value=keys
		), patch.object(fast_gross_profit, "validate_date_range"), patch.object(
			fast_gross_profit, "run_detail_gross_profit", side_effect=render
		), patch.object(fast_gross_profit, "invoice_total_row", return_value={"sales_invoice": "Total"}):
			self.assertEqual(names(1), ["A", "A.1", "A.2", "B"])
			self.assertEqual(calls, [(1, ("A", "B"))])
			self.assertEqual(names(2), ["B.1", "R", "R.1", "Total"])
			self.assertEqual(names(3), [])

	def test_invoice_stream_splits_over_budget_batches_before_emitting_rows(self):
		def render(filters, *, invoice_ids, invoice_phase, invoice_row_limit):
			if len(invoice_ids) > 1:
				raise fast_gross_profit.InvoiceBatchTooLarge()
			return [], [{"sales_invoice": invoice_ids[0]}, {"sales_invoice": "Total"}]

		with patch.object(fast_gross_profit, "run_detail_gross_profit", side_effect=render) as runner:
			batches = list(invoice_batches._render_invoice_batch({"group_by": "Invoice"}, 1, ["A", "B"]))

		self.assertEqual(batches, [([], [{"sales_invoice": "A"}]), ([], [{"sales_invoice": "B"}])])
		self.assertEqual(
			[call.kwargs["invoice_row_limit"] for call in runner.call_args_list],
			[invoice_batches.INVOICE_BATCH_ROWS, invoice_batches.MAX_SINGLE_FAMILY_ROWS, invoice_batches.MAX_SINGLE_FAMILY_ROWS],
		)

	def test_cleanup_deletes_artifacts_but_preserves_publish_flag_and_digest(self):
		path = self.write_artifact()
		doc = self.make_log(
			status="Completed", artifact_published=1, exported_invoice_sha256="digest", expires_at=self.state.now
		)
		self.state.expired_docs = [doc]
		scope_tmp, scope_path = export_service._export_scope_paths(doc["export_uuid"])
		Path(scope_tmp).touch()
		Path(scope_path).touch()

		count = export_service.cleanup_expired_exports()

		self.assertEqual(count, 1)
		self.assertFalse(os.path.exists(path))
		self.assertFalse(os.path.exists(scope_tmp))
		self.assertFalse(os.path.exists(scope_path))
		self.assertTrue(self.fake.db.update_sql)
		update_sql, params = self.fake.db.update_sql[-1]
		self.assertIn("`status` = %s", update_sql)
		self.assertIn("`active_dedupe_key` = NULL", update_sql)
		self.assertNotIn("`artifact_published`", update_sql)
		self.assertNotIn("`exported_invoice_sha256`", update_sql)
		self.assertEqual(params[0], "Expired")


class TestDownloadPrecedence(ExportServiceTestCase):
	def test_snapshot_scope_ignores_new_postings_and_denies_revoked_snapshot_invoice(self):
		doc = self.make_log()
		filters = json.loads(doc["filters_json"])
		doc["filters_hash"] = export_service.compute_filters_hash(filters)
		_, scope_path = export_service._export_scope_paths(doc["export_uuid"])
		batch_size = export_service.EXPORT_SCOPE_CHECK_BATCH_SIZE
		captured = [f"SI-{index:05d}" for index in range(batch_size + 17)]
		export_service._write_export_scope(scope_path, doc, "digest", captured)
		self.state.invoice_names = [*captured, "SI-POSTED-AFTER-SNAPSHOT"]

		self.assertTrue(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))
		queries = [(sql, params) for sql, params, _ in self.fake.db.sql_calls if "FROM `tabSales Invoice`" in sql]
		self.assertEqual([params for _, params in queries], [["Company", *captured[:batch_size]], ["Company", *captured[batch_size:]]])
		self.assertEqual([sql.count("%s") - 1 for sql, _ in queries], [batch_size, 17])

		self.state.invoice_names.remove(captured[-1])
		self.assertFalse(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))
		self.digest.assert_not_called()

	def test_signed_scope_rejects_tampering_and_replay_against_another_export(self):
		doc = self.make_log()
		filters = json.loads(doc["filters_json"])
		doc["filters_hash"] = export_service.compute_filters_hash(filters)
		_, scope_path = export_service._export_scope_paths(doc["export_uuid"])
		export_service._write_export_scope(scope_path, doc, "digest", {"SI-1"})
		with patch.object(export_service, "select_accessible_invoice_names") as lookup:
			self.assertFalse(export_service.exported_invoice_scope_is_current(doc, filters, "other"))
			self.assertFalse(export_service.exported_invoice_scope_is_current({**doc, "name": "OTHER"}, filters, "owner"))
			self.assertFalse(export_service.exported_invoice_scope_is_current(doc, {**filters, "company": "other"}, "owner"))
			manifest = json.loads(Path(scope_path).read_bytes())
			manifest["scope"]["invoice_names"] = []
			Path(scope_path).write_text(json.dumps(manifest), encoding="utf-8")
			self.assertFalse(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))
			Path(scope_path).write_bytes(b"{not json")
			self.assertFalse(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))
			lookup.assert_not_called()
		self.digest.assert_not_called()

	def test_restricted_legacy_export_without_complete_manifest_fails_closed(self):
		doc = self.make_log()
		filters = json.loads(doc["filters_json"])
		doc["filters_hash"] = export_service.compute_filters_hash(filters)
		self.assertFalse(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))
		_, scope_path = export_service._export_scope_paths(doc["export_uuid"])
		export_service._write_export_scope(scope_path, doc, "digest", {"SI-1"})
		manifest = json.loads(Path(scope_path).read_bytes())
		manifest["scope"]["version"] = 1
		manifest["signature"] = export_service._sign_export_scope(manifest["scope"])
		Path(scope_path).write_text(json.dumps(manifest), encoding="utf-8")
		self.assertFalse(export_service.exported_invoice_scope_is_current(doc, filters, "owner"))
		self.digest.assert_not_called()

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


class TestDetailBudgetIntegration(ExportServiceTestCase):
	def test_enforce_budget_preflights_before_generator_construction(self):
		budget_module = types.ModuleType("khitmee_ops.profit_summary.budget")
		budget_module.preflight_detail_input_rows = lambda filters: 35001
		budget_module.evaluate_work_budget = lambda filters, counts: {
			"outcome": "budget_exceeded",
			"reason": "detail_input_rows_exceeds_sync_row_budget",
		}
		with patch.dict(sys.modules, {"khitmee_ops.profit_summary.budget": budget_module}), patch.object(
			fast_gross_profit, "standard_gross_profit", object()
		), patch.object(fast_gross_profit, "validate_date_range", lambda filters: None), patch.object(
			fast_gross_profit, "frappe", self.fake
		), patch.object(fast_gross_profit, "FastGrossProfitGenerator") as generator:
			with self.assertRaises(FakeValidationError) as raised:
				fast_gross_profit.run_detail_gross_profit({"company": "Company"}, enforce_budget=True)
		generator.assert_not_called()
		self.assertIn("detail_input_rows_exceeds_sync_row_budget", str(raised.exception))


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
				[
					"khitmee-setup-indexes",
					"khitmee-export-profit",
					"khitmee-check-profit-parity",
					"khitmee-reconcile-profit-summary",
					"khitmee-prepare-profit-summary",
				],
			)
			reconcile_source = inspect.getsource(commands_module.khitmee_reconcile_profit_summary.callback)
			self.assertIn("from khitmee_ops.profit_summary.reconcile import reconcile_with_coverage", reconcile_source)
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
