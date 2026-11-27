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

_profit_stock_voucher_hook = "khitmee_ops.profit_summary.store.on_stock_voucher_change"

doc_events = {
	"Sales Invoice": {
		"validate": "khitmee_ops.api.vouchers.sync_counter_autopay_payment",
		"on_submit": "khitmee_ops.profit_summary.store.on_sales_invoice_change",
		"on_cancel": "khitmee_ops.profit_summary.store.on_sales_invoice_change",
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
	"Purchase Invoice": {
		"on_submit": _profit_stock_voucher_hook,
		"on_cancel": _profit_stock_voucher_hook,
	},
	"Purchase Receipt": {
		"on_submit": _profit_stock_voucher_hook,
		"on_cancel": _profit_stock_voucher_hook,
	},
	"Stock Entry": {
		"on_submit": _profit_stock_voucher_hook,
		"on_cancel": _profit_stock_voucher_hook,
	},
	"Stock Reconciliation": {
		"on_submit": _profit_stock_voucher_hook,
		"on_cancel": _profit_stock_voucher_hook,
	},
	"Delivery Note": {
		"on_submit": _profit_stock_voucher_hook,
		"on_cancel": _profit_stock_voucher_hook,
	},
	"Repost Item Valuation": {
		"on_trash": "khitmee_ops.profit_summary.store.on_repost_trash",
	},
}

scheduler_events = {
	"cron": {
		"*/10 * * * *": ["khitmee_ops.profit_summary.rebuilder.process_dirty_slices_scheduled"],
		"5 0 * * *": ["khitmee_ops.profit_summary.rebuilder.enqueue_yesterday_rollover"],
		"40 2 * * *": ["khitmee_ops.profit_summary.reconcile.nightly_reconcile"],
	},
	"hourly": ["khitmee_ops.profit_summary.export_service.cleanup_expired_exports"],
}

after_request = ["khitmee_ops.api.memory.maybe_trim_heap"]
