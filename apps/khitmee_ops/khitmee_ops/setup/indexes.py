import frappe
from frappe.utils import add_days, cint, nowdate

OBSOLETE_INDEXES = (
	("tabSales Invoice Item", "khitmee_sii_warehouse_item_parent"),
	("tabSales Invoice", "khitmee_si_company_docstatus_posting"),
	("tabSales Invoice", "khitmee_si_co_ds_date_time_ret"),
	("tabSales Invoice Item", "khitmee_sii_parent_name_warehouse"),
)

COMPOSITE_INDEXES = (
	("tabItem Price", "khitmee_item_price_price_list_item", ("price_list", "item_code")),
	(
		"tabSales Invoice",
		"khitmee_si_co_ds_ret_date",
		("company", "docstatus", "is_return", "posting_date", "posting_time", "return_against", "is_opening"),
	),
	(
		"tabSales Invoice Item",
		"khitmee_sii_parent_warehouse",
		("parent", "warehouse"),
	),
	("tabGL Entry", "khitmee_gl_company_posting_date", ("company", "posting_date")),
	(
		"tabStock Ledger Entry",
		"khitmee_sle_company_posting_creation",
		("company", "posting_datetime", "creation"),
	),
	(
		"tabStock Ledger Entry",
		"khitmee_sle_company_cancelled_date_wh",
		("company", "is_cancelled", "warehouse", "posting_date"),
	),
	("tabStock Closing Balance", "khitmee_scb_entry_warehouse", ("stock_closing_entry", "warehouse")),
	("tabPacked Item", "khitmee_pi_pt_ds_item", ("parenttype", "docstatus", "parent_item")),
)


def _get_index_columns(table_name, index_name):
	return frappe.db.sql(
		"""
		SELECT SEQ_IN_INDEX, COLUMN_NAME
		FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
		ORDER BY SEQ_IN_INDEX
		""",
		(table_name, index_name),
	)


def _index_columns_match(index_rows, expected_columns):
	ordered_columns = [
		column_name
		for _sequence, column_name in sorted(index_rows or [], key=lambda row: cint(row[0]))
	]
	return ordered_columns == list(expected_columns)


def _index_exists(table_name, index_name, expected_columns=None):
	index_rows = _get_index_columns(table_name, index_name)
	if expected_columns is None:
		return bool(index_rows)
	return _index_columns_match(index_rows, expected_columns)


def create_composite_indexes():
	"""Create Khitmee's composite MariaDB indexes idempotently."""
	created = []
	for table_name, index_name, columns in COMPOSITE_INDEXES:
		index_rows = _get_index_columns(table_name, index_name)
		if _index_columns_match(index_rows, columns):
			continue
		if index_rows:
			frappe.db.sql(f"DROP INDEX `{index_name}` ON `{table_name}`")

		column_sql = ", ".join(f"`{column}`" for column in columns)
		frappe.db.sql(f"CREATE INDEX `{index_name}` ON `{table_name}` ({column_sql})")
		created.append(index_name)

	return created


def drop_obsolete_indexes():
	"""Drop superseded Khitmee indexes idempotently."""
	dropped = []
	for table_name, index_name in OBSOLETE_INDEXES:
		if not _index_exists(table_name, index_name):
			continue
		frappe.db.sql(f"DROP INDEX `{index_name}` ON `{table_name}`")
		dropped.append(index_name)

	return dropped


def _column_exists(table_name, column_name):
	return bool(
		frappe.db.sql(
			"""
			SELECT COUNT(*)
			FROM information_schema.COLUMNS
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
			""",
			(table_name, column_name),
		)[0][0]
	)


def _invalidate_fast_report_cache():
	frappe.clear_document_cache("Report", "Fast Gross Profit")
	cache = getattr(frappe, "cache", None)
	if callable(cache) and not hasattr(cache, "get_value"):
		cache = cache()
	if cache is not None:
		cache.delete_key("bootinfo")
	try:
		cache.hdel("report_execution_time", "Fast Gross Profit")
	except Exception:
		pass


def sync_fast_report_settings():
	"""Keep Fast Gross Profit synchronous without prepared-report automation."""
	if not _column_exists("tabReport", "disable_prepared_report_automation"):
		return False
	if not frappe.db.sql("SELECT 1 FROM `tabReport` WHERE `name` = 'Fast Gross Profit' LIMIT 1"):
		return False

	frappe.db.sql(
		"UPDATE `tabReport` SET `prepared_report` = 0, `disable_prepared_report_automation` = 1 "
		"WHERE `name` = 'Fast Gross Profit'"
	)
	_invalidate_fast_report_cache()
	rows = frappe.db.sql(
		"SELECT `prepared_report`, `disable_prepared_report_automation` "
		"FROM `tabReport` WHERE `name` = 'Fast Gross Profit' LIMIT 1",
		as_dict=True,
	)
	if not rows or cint(rows[0].get("prepared_report")) != 0 or cint(rows[0].get("disable_prepared_report_automation")) != 1:
		raise RuntimeError("Fast Gross Profit prepared-report settings failed closed verification")
	return True


def enable_item_search_index(rebuild=True):
	"""Enable ERPNext's SQLite FTS5 item index and optionally build it in the foreground."""
	frappe.db.set_single_value("Global Defaults", "enable_item_search_index", 1)
	if not rebuild:
		return False

	from frappe.search.sqlite_search import build_index
	from erpnext.stock.doctype.item.item_search import ItemSearch, validate_sqlite_version

	validate_sqlite_version()
	build_index(ItemSearch, force=True)
	return True


def ensure_catalog_tombstone_table():
	"""DDL for Item delete/rename markers used by incremental catalog sync."""
	frappe.db.sql(
		"""
		CREATE TABLE IF NOT EXISTS `tabKhitmee Catalog Tombstone` (
			`item_code` varchar(140) NOT NULL PRIMARY KEY,
			`modified` datetime(6) NOT NULL,
			KEY `khitmee_catalog_tombstone_modified` (`modified`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
		"""
	)


def ensure_daily_stock_closing_table():
	"""DDL for the daily closing table; must run before any DML in the transaction."""
	frappe.db.sql(
		"""
		CREATE TABLE IF NOT EXISTS `tabKhitmee Daily Stock Closing` (
			`name` varchar(140) NOT NULL PRIMARY KEY,
			`company` varchar(140) NOT NULL,
			`item_code` varchar(140) NOT NULL,
			`warehouse` varchar(140) NOT NULL,
			`closing_date` date NOT NULL,
			`qty_after_transaction` decimal(21, 9) NOT NULL DEFAULT 0,
			`stock_value` decimal(21, 9) NOT NULL DEFAULT 0,
			`valuation_rate` decimal(21, 9) NOT NULL DEFAULT 0,
			`creation` datetime NOT NULL,
			`modified` datetime NOT NULL,
			UNIQUE KEY `khitmee_daily_stock_closing_key` (`company`, `item_code`, `warehouse`, `closing_date`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
		"""
	)


def create_daily_stock_closing(posting_date=None, company=None, _ddl_done=False):
	"""Create or refresh one latest-SLE-per-item/warehouse closing snapshot for a date.

	This helper is intentionally additive and idempotent. It does not feed the Gross Profit
	report yet; it gives downstream reconciliation jobs a bounded daily closing table without
	rescanning all historical Stock Ledger Entries. Rows for the requested scope are
	replaced atomically so cancelled entries cannot leave stale closing snapshots behind.
	"""
	posting_date = posting_date or nowdate()
	if not _ddl_done:
		ensure_daily_stock_closing_table()

	# Strict end-of-day cutoff: only entries strictly before the next midnight count.
	conditions = ["`is_cancelled` = 0", "`posting_datetime` < %s"]
	params = [f"{add_days(posting_date, 1)} 00:00:00"]
	if company:
		conditions.append("`company` = %s")
		params.append(company)

	delete_sql = "DELETE FROM `tabKhitmee Daily Stock Closing` WHERE `closing_date` = %s"
	delete_params = [posting_date]
	if company:
		delete_sql += " AND `company` = %s"
		delete_params.append(company)
	frappe.db.sql(delete_sql, delete_params)

	frappe.db.sql(
		"""
		INSERT INTO `tabKhitmee Daily Stock Closing` (
			`name`, `company`, `item_code`, `warehouse`, `closing_date`,
			`qty_after_transaction`, `stock_value`, `valuation_rate`, `creation`, `modified`
		)
		SELECT
			MD5(CONCAT_WS('|', ranked.company, ranked.item_code, ranked.warehouse, %s)),
			ranked.company,
			ranked.item_code,
			ranked.warehouse,
			%s,
			COALESCE(ranked.qty_after_transaction, 0),
			COALESCE(ranked.stock_value, 0),
			COALESCE(ranked.valuation_rate, 0),
			NOW(),
			NOW()
		FROM (
			SELECT
				`company`,
				`item_code`,
				`warehouse`,
				`qty_after_transaction`,
				`stock_value`,
				`valuation_rate`,
				ROW_NUMBER() OVER (
					PARTITION BY `company`, `item_code`, `warehouse`
					ORDER BY `posting_datetime` DESC, `creation` DESC
				) AS `khitmee_row_number`
			FROM `tabStock Ledger Entry`
			WHERE {conditions}
		) AS ranked
		WHERE ranked.khitmee_row_number = 1
		ON DUPLICATE KEY UPDATE
			`qty_after_transaction` = VALUES(`qty_after_transaction`),
			`stock_value` = VALUES(`stock_value`),
			`valuation_rate` = VALUES(`valuation_rate`),
			`modified` = VALUES(`modified`)
		""".format(conditions=" AND ".join(conditions)),
		[posting_date, posting_date, *params],
	)

	count_params = [posting_date]
	count_sql = "SELECT COUNT(*) FROM `tabKhitmee Daily Stock Closing` WHERE closing_date = %s"
	if company:
		count_sql += " AND company = %s"
		count_params.append(company)
	return frappe.db.sql(count_sql, count_params)[0][0]


def setup_database_objects(rebuild_item_search=False, stock_closing_date=None, company=None):
	# All DDL first: mixing DDL after DML writes in one transaction makes Frappe throw
	# ImplicitCommitError, so index changes and the closing table are created before any write.
	dropped = drop_obsolete_indexes()
	created = create_composite_indexes()
	ensure_catalog_tombstone_table()
	if stock_closing_date:
		ensure_daily_stock_closing_table()

	search_rebuilt = enable_item_search_index(rebuild=rebuild_item_search)
	sync_fast_report_settings()
	closing_rows = None
	if stock_closing_date:
		closing_rows = create_daily_stock_closing(stock_closing_date, company=company, _ddl_done=True)
	return {
		"dropped_indexes": dropped,
		"created_indexes": created,
		"item_search_rebuilt": search_rebuilt,
		"daily_stock_closing_rows": closing_rows,
	}
