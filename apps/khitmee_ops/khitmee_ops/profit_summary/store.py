"""Storage boundary for the MariaDB daily profit summary tables."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date, datetime
from typing import Literal, TypedDict

try:  # pragma: no cover - offline tests install a stub before importing
	import frappe
except ImportError:  # pragma: no cover
	frappe = None


class ShopDayRecord(TypedDict):
	company: str
	posting_date: str
	warehouse: str
	normal_has_rows: int
	normal_qty: str
	normal_base_amount: str
	normal_buying_amount: str
	normal_abs_qty_sum: str
	normal_abs_base_sum: str
	normal_abs_buying_sum: str
	normal_first_seen_key: str | None
	unlinked_return_has_rows: int
	unlinked_return_qty: str
	unlinked_return_base_amount: str
	unlinked_return_buying_amount: str
	unlinked_return_abs_qty_sum: str
	unlinked_return_abs_base_sum: str
	unlinked_return_abs_buying_sum: str
	unlinked_return_first_seen_key: str | None


class ReplayCandidateRecord(TypedDict):
	company: str
	posting_date: str
	sales_invoice: str
	reason: str


class ClaimRecord(TypedDict):
	company: str
	posting_date: str
	claim_token: str
	claim_expires_at: str
	dirty_generation: int


class DayFlags(TypedDict):
	calc_version: int
	currency_precision: int
	float_precision: int
	rounding_method: str
	is_additive_safe: bool


class PublishResult(TypedDict):
	published: bool
	status: Literal["clean", "dirty", "rebuilding", "failed"]
	superseded: bool
	blocked_by_repost: bool
	built_generation: int


CALC_VERSION = 1
DEFAULT_ROUNDING_METHOD = "Banker's Rounding (legacy)"

SHOP_DAY_FIELDS = (
	"company", "posting_date", "warehouse",
	"normal_has_rows", "normal_qty", "normal_base_amount", "normal_buying_amount",
	"normal_abs_qty_sum", "normal_abs_base_sum", "normal_abs_buying_sum", "normal_first_seen_key",
	"unlinked_return_has_rows", "unlinked_return_qty", "unlinked_return_base_amount", "unlinked_return_buying_amount",
	"unlinked_return_abs_qty_sum", "unlinked_return_abs_base_sum", "unlinked_return_abs_buying_sum",
	"unlinked_return_first_seen_key",
)

DAY_STATE_FIELDS = (
	"company", "posting_date", "dirty_generation", "built_generation", "status", "claim_token",
	"claim_expires_at", "repost_watermark", "repost_completed_count", "repost_set_digest",
	"calc_version", "currency_precision", "float_precision", "rounding_method", "is_additive_safe",
	"last_error", "rebuilt_at",
)


def _require_frappe():
	if frappe is None:
		raise RuntimeError("khitmee_ops profit summary storage requires Frappe")
	return frappe


def _as_date_string(value):
	if value is None:
		return None
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	return str(value)[:10]


def _as_datetime_string(value):
	if value is None:
		return None
	if isinstance(value, datetime):
		return value.isoformat(sep=" ")
	return str(value)


def _first(row, default=None):
	if row is None:
		return default
	if hasattr(row, "get"):
		return next(iter(row.values()), default)
	return row[0] if len(row) else default


def _row_dict(row, fields):
	if row is None:
		return None
	if hasattr(row, "get"):
		return {field: row.get(field) for field in fields}
	return {field: row[index] for index, field in enumerate(fields)}


def _commit():
	commit = getattr(getattr(_require_frappe(), "db", None), "commit", None)
	if callable(commit):
		commit()


def _rollback():
	rollback = getattr(getattr(_require_frappe(), "db", None), "rollback", None)
	if callable(rollback):
		rollback()


def verify_repost_triggers_installed() -> tuple[bool, list[str]]:
	"""Delegate to setup's active-database check; never cache a positive result."""
	from khitmee_ops.setup.indexes import verify_repost_triggers_installed as verify

	return verify()


def current_rounding_method() -> str:
	get_default = getattr(getattr(_require_frappe(), "db", None), "get_default", None)
	if callable(get_default):
		return get_default("rounding_method") or DEFAULT_ROUNDING_METHOD
	return DEFAULT_ROUNDING_METHOD


def current_day_flags(is_additive_safe=True) -> DayFlags:
	get_default = getattr(getattr(_require_frappe(), "db", None), "get_default", None)

	def default_int(name, fallback):
		if not callable(get_default):
			return fallback
		try:
			return int(get_default(name) or fallback)
		except (TypeError, ValueError):
			return fallback

	return {
		"calc_version": CALC_VERSION,
		"currency_precision": default_int("currency_precision", 3),
		"float_precision": default_int("float_precision", 2),
		"rounding_method": current_rounding_method(),
		"is_additive_safe": bool(is_additive_safe),
	}


def has_blocking_repost(company: str, up_to_date: str) -> tuple[bool, str | None]:
	rows = _require_frappe().db.sql(
		"""
		SELECT MIN(`posting_date`)
		FROM `tabRepost Item Valuation`
		WHERE `company` = %s
			AND `docstatus` = 1
			AND `status` IN ('Queued', 'In Progress', 'Failed')
			AND `posting_date` <= %s
		""",
		(company, up_to_date),
	)
	blocking_date = _as_date_string(_first(rows[0] if rows else None)) if rows else None
	return bool(blocking_date), blocking_date


def get_completed_repost_signature(company: str, up_to_date: str) -> tuple[int, str | None, int]:
	rows = _require_frappe().db.sql(
		"""
		SELECT
			COUNT(*) AS `completed_count`,
			MAX(`modified`) AS `watermark`,
			COALESCE(BIT_XOR(CRC32(CONCAT(`name`, '|', UNIX_TIMESTAMP(`modified`)))), 0) AS `set_digest`
		FROM `tabRepost Item Valuation`
		WHERE `company` = %s
			AND `docstatus` = 1
			AND `status` = 'Completed'
			AND `posting_date` <= %s
		""",
		(company, up_to_date),
	)
	row = rows[0] if rows else None
	if row is None:
		return 0, None, 0
	if hasattr(row, "get"):
		count, watermark, digest = row.get("completed_count", 0), row.get("watermark"), row.get("set_digest", 0)
	else:
		count, watermark, digest = row[0], row[1], row[2]
	return int(count or 0), _as_datetime_string(watermark), int(digest or 0)


def get_day_state(company: str, posting_date: str, *, for_update=False):
	fields = ", ".join(f"`{field}`" for field in DAY_STATE_FIELDS)
	rows = _require_frappe().db.sql(
		f"""
		SELECT {fields}
		FROM `tabKhitmee Profit Day State`
		WHERE `company` = %s AND `posting_date` = %s
		{"FOR UPDATE" if for_update else ""}
		""",
		(company, posting_date),
		as_dict=True,
	)
	return _row_dict(rows[0], DAY_STATE_FIELDS) if rows else None


def get_day_states(company: str, from_date: str, to_date: str):
	fields = ", ".join(f"`{field}`" for field in DAY_STATE_FIELDS)
	rows = _require_frappe().db.sql(
		f"""
		SELECT {fields}
		FROM `tabKhitmee Profit Day State`
		WHERE `company` = %s AND `posting_date` BETWEEN %s AND %s
		ORDER BY `company` ASC, `posting_date` ASC
		""",
		(company, from_date, to_date),
		as_dict=True,
	)
	result = {}
	for row in rows or []:
		decoded = _row_dict(row, DAY_STATE_FIELDS)
		decoded["posting_date"] = _as_date_string(decoded.get("posting_date"))
		result[decoded["posting_date"]] = decoded
	return result


def _day_state_insert_sql():
	return """
		INSERT INTO `tabKhitmee Profit Day State` (
			`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
			`company`, `posting_date`, `dirty_generation`, `built_generation`, `status`,
			`claim_token`, `claim_expires_at`, `repost_watermark`, `repost_completed_count`,
			`repost_set_digest`, `calc_version`, `currency_precision`, `float_precision`,
			`rounding_method`, `is_additive_safe`, `last_error`, `rebuilt_at`
		) VALUES (
			%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0,
			%s, %s, 1, 0, 'dirty', NULL, NULL, NULL, 0, 0, %s, %s, %s, %s, 1, NULL, NULL
		)
	"""


def _day_state_params(company, posting_date, flags):
	return (
		str(uuid.uuid4()), company, posting_date,
		flags["calc_version"], flags["currency_precision"], flags["float_precision"], flags["rounding_method"],
	)


def ensure_day_state(company: str, posting_date: str) -> None:
	flags = current_day_flags()
	_require_frappe().db.sql(
		_day_state_insert_sql() + "\n\t\tON DUPLICATE KEY UPDATE `name` = `name`",
		_day_state_params(company, posting_date, flags),
	)


def bump_dirty_day(company: str, posting_date: str) -> None:
	flags = current_day_flags()
	_require_frappe().db.sql(
		_day_state_insert_sql()
		+ """
		ON DUPLICATE KEY UPDATE
			`dirty_generation` = `dirty_generation` + 1,
			`status` = 'dirty',
			`claim_token` = NULL,
			`claim_expires_at` = NULL,
			`modified` = NOW(6)
		""",
		_day_state_params(company, posting_date, flags),
	)


def bump_dirty_days_ordered(keys: Iterable[tuple[str, str]]) -> None:
	"""Lock and bump multiple days in strict ascending order to avoid lock inversion."""
	ordered_keys = sorted(set((company, _as_date_string(day)) for company, day in keys if company and day))
	for company, posting_date in ordered_keys:
		get_day_state(company, posting_date, for_update=True)
	for company, posting_date in ordered_keys:
		bump_dirty_day(company, posting_date)


def record_dirty_range(company: str, from_date: str, source_doctype: str, source_docname: str) -> None:
	_require_frappe().db.sql(
		"""
		INSERT INTO `tabKhitmee Profit Dirty Range` (
			`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
			`company`, `from_date`, `cursor_date`, `processed`, `source_doctype`, `source_docname`
		) VALUES (
			%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0,
			%s, %s, NULL, 0, %s, %s
		)
		""",
		(str(uuid.uuid4()), company, from_date, source_doctype, source_docname),
	)


def advance_dirty_range_cursor(range_name: str, new_cursor_date: str, processed: bool) -> None:
	"""Advance one range cursor atomically; callers commit this with its day bumps."""
	_require_frappe().db.sql(
		"""
		UPDATE `tabKhitmee Profit Dirty Range`
		SET `cursor_date` = %s, `processed` = %s, `modified` = NOW(6)
		WHERE `name` = %s
			AND `processed` = 0
			AND (
				`cursor_date` IS NULL
				OR `cursor_date` < %s
				OR (`cursor_date` = %s AND %s = 1)
			)
		""",
		(new_cursor_date, 1 if processed else 0, range_name, new_cursor_date, new_cursor_date, 1 if processed else 0),
	)


def get_unprocessed_dirty_ranges(company: str | None = None, limit: int | None = None):
	sql = """
		SELECT `name`, `company`, `from_date`, `cursor_date`, `processed`, `source_doctype`, `source_docname`
		FROM `tabKhitmee Profit Dirty Range`
		WHERE `processed` = 0
	"""
	params = []
	if company:
		sql += " AND `company` = %s"
		params.append(company)
	sql += " ORDER BY `company` ASC, `from_date` ASC, `name` ASC"
	if limit:
		sql += " LIMIT %s"
		params.append(int(limit))
	rows = _require_frappe().db.sql(sql, params, as_dict=True)
	for row in rows or []:
		row["from_date"] = _as_date_string(row.get("from_date"))
		row["cursor_date"] = _as_date_string(row.get("cursor_date"))
	return rows or []


def has_active_dirty_range_covering(company: str, posting_date: str) -> bool:
	rows = _require_frappe().db.sql(
		"""
		SELECT COUNT(*)
		FROM `tabKhitmee Profit Dirty Range`
		WHERE `company` = %s
			AND `processed` = 0
			AND %s >= COALESCE(DATE_ADD(`cursor_date`, INTERVAL 1 DAY), `from_date`)
		""",
		(company, posting_date),
	)
	return bool(_first(rows[0] if rows else None, 0))


def get_active_dirty_ranges(company: str, to_date: str):
	rows = _require_frappe().db.sql(
		"""
		SELECT `name`, `company`, `from_date`, `cursor_date`, `processed`, `source_doctype`, `source_docname`,
			COALESCE(DATE_ADD(`cursor_date`, INTERVAL 1 DAY), `from_date`) AS `effective_date`
		FROM `tabKhitmee Profit Dirty Range`
		WHERE `company` = %s
			AND `processed` = 0
			AND COALESCE(DATE_ADD(`cursor_date`, INTERVAL 1 DAY), `from_date`) <= %s
		ORDER BY `effective_date` ASC, `name` ASC
		""",
		(company, to_date),
		as_dict=True,
	)
	for row in rows or []:
		for key in ("from_date", "cursor_date", "effective_date"):
			row[key] = _as_date_string(row.get(key))
	return rows or []


def batched(values: Iterable, chunk_size: int = 1000):
	"""Yield bounded slices of values so SQL IN predicates stay capped."""
	items = list(values or [])
	size = max(1, int(chunk_size))
	for offset in range(0, len(items), size):
		yield items[offset : offset + size]


def get_shop_day_rows(company: str, posting_dates: Iterable[str]):
	dates = sorted(set(_as_date_string(value) for value in posting_dates if value))
	if not dates:
		return []
	fields = ", ".join(f"`{field}`" for field in SHOP_DAY_FIELDS)
	result = []
	for chunk in batched(dates, 1000):
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = _require_frappe().db.sql(
			f"""
			SELECT {fields}
			FROM `tabDaily Profit by Shop`
			WHERE `company` = %s AND `posting_date` IN ({placeholders})
			ORDER BY `company` ASC, `posting_date` ASC, `warehouse` ASC
			""",
			[company, *chunk],
			as_dict=True,
		)
		for row in rows or []:
			decoded = _row_dict(row, SHOP_DAY_FIELDS)
			decoded["posting_date"] = _as_date_string(decoded.get("posting_date"))
			result.append(decoded)
	return result


def get_replay_candidates(company: str, posting_dates: Iterable[str]):
	dates = sorted(set(_as_date_string(value) for value in posting_dates if value))
	if not dates:
		return []
	result = []
	for chunk in batched(dates, 1000):
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = _require_frappe().db.sql(
			f"""
			SELECT `company`, `posting_date`, `sales_invoice`, `reason`
			FROM `tabKhitmee Profit Replay Candidate`
			WHERE `company` = %s AND `posting_date` IN ({placeholders})
			ORDER BY `company` ASC, `posting_date` ASC, `sales_invoice` ASC
			""",
			[company, *chunk],
			as_dict=True,
		)
		for row in rows or []:
			row["posting_date"] = _as_date_string(row.get("posting_date"))
			result.append(row)
	return result


def claim_next_dirty_day(company: str | None = None) -> ClaimRecord | None:
	"""Tx 1: expire stale claims, lock one dirty past day, claim it, then commit."""
	frappe_obj = _require_frappe()
	try:
		from frappe.utils import nowdate
	except Exception:  # pragma: no cover - offline fallback
		def nowdate():
			return date.today().isoformat()

	params = []
	company_clause = ""
	company_candidate_clause = ""
	if company:
		company_clause = " AND `company` = %s"
		company_candidate_clause = " AND `profit_day`.`company` = %s"
		params.append(company)

	frappe_obj.db.sql(
		f"""
		UPDATE `tabKhitmee Profit Day State`
		SET `status` = 'dirty', `claim_token` = NULL, `claim_expires_at` = NULL, `modified` = NOW(6)
		WHERE `status` = 'rebuilding' AND `claim_expires_at` < NOW(6){company_clause}
		""",
		params,
	)

	today = nowdate()
	candidates = frappe_obj.db.sql(
		f"""
		SELECT `profit_day`.`company`, `profit_day`.`posting_date`
		FROM `tabKhitmee Profit Day State` AS `profit_day`
		WHERE `profit_day`.`posting_date` < %s
			AND `profit_day`.`status` = 'dirty'{company_candidate_clause}
			AND NOT EXISTS (
				SELECT 1
				FROM `tabRepost Item Valuation` AS `repost`
				WHERE `repost`.`company` = `profit_day`.`company`
					AND `repost`.`docstatus` = 1
					AND `repost`.`status` IN ('Queued', 'In Progress', 'Failed')
					AND `repost`.`posting_date` <= `profit_day`.`posting_date`
			)
		ORDER BY `profit_day`.`company` ASC, `profit_day`.`posting_date` ASC
		LIMIT 1
		FOR UPDATE
		""",
		[today, *params],
		as_dict=True,
	)
	for candidate in candidates or []:
		candidate_company = candidate.get("company") if hasattr(candidate, "get") else candidate[0]
		posting_date = _as_date_string(candidate.get("posting_date") if hasattr(candidate, "get") else candidate[1])
		row = get_day_state(candidate_company, posting_date, for_update=True)
		if not row or row.get("status") != "dirty":
			continue
		blocked, _blocking_date = has_blocking_repost(candidate_company, posting_date)
		if blocked:
			continue

		claim_token = str(uuid.uuid4())
		frappe_obj.db.sql(
			"""
			UPDATE `tabKhitmee Profit Day State`
			SET `status` = 'rebuilding',
				`claim_token` = %s,
				`claim_expires_at` = DATE_ADD(NOW(6), INTERVAL 5 MINUTE),
				`modified` = NOW(6)
			WHERE `company` = %s AND `posting_date` = %s AND `status` = 'dirty'
			""",
			(claim_token, candidate_company, posting_date),
		)
		locked = get_day_state(candidate_company, posting_date)
		_commit()
		return {
			"company": candidate_company,
			"posting_date": posting_date,
			"claim_token": claim_token,
			"claim_expires_at": _as_datetime_string(locked.get("claim_expires_at")) if locked else "",
			"dirty_generation": int(locked.get("dirty_generation") if locked else row.get("dirty_generation") or 0),
		}

	_commit()
	return None


def _insert_shop_rows(shop_rows: list[ShopDayRecord]) -> None:
	fields_sql = ", ".join(f"`{field}`" for field in SHOP_DAY_FIELDS)
	placeholders = ", ".join(["%s"] * len(SHOP_DAY_FIELDS))
	for row in shop_rows:
		_require_frappe().db.sql(
			f"""
			INSERT INTO `tabDaily Profit by Shop` (
				`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`, {fields_sql}
			) VALUES (
				%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, {placeholders}
			)
			""",
			[str(uuid.uuid4()), *[row[field] for field in SHOP_DAY_FIELDS]],
		)


def _insert_replay_rows(replay_rows: list[ReplayCandidateRecord]) -> None:
	for row in replay_rows:
		_require_frappe().db.sql(
			"""
			INSERT INTO `tabKhitmee Profit Replay Candidate` (
				`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
				`company`, `posting_date`, `sales_invoice`, `reason`
			) VALUES (
				%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, %s, %s, %s, %s
			)
			""",
			(str(uuid.uuid4()), row["company"], row["posting_date"], row["sales_invoice"], row["reason"]),
		)


def _is_deadlock_error(exc) -> bool:
	deadlock_type = getattr(_require_frappe(), "QueryDeadlockError", None)
	if deadlock_type and isinstance(exc, deadlock_type):
		return True
	args = getattr(exc, "args", ())
	return bool(args and args[0] == 1213) or "deadlock" in str(exc).lower()


def _publish_day_snapshot_once(claim, target_gen, repost_sig, shop_rows, replay_rows, day_flags) -> PublishResult:
	company = claim["company"]
	posting_date = claim["posting_date"]
	row = get_day_state(company, posting_date, for_update=True)
	if not row or row.get("claim_token") != claim["claim_token"]:
		_rollback()
		return {
			"published": False,
			"status": row.get("status", "dirty") if row else "dirty",
			"superseded": True,
			"blocked_by_repost": False,
			"built_generation": int(row.get("built_generation") or 0) if row else 0,
		}

	blocked, _blocking_date = has_blocking_repost(company, posting_date)
	triggers_installed, _missing = verify_repost_triggers_installed()
	if blocked or not triggers_installed:
		_rollback()
		return {
			"published": False,
			"status": "rebuilding",
			"superseded": False,
			"blocked_by_repost": blocked,
			"built_generation": int(row.get("built_generation") or 0),
		}

	_require_frappe().db.sql(
		"DELETE FROM `tabDaily Profit by Shop` WHERE `company` = %s AND `posting_date` = %s",
		(company, posting_date),
	)
	_require_frappe().db.sql(
		"DELETE FROM `tabKhitmee Profit Replay Candidate` WHERE `company` = %s AND `posting_date` = %s",
		(company, posting_date),
	)
	_insert_shop_rows(shop_rows)
	_insert_replay_rows(replay_rows)

	covered = has_active_dirty_range_covering(company, posting_date)
	final_status = "clean" if int(row.get("dirty_generation") or 0) == int(target_gen) and not covered else "dirty"
	repost_count, repost_watermark, repost_digest = repost_sig
	_require_frappe().db.sql(
		"""
		UPDATE `tabKhitmee Profit Day State`
		SET `built_generation` = %s,
			`repost_completed_count` = %s,
			`repost_watermark` = %s,
			`repost_set_digest` = %s,
			`calc_version` = %s,
			`currency_precision` = %s,
			`float_precision` = %s,
			`rounding_method` = %s,
			`is_additive_safe` = %s,
			`claim_token` = NULL,
			`claim_expires_at` = NULL,
			`status` = %s,
			`last_error` = NULL,
			`rebuilt_at` = NOW(6),
			`modified` = NOW(6)
		WHERE `company` = %s AND `posting_date` = %s AND `claim_token` = %s
		""",
		(
			int(target_gen), int(repost_count), repost_watermark, int(repost_digest),
			int(day_flags["calc_version"]), int(day_flags["currency_precision"]), int(day_flags["float_precision"]),
			day_flags["rounding_method"], 1 if day_flags["is_additive_safe"] else 0,
			final_status, company, posting_date, claim["claim_token"],
		),
	)
	_commit()
	return {
		"published": True,
		"status": final_status,
		"superseded": False,
		"blocked_by_repost": False,
		"built_generation": int(target_gen),
	}


def publish_day_snapshot(
	claim: ClaimRecord,
	target_gen: int,
	repost_sig: tuple[int, str | None, int],
	shop_rows: list[ShopDayRecord],
	replay_rows: list[ReplayCandidateRecord],
	day_flags: DayFlags,
) -> PublishResult:
	"""Tx 3: conditional CAS publish with up to three deadlock retries."""
	last_error = None
	for _attempt in range(3):
		try:
			return _publish_day_snapshot_once(claim, target_gen, repost_sig, shop_rows, replay_rows, day_flags)
		except Exception as exc:  # pragma: no cover - integration-only database race
			last_error = exc
			_rollback()
			if not _is_deadlock_error(exc):
				raise
	raise last_error


def mark_claim_failed(claim: ClaimRecord, error_message: str) -> None:
	_require_frappe().db.sql(
		"""
		UPDATE `tabKhitmee Profit Day State`
		SET `status` = 'failed', `last_error` = %s,
			`claim_token` = NULL, `claim_expires_at` = NULL, `modified` = NOW(6)
		WHERE `company` = %s AND `posting_date` = %s AND `claim_token` = %s
		""",
		(str(error_message)[:4000], claim["company"], claim["posting_date"], claim["claim_token"]),
	)
	_commit()


def _doc_value(doc, fieldname, default=None):
	if hasattr(doc, "get"):
		return doc.get(fieldname, default)
	return getattr(doc, fieldname, default)


def on_sales_invoice_change(doc, method=None) -> None:
	"""Unconditionally invalidate invoice day and a linked original's day in sorted order."""
	company = _doc_value(doc, "company")
	posting_date = _as_date_string(_doc_value(doc, "posting_date"))
	keys = set()
	if company and posting_date:
		keys.add((company, posting_date))
	return_against = _doc_value(doc, "return_against")
	if company and _doc_value(doc, "is_return") and return_against:
		original_date = _as_date_string(_require_frappe().db.get_value("Sales Invoice", return_against, "posting_date"))
		if original_date:
			keys.add((company, original_date))
	if keys:
		bump_dirty_days_ordered(keys)


def on_stock_voucher_change(doc, method=None) -> None:
	"""Record one compact dirty range for stock/valuation-affecting vouchers."""
	doctype = _doc_value(doc, "doctype") or getattr(doc, "doctype", None)
	if doctype == "Purchase Invoice" and not _doc_value(doc, "update_stock"):
		return
	company = _doc_value(doc, "company")
	posting_date = _as_date_string(_doc_value(doc, "posting_date"))
	name = _doc_value(doc, "name")
	if company and posting_date and name:
		record_dirty_range(company, posting_date, doctype, name)


def on_repost_trash(doc, method=None) -> None:
	"""Defense-in-depth ORM purge hook; database triggers remain the durable mechanism."""
	triggers_installed, _missing = verify_repost_triggers_installed()
	if triggers_installed:
		return
	if int(_doc_value(doc, "docstatus", 0) or 0) == 1 and _doc_value(doc, "status") == "Completed":
		company = _doc_value(doc, "company")
		posting_date = _as_date_string(_doc_value(doc, "posting_date"))
		name = _doc_value(doc, "name")
		if company and posting_date and name:
			record_dirty_range(company, posting_date, "Repost Item Valuation", name)
