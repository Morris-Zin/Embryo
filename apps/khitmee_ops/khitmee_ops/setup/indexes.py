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

REQUIRED_RIV_TRIGGERS = (
	"khitmee_riv_after_insert_dirty_range",
	"khitmee_riv_after_update_dirty_range",
	"khitmee_riv_before_delete_dirty_range",
)

PROFIT_SUMMARY_UNIQUE_INDEXES = (
	(
		"tabDaily Profit by Shop",
		"khitmee_daily_profit_company_date_warehouse_unique",
		("company", "posting_date", "warehouse"),
	),
	(
		"tabKhitmee Profit Day State",
		"khitmee_profit_day_company_date_unique",
		("company", "posting_date"),
	),
	(
		"tabKhitmee Profit Replay Candidate",
		"khitmee_profit_replay_company_date_invoice_unique",
		("company", "posting_date", "sales_invoice"),
	),
)

PROFIT_SUMMARY_INDEXES = (
	(
		"tabKhitmee Profit Replay Candidate",
		"khitmee_profit_replay_company_invoice",
		("company", "sales_invoice"),
	),
	(
		"tabKhitmee Profit Dirty Range",
		"khitmee_profit_dirty_company_processed_from",
		("company", "processed", "from_date"),
	),
)

_STANDARD_DOCTYPE_COLUMNS_SQL = """\
	`name` varchar(140) NOT NULL,
	`creation` datetime(6) NULL,
	`modified` datetime(6) NULL,
	`modified_by` varchar(140) NULL,
	`owner` varchar(140) NULL,
	`docstatus` int(11) NOT NULL DEFAULT 0,
	`idx` int(8) NOT NULL DEFAULT 0,
"""

PROFIT_SUMMARY_TABLE_DDLS = {
	"tabDaily Profit by Shop": f"""
		CREATE TABLE IF NOT EXISTS `tabDaily Profit by Shop` (
{_STANDARD_DOCTYPE_COLUMNS_SQL}
			`company` varchar(140) NOT NULL,
			`posting_date` date NOT NULL,
			`warehouse` varchar(140) NOT NULL DEFAULT '',
			`normal_has_rows` tinyint(1) NOT NULL DEFAULT 0,
			`normal_qty` decimal(18, 6) NOT NULL DEFAULT 0,
			`normal_base_amount` decimal(18, 6) NOT NULL DEFAULT 0,
			`normal_buying_amount` decimal(18, 6) NOT NULL DEFAULT 0,
			`normal_abs_qty_sum` decimal(20, 6) NOT NULL DEFAULT 0,
			`normal_abs_base_sum` decimal(20, 6) NOT NULL DEFAULT 0,
			`normal_abs_buying_sum` decimal(20, 6) NOT NULL DEFAULT 0,
			`normal_first_seen_key` varchar(96) NULL,
			`unlinked_return_has_rows` tinyint(1) NOT NULL DEFAULT 0,
			`unlinked_return_qty` decimal(18, 6) NOT NULL DEFAULT 0,
			`unlinked_return_base_amount` decimal(18, 6) NOT NULL DEFAULT 0,
			`unlinked_return_buying_amount` decimal(18, 6) NOT NULL DEFAULT 0,
			`unlinked_return_abs_qty_sum` decimal(20, 6) NOT NULL DEFAULT 0,
			`unlinked_return_abs_base_sum` decimal(20, 6) NOT NULL DEFAULT 0,
			`unlinked_return_abs_buying_sum` decimal(20, 6) NOT NULL DEFAULT 0,
			`unlinked_return_first_seen_key` varchar(96) NULL,
			PRIMARY KEY (`name`),
			UNIQUE KEY `khitmee_daily_profit_company_date_warehouse_unique` (`company`, `posting_date`, `warehouse`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
	""",
	"tabKhitmee Profit Day State": f"""
		CREATE TABLE IF NOT EXISTS `tabKhitmee Profit Day State` (
{_STANDARD_DOCTYPE_COLUMNS_SQL}
			`company` varchar(140) NOT NULL,
			`posting_date` date NOT NULL,
			`dirty_generation` bigint NOT NULL DEFAULT 1,
			`built_generation` bigint NOT NULL DEFAULT 0,
			`status` varchar(16) NOT NULL DEFAULT 'dirty',
			`claim_token` varchar(64) NULL,
			`claim_expires_at` datetime(6) NULL,
			`repost_watermark` datetime(6) NULL,
			`repost_completed_count` int NOT NULL DEFAULT 0,
			`repost_set_digest` bigint unsigned NOT NULL DEFAULT 0,
			`calc_version` int NOT NULL DEFAULT 1,
			`currency_precision` int NOT NULL DEFAULT 3,
			`float_precision` int NOT NULL DEFAULT 2,
			`rounding_method` varchar(64) NOT NULL,
			`is_additive_safe` tinyint(1) NOT NULL DEFAULT 1,
			`last_error` text NULL,
			`rebuilt_at` datetime(6) NULL,
			PRIMARY KEY (`name`),
			UNIQUE KEY `khitmee_profit_day_company_date_unique` (`company`, `posting_date`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
	""",
	"tabKhitmee Profit Replay Candidate": f"""
		CREATE TABLE IF NOT EXISTS `tabKhitmee Profit Replay Candidate` (
{_STANDARD_DOCTYPE_COLUMNS_SQL}
			`company` varchar(140) NOT NULL,
			`posting_date` date NOT NULL,
			`sales_invoice` varchar(140) NOT NULL,
			`reason` varchar(40) NOT NULL,
			PRIMARY KEY (`name`),
			UNIQUE KEY `khitmee_profit_replay_company_date_invoice_unique` (`company`, `posting_date`, `sales_invoice`),
			KEY `khitmee_profit_replay_company_invoice` (`company`, `sales_invoice`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
	""",
	"tabKhitmee Profit Dirty Range": f"""
		CREATE TABLE IF NOT EXISTS `tabKhitmee Profit Dirty Range` (
{_STANDARD_DOCTYPE_COLUMNS_SQL}
			`company` varchar(140) NOT NULL,
			`from_date` date NOT NULL,
			`cursor_date` date NULL,
			`processed` tinyint(1) NOT NULL DEFAULT 0,
			`source_doctype` varchar(64) NULL,
			`source_docname` varchar(140) NULL,
			PRIMARY KEY (`name`),
			KEY `khitmee_profit_dirty_company_processed_from` (`company`, `processed`, `from_date`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
	""",
}

PROFIT_SUMMARY_COLUMN_DEFINITIONS = {
	"tabDaily Profit by Shop": {
		"company": "varchar(140) NOT NULL",
		"posting_date": "date NOT NULL",
		"warehouse": "varchar(140) NOT NULL DEFAULT ''",
		"normal_has_rows": "tinyint(1) NOT NULL DEFAULT 0",
		"normal_qty": "decimal(18, 6) NOT NULL DEFAULT 0",
		"normal_base_amount": "decimal(18, 6) NOT NULL DEFAULT 0",
		"normal_buying_amount": "decimal(18, 6) NOT NULL DEFAULT 0",
		"normal_abs_qty_sum": "decimal(20, 6) NOT NULL DEFAULT 0",
		"normal_abs_base_sum": "decimal(20, 6) NOT NULL DEFAULT 0",
		"normal_abs_buying_sum": "decimal(20, 6) NOT NULL DEFAULT 0",
		"normal_first_seen_key": "varchar(96) NULL",
		"unlinked_return_has_rows": "tinyint(1) NOT NULL DEFAULT 0",
		"unlinked_return_qty": "decimal(18, 6) NOT NULL DEFAULT 0",
		"unlinked_return_base_amount": "decimal(18, 6) NOT NULL DEFAULT 0",
		"unlinked_return_buying_amount": "decimal(18, 6) NOT NULL DEFAULT 0",
		"unlinked_return_abs_qty_sum": "decimal(20, 6) NOT NULL DEFAULT 0",
		"unlinked_return_abs_base_sum": "decimal(20, 6) NOT NULL DEFAULT 0",
		"unlinked_return_abs_buying_sum": "decimal(20, 6) NOT NULL DEFAULT 0",
		"unlinked_return_first_seen_key": "varchar(96) NULL",
	},
	"tabKhitmee Profit Day State": {
		"company": "varchar(140) NOT NULL",
		"posting_date": "date NOT NULL",
		"dirty_generation": "bigint NOT NULL DEFAULT 1",
		"built_generation": "bigint NOT NULL DEFAULT 0",
		"status": "varchar(16) NOT NULL DEFAULT 'dirty'",
		"claim_token": "varchar(64) NULL",
		"claim_expires_at": "datetime(6) NULL",
		"repost_watermark": "datetime(6) NULL",
		"repost_completed_count": "int NOT NULL DEFAULT 0",
		"repost_set_digest": "bigint unsigned NOT NULL DEFAULT 0",
		"calc_version": "int NOT NULL DEFAULT 1",
		"currency_precision": "int NOT NULL DEFAULT 3",
		"float_precision": "int NOT NULL DEFAULT 2",
		"rounding_method": "varchar(64) NOT NULL",
		"is_additive_safe": "tinyint(1) NOT NULL DEFAULT 1",
		"last_error": "text NULL",
		"rebuilt_at": "datetime(6) NULL",
	},
	"tabKhitmee Profit Replay Candidate": {
		"company": "varchar(140) NOT NULL",
		"posting_date": "date NOT NULL",
		"sales_invoice": "varchar(140) NOT NULL",
		"reason": "varchar(40) NOT NULL",
	},
	"tabKhitmee Profit Dirty Range": {
		"company": "varchar(140) NOT NULL",
		"from_date": "date NOT NULL",
		"cursor_date": "date NULL",
		"processed": "tinyint(1) NOT NULL DEFAULT 0",
		"source_doctype": "varchar(64) NULL",
		"source_docname": "varchar(140) NULL",
	},
}

_REPOST_TRIGGER_INSERT_COLUMNS = (
	"`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`, "
	"`company`, `from_date`, `cursor_date`, `processed`, `source_doctype`, `source_docname`"
)


def _repost_trigger_insert_values(row_alias):
	return (
		"UUID(), NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, "
		f"{row_alias}.`company`, {row_alias}.`posting_date`, NULL, 0, "
		f"'Repost Item Valuation', {row_alias}.`name`"
	)


def _build_repost_trigger_sql(trigger_name, timing, event, condition, row_alias):
	return f"""
		CREATE TRIGGER `{trigger_name}` {timing} {event} ON `tabRepost Item Valuation`
		FOR EACH ROW
		BEGIN
			IF {condition} THEN
				INSERT INTO `tabKhitmee Profit Dirty Range` ({_REPOST_TRIGGER_INSERT_COLUMNS})
				VALUES ({_repost_trigger_insert_values(row_alias)});
			END IF;
		END
	"""


REPOST_TRIGGER_DDLS = {
	"khitmee_riv_after_insert_dirty_range": _build_repost_trigger_sql(
		"khitmee_riv_after_insert_dirty_range",
		"AFTER",
		"INSERT",
		"NEW.`docstatus` = 1 AND NEW.`status` = 'Completed'",
		"NEW",
	),
	"khitmee_riv_after_update_dirty_range": _build_repost_trigger_sql(
		"khitmee_riv_after_update_dirty_range",
		"AFTER",
		"UPDATE",
		"NEW.`docstatus` = 1 AND NEW.`status` = 'Completed' "
		"AND NOT (OLD.`docstatus` = 1 AND OLD.`status` = 'Completed')",
		"NEW",
	),
	"khitmee_riv_before_delete_dirty_range": _build_repost_trigger_sql(
		"khitmee_riv_before_delete_dirty_range",
		"BEFORE",
		"DELETE",
		"OLD.`docstatus` = 1 AND OLD.`status` = 'Completed'",
		"OLD",
	),
}


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


def _index_is_unique(table_name, index_name):
	rows = frappe.db.sql(
		"""
		SELECT NON_UNIQUE
		FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
		LIMIT 1
		""",
		(table_name, index_name),
	)
	if not rows:
		return False
	row = rows[0]
	value = row.get("NON_UNIQUE") if hasattr(row, "get") else row[0]
	return not bool(cint(value))


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


def _normalize_column_type(column_type):
	normalized = "".join(str(column_type or "").lower().split())
	# MariaDB reports integer display widths (bigint(20), int(11)) even when the
	# frozen DDL says bigint/int. Display width is not part of the logical type.
	for prefix, replacement in (("bigint(20)", "bigint"), ("int(11)", "int")):
		if normalized.startswith(prefix):
			normalized = replacement + normalized[len(prefix):]
	return normalized


def _column_type_definition(column_definition):
	for marker in (" NOT NULL", " NULL"):
		position = column_definition.upper().find(marker)
		if position >= 0:
			return column_definition[:position]
	return column_definition


def _get_column_types(table_name):
	rows = frappe.db.sql(
		"""
		SELECT COLUMN_NAME, COLUMN_TYPE
		FROM information_schema.COLUMNS
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
		""",
		(table_name,),
	)
	result = {}
	for row in rows or []:
		if hasattr(row, "get"):
			result[row.get("COLUMN_NAME") or row.get("column_name")] = row.get("COLUMN_TYPE") or row.get("column_type")
		else:
			result[row[0]] = row[1]
	return result


def ensure_exact_profit_summary_columns():
	"""Bring pre-existing Frappe-synced summary tables to the frozen decimal integer widths."""
	altered = []
	for table_name, columns in PROFIT_SUMMARY_COLUMN_DEFINITIONS.items():
		existing_types = _get_column_types(table_name)
		for column_name, column_definition in columns.items():
			expected_type = _column_type_definition(column_definition)
			if column_name not in existing_types:
				frappe.db.sql(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_definition}")
				altered.append(f"{table_name}.{column_name}")
				continue
			if _normalize_column_type(existing_types.get(column_name)) == _normalize_column_type(expected_type):
				continue
			frappe.db.sql(f"ALTER TABLE `{table_name}` MODIFY COLUMN `{column_name}` {column_definition}")
			altered.append(f"{table_name}.{column_name}")
	return altered


def ensure_profit_summary_tables():
	"""Create the four profit summary tables with exact frozen MariaDB column types."""
	for table_name in PROFIT_SUMMARY_TABLE_DDLS:
		frappe.db.sql(PROFIT_SUMMARY_TABLE_DDLS[table_name])
	return ensure_exact_profit_summary_columns()


def _create_index_definitions(index_definitions, unique=False):
	created = []
	for table_name, index_name, columns in index_definitions:
		index_rows = _get_index_columns(table_name, index_name)
		columns_match = _index_columns_match(index_rows, columns)
		if columns_match and (not unique or _index_is_unique(table_name, index_name)):
			continue
		if index_rows:
			frappe.db.sql(f"DROP INDEX `{index_name}` ON `{table_name}`")

		column_sql = ", ".join(f"`{column}`" for column in columns)
		unique_sql = "UNIQUE " if unique else ""
		frappe.db.sql(f"CREATE {unique_sql}INDEX `{index_name}` ON `{table_name}` ({column_sql})")
		created.append(index_name)
	return created


def create_profit_summary_indexes():
	"""Create composite and unique profit-summary indexes idempotently."""
	return _create_index_definitions(PROFIT_SUMMARY_UNIQUE_INDEXES, unique=True) + _create_index_definitions(
		PROFIT_SUMMARY_INDEXES
	)


def verify_repost_triggers_installed():
	"""Verify required repost triggers on the active database on every call.

	This intentionally never caches a positive result: a trigger dropped mid-run must make
	the next read or publish decision fail closed.
	"""
	placeholders = ", ".join(["%s"] * len(REQUIRED_RIV_TRIGGERS))
	rows = frappe.db.sql(
		f"""
		SELECT TRIGGER_NAME
		FROM information_schema.TRIGGERS
		WHERE TRIGGER_SCHEMA = DATABASE()
			AND EVENT_OBJECT_TABLE = 'tabRepost Item Valuation'
			AND TRIGGER_NAME IN ({placeholders})
		""",
		REQUIRED_RIV_TRIGGERS,
	)
	installed = set()
	for row in rows or []:
		installed.add(row.get("TRIGGER_NAME") if hasattr(row, "get") else row[0])
	missing = [name for name in REQUIRED_RIV_TRIGGERS if name not in installed]
	return not missing, missing


def install_repost_item_valuation_triggers():
	"""Install all transactional completion-to-purge triggers idempotently."""
	installed = []
	for trigger_name in REQUIRED_RIV_TRIGGERS:
		frappe.db.sql(f"DROP TRIGGER IF EXISTS `{trigger_name}`")
		frappe.db.sql(REPOST_TRIGGER_DDLS[trigger_name])
		installed.append(trigger_name)

	verified, missing = verify_repost_triggers_installed()
	if not verified:
		raise RuntimeError(f"Repost Item Valuation trigger verification failed: missing {', '.join(missing)}")
	return installed


def execute():
	"""Migration patch entrypoint for the profit summary schema and repost triggers."""
	ensure_profit_summary_tables()
	create_profit_summary_indexes()
	install_repost_item_valuation_triggers()
	# Existing sites have history but no day states; register it so the scheduler
	# builds it instead of leaving long-range reports pending indefinitely.
	from khitmee_ops.profit_summary.rebuilder import register_missing_history

	register_missing_history()


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

	from erpnext.stock.doctype.item.item_search import ItemSearch, validate_sqlite_version
	from frappe.search.sqlite_search import build_index

	validate_sqlite_version()
	build_index(ItemSearch, force=True)
	return True


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
	# ImplicitCommitError, so table/index/trigger changes run before any data write.
	profit_columns_altered = ensure_profit_summary_tables()
	profit_indexes_created = create_profit_summary_indexes()
	repost_triggers_installed = install_repost_item_valuation_triggers()
	dropped = drop_obsolete_indexes()
	created = create_composite_indexes()
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
		"profit_columns_altered": profit_columns_altered,
		"profit_indexes_created": profit_indexes_created,
		"repost_triggers_installed": repost_triggers_installed,
		"item_search_rebuilt": search_rebuilt,
		"daily_stock_closing_rows": closing_rows,
	}
