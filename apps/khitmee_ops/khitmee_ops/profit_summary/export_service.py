import csv
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Literal, TypedDict

try:
	import frappe
except ImportError:  # pragma: no cover - offline unit tests install a frappe stub
	frappe = None


class ExportEnqueueResult(TypedDict):
	export_log_name: str
	status: Literal["Queued", "Running", "Completed"]
	deduplicated: bool
	expires_at: str
	artifact_published: int


EXPORT_DOCTYPE = "Khitmee Profit Export Log"
EXPORT_DIRECTORY = "khitmee_profit_exports"
RESTRICTED_EXPORT_MAX_INVOICES = 5000
MAX_SINGLE_FAMILY_ROWS = 5000
MAX_EXPORT_GROUPS = 50000
EXPORT_FLUSH_LINES = 2000


def _require_frappe():
	if frappe is None:
		raise ImportError("khitmee profit export must run inside a bench")
	return frappe


def authorize_profit_request(filters, user, *, require_export=False):
	from khitmee_ops.profit_summary.eligibility import authorize_profit_request as implementation

	return implementation(filters, user, require_export=require_export)


def run_detail_gross_profit(filters, *, invoice_ids=None, enforce_budget=False):
	from khitmee_ops.khitmee_ops.report.fast_gross_profit.fast_gross_profit import (
		run_detail_gross_profit as implementation,
	)

	return implementation(filters, invoice_ids=invoice_ids, enforce_budget=enforce_budget)


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
		if value not in (None, "", [], {})
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
	try:
		from frappe.desk.reportview import build_match_conditions
	except (ImportError, ModuleNotFoundError, AttributeError) as error:
		if frappe is not None:
			raise RuntimeError("Sales Invoice match conditions are unavailable") from error
		return ""
	return build_match_conditions("Sales Invoice") or ""


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


def _strip_outer_parentheses(condition):
	condition = condition.strip()
	while condition.startswith("(") and condition.endswith(")"):
		depth = 0
		encloses_all = True
		for index, character in enumerate(condition):
			if character == "(":
				depth += 1
			elif character == ")":
				depth -= 1
				if depth == 0 and index != len(condition) - 1:
					encloses_all = False
					break
		if not encloses_all:
			break
		condition = condition[1:-1].strip()
	return condition


def is_company_only_match_condition(match_conditions, company):
	condition = " ".join(str(match_conditions or "").split()).strip().rstrip(";")
	if not condition:
		return True
	condition = _strip_outer_parentheses(condition).replace("`", "")
	pattern = re.compile(
		r"^(?:(?:tabSales Invoice)\.)?company\s*=\s*(['\"])(?P<company>.*?)\1$",
		flags=re.IGNORECASE,
	)
	match = pattern.match(condition)
	return bool(match and match.group("company") == str(company or ""))


def has_restricted_match_conditions(filters, user):
	previous_user = frappe.session.user
	frappe.set_user(user)
	try:
		match_conditions = build_sales_invoice_match_conditions() or ""
	finally:
		frappe.set_user(previous_user)
	return bool(match_conditions) and not is_company_only_match_condition(
		match_conditions, (filters or {}).get("company")
	)


def select_accessible_invoice_names(filters, user, *, limit=RESTRICTED_EXPORT_MAX_INVOICES):
	conditions = ["`docstatus` = 1", "`is_opening` != 'Yes'"]
	params = []
	if filters.get("company"):
		conditions.append("`company` = %s")
		params.append(filters.get("company"))
	if filters.get("from_date"):
		conditions.append("`posting_date` >= %s")
		params.append(filters.get("from_date"))
	if filters.get("to_date"):
		conditions.append("`posting_date` <= %s")
		params.append(filters.get("to_date"))
	if filters.get("sales_invoice"):
		conditions.append("`name` = %s")
		params.append(filters.get("sales_invoice"))

	previous_user = frappe.session.user
	frappe.set_user(user)
	try:
		match_conditions = build_sales_invoice_match_conditions() or ""
	finally:
		frappe.set_user(previous_user)
	if match_conditions:
		conditions.append(f"({match_conditions})")

	sql_limit = int(limit) + 1
	rows = frappe.db.sql(
		"""
		SELECT `name`
		FROM `tabSales Invoice`
		WHERE {conditions}
		ORDER BY `name` ASC
		LIMIT {sql_limit}
		""".format(conditions=" AND ".join(conditions), sql_limit=sql_limit),
		params,
		as_dict=True,
	)
	names = [str(row.get("name") if hasattr(row, "get") else row[0]) for row in rows]
	truncated = len(names) > limit
	return names[:limit], truncated


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


def _export_result(doc, *, deduplicated):
	expires_at = _doc_get(doc, "expires_at")
	return {
		"export_log_name": _doc_get(doc, "name"),
		"status": _doc_get(doc, "status"),
		"deduplicated": bool(deduplicated),
		"expires_at": expires_at.isoformat(sep=" ") if hasattr(expires_at, "isoformat") else str(expires_at),
		"artifact_published": int(_doc_get(doc, "artifact_published", 0) or 0),
	}


def _get_existing_active_export(active_dedupe_key):
	return frappe.db.get_value(
		EXPORT_DOCTYPE,
		{"active_dedupe_key": active_dedupe_key},
		["name", "status", "expires_at", "artifact_published"],
		as_dict=True,
	)


def _duplicate_error(error):
	message = str(error).lower()
	return "duplicate" in message or "unique" in message or "1062" in message


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

	existing = _get_existing_active_export(active_dedupe_key)
	if existing:
		return _export_result(existing, deduplicated=True)

	permission_fingerprint = compute_permission_fingerprint(normalized_filters, user)
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
	try:
		doc.insert(ignore_permissions=True)
	except Exception as error:
		if not _duplicate_error(error):
			raise
		existing = _get_existing_active_export(active_dedupe_key)
		if existing:
			return _export_result(existing, deduplicated=True)
		raise

	def enqueue_after_commit():
		frappe.enqueue(
			"khitmee_ops.profit_summary.export_service.run_profit_csv_export",
			queue="long",
			timeout=3600,
			export_log_name=_doc_get(doc, "name"),
		)

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


def _mark_export_failed(export_log_name, error):
	_update_export_log(
		export_log_name,
		status="Failed",
		active_dedupe_key=NULL,
		error=str(error)[:1000],
	)
	frappe.db.commit()


def _claim_export(export_log_name) -> bool:
	"""Atomically move one Queued export to Running; False if another worker has it.

	`--run-sync` runs the worker inline while the after-commit enqueue also starts
	a queued copy; both used to accept the log and write the same temporary path.
	"""
	rows = frappe.db.sql(
		f"SELECT `status` FROM `tab{EXPORT_DOCTYPE}` WHERE `name` = %s FOR UPDATE",
		(export_log_name,),
	)
	row = rows[0] if rows else None
	status = (row.get("status") if hasattr(row, "get") else row[0]) if row else None
	if status != "Queued":
		frappe.db.rollback()
		return False
	_update_export_log(export_log_name, status="Running", error=NULL)
	frappe.db.commit()
	return True


def _family_key(row):
	if row.get("sales_invoice") == "Total":
		return None
	if row.get("indent") == 0 or row.get("indent") == 0.0:
		return row.get("sales_invoice")
	return row.get("parent_invoice") or row.get("sales_invoice")


def _enforce_invoice_family_caps(data):
	counts = {}
	for row in data:
		if not hasattr(row, "get"):
			continue
		key = _family_key(row)
		if key:
			counts[key] = counts.get(key, 0) + 1
	oversized = [key for key, count in counts.items() if count > MAX_SINGLE_FAMILY_ROWS]
	if oversized:
		raise frappe.ValidationError(
			f"Profit export family row cap exceeded for {oversized[0]} ({MAX_SINGLE_FAMILY_ROWS} rows)"
		)


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


# The export worker materializes the whole report before writing, so cap its input
# up front instead of letting a year-long Invoice export exhaust the long worker.
MAX_EXPORT_INPUT_ROWS = 50_000


def write_profit_csv(filters, path, *, invoice_ids=None):
	if invoice_ids is None:
		from khitmee_ops.profit_summary.budget import preflight_detail_input_rows

		input_rows = preflight_detail_input_rows(filters, cap=MAX_EXPORT_INPUT_ROWS)
		if input_rows > MAX_EXPORT_INPUT_ROWS:
			raise frappe.ValidationError(
				f"Profit export input cap exceeded ({MAX_EXPORT_INPUT_ROWS} invoice lines); narrow the date range"
			)
	columns, data = run_detail_gross_profit(filters, invoice_ids=invoice_ids, enforce_budget=False)
	group_by = (filters or {}).get("group_by") or "Invoice"
	if group_by != "Invoice" and max(len(data) - 1, 0) > MAX_EXPORT_GROUPS:
		raise frappe.ValidationError(f"Profit export group cap exceeded ({MAX_EXPORT_GROUPS} groups)")
	if group_by == "Invoice":
		_enforce_invoice_family_caps(data)

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
	frappe.db.sql("START TRANSACTION")


def run_profit_csv_export(export_log_name):
	_require_frappe()
	doc = frappe.get_doc(EXPORT_DOCTYPE, export_log_name)
	if _doc_get(doc, "status") in {"Completed", "Failed", "Expired"}:
		return

	if not _claim_export(export_log_name):
		return

	previous_user = frappe.session.user
	frappe.set_user(_doc_get(doc, "requested_by"))
	temporary_path = None
	try:
		_begin_repeatable_read_snapshot()
		filters = json.loads(_doc_get(doc, "filters_json") or "{}")
		requested_by = _doc_get(doc, "requested_by")
		authorize_profit_request(filters, requested_by, require_export=True)
		live_fingerprint = compute_permission_fingerprint(filters, requested_by)
		if live_fingerprint != _doc_get(doc, "permission_fingerprint"):
			raise frappe.PermissionError("Profit export permission fingerprint changed")

		invoice_ids = None
		exported_invoice_sha256 = None
		if has_restricted_match_conditions(filters, requested_by):
			exported_invoice_sha256, invoice_ids, truncated = compute_exported_invoice_digest(
				filters, requested_by
			)
			if truncated:
				raise frappe.PermissionError(
					f"Restricted profit exports are limited to {RESTRICTED_EXPORT_MAX_INVOICES} invoices"
				)

		temporary_path, final_path = get_export_paths(_doc_get(doc, "export_uuid"))
		row_count = write_profit_csv(filters, temporary_path, invoice_ids=invoice_ids)
		os.replace(temporary_path, final_path)
		_commit_completed_export(
			export_log_name,
			exported_invoice_sha256,
			row_count,
		)
	except Exception as error:
		_mark_export_failed(export_log_name, error)
		raise
	finally:
		if temporary_path:
			_remove_file_quietly(temporary_path)
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
		temporary_path, final_path = get_export_paths(_doc_get(doc, "export_uuid"))
		_remove_file_quietly(final_path)
		_remove_file_quietly(temporary_path)
		if _doc_get(doc, "status") != "Expired":
			_update_export_log(
				_doc_get(doc, "name"),
				status="Expired",
				active_dedupe_key=NULL,
			)
			expired_count += 1
	frappe.db.commit()
	return expired_count
