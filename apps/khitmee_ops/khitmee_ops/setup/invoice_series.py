"""Install the independent invoice reservation audit table."""

import frappe

from khitmee_ops.api.invoice_series import AUDIT_TABLE


def ensure_invoice_series_audit():
	if frappe.db.db_type != "mariadb":
		return
	frappe.db.sql(
		f"""CREATE TABLE IF NOT EXISTS `{AUDIT_TABLE}` (
			invoice_name VARCHAR(140) NOT NULL,
			prefix VARCHAR(140) NOT NULL,
			serial_number BIGINT NOT NULL,
			reserved_at DATETIME(6) NOT NULL,
			reserved_by VARCHAR(140) NOT NULL,
			request_key VARCHAR(140) NOT NULL,
			PRIMARY KEY (invoice_name),
			UNIQUE KEY prefix_serial (prefix, serial_number),
			KEY request_reservations (request_key, reserved_at)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
	)
