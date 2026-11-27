from urllib.parse import urlencode

import frappe

from khitmee_ops.counter_assets import counter_asset_versions

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		path = frappe.request.full_path
		if path.endswith("?"):
			path = path[:-1]
		frappe.redirect(f"/login?{urlencode({'redirect-to': path})}")

	context.csrf_token = frappe.sessions.get_csrf_token()
	context.asset_versions = counter_asset_versions()
	frappe.db.commit()  # nosemgrep
	context.title = "Khitmee Sales Counter"
