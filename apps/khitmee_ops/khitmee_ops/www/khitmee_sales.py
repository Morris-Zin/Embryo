from urllib.parse import urlencode

import frappe

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.redirect(f"/login?{urlencode({'redirect-to': frappe.request.path})}")

	context.csrf_token = frappe.sessions.get_csrf_token()
	frappe.db.commit()  # nosemgrep
	context.title = "Khitmee Sales Counter"
