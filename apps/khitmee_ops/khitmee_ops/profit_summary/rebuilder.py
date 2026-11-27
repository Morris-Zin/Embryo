"""Dirty-range expansion and 3-transaction daily snapshot rebuilder."""

from __future__ import annotations

import gc
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

try:  # pragma: no cover - offline tests install a stub before importing
	import frappe
except ImportError:  # pragma: no cover
	frappe = None

from khitmee_ops.profit_summary import store
from khitmee_ops.profit_summary.budget import MAX_SAFE_ABS_SUM
from khitmee_ops.profit_summary.store import batched

POSITIVE_VALUATION_SOURCES = frozenset({"sle", "bundle_sle"})
SET_B_REASONS = frozenset(
	{
		"linked_return_original",
		"linked_return",
		"non_update_stock",
		"dropship",
		"missing_sle_zero",
		"request_dependent_valuation",
	}
)
SINGLE_FLIGHT_LOCK_NAME = "khitmee_profit_summary_rebuilder"


def _decimal(value) -> Decimal:
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value or 0))


def _as_date_string(value):
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	return str(value or "")[:10]


def _add_days(value, days):
	if hasattr(value, "isoformat") and not isinstance(value, str):
		return (value + timedelta(days=days)).isoformat()[:10]
	parsed = datetime.fromisoformat(str(value)[:10]).date()
	return (parsed + timedelta(days=days)).isoformat()


def _today():
	try:
		from frappe.utils import nowdate

		return nowdate()
	except Exception:
		return date.today().isoformat()


def make_first_seen_key(query_phase: int, posting_date: str, posting_time="") -> str:
	if hasattr(posting_time, "total_seconds"):
		total_us = max(0, round(posting_time.total_seconds() * 1_000_000))
		hours, remainder = divmod(total_us, 3_600_000_000)
		minutes, remainder = divmod(remainder, 60_000_000)
		seconds, microseconds = divmod(remainder, 1_000_000)
		time_text = f"{hours:02d}{minutes:02d}{seconds:02d}{microseconds:06d}"
	else:
		digits = re.sub(r"\D", "", str(posting_time or ""))
		time_text = digits[:12].ljust(12, "0")
	return f"{int(query_phase)}|{_as_date_string(posting_date)}|{time_text}"


def canonical_stored_first_seen_key(row) -> str:
	key = str(row.get("first_seen_key") or "")
	if re.match(r"^[01]\|\d{4}-\d{2}-\d{2}\|\d{12}", key):
		return "1" + key[1:]
	return make_first_seen_key(1, row.get("posting_date"), row.get("posting_time"))


def classify_invoice_rows(invoice_rows, replay_reason: str | None = None) -> tuple[bool, str | None]:
	"""Apply the five positive-SLE Set A certification rules to one invoice."""
	rows = list(invoice_rows or [])
	if not rows:
		return False, "request_dependent_valuation"
	if replay_reason in SET_B_REASONS:
		return False, replay_reason

	first = rows[0]
	if int(first.get("is_return") or 0) == 1 and first.get("return_against"):
		return False, "linked_return"
	if any(int(row.get("update_stock") or 0) != 1 for row in rows):
		return False, "non_update_stock"
	if any(int(row.get("delivered_by_supplier") or 0) != 0 for row in rows):
		return False, "dropship"
	if any(row.get("valuation_source") == "missing_detail_zero" for row in rows):
		return False, "missing_sle_zero"
	if any(row.get("valuation_source") not in POSITIVE_VALUATION_SOURCES for row in rows):
		return False, "request_dependent_valuation"
	if any(not row.get("is_precision_safe") for row in rows):
		return False, "request_dependent_valuation"
	return True, None


def _new_accumulator():
	return {
		"has_rows": 0,
		"qty": Decimal("0"),
		"base_amount": Decimal("0"),
		"buying_amount": Decimal("0"),
		"abs_qty_sum": Decimal("0"),
		"abs_base_sum": Decimal("0"),
		"abs_buying_sum": Decimal("0"),
		"first_seen_key": None,
	}


def _accumulate(bucket, row) -> None:
	qty = _decimal(row.get("qty"))
	base_amount = _decimal(row.get("base_amount"))
	buying_amount = _decimal(row.get("buying_amount"))
	bucket["has_rows"] = 1
	bucket["qty"] += qty
	bucket["base_amount"] += base_amount
	bucket["buying_amount"] += buying_amount
	bucket["abs_qty_sum"] += abs(qty)
	bucket["abs_base_sum"] += abs(base_amount)
	bucket["abs_buying_sum"] += abs(buying_amount)
	key = canonical_stored_first_seen_key(row)
	if bucket["first_seen_key"] is None or key < bucket["first_seen_key"]:
		bucket["first_seen_key"] = key


def _format_decimal(value: Decimal) -> str:
	return format(value.normalize(), "f") if value == value.normalize() else format(value, "f")


def partition_day_contributions(batch, company: str, posting_date: str):
	"""Partition one day's rows into additive Set A shop rows and Set B replay rows."""
	contributions = list((batch or {}).get("contributions") or [])
	replay_reasons = dict((batch or {}).get("replay_reasons_by_invoice") or {})
	posting_date = _as_date_string(posting_date)
	rows_by_invoice = {}
	for row in contributions:
		if _as_date_string(row.get("posting_date")) != posting_date:
			continue
		rows_by_invoice.setdefault(row.get("sales_invoice"), []).append(row)

	buckets_by_warehouse = {}
	replay_rows = []
	for sales_invoice in sorted(rows_by_invoice):
		rows = rows_by_invoice[sales_invoice]
		is_set_a, reason = classify_invoice_rows(rows, replay_reasons.get(sales_invoice))
		if not is_set_a:
			replay_rows.append(
				{
					"company": company,
					"posting_date": posting_date,
					"sales_invoice": sales_invoice,
					"reason": reason or "request_dependent_valuation",
				}
			)
			continue
		for row in rows:
			warehouse = str(row.get("warehouse") or "")
			buckets = buckets_by_warehouse.setdefault(warehouse, {"normal": _new_accumulator(), "unlinked": _new_accumulator()})
			bucket = buckets["unlinked"] if int(row.get("is_return") or 0) == 1 and not row.get("return_against") else buckets["normal"]
			_accumulate(bucket, row)

	shop_rows = []
	currency_precision = int((batch or {}).get("currency_precision") or 3)
	float_precision = int((batch or {}).get("float_precision") or 2)
	qty_cap = Decimal(str(MAX_SAFE_ABS_SUM(float_precision)))
	money_cap = Decimal(str(MAX_SAFE_ABS_SUM(currency_precision)))
	is_additive_safe = True
	for warehouse in sorted(buckets_by_warehouse):
		normal = buckets_by_warehouse[warehouse]["normal"]
		unlinked = buckets_by_warehouse[warehouse]["unlinked"]
		for bucket in (normal, unlinked):
			if bucket["abs_qty_sum"] > qty_cap or max(bucket["abs_base_sum"], bucket["abs_buying_sum"]) > money_cap:
				is_additive_safe = False
		shop_rows.append(
			{
				"company": company,
				"posting_date": posting_date,
				"warehouse": warehouse,
				"normal_has_rows": normal["has_rows"],
				"normal_qty": _format_decimal(normal["qty"]),
				"normal_base_amount": _format_decimal(normal["base_amount"]),
				"normal_buying_amount": _format_decimal(normal["buying_amount"]),
				"normal_abs_qty_sum": _format_decimal(normal["abs_qty_sum"]),
				"normal_abs_base_sum": _format_decimal(normal["abs_base_sum"]),
				"normal_abs_buying_sum": _format_decimal(normal["abs_buying_sum"]),
				"normal_first_seen_key": normal["first_seen_key"],
				"unlinked_return_has_rows": unlinked["has_rows"],
				"unlinked_return_qty": _format_decimal(unlinked["qty"]),
				"unlinked_return_base_amount": _format_decimal(unlinked["base_amount"]),
				"unlinked_return_buying_amount": _format_decimal(unlinked["buying_amount"]),
				"unlinked_return_abs_qty_sum": _format_decimal(unlinked["abs_qty_sum"]),
				"unlinked_return_abs_base_sum": _format_decimal(unlinked["abs_base_sum"]),
				"unlinked_return_abs_buying_sum": _format_decimal(unlinked["abs_buying_sum"]),
				"unlinked_return_first_seen_key": unlinked["first_seen_key"],
			}
		)

	flags = store.current_day_flags(is_additive_safe)
	flags["currency_precision"] = int((batch or {}).get("currency_precision") or flags["currency_precision"])
	flags["float_precision"] = int((batch or {}).get("float_precision") or flags["float_precision"])
	flags["rounding_method"] = (batch or {}).get("rounding_method") or flags["rounding_method"]
	return shop_rows, replay_rows, flags


def find_globally_linked_originals(company: str, invoice_ids) -> set[str]:
	invoice_ids = sorted(set(invoice_ids or []))
	if not invoice_ids or frappe is None:
		return set()
	found: set[str] = set()
	for chunk in batched(invoice_ids, 1000):
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = frappe.db.sql(
			f"""
			SELECT DISTINCT `return_against`
			FROM `tabSales Invoice`
			WHERE `company` = %s
				AND `docstatus` = 1
				AND `is_return` = 1
				AND `return_against` IN ({placeholders})
			""",
			[company, *chunk],
		)
		found.update(row.get("return_against") if hasattr(row, "get") else row[0] for row in rows or [])
	return found


def find_same_day_linked_returns(company: str, posting_date: str, original_ids) -> list[str]:
	"""Linked returns posted on ``posting_date`` against originals in the day batch.

	Stock Gross Profit folds such a return into its original when both are in the
	result, so the day build never sees it as its own row.  Under a warehouse filter
	that excludes the original, stock lists the return separately; replaying only the
	original would drop it, so the return must be a replay candidate too.
	"""
	original_ids = sorted(set(original_ids or []))
	if not original_ids or frappe is None:
		return []
	found: set[str] = set()
	for chunk in batched(original_ids, 1000):
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = frappe.db.sql(
			f"""
			SELECT `name`
			FROM `tabSales Invoice`
			WHERE `company` = %s
				AND `docstatus` = 1
				AND `is_return` = 1
				AND `posting_date` = %s
				AND `return_against` IN ({placeholders})
			""",
			[company, posting_date, *chunk],
		)
		found.update(row.get("name") if hasattr(row, "get") else row[0] for row in rows or [])
	return sorted(found)


def add_same_day_linked_return_candidates(replay_rows, company: str, posting_date: str):
	"""Append same-day linked returns of replayed originals as Set B candidates."""
	replay_rows = list(replay_rows or [])
	present = {row.get("sales_invoice") for row in replay_rows}
	originals = [row.get("sales_invoice") for row in replay_rows if row.get("reason") == "linked_return_original"]
	for return_id in find_same_day_linked_returns(company, posting_date, originals):
		if return_id in present:
			continue
		replay_rows.append(
			{
				"company": company,
				"posting_date": _as_date_string(posting_date),
				"sales_invoice": return_id,
				"reason": "linked_return",
			}
		)
	return replay_rows


def augment_linked_original_reasons(batch, company: str):
	contributions = list((batch or {}).get("contributions") or [])
	invoice_ids = {row.get("sales_invoice") for row in contributions if row.get("sales_invoice")}
	linked_originals = find_globally_linked_originals(company, invoice_ids)
	if not linked_originals:
		return batch
	batch = dict(batch or {})
	reasons = dict(batch.get("replay_reasons_by_invoice") or {})
	for invoice in linked_originals:
		reasons.setdefault(invoice, "linked_return_original")
	batch["replay_reasons_by_invoice"] = reasons
	return batch


def _fast_gross_profit_module():
	from khitmee_ops.khitmee_ops.report.fast_gross_profit import fast_gross_profit

	return fast_gross_profit


def build_day_filters(company: str, posting_date: str) -> dict:
	return {
		"company": company,
		"from_date": posting_date,
		"to_date": posting_date,
		"group_by": "Warehouse",
		"include_returned_invoices": 1,
	}


def _unpublished_result(status="rebuilding", blocked=False):
	return {
		"published": False,
		"status": status,
		"superseded": False,
		"blocked_by_repost": blocked,
		"built_generation": 0,
	}


def rebuild_claimed_day(claim: store.ClaimRecord):
	"""Tx 2 fresh snapshot followed by Tx 3 conditional CAS publish."""
	if frappe is None:
		raise RuntimeError("profit rebuilder requires Frappe")
	triggers_installed, _missing = store.verify_repost_triggers_installed()
	if not triggers_installed:
		return _unpublished_result()

	company = claim["company"]
	posting_date = claim["posting_date"]
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
	frappe.db.sql("START TRANSACTION")
	try:
		day_state = store.get_day_state(company, posting_date)
		if not day_state:
			return _unpublished_result()
		target_gen = int(day_state.get("dirty_generation") or 0)
		repost_sig = store.get_completed_repost_signature(company, posting_date)
		blocked, _blocking_date = store.has_blocking_repost(company, posting_date)
		if blocked:
			return _unpublished_result(blocked=True)
		batch = _fast_gross_profit_module().collect_profit_contributions(build_day_filters(company, posting_date))
		batch = augment_linked_original_reasons(batch, company)
		shop_rows, replay_rows, day_flags = partition_day_contributions(batch, company, posting_date)
		replay_rows = add_same_day_linked_return_candidates(replay_rows, company, posting_date)
	finally:
		rollback = getattr(frappe.db, "rollback", None)
		if callable(rollback):
			rollback()

	return store.publish_day_snapshot(claim, target_gen, repost_sig, shop_rows, replay_rows, day_flags)


def expand_dirty_ranges(company: str | None = None, batch_days: int = 30, deadline: float | None = None) -> int:
	"""Expand active dirty ranges in ascending cursor batches of at most 30 days."""
	if frappe is None:
		raise RuntimeError("dirty range expansion requires Frappe")
	expanded = 0
	today = _today()
	yesterday = _add_days(today, -1)
	for dirty_range in store.get_unprocessed_dirty_ranges(company=company):
		if deadline is not None and time.monotonic() >= deadline:
			break
		if company and dirty_range.get("company") != company:
			continue
		start = _add_days(dirty_range["cursor_date"], 1) if dirty_range.get("cursor_date") else dirty_range["from_date"]
		if start > yesterday:
			# Future-only ranges must remain open until time catches up; a range whose
			# cursor already reached site-yesterday is complete, however.
			if dirty_range.get("cursor_date") and dirty_range["cursor_date"] >= yesterday:
				store.advance_dirty_range_cursor(dirty_range["name"], dirty_range["cursor_date"], True)
				commit = getattr(frappe.db, "commit", None)
				if callable(commit):
					commit()
			continue
		end = min(_add_days(start, int(batch_days) - 1), yesterday)
		keys = []
		current = start
		while current <= end:
			keys.append((dirty_range["company"], current))
			current = _add_days(current, 1)
		store.bump_dirty_days_ordered(keys)
		store.advance_dirty_range_cursor(dirty_range["name"], end, end >= yesterday)
		commit = getattr(frappe.db, "commit", None)
		if callable(commit):
			commit()
		expanded += len(keys)
		gc.collect()
	return expanded


def _get_lock() -> bool:
	rows = frappe.db.sql("SELECT GET_LOCK(%s, 0)", (SINGLE_FLIGHT_LOCK_NAME,))
	row = rows[0] if rows else None
	value = row[0] if row and not hasattr(row, "get") else next(iter(row.values()), 0) if row else 0
	return bool(value)


def _release_lock() -> None:
	frappe.db.sql("SELECT RELEASE_LOCK(%s)", (SINGLE_FLIGHT_LOCK_NAME,))


def _trim_heap() -> None:
	try:
		from khitmee_ops.api.memory import _release_message_and_trim_heap

		_release_message_and_trim_heap()
	except Exception:
		pass


SCHEDULED_MAX_SLICES = 50
SCHEDULED_TIME_BUDGET_SECONDS = 120


def process_dirty_slices(
	company: str | None = None, max_slices: int = 1, time_budget_seconds: float | None = None
) -> int:
	"""Single-flight dirty range expansion plus bounded claim/rebuild work."""
	if frappe is None:
		raise RuntimeError("profit dirty slice processor requires Frappe")
	if not _get_lock():
		return 0
	processed = 0
	deadline = time.monotonic() + float(time_budget_seconds) if time_budget_seconds else None
	try:
		expand_dirty_ranges(company=company, deadline=deadline)
		while processed < int(max_slices or 1):
			if deadline is not None and time.monotonic() >= deadline:
				break
			claim = store.claim_next_dirty_day(company=company)
			if not claim:
				break
			try:
				rebuild_claimed_day(claim)
			except Exception as exc:  # pragma: no cover - integration failure path
				store.mark_claim_failed(claim, str(exc))
			processed += 1
			gc.collect()
			_trim_heap()
		return processed
	finally:
		_release_lock()


def process_dirty_slices_scheduled() -> int:
	"""Scheduler entry: drain up to 50 days per tick within a 2 minute budget.

	One day rebuild costs ~1 s and ~110 MiB, so a backlog (initial history, a
	precision change) converges in hours instead of one day per 10 minute tick.
	"""
	return process_dirty_slices(
		max_slices=SCHEDULED_MAX_SLICES, time_budget_seconds=SCHEDULED_TIME_BUDGET_SECONDS
	)


def _company_names():
	rows = frappe.db.sql("SELECT `name` FROM `tabCompany` ORDER BY `name` ASC")
	return [row.get("name") if hasattr(row, "get") else row[0] for row in rows or []]


def _fiscal_year_start(company: str, posting_date: str) -> str:
	try:
		from erpnext.accounts.utils import get_fiscal_year

		return _as_date_string(get_fiscal_year(posting_date, company=company)[1])
	except Exception:
		return f"{posting_date[:4]}-01-01"


def register_missing_history(company: str | None = None) -> dict:
	"""Idempotently register rebuild work for history the summary has never built.

	Creates dirty day states for every past day from the first submitted invoice
	that has none (initial deployment, restored backups), and re-dirties clean
	days built under different calc/precision/rounding flags, which queries no
	longer trust and the claimer would otherwise never pick up.
	"""
	if frappe is None:
		raise RuntimeError("profit history registration requires Frappe")
	yesterday = _add_days(_today(), -1)
	expected_flags = store.current_day_flags()
	registered = 0
	restaled = 0
	for name in [company] if company else _company_names():
		start = store.get_history_start(name, yesterday)
		if start:
			# Reports default to the fiscal year, so also cover the empty days
			# between the fiscal-year start and the first sale.
			start = min(start, _fiscal_year_start(name, start))
			states = store.get_day_states(name, start, yesterday)
			current = start
			while current <= yesterday:
				if current not in states:
					store.ensure_day_state(name, current)
					registered += 1
				current = _add_days(current, 1)
		stale_days = store.get_stale_flag_days(name, expected_flags)
		if stale_days:
			store.bump_dirty_days_ordered((name, day) for day in stale_days)
			restaled += len(stale_days)
		commit = getattr(frappe.db, "commit", None)
		if callable(commit):
			commit()
	return {"registered_days": registered, "restaled_days": restaled}


def enqueue_yesterday_rollover() -> int:
	"""Register every company/site-yesterday pair at site-timezone midnight."""
	if frappe is None:
		raise RuntimeError("profit rollover requires Frappe")
	yesterday = _add_days(_today(), -1)
	rows = frappe.db.sql("SELECT `name` FROM `tabCompany` ORDER BY `name` ASC")
	count = 0
	for row in rows or []:
		company = row.get("name") if hasattr(row, "get") else row[0]
		store.ensure_day_state(company, yesterday)
		count += 1
	commit = getattr(frappe.db, "commit", None)
	if callable(commit):
		commit()
	# Self-heal any history gaps or stale-flag days (idempotent, a few queries).
	register_missing_history()
	return count
