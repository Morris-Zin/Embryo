"""Default-off experiment for durable, independently reserved counter invoice numbers."""

from contextlib import contextmanager
import threading

import frappe
from frappe.utils import cint


FLAG = "khitmee_independent_invoice_series"
AUDIT_TABLE = "tabKhitmee Invoice Series Reservation"
_SCOPE = threading.local()
_INSTALL_LOCK = threading.Lock()


def _connect_site_db():
	from frappe.database import get_db

	conf = frappe.local.conf
	return get_db(
		socket=conf.db_socket,
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user or conf.db_name,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)


def _reserve(prefix, digits, request_key):
	connection = _connect_site_db()
	try:
		# The upsert uses the same primary-key lock as native getseries,
		# including when two transactions create a new prefix.
		connection.sql(
			"INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 1) "
			"ON DUPLICATE KEY UPDATE `current` = `current` + 1",
			(prefix,),
		)
		counter = connection.sql(
			"SELECT `current` FROM `tabSeries` WHERE `name` = %s", (prefix,)
		)[0][0]
		number = f"{counter:0{digits}d}"
		connection.sql(
			f"""INSERT INTO `{AUDIT_TABLE}`
				(invoice_name, prefix, serial_number, reserved_at, reserved_by, request_key)
				VALUES (%s, %s, %s, NOW(6), %s, %s)""",
			(prefix + number, prefix, counter, frappe.session.user, request_key),
		)
		connection.commit()
		return number
	except Exception:
		connection.rollback()
		raise
	finally:
		connection.close()


def _make_generate_dispatcher(original, parse):
	def generate(series, doc, *, ignore_validate=False):
		scope = getattr(_SCOPE, "reservation", None)
		if scope is not None and doc is scope[0] and not doc.get("amended_from"):
			parts = series.series.split(".")
			number_parts = [part for part in parts if part.startswith("#")]
			# Terminal counters give us the exact final invoice name at
			# allocation time. Delegate other formats to stock naming.
			if len(number_parts) == 1 and parts[-1] == number_parts[0] and set(parts[-1]) == {"#"}:
				if not ignore_validate:
					series.validate()
				return parse(parts, doc=doc, number_generator=lambda prefix, width: _reserve(prefix, width, scope[1]))
		return original(series, doc, ignore_validate=ignore_validate)

	generate._khitmee_dispatcher = True
	return generate


def _make_revert_dispatcher(original):
	def revert(key, name, doc=None):
		db = frappe.db
		if (
			(doc is None or doc.doctype == "Sales Invoice")
			and db.db_type == "mariadb"
			and db.table_exists(AUDIT_TABLE.removeprefix("tab"))
			and db.sql(f"SELECT 1 FROM `{AUDIT_TABLE}` WHERE invoice_name = %s", (name,))
		):
			# An audited number is spent permanently, including after the
			# experiment is switched off or the invoice is deleted.
			return None
		return original(key, name, doc)

	revert._khitmee_dispatcher = True
	return revert


def install_series_dispatchers(*args, **kwargs):
	from frappe.model import delete_doc, naming

	with _INSTALL_LOCK:
		generate = naming.NamingSeries.generate_next_name
		if not getattr(generate, "_khitmee_dispatcher", False):
			naming.NamingSeries.generate_next_name = _make_generate_dispatcher(generate, naming.parse_naming_series)
		revert = naming.revert_series_if_last
		if not getattr(revert, "_khitmee_dispatcher", False):
			naming.revert_series_if_last = _make_revert_dispatcher(revert)
			if delete_doc.revert_series_if_last is revert:
				delete_doc.revert_series_if_last = naming.revert_series_if_last


@contextmanager
def counter_series_reservation(doc, request_key, *, fresh_snapshot):
	conf = getattr(frappe, "conf", None)
	if not fresh_snapshot or not conf or not cint(conf.get(FLAG)):
		yield
		return
	if frappe.db.db_type != "mariadb":
		frappe.throw(frappe._("Independent invoice reservations require MariaDB."), frappe.ValidationError)
	install_series_dispatchers()
	previous = getattr(_SCOPE, "reservation", None)
	_SCOPE.reservation = (doc, request_key)
	try:
		yield
	finally:
		_SCOPE.reservation = previous
