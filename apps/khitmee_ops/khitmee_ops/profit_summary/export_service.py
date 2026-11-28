import csv
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Literal, TypedDict

from khitmee_ops.profit_summary.eligibility import (
	SALES_INVOICE_SCOPE_BATCH_SIZE,
	company_only_match_conditions,
	sales_invoice_match_conditions,
)
from khitmee_ops.profit_summary.store import batched

try:
	import frappe
except ImportError:  # pragma: no cover - offline unit tests install a frappe stub
	frappe = None


class ExportEnqueueResult(TypedDict):
	export_log_name: str
	status: Literal["Queued", "Running", "Completed", "Failed", "Expired"]
	deduplicated: bool
	expires_at: str
	artifact_published: int


class ProfitExportBusyError(Exception):
	http_status_code = 429


EXPORT_DOCTYPE = "Khitmee Profit Export Log"
EXPORT_TIMEOUT_SECONDS = 3600
EXPORT_DIRECTORY = "khitmee_profit_exports"
RESTRICTED_EXPORT_MAX_INVOICES = 5000
EXPORT_SCOPE_CHECK_BATCH_SIZE = SALES_INVOICE_SCOPE_BATCH_SIZE
MAX_EXPORT_SCOPE_BYTES = RESTRICTED_EXPORT_MAX_INVOICES * (140 * 6 + 3) + 4096
MAX_EXPORT_GROUPS = 50000
EXPORT_FLUSH_LINES = 2000


def _require_frappe():
	if frappe is None:
		raise ImportError("khitmee profit export must run inside a bench")
	return frappe


def authorize_profit_request(filters, user, *, require_export=False):
	from khitmee_ops.profit_summary.eligibility import authorize_profit_request as implementation

	return implementation(filters, user, require_export=require_export)


def run_detail_gross_profit(filters, *, invoice_ids=None, enforce_budget=False, **options):
	from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import (
		run_detail_gross_profit as implementation,
	)

	return implementation(filters, invoice_ids=invoice_ids, enforce_budget=enforce_budget, **options)


def _doc_get(doc, fieldname, default=None):
	if hasattr(doc, "get"):
		return doc.get(fieldname, default)
	return getattr(doc, fieldname, default)


def _now_datetime():
	if frappe is not None:
		try:
			from frappe.utils import now_datetime

			return now_datetime()
		except Exception:
			pass
	return datetime.now()


def _json_value(value):
	if isinstance(value, dict):
		return {str(key): _json_value(value[key]) for key in sorted(value)}
	if isinstance(value, (list, tuple)):
		return [_json_value(item) for item in value]
	if hasattr(value, "isoformat"):
		return value.isoformat()
	return value


def canonical_filters(filters):
	if isinstance(filters, str):
		filters = json.loads(filters)
	filters = dict(filters or {})
	return {
		str(key): _json_value(value)
		for key, value in sorted(filters.items())
		if key != "invoice_page" and value not in (None, "", [], {})
	}


def canonical_filters_json(filters):
	return json.dumps(canonical_filters(filters), sort_keys=True, separators=(",", ":"), default=str)


def compute_filters_hash(filters):
	return hashlib.sha256(canonical_filters_json(filters).encode("utf-8")).hexdigest()


def _frappe_permissions():
	try:
		import frappe.permissions as permissions_module

		return permissions_module
	except Exception:
		return getattr(frappe, "permissions", None)


def build_sales_invoice_match_conditions():
	return sales_invoice_match_conditions(frappe.session.user)


def _permission_names(user_permissions, doctype):
	entries = (user_permissions or {}).get(doctype) or []
	names = []
	for entry in entries:
		if isinstance(entry, str):
			names.append(entry)
		elif hasattr(entry, "get"):
			names.append(entry.get("doc") or entry.get("docname") or entry.get("name") or entry.get("for_value"))
	return sorted(str(name) for name in names if name)


def _active_sales_invoice_docshares(user):
	rows = frappe.db.sql(
		"""
		SELECT `name`, `share_name`, `user`, `everyone`, `read`, `write`, `submit`, `share`
		FROM `tabDocShare`
		WHERE `share_doctype` = 'Sales Invoice'
			AND (`user` = %s OR `everyone` = 1)
		ORDER BY `share_name` ASC, `name` ASC
		""",
		(user,),
		as_dict=True,
	)
	return [dict(row) for row in rows]


def compute_permission_fingerprint(filters, user):
	_require_frappe()
	previous_user = frappe.session.user
	frappe.set_user(user)
	try:
		roles = sorted(str(role) for role in (frappe.get_roles(user) or []))
		permissions_module = _frappe_permissions()
		can_export = bool(
			permissions_module
			and permissions_module.can_export("Sales Invoice", raise_exception=False)
		)
		match_conditions = " ".join((build_sales_invoice_match_conditions() or "").split())
		get_user_permissions = getattr(permissions_module, "get_user_permissions", None)
		user_permissions = get_user_permissions(user) if get_user_permissions else {}
		docshares = _active_sales_invoice_docshares(user)
	finally:
		frappe.set_user(previous_user)

	payload = {
		"roles": roles,
		"can_export": can_export,
		"normalized_match_conditions": match_conditions,
		"companies": _permission_names(user_permissions, "Company"),
		"warehouses": _permission_names(user_permissions, "Warehouse"),
		"active_si_docshares": docshares,
	}
	serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def has_restricted_match_conditions(filters, user):
	previous_user = frappe.session.user
	frappe.set_user(user)
	try:
		match_conditions = build_sales_invoice_match_conditions() or ""
	finally:
		frappe.set_user(previous_user)
	return not company_only_match_conditions(match_conditions, (filters or {}).get("company"))


def select_accessible_invoice_names(filters, user, *, limit=RESTRICTED_EXPORT_MAX_INVOICES, invoice_names=None):
	conditions = []
	params = []
	if filters.get("company"):
		conditions.append("`company` = %s")
		params.append(filters.get("company"))
	if invoice_names is None:
		conditions.extend(["`docstatus` = 1", "`is_opening` != 'Yes'"])
		for field, condition in (
			("from_date", "`posting_date` >= %s"), ("to_date", "`posting_date` <= %s"), ("sales_invoice", "`name` = %s")
		):
			if filters.get(field):
				conditions.append(condition)
				params.append(filters[field])

	previous_user = frappe.session.user
	frappe.set_user(user)
	try:
		match_conditions = build_sales_invoice_match_conditions() or ""
	finally:
		frappe.set_user(previous_user)
	if match_conditions:
		conditions.append(f"({match_conditions})")

	# Report eligibility was checked inside the snapshot. Download rechecks visibility for captured names.
	chunks = (None,) if invoice_names is None else batched(sorted(set(invoice_names)), EXPORT_SCOPE_CHECK_BATCH_SIZE)
	names = []
	for chunk in chunks:
		batch_conditions, batch_params = conditions, params
		sql_limit = limit + 1 - len(names)
		if chunk is not None:
			placeholders = ", ".join(["%s"] * len(chunk))
			batch_conditions = [*conditions, f"`name` IN ({placeholders})"]
			batch_params = [*params, *chunk]
			sql_limit = min(sql_limit, len(chunk) + 1)
		rows = frappe.db.sql(
			"""
			SELECT `name`
			FROM `tabSales Invoice`
			WHERE {conditions}
			ORDER BY `name` ASC
			LIMIT {sql_limit}
			""".format(conditions=" AND ".join(batch_conditions), sql_limit=sql_limit),
			batch_params,
			as_dict=True,
		)
		names.extend(str(row.get("name") if hasattr(row, "get") else row[0]) for row in rows)
		if len(names) > limit:
			return names[:limit], True
	return names, False


def invoice_names_sha256(invoice_names):
	ordered_names = sorted(str(name) for name in invoice_names)
	return hashlib.sha256("\n".join(ordered_names).encode("utf-8")).hexdigest()


def compute_exported_invoice_digest(filters, user, *, limit=RESTRICTED_EXPORT_MAX_INVOICES):
	names, truncated = select_accessible_invoice_names(filters, user, limit=limit)
	return invoice_names_sha256(names), names, truncated


def get_export_directory():
	_require_frappe()
	try:
		path = frappe.get_site_path("private", EXPORT_DIRECTORY)
	except Exception:
		site = getattr(getattr(frappe, "local", None), "site", "site")
		path = os.path.join("sites", str(site), "private", EXPORT_DIRECTORY)
	os.makedirs(path, exist_ok=True)
	return path


def get_export_paths(export_uuid):
	safe_uuid = os.path.basename(str(export_uuid))
	directory = get_export_directory()
	final_path = os.path.join(directory, f"{safe_uuid}.csv")
	temporary_path = os.path.join(directory, f"{safe_uuid}.csv.tmp")
	return temporary_path, final_path


def _export_scope_paths(export_uuid):
	_, csv_path = get_export_paths(export_uuid)
	return f"{csv_path}.scope.json.tmp", f"{csv_path}.scope.json"


def _export_scope_binding(doc, report_digest):
	return {
		"version": 3,
		"export_log_name": _doc_get(doc, "name"),
		"export_uuid": _doc_get(doc, "export_uuid"),
		"requested_by": _doc_get(doc, "requested_by"),
		"filters_hash": _doc_get(doc, "filters_hash"),
		"permission_fingerprint": _doc_get(doc, "permission_fingerprint"),
		"exported_invoice_sha256": report_digest,
	}


def _scope_json(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_export_scope(payload):
	secret = frappe.conf.get("encryption_key") or frappe.conf.get("db_password")
	if not secret:
		raise frappe.PermissionError("The site has no key for signing profit export scope")
	message = b"khitmee:profit-export-scope:v1\x00" + _scope_json(payload)
	return hmac.new(secret.encode("utf-8"), message, "sha256").hexdigest()


def _write_export_scope(path, doc, report_digest, invoice_names, *, hidden_invoice_names=()):
	names = sorted(invoice_names)
	payload = {
		**_export_scope_binding(doc, report_digest),
		"invoice_names": names,
		"hidden_invoice_names": sorted(hidden_invoice_names),
		"invoice_names_sha256": hashlib.sha256(_scope_json(names)).hexdigest(),
	}
	encoded = _scope_json({"scope": payload, "signature": _sign_export_scope(payload)})
	if len(encoded) > MAX_EXPORT_SCOPE_BYTES:
		raise frappe.ValidationError("Profit export scope manifest exceeds its size limit")
	with open(path, "wb", opener=_open_private_write) as handle:
		handle.write(encoded)


def _open_private_write(path, flags):
	return os.open(path, flags, 0o600)


class _ExportInvoiceScope:
	def __init__(self, match_conditions):
		self.match_conditions = match_conditions
		self.invoice_names = set()
		self.hidden_invoice_names = set()

	def include(self, names):
		self._include(self.invoice_names, names)

	def include_hidden(self, names):
		self._include(self.hidden_invoice_names, names)

	def _include(self, scope, names):
		for name in names:
			scope.add(name)
			if len(self.invoice_names) + len(self.hidden_invoice_names) > RESTRICTED_EXPORT_MAX_INVOICES:
				raise frappe.PermissionError(
					f"Restricted profit exports are limited to {RESTRICTED_EXPORT_MAX_INVOICES} contributing invoices"
				)


def exported_invoice_scope_is_current(doc, filters, user):
	"""Require a signed scope that attests to the invoices and returns read during export."""
	report_digest = _doc_get(doc, "exported_invoice_sha256")
	if user != _doc_get(doc, "requested_by") or not report_digest:
		return False
	_, final_path = _export_scope_paths(_doc_get(doc, "export_uuid"))
	try:
		with open(final_path, "rb") as handle:
			encoded = handle.read(MAX_EXPORT_SCOPE_BYTES + 1)
	except FileNotFoundError:
		return False
	if len(encoded) > MAX_EXPORT_SCOPE_BYTES:
		return False
	try:
		manifest = json.loads(encoded)
		payload = manifest["scope"]
		signature_valid = hmac.compare_digest(manifest["signature"], _sign_export_scope(payload))
	except (KeyError, ValueError, TypeError):
		return False
	if not signature_valid:
		return False
	if any(payload.get(key) != value for key, value in _export_scope_binding(doc, report_digest).items()):
		return False
	if payload["filters_hash"] != compute_filters_hash(filters):
		return False
	names = payload["invoice_names"]
	hidden_names = payload.get("hidden_invoice_names")
	if not isinstance(names, list) or not isinstance(hidden_names, list):
		return False
	if len(names) + len(hidden_names) > RESTRICTED_EXPORT_MAX_INVOICES or set(names).intersection(hidden_names):
		return False
	if hashlib.sha256(_scope_json(names)).hexdigest() != payload["invoice_names_sha256"]:
		return False
	scope_names = [*names, *hidden_names]
	visible, truncated = select_accessible_invoice_names(filters, user, limit=len(scope_names), invoice_names=scope_names)
	return not truncated and set(visible) == set(names)


def _export_result(doc, *, deduplicated):
	expires_at = _doc_get(doc, "expires_at")
	return {
		"export_log_name": _doc_get(doc, "name"),
		"status": _doc_get(doc, "status"),
		"deduplicated": bool(deduplicated),
		"expires_at": expires_at.isoformat(sep=" ") if hasattr(expires_at, "isoformat") else str(expires_at),
		"artifact_published": int(_doc_get(doc, "artifact_published", 0) or 0),
	}


def _lock_export_admission():
	db = frappe.db
	lock_name = "CONCAT(DATABASE(), ':khitmee-profit-export-admission')"
	if db.sql(f"SELECT GET_LOCK({lock_name}, 0)")[0][0] != 1:
		raise ProfitExportBusyError("A profit export is being queued. Retry shortly.")

	def release():
		db.sql(f"SELECT RELEASE_LOCK({lock_name})")

	db.after_commit.add(release)
	db.after_rollback.add(release)


def _get_active_export(active_dedupe_key):
	# A current read must see admissions committed after this request began.
	rows = frappe.db.sql(
		f"""
		SELECT `name`, `status`, `expires_at`, `artifact_published`, `active_dedupe_key`, `permission_fingerprint`
		FROM `tab{EXPORT_DOCTYPE}`
		WHERE `active_dedupe_key` IS NOT NULL AND `status` IN ('Queued', 'Running')
		ORDER BY (`active_dedupe_key` = %s) DESC
		LIMIT 1 FOR UPDATE
		""",
		(active_dedupe_key,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _whitelist():
	if frappe is not None and hasattr(frappe, "whitelist"):
		return frappe.whitelist()
	return lambda function: function


@_whitelist()
def enqueue_profit_csv_export(filters: dict | str | None = None) -> ExportEnqueueResult:
	_require_frappe()
	if isinstance(filters, str):
		filters = json.loads(filters)
	return enqueue_profit_csv_export_for_user(filters or {}, frappe.session.user, source="ui")


@_whitelist()
def download_profit_csv_export(export_log_name: str):
	from khitmee_ops.api.profit_export import download_export

	return download_export(export_log_name)


@_whitelist()
def get_profit_csv_export_status(export_log_name: str):
	_require_frappe()
	doc = frappe.get_doc(EXPORT_DOCTYPE, export_log_name)
	user = frappe.session.user
	if user != _doc_get(doc, "requested_by"):
		raise frappe.PermissionError("Not permitted to view this profit export")
	filters = json.loads(_doc_get(doc, "filters_json"))
	authorize_profit_request(filters, user, require_export=True)
	if compute_permission_fingerprint(filters, user) != _doc_get(doc, "permission_fingerprint"):
		raise frappe.PermissionError("Profit export permission fingerprint changed")
	return _export_result(doc, deduplicated=False)


def enqueue_profit_csv_export_for_user(
	filters: dict, user: str, source: str = "cli"
) -> ExportEnqueueResult:
	_require_frappe()
	if not user:
		raise frappe.PermissionError("A profit export must be bound to an explicit user")
	if source not in {"ui", "cli"}:
		raise frappe.ValidationError("Profit export source must be ui or cli")

	normalized_filters = canonical_filters(filters)
	authorize_profit_request(normalized_filters, user, require_export=True)
	filters_json = canonical_filters_json(normalized_filters)
	filters_hash = hashlib.sha256(filters_json.encode("utf-8")).hexdigest()
	active_dedupe_key = f"{user}:{filters_hash}"

	permission_fingerprint = compute_permission_fingerprint(normalized_filters, user)
	_lock_export_admission()
	existing = _get_active_export(active_dedupe_key)
	if existing:
		if _doc_get(existing, "active_dedupe_key") != active_dedupe_key:
			raise ProfitExportBusyError("A profit export is already queued or running. Retry after it finishes.")
		if _doc_get(existing, "permission_fingerprint") != permission_fingerprint:
			raise frappe.PermissionError("Profit export permission fingerprint changed")
		return _export_result(existing, deduplicated=True)

	export_uuid = str(uuid.uuid4())
	expires_at = _now_datetime() + timedelta(hours=24)
	doc = frappe.get_doc(
		{
			"doctype": EXPORT_DOCTYPE,
			"requested_by": user,
			"filters_json": filters_json,
			"filters_hash": filters_hash,
			"source": source,
			"status": "Queued",
			"active_dedupe_key": active_dedupe_key,
			"export_uuid": export_uuid,
			"permission_fingerprint": permission_fingerprint,
			"artifact_published": 0,
			"exported_invoice_sha256": None,
			"row_count": 0,
			"expires_at": expires_at,
		}
	)
	doc.insert(ignore_permissions=True)

	def enqueue_after_commit():
		export_log_name = _doc_get(doc, "name")
		if source == "cli" and frappe.db.get_value(EXPORT_DOCTYPE, export_log_name, "status") != "Queued":
			return  # --run-sync claimed the job in the transaction that just committed.
		try:
			frappe.enqueue(
				"khitmee_ops.profit_summary.export_service.run_profit_csv_export",
				queue="long",
				timeout=EXPORT_TIMEOUT_SECONDS,
				export_log_name=export_log_name,
			)
		except Exception as error:
			_mark_export_failed(export_log_name, error, expected_status="Queued")
			raise

	frappe.db.after_commit.add(enqueue_after_commit)
	return _export_result(doc, deduplicated=False)


def _update_export_log(export_log_name, **fields):
	assignments = []
	params = []
	for fieldname, value in fields.items():
		if value is NULL:
			assignments.append(f"`{fieldname}` = NULL")
		else:
			assignments.append(f"`{fieldname}` = %s")
			params.append(value)
	assignments.append("`modified` = %s")
	params.append(_now_datetime())
	params.append(export_log_name)
	frappe.db.sql(
		f"UPDATE `tab{EXPORT_DOCTYPE}` SET {', '.join(assignments)} WHERE `name` = %s",
		params,
	)


class _NullSentinel:
	pass


NULL = _NullSentinel()


def _commit_completed_export(export_log_name, exported_invoice_sha256, row_count):
	frappe.db.sql(
		f"""
		UPDATE `tab{EXPORT_DOCTYPE}`
		SET `artifact_published` = 1,
			`exported_invoice_sha256` = %s,
			`active_dedupe_key` = NULL,
			`row_count` = %s,
			`status` = 'Completed',
			`error` = NULL,
			`modified` = %s
		WHERE `name` = %s
		""",
		(exported_invoice_sha256, row_count, _now_datetime(), export_log_name),
	)
	frappe.db.commit()


def _lock_export_state(export_log_name):
	rows = frappe.db.sql(
		f"SELECT `status`, `expires_at`, `modified` FROM `tab{EXPORT_DOCTYPE}` WHERE `name` = %s FOR UPDATE",
		(export_log_name,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _mark_export_failed(export_log_name, error, *, expected_status):
	state = _lock_export_state(export_log_name)
	if not state or state["status"] != expected_status:
		frappe.db.rollback()
		return False
	_update_export_log(
		export_log_name,
		status="Failed",
		active_dedupe_key=NULL,
		error=str(error)[:1000],
	)
	frappe.db.commit()
	return True


def _claim_export(export_log_name) -> bool:
	"""Claim the temporary paths once, including when a queued job is redelivered."""
	state = _lock_export_state(export_log_name)
	if not state or state["status"] != "Queued" or state["expires_at"] <= _now_datetime():
		frappe.db.rollback()
		return False
	_update_export_log(export_log_name, status="Running", error=NULL)
	frappe.db.commit()
	return True


def _scrub_column_label(label):
	if frappe is not None and hasattr(frappe, "scrub"):
		return frappe.scrub(label)
	return str(label or "").replace(" ", "_").replace("-", "_").lower()


def _column_fieldnames(columns):
	return [
		str(column.get("fieldname") or _scrub_column_label(column.get("label")) or index)
		for index, column in enumerate(columns or [])
	]


def _column_headers(columns):
	return [str(column.get("label") or column.get("fieldname") or "") for column in columns or []]


CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", chr(9), chr(13))


def _neutralize_formula(text: str) -> str:
	"""Stop spreadsheet apps from evaluating editable text (customer names, items).

	OWASP CSV injection guidance: prefix a quote to cells that start with a formula
	trigger.  Plain numbers such as "-12.5" are left alone.
	"""
	if not text or not text.startswith(CSV_FORMULA_PREFIXES):
		return text
	try:
		float(text)
		return text
	except ValueError:
		return "'" + text


def _csv_value(value):
	if value is None:
		return ""
	if hasattr(value, "isoformat"):
		return value.isoformat()
	if isinstance(value, str):
		return _neutralize_formula(value)
	return value


def _row_values(row, fieldnames):
	if hasattr(row, "get"):
		return [_csv_value(row.get(fieldname)) for fieldname in fieldnames]
	if isinstance(row, (list, tuple)):
		return [_csv_value(value) for value in row]
	return [_csv_value(row)]


# Other groupings still use the materialized report and retain their input cap.
MAX_EXPORT_INPUT_ROWS = 50_000


def _write_invoice_csv(filters, path, invoice_ids, export_scope):
	from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import (
		_copy_report_filters,
		flag_restricted_adjustment,
		invoice_total_row,
	)
	from khitmee_ops.khitmee_ops.report.fast_gross_profit.invoice_batches import (
		invoice_detail_columns,
		iter_invoice_detail_batches,
	)

	filters = _copy_report_filters({**filters, "group_by": "Invoice"})
	total_base_amount = total_buying_amount = 0
	row_count = 0
	incomplete_total = False
	with open(path, "w", newline="", encoding="utf-8") as csv_file:
		writer = csv.writer(csv_file)
		fieldnames = None
		options = {} if export_scope is None else {"export_scope": export_scope}
		for columns, data in iter_invoice_detail_batches(filters, invoice_ids=invoice_ids, **options):
			if fieldnames is None:
				writer.writerow(_column_headers(columns))
				fieldnames = _column_fieldnames(columns)
			for row in data:
				if row.get("indent") == 1:
					if row.get("restricted_adjustment"):
						incomplete_total = True
					else:
						total_base_amount += row.get("selling_amount") or 0.0
						total_buying_amount += row.get("buying_amount") or 0.0
				writer.writerow(_row_values(row, fieldnames))
				row_count += 1
			csv_file.flush()
		if fieldnames is None:
			columns = invoice_detail_columns(filters)
			writer.writerow(_column_headers(columns))
			fieldnames = _column_fieldnames(columns)
		if not filters.currency:
			filters.currency = frappe.get_cached_value("Company", filters.company, "default_currency")
		total = invoice_total_row(filters, total_base_amount, total_buying_amount)
		if incomplete_total:
			flag_restricted_adjustment(total)
		writer.writerow(_row_values(total, fieldnames))
	return row_count + 1


def write_profit_csv(filters, path, *, invoice_ids=None, export_scope=None):
	group_by = (filters or {}).get("group_by") or "Invoice"
	if group_by == "Invoice":
		return _write_invoice_csv(filters or {}, path, invoice_ids, export_scope)

	if invoice_ids is None:
		from khitmee_ops.profit_summary.budget import preflight_detail_input_rows

		input_rows = preflight_detail_input_rows(filters, cap=MAX_EXPORT_INPUT_ROWS)
		if input_rows > MAX_EXPORT_INPUT_ROWS:
			raise frappe.ValidationError(
				f"Profit export input cap exceeded ({MAX_EXPORT_INPUT_ROWS} invoice lines); narrow the date range"
			)
	options = {} if export_scope is None else {"export_scope": export_scope}
	columns, data = run_detail_gross_profit(filters, invoice_ids=invoice_ids, enforce_budget=False, **options)
	if max(len(data) - 1, 0) > MAX_EXPORT_GROUPS:
		raise frappe.ValidationError(f"Profit export group cap exceeded ({MAX_EXPORT_GROUPS} groups)")

	fieldnames = _column_fieldnames(columns)
	row_count = 0
	with open(path, "w", newline="", encoding="utf-8") as csv_file:
		writer = csv.writer(csv_file)
		writer.writerow(_column_headers(columns))
		for row in data:
			writer.writerow(_row_values(row, fieldnames))
			row_count += 1
			if row_count % EXPORT_FLUSH_LINES == 0:
				csv_file.flush()
	return row_count


def _remove_file_quietly(path):
	try:
		if path and os.path.exists(path):
			os.remove(path)
	except OSError:
		pass


def _begin_repeatable_read_snapshot():
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
	frappe.db.sql("START TRANSACTION WITH CONSISTENT SNAPSHOT")


def run_profit_csv_export(export_log_name):
	_require_frappe()
	doc = frappe.get_doc(EXPORT_DOCTYPE, export_log_name)
	if _doc_get(doc, "status") in {"Completed", "Failed", "Expired"}:
		return

	if not _claim_export(export_log_name):
		return

	previous_user = frappe.session.user
	temporary_paths = []
	renamed_paths = []
	try:
		_begin_repeatable_read_snapshot()
		filters = json.loads(_doc_get(doc, "filters_json") or "{}")
		requested_by = _doc_get(doc, "requested_by")
		frappe.set_user(requested_by)
		authorize_profit_request(filters, requested_by, require_export=True)
		live_fingerprint = compute_permission_fingerprint(filters, requested_by)
		if live_fingerprint != _doc_get(doc, "permission_fingerprint"):
			raise frappe.PermissionError("Profit export permission fingerprint changed")

		invoice_ids = None
		exported_invoice_sha256 = None
		export_scope = None
		if has_restricted_match_conditions(filters, requested_by):
			exported_invoice_sha256, invoice_ids, truncated = compute_exported_invoice_digest(
				filters, requested_by
			)
			if truncated:
				raise frappe.PermissionError(
					f"Restricted profit exports are limited to {RESTRICTED_EXPORT_MAX_INVOICES} invoices"
				)
			export_scope = _ExportInvoiceScope(build_sales_invoice_match_conditions())

		export_uuid = _doc_get(doc, "export_uuid")
		temporary_path, final_path = get_export_paths(export_uuid)
		temporary_paths.append(temporary_path)
		row_count = write_profit_csv(
			filters, temporary_path, invoice_ids=invoice_ids, export_scope=export_scope
		)
		if export_scope is not None:
			scope_tmp, scope_path = _export_scope_paths(export_uuid)
			temporary_paths.append(scope_tmp)
			_write_export_scope(
				scope_tmp, doc, exported_invoice_sha256, export_scope.invoice_names,
				hidden_invoice_names=export_scope.hidden_invoice_names,
			)
			os.replace(scope_tmp, scope_path)
			renamed_paths.append(scope_path)
		os.replace(temporary_path, final_path)
		renamed_paths.append(final_path)
		_commit_completed_export(
			export_log_name,
			exported_invoice_sha256,
			row_count,
		)
	except Exception as error:
		frappe.db.rollback()
		if _mark_export_failed(export_log_name, error, expected_status="Running"):
			for path in renamed_paths:
				_remove_file_quietly(path)
		raise
	finally:
		for path in temporary_paths:
			_remove_file_quietly(path)
		frappe.set_user(previous_user)


def _expired_export_logs(now):
	return frappe.get_all(
		EXPORT_DOCTYPE,
		filters=[["expires_at", "<=", now]],
		fields=["name", "status", "export_uuid", "expires_at", "artifact_published", "exported_invoice_sha256"],
	)


def cleanup_expired_exports():
	_require_frappe()
	now = _now_datetime()
	expired_count = 0
	for doc in _expired_export_logs(now):
		name = _doc_get(doc, "name")
		state = _lock_export_state(name)
		if not state or state["expires_at"] > now:
			continue
		if state["status"] == "Running" and state["modified"] + timedelta(seconds=EXPORT_TIMEOUT_SECONDS) > now:
			continue  # A worker can start just before the original expiry.
		export_uuid = _doc_get(doc, "export_uuid")
		for path in (*get_export_paths(export_uuid), *_export_scope_paths(export_uuid)):
			_remove_file_quietly(path)
		if state["status"] != "Expired":
			_update_export_log(
				name,
				status="Expired",
				active_dedupe_key=NULL,
			)
			expired_count += 1
	frappe.db.commit()
	return expired_count
