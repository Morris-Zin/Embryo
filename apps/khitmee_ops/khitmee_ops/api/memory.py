import ctypes

import frappe

HEAVY_REPORT_THRESHOLD_BYTES = 2 * 1024 * 1024
_MALLOC_TRIM = None
_MALLOC_TRIM_UNAVAILABLE = False


def _malloc_trim():
	global _MALLOC_TRIM, _MALLOC_TRIM_UNAVAILABLE
	if _MALLOC_TRIM_UNAVAILABLE:
		return None
	if _MALLOC_TRIM is None:
		try:
			_MALLOC_TRIM = ctypes.CDLL("libc.so.6", use_errno=False).malloc_trim
		except Exception:
			_MALLOC_TRIM_UNAVAILABLE = True
			return None
	return _MALLOC_TRIM


def _response_size(response):
	if response is None:
		return 0
	try:
		content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
		if content_length is not None:
			return int(content_length)
	except Exception:
		pass
	try:
		return len(response.get_data(as_text=False))
	except Exception:
		return 0


def _is_heavy_report_response(response):
	form_dict = getattr(frappe, "form_dict", {}) or {}
	is_report_run = form_dict.get("cmd") == "frappe.desk.query_report.run"
	flags = getattr(frappe, "flags", {}) or {}
	large_report_flag = bool(getattr(flags, "khitmee_large_report_response", False))
	return (is_report_run and _response_size(response) >= HEAVY_REPORT_THRESHOLD_BYTES) or large_report_flag


def _release_message_and_trim_heap():
	try:
		local_response = getattr(getattr(frappe, "local", None), "response", None)
		if hasattr(local_response, "pop"):
			local_response.pop("message", None)
		malloc_trim = _malloc_trim()
		if malloc_trim is None:
			return False
		malloc_trim(0)
		return True
	except Exception:
		return False


def maybe_trim_heap(response=None, request=None):
	"""Return glibc heap pages after very large report responses; never fail the response."""
	try:
		if not _is_heavy_report_response(response):
			return False
		after_response = getattr(request, "after_response", None)
		add_after_response = getattr(after_response, "add", None)
		if callable(add_after_response):
			add_after_response(_release_message_and_trim_heap)
			return True
		return _release_message_and_trim_heap()
	except Exception:
		return False
