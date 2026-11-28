import frappe
from frappe.utils import get_file_timestamp

COUNTER_ASSETS = (
	"css/counter.css",
	"js/counter_core.js",
	"js/print_bridge.js",
	"js/sales_counter.js",
	"js/stock_transfer.js",
)


def counter_asset_versions():
	def version(path):
		return get_file_timestamp(frappe.get_app_path("khitmee_ops", "public", *path.split("/"))) or ""

	return {path: version(path) for path in COUNTER_ASSETS}
