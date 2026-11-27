import json
import re
from datetime import datetime

try:
	import frappe
except ImportError:  # pragma: no cover - offline unit tests install a frappe stub
	frappe = None

try:
	from werkzeug import Response
except ImportError:  # pragma: no cover - production benches always provide werkzeug

	class Response:
		def __init__(self, response=None, status=200, headers=None, direct_passthrough=False):
			self.response = response
			self.status_code = status
			self.status = str(status)
			self.headers = dict(headers or {})
			self.direct_passthrough = direct_passthrough

		def get_data(self):
			body = self.response
			if hasattr(body, "__iter__") and not isinstance(body, (bytes, str)):
				body = b"".join(body)
			return body if isinstance(body, bytes) else str(body or "").encode("utf-8")


from khitmee_ops.profit_summary import export_service


def _whitelist():
	if frappe is not None and hasattr(frappe, "whitelist"):
		return frappe.whitelist()
	return lambda function: function


def _http_response(body, status):
	return Response(body, status=status)


def _valid_active_user(user):
	if not user or user == "Guest":
		return False
	try:
		enabled = frappe.db.get_value("User", user, "enabled")
	except Exception:
		return False
	try:
		return int(enabled or 0) == 1
	except (TypeError, ValueError):
		return False


def _missing_document_error(error):
	if isinstance(error, KeyError):
		return True
	error_name = error.__class__.__name__.lower()
	return "doesnotexist" in error_name or "doesnotexist" in str(error).lower()


def get_export_log(export_log_name):
	try:
		return frappe.get_doc(export_service.EXPORT_DOCTYPE, export_log_name)
	except Exception as error:
		if _missing_document_error(error):
			return None
		raise


def _parse_datetime(value):
	if hasattr(value, "year") and hasattr(value, "month"):
		return value
	if not value:
		return None
	try:
		return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
	except (TypeError, ValueError):
		return None


def _now():
	try:
		from frappe.utils import now_datetime

		return now_datetime()
	except Exception:
		return datetime.now()


def stream_64k(path):
	with open(path, "rb") as artifact:
		while True:
			chunk = artifact.read(65536)
			if not chunk:
				break
			yield chunk


def _safe_filename_part(value):
	return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "all"))


@_whitelist()
def enqueue_export(filters):
	if isinstance(filters, str):
		filters = json.loads(filters)
	return export_service.enqueue_profit_csv_export(filters or {})


@_whitelist()
def download_export(export_log_name: str) -> Response:
	user = getattr(frappe, "session", None) and frappe.session.user
	if not _valid_active_user(user):
		return _http_response("Forbidden", 403)

	doc = get_export_log(export_log_name)
	if doc is None:
		return _http_response("Not Found", 404)

	if user != export_service._doc_get(doc, "requested_by"):
		return _http_response("Forbidden", 403)

	try:
		filters = json.loads(export_service._doc_get(doc, "filters_json") or "{}")
	except (TypeError, ValueError):
		return _http_response("Forbidden", 403)

	try:
		export_service.authorize_profit_request(filters, user, require_export=True)
		live_fingerprint = export_service.compute_permission_fingerprint(filters, user)
		restricted = export_service.has_restricted_match_conditions(filters, user)
	except Exception:
		return _http_response("Forbidden", 403)

	if live_fingerprint != export_service._doc_get(doc, "permission_fingerprint"):
		return _http_response("Forbidden", 403)

	if int(export_service._doc_get(doc, "artifact_published", 0) or 0) == 1 and restricted:
		published_digest = export_service._doc_get(doc, "exported_invoice_sha256")
		if not published_digest:
			return _http_response("Forbidden", 403)
		try:
			live_digest, _invoice_names, truncated = export_service.compute_exported_invoice_digest(
				filters, user
			)
		except Exception:
			return _http_response("Forbidden", 403)
		if truncated or live_digest != published_digest:
			return _http_response("Forbidden", 403)

	expires_at = _parse_datetime(export_service._doc_get(doc, "expires_at"))
	if export_service._doc_get(doc, "status") == "Expired" or expires_at is None or _now() >= expires_at:
		return _http_response("Gone", 410)

	if export_service._doc_get(doc, "status") != "Completed" or int(
		export_service._doc_get(doc, "artifact_published", 0) or 0
	) != 1:
		return _http_response("Conflict", 409)

	_temporary_path, final_path = export_service.get_export_paths(
		export_service._doc_get(doc, "export_uuid")
	)
	try:
		artifact = open(final_path, "rb")
	except OSError:
		return _http_response("Conflict", 409)
	artifact.close()

	response = Response(stream_64k(final_path), status=200, direct_passthrough=True)
	filename = "fast-gross-profit-{}-to-{}.csv".format(
		_safe_filename_part(filters.get("from_date")),
		_safe_filename_part(filters.get("to_date")),
	)
	response.headers["Content-Type"] = "text/csv; charset=utf-8"
	response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
	response.headers["Cache-Control"] = "private, no-store"
	response.headers["X-Content-Type-Options"] = "nosniff"
	return response
