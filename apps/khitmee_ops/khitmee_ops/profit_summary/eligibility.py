"""Authorization and fail-closed Summary eligibility checks."""

from __future__ import annotations

import re
from contextlib import contextmanager

try:  # pragma: no cover - offline tests install a stub before importing
	import frappe
except ImportError:  # pragma: no cover
	frappe = None

ALLOWED_SUMMARY_FILTER_KEYS = frozenset(
	{"company", "from_date", "to_date", "group_by", "warehouse", "include_returned_invoices", "currency"}
)

LINK_FILTER_DOCTYPES = {
	"company": "Company",
	"warehouse": "Warehouse",
	"sales_invoice": "Sales Invoice",
	"customer": "Customer",
	"item_code": "Item",
	"item_group": "Item Group",
	"brand": "Brand",
	"cost_center": "Cost Center",
	"project": "Project",
	"territory": "Territory",
}


def _require_frappe():
	if frappe is None:
		raise RuntimeError("profit eligibility requires Frappe")
	return frappe


def _validation_error(message):
	exc_type = getattr(_require_frappe(), "ValidationError", Exception)
	raise exc_type(message)


def _permission_error(message):
	exc_type = getattr(_require_frappe(), "PermissionError", PermissionError)
	raise exc_type(message)


def _session_user():
	session = getattr(_require_frappe(), "session", None)
	return getattr(session, "user", None) or "Guest"


@contextmanager
def _bound_user(user):
	frappe_obj = _require_frappe()
	bound_user = user or _session_user()
	set_user = getattr(frappe_obj, "set_user", None)
	if not callable(set_user):
		yield bound_user
		return
	previous = _session_user()
	set_user(bound_user)
	try:
		yield bound_user
	finally:
		set_user(previous)


def _has_permission(doctype, ptype, user=None, doc=None):
	frappe_obj = _require_frappe()
	kwargs = {"user": user}
	if doc is not None:
		kwargs["doc"] = doc
	try:
		return bool(frappe_obj.has_permission(doctype, ptype, **kwargs))
	except TypeError:
		if doc is not None:
			try:
				return bool(frappe_obj.has_permission(doctype, ptype, doc, user))
			except TypeError:
				return bool(frappe_obj.has_permission(doctype, ptype, doc))
		try:
			return bool(frappe_obj.has_permission(doctype, ptype, user))
		except TypeError:
			return bool(frappe_obj.has_permission(doctype, ptype))


def _report_permission(user=None):
	frappe_obj = _require_frappe()
	permissions = getattr(frappe_obj, "permissions", None)
	has_permission = getattr(permissions, "has_permission", None) if permissions is not None else None
	if callable(has_permission):
		for kwargs in (
			{"ptype": "read", "doc": "Fast Gross Profit", "user": user},
			{"ptype": "read", "user": user},
		):
			try:
				return bool(has_permission("Report", **kwargs))
			except TypeError:
				continue
	return _has_permission("Report", "read", user=user, doc="Fast Gross Profit")


def _iter_filter_values(value):
	if isinstance(value, (list, tuple, set)):
		for item in value:
			yield item
	elif isinstance(value, str) and "," in value:
		for item in value.split(","):
			yield item.strip()
	else:
		yield value


def _validate_link_filters(filters, user):
	for fieldname, linked_doctype in LINK_FILTER_DOCTYPES.items():
		value = (filters or {}).get(fieldname)
		if value in (None, "", False):
			continue
		for linked_name in _iter_filter_values(value):
			if not linked_name:
				continue
			if not _has_permission(linked_doctype, "read", user=user, doc=linked_name):
				_permission_error(f"Not permitted to access {linked_doctype} {linked_name}")


def authorize_profit_request(filters: dict, user: str | None = None, *, require_export: bool = False) -> None:
	"""Fail-closed principal-bound authorization for all profit report/export paths."""
	filters = filters or {}
	frappe_obj = _require_frappe()
	bound_user = user or _session_user()
	for required in ("company", "from_date", "to_date"):
		if not filters.get(required):
			_validation_error(f"Missing required profit filter: {required}")

	if not _report_permission(user=bound_user):
		_permission_error("Not permitted to read Fast Gross Profit")
	if not _has_permission("Sales Invoice", "report", user=bound_user):
		_permission_error("Not permitted to report Sales Invoice")
	if not _has_permission("Sales Invoice", "read", user=bound_user):
		_permission_error("Not permitted to read Sales Invoice")
	_validate_link_filters(filters, bound_user)

	if require_export:
		permissions = getattr(frappe_obj, "permissions", None)
		can_export = getattr(permissions, "can_export", None) if permissions is not None else None
		with _bound_user(bound_user):
			try:
				if callable(can_export):
					allowed = bool(can_export("Sales Invoice", raise_exception=False))
				else:
					allowed = _has_permission("Sales Invoice", "export", user=bound_user)
			except Exception:
				allowed = False
		if not allowed:
			_permission_error("Not permitted to export Sales Invoice")


def _empty_filter_value(value) -> bool:
	return value is None or value is False or value == "" or value == [] or value == {} or value == () or value == 0


def build_match_conditions(doctype: str, user: str | None = None):
	"""Call Frappe/ERPNext's match-condition builder, preserving user binding in tests."""
	frappe_obj = _require_frappe()
	permissions = getattr(frappe_obj, "permissions", None)
	builder = getattr(permissions, "build_match_conditions", None) if permissions is not None else None
	if not callable(builder):
		builder = getattr(frappe_obj, "build_match_conditions", None)
	if not callable(builder):
		reportview = getattr(getattr(frappe_obj, "desk", None), "reportview", None)
		builder = getattr(reportview, "build_match_conditions", None) if reportview is not None else None
	if not callable(builder):
		try:
			from frappe.desk.reportview import build_match_conditions as reportview_builder
		except ImportError:
			reportview_builder = None
		builder = reportview_builder
	if not callable(builder):
		raise RuntimeError("build_match_conditions is unavailable")
	try:
		return builder(doctype, user=user)
	except TypeError:
		return builder(doctype)


def company_only_match_conditions(match_conditions: str | None, company: str) -> bool:
	"""Strict proof that visibility is empty or exactly one company equality."""
	if not match_conditions:
		return True
	condition = str(match_conditions).strip().rstrip(";").strip()
	while condition.startswith("(") and condition.endswith(")"):
		condition = condition[1:-1].strip()
	pattern = r"^(?:`?(?:tab)?Sales\s+Invoice`?\s*\.\s*)?`?company`?\s*=\s*([\"'])(.*?)\1$"
	match = re.match(pattern, condition, flags=re.IGNORECASE)
	return bool(match and match.group(2) == str(company))


def evaluate_summary_eligibility(filters: dict, user: str | None = None) -> dict:
	"""Return the frozen eligibility dictionary without ever widening visibility."""
	filters = filters or {}
	result = {
		"eligible": True,
		"reason": "eligible",
		"match_conditions_safe": False,
		"has_blocking_repost": False,
		"dirty_days": [],
		"replay_invoices": 0,
	}
	for required in ("company", "from_date", "to_date"):
		if not filters.get(required):
			result.update({"eligible": False, "reason": f"missing_{required}"})
			return result

	extra_keys = sorted(
		key for key, value in filters.items() if key not in ALLOWED_SUMMARY_FILTER_KEYS and not _empty_filter_value(value)
	)
	if extra_keys:
		result.update({"eligible": False, "reason": "disallowed_filter_keys:" + ",".join(extra_keys)})
		return result

	if filters.get("group_by") not in {"Warehouse", "Monthly"}:
		result.update({"eligible": False, "reason": "unsupported_group_by"})
		return result

	with _bound_user(user or _session_user()) as bound_user:
		try:
			match_conditions = build_match_conditions("Sales Invoice", user=bound_user)
		except Exception:
			result.update({"eligible": False, "reason": "match_conditions_unproven"})
			return result
	safe = company_only_match_conditions(match_conditions, filters.get("company"))
	result["match_conditions_safe"] = safe
	if not safe:
		result.update({"eligible": False, "reason": "match_conditions_not_company_only"})
	return result


def is_summary_eligible(filters: dict, user: str | None = None) -> bool:
	return bool(evaluate_summary_eligibility(filters, user).get("eligible"))
