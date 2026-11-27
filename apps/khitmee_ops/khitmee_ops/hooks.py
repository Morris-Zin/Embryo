app_name = "khitmee_ops"
app_title = "Khitmee Ops"
app_publisher = "Khitmee"
app_description = "Counter operations, voucher printing, and fast ERPNext reports"
app_email = "admin@example.invalid"
app_license = "GNU General Public License (v3)"
app_version = "0.1.0"

# The bench CLI discovers click commands from khitmee_ops.commands in Frappe v16.
# They are intentionally not imported here: importing click/frappe command modules in hooks
# slows every web worker startup.

doc_events = {
	"Sales Invoice": {
		"validate": "khitmee_ops.api.vouchers.sync_counter_autopay_payment",
	},
	"Item": {
		"after_insert": "khitmee_ops.api.search.invalidate_item_search_cache",
		"on_update": "khitmee_ops.api.search.invalidate_item_search_cache",
		"on_trash": [
			"khitmee_ops.api.search.invalidate_item_search_cache",
			"khitmee_ops.api.catalog.record_item_catalog_tombstone",
		],
		"after_rename": [
			"khitmee_ops.api.search.invalidate_item_search_cache",
			"khitmee_ops.api.catalog.record_item_catalog_tombstone",
		],
	},
}

after_request = ["khitmee_ops.api.memory.maybe_trim_heap"]
