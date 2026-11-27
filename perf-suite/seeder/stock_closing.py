#!/usr/bin/env python3
"""Synchronous, locked, streaming Stock Closing Entry generation for benchmark seeding.

The module is importable without Frappe so pure-Python tests can exercise its planning and
classification helpers. Frappe/ERPNext imports are deliberately lazy; live generation still
requires an initialized bench site.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Sequence

try:
    import frappe
except ImportError:  # pure-Python tests do not require Frappe
    frappe = None


STOCK_CLOSING_SCOPE_FIELDS = ("warehouse", "item_code", "item_group", "warehouse_type")
STOCK_CLOSING_ACTIVE_STATUSES = frozenset({"Queued", "In Progress"})
STOCK_CLOSING_FAILURE_STATUSES = frozenset({"Failed", "Outdated", "Cancelled"})
BLOCKING_REPOST_STATUSES = ("Queued", "In Progress", "Failed")
DEFAULT_LOCK_TIMEOUT_SECONDS = 10
DEFAULT_ABANDONED_AFTER_SECONDS = 1800
DEFAULT_CHUNK_SIZE = 250

BASE_SCB_COLUMNS = [
    "name", "creation", "modified", "modified_by", "owner", "docstatus", "idx",
    "item_code", "warehouse", "batch_no", "posting_date", "posting_time", "posting_datetime",
    "actual_qty", "valuation_rate", "stock_value", "stock_value_difference", "company",
    "stock_closing_entry", "item_name", "item_group", "stock_uom", "inventory_dimension_key",
    "fifo_queue", "_user_tags", "_comments", "_assign", "_liked_by",
]


class StockClosingError(RuntimeError):
    pass


class StockClosingLockError(StockClosingError):
    pass


class StockClosingConflictError(StockClosingError):
    pass


class StockClosingStateError(StockClosingError):
    pass


class StockClosingSafetyError(StockClosingError):
    pass


def require_frappe() -> None:
    if frappe is None:
        raise RuntimeError("frappe is not importable; run inside the bench Python environment")


def coerce_stock_closing_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def stock_closing_lock_name(company: str, to_date: date | datetime | str) -> str:
    """MySQL GET_LOCK names are limited to 64 characters; hash the scoped closing key."""
    cutoff = coerce_stock_closing_date(to_date).isoformat()
    digest = hashlib.sha256(f"{company}|{cutoff}".encode("utf-8")).hexdigest()[:40]
    return f"khitmee_sc_{digest}"


def dimension_fieldnames(dimensions: Iterable[Any]) -> list[str]:
    fields: list[str] = []
    for dimension in dimensions or []:
        if isinstance(dimension, dict):
            fieldname = dimension.get("fieldname") or dimension.get("target_fieldname")
        else:
            fieldname = getattr(dimension, "fieldname", None) or getattr(dimension, "target_fieldname", None)
        if fieldname and fieldname not in fields:
            fields.append(str(fieldname))
    return fields


def stock_closing_balance_columns(
    dimensions: Iterable[Any], column_exists: Callable[[str], bool] | None = None
) -> list[str]:
    """Core Stock Closing Balance columns plus dynamically enabled inventory dimension columns."""
    columns = list(BASE_SCB_COLUMNS)
    for fieldname in dimension_fieldnames(dimensions):
        if fieldname in columns:
            continue
        if column_exists is not None and not column_exists(fieldname):
            continue
        columns.append(fieldname)
    return columns


def row_is_unscoped(row: dict[str, Any]) -> bool:
    return all(not row.get(field) for field in STOCK_CLOSING_SCOPE_FIELDS if field in row)


def row_age_seconds(row: dict[str, Any], now: datetime | None = None) -> float:
    modified = row.get("modified")
    if isinstance(modified, str):
        try:
            modified = datetime.fromisoformat(modified.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            modified = None
    if not isinstance(modified, datetime):
        return float("inf")
    if modified.tzinfo is not None:
        modified = modified.astimezone(timezone.utc).replace(tzinfo=None)
    if now is None:
        now = datetime.utcnow()
    elif now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0.0, (now - modified).total_seconds())


def inspect_stock_closing_rows(
    rows: Sequence[dict[str, Any]],
    cutoff: date | datetime | str,
    abandoned_after_seconds: int = DEFAULT_ABANDONED_AFTER_SECONDS,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any] | None, str]:
    """Classify exact/later closing rows without touching the database.

    Returns (action, row, reason). Actions are create, reuse, recover, active, or error.
    This pure function is the recovery policy used after the MySQL lock is acquired.
    """
    cutoff_date = coerce_stock_closing_date(cutoff)
    exact_unscoped: list[dict[str, Any]] = []
    exact_scoped: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    for row in rows:
        row_cutoff = coerce_stock_closing_date(row["to_date"])
        if row_cutoff == cutoff_date:
            (exact_unscoped if row_is_unscoped(row) else exact_scoped).append(row)
        elif row_cutoff > cutoff_date:
            later.append(row)

    if len(exact_unscoped) > 1:
        names = ", ".join(str(row.get("name")) for row in exact_unscoped)
        return "error", None, f"multiple unscoped Stock Closing Entries exist for {cutoff_date}: {names}"

    if exact_unscoped:
        row = exact_unscoped[0]
        status = str(row.get("status") or "Draft")
        if status == "Completed":
            return "reuse", row, "completed closing exists"
        if status == "Failed":
            return "recover", row, "failed closing has no live owner"
        if status == "Draft" and int(row.get("docstatus") or 0) == 1:
            return "recover", row, "submitted closing never started generation"
        if status in STOCK_CLOSING_ACTIVE_STATUSES:
            age = row_age_seconds(row, now)
            if age >= abandoned_after_seconds:
                return "recover", row, f"{status} closing is stale for {int(age)}s"
            return "active", row, f"{status} closing was modified {int(age)}s ago and may have a live owner"
        detail = str(row.get("error_message") or "").strip()
        suffix = f": {detail}" if detail else ""
        return "error", row, f"Stock Closing Entry {row.get('name')} for {cutoff_date} is {status}{suffix}"

    if exact_scoped:
        names = ", ".join(str(row.get("name")) for row in exact_scoped)
        return "error", None, f"scoped Stock Closing Entries already exist for {cutoff_date}: {names}"

    if later:
        descriptions = ", ".join(f"{row.get('name')} ({coerce_stock_closing_date(row['to_date'])})" for row in later)
        return "error", None, f"later closing entries already exist: {descriptions}"

    return "create", None, "no conflicting closing exists"


def get_stock_closing_module():
    import erpnext.stock.doctype.stock_closing_entry.stock_closing_entry as sce_mod

    return sce_mod


@contextmanager
def mysql_stock_closing_lock(company: str, cutoff: date, timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS):
    """Hold an exclusive MySQL named lock for one (company, to_date) closing scope."""
    require_frappe()
    lock_name = stock_closing_lock_name(company, cutoff)
    rows = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, int(timeout_seconds)))
    acquired = rows and rows[0][0] == 1
    if not acquired:
        owner_rows = frappe.db.sql("SELECT IS_USED_LOCK(%s)", (lock_name,))
        owner = owner_rows[0][0] if owner_rows else None
        owner_text = f" by connection {owner}" if owner else ""
        raise StockClosingLockError(
            f"Could not acquire Stock Closing lock for {company} through {cutoff}; a live owner{owner_text} holds it"
        )
    try:
        yield lock_name
    finally:
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
        except Exception:
            # Connection teardown releases MySQL named locks automatically.
            pass


def stock_closing_status_fields() -> list[str]:
    require_frappe()
    fields = ["name", "from_date", "to_date", "status", "modified", "docstatus"]
    meta = frappe.get_meta("Stock Closing Entry")
    if meta.has_field("error_message"):
        fields.append("error_message")
    for field in STOCK_CLOSING_SCOPE_FIELDS:
        if meta.has_field(field):
            fields.append(field)
    return fields


def get_stock_closing_rows(company: str) -> list[dict[str, Any]]:
    rows = frappe.db.get_values(
        "Stock Closing Entry",
        {"company": company, "docstatus": 1},
        stock_closing_status_fields(),
        as_dict=True,
        order_by="to_date asc",
    )
    return [dict(row) for row in rows or []]


def blocking_repost_rows(company: str, cutoff: date) -> list[dict[str, Any]]:
    rows = frappe.db.sql(
        """
        SELECT name, status
        FROM `tabRepost Item Valuation`
        WHERE docstatus = 1
          AND company = %s
          AND posting_date <= %s
          AND status IN ({})
        ORDER BY modified DESC
        LIMIT 10
        """.format(", ".join(["%s"] * len(BLOCKING_REPOST_STATUSES))),
        (company, cutoff, *BLOCKING_REPOST_STATUSES),
        as_dict=True,
    )
    return [dict(row) for row in rows or []]


def assert_no_blocking_reposts(company: str, cutoff: date) -> None:
    rows = blocking_repost_rows(company, cutoff)
    if rows:
        details = ", ".join(f"{row['name']} ({row['status']})" for row in rows)
        raise StockClosingSafetyError(
            f"Cannot generate Stock Closing through {cutoff}; Repost Item Valuation rows are active, queued, or failed: {details}"
        )


def assert_no_overlapping_closings(company: str, from_date: date, to_date: date, exclude_name: str) -> None:
    rows = frappe.db.sql(
        """
        SELECT name, from_date, to_date, status
        FROM `tabStock Closing Entry`
        WHERE company = %s
          AND docstatus = 1
          AND name <> %s
          AND NOT (to_date < %s OR from_date > %s)
        ORDER BY to_date
        LIMIT 10
        """,
        (company, exclude_name, from_date, to_date),
        as_dict=True,
    )
    if rows:
        details = ", ".join(f"{row.name} ({row.from_date}..{row.to_date}, {row.status})" for row in rows)
        raise StockClosingConflictError(f"Overlapping Stock Closing Entries exist for {company}: {details}")


def scb_column_exists(fieldname: str) -> bool:
    rows = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabStock Closing Balance' AND COLUMN_NAME = %s
        """,
        (fieldname,),
    )
    return bool(rows and rows[0][0])


def build_streaming_stock_closing_class():
    sce_mod = get_stock_closing_module()

    class StreamingStockClosing(sce_mod.StockClosing):
        """StockClosing aggregation with O(1)-memory SLE input and deterministic key order."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.item_details_map = {
                row.name: row
                for row in frappe.get_all(
                    "Item", fields=["name", "item_group", "item_name", "stock_uom", "has_serial_no"]
                )
            }

        def build_entries_query(self, doctype, fields, filters, **kwargs):
            fields = list(fields)
            for dimension in self.inv_dimensions:
                if dimension.fieldname not in fields:
                    fields.append(dimension.fieldname)
            table = frappe.qb.DocType(doctype)
            query = (
                frappe.qb.from_(table)
                .select(*fields)
                .orderby(table.item_code, table.warehouse, table.posting_datetime, table.creation)
            )
            if filters:
                for field, value in filters.items():
                    if field == "posting_date":
                        query = query.where(table[field].between(value[0], value[1]))
                    elif isinstance(value, (list, tuple)):
                        query = query.where(table[field].isin(value))
                    else:
                        query = query.where(table[field] == value)
            for key, value in kwargs.items():
                if value:
                    if isinstance(value, (list, tuple)):
                        query = query.where(table[key].isin(value))
                    else:
                        query = query.where(table[key] == value)
            if doctype == "Stock Ledger Entry":
                sabb_table = frappe.qb.DocType("Serial and Batch Entry")
                query = query.left_join(sabb_table).on(
                    (sabb_table.parent == table.serial_and_batch_bundle) & (table.has_batch_no == 1)
                )
                query = query.select(sabb_table.batch_no.as_("sabb_batch_no"))
                query = query.select(sabb_table.qty.as_("sabb_qty"))
                query = query.select(sabb_table.stock_value_difference.as_("sabb_stock_value_difference"))
            return query

        def get_entries(self, doctype, fields, filters, **kwargs):
            query = self.build_entries_query(doctype, fields, filters, **kwargs)
            if doctype == "Stock Ledger Entry":
                return query.run(as_dict=True, as_iterator=True)
            return query.run(as_dict=True)

        def get_sle_entries(self):
            add_days, flt_utils = _frappe_date_utils()
            closing_balances = []
            if self.last_closing_balance:
                self.from_date = add_days(self.last_closing_balance.to_date, 1)
                closing_balances = self.get_entries(
                    "Stock Closing Balance",
                    fields=[
                        "name", "creation", "item_code", "warehouse", "posting_date", "posting_time", "posting_datetime",
                        "batch_no", "inventory_dimension_key", "actual_qty", "valuation_rate", "stock_value",
                        "stock_value_difference", "fifo_queue",
                    ],
                    filters={"company": self.company, "stock_closing_entry": self.last_closing_balance.name},
                )
                for row in closing_balances:
                    row.from_closing_balance = True
            if not self.last_closing_balance:
                self.from_date = "1900-01-01"

            def generate():
                for row in closing_balances:
                    yield row
                with frappe.db.unbuffered_cursor():
                    for row in self.get_entries(
                        "Stock Ledger Entry",
                        fields=[
                            "name", "creation", "item_code", "warehouse", "posting_date", "posting_time", "posting_datetime",
                            "batch_no", "actual_qty", "valuation_rate", "stock_value", "stock_value_difference",
                            "qty_after_transaction", "stock_uom",
                        ],
                        filters={
                            "company": self.company,
                            "posting_date": [self.from_date, self.to_date],
                            "is_cancelled": 0,
                        },
                    ):
                        yield row

            return generate()

        def get_initialized_entry(self, row, dimension_fields, value_difference):
            _add_days, utils = _frappe_date_utils()
            flt = utils["flt"]
            item_details = self.item_details_map[row.item_code]
            inventory_dimension_key = None
            if dimension_fields not in [("item_code", "warehouse"), ("item_code", "warehouse", "batch_no")]:
                inventory_dimension_key = json.dumps(dimension_fields)
            actual_qty = flt(row.sabb_qty if row.sabb_qty is not None else row.actual_qty or row.qty_after_transaction)
            entry = frappe._dict(
                {
                    "item_code": row.item_code,
                    "warehouse": row.warehouse,
                    "actual_qty": actual_qty,
                    "stock_value_difference": value_difference,
                    "item_group": item_details.item_group,
                    "item_name": item_details.item_name,
                    "stock_uom": item_details.stock_uom,
                    "inventory_dimension_key": inventory_dimension_key,
                    "fifo_queue": self.get_initial_fifo_queue(row, actual_qty, item_details.has_serial_no),
                }
            )
            if row.sabb_batch_no:
                row.batch_no = row.sabb_batch_no
            for field in dimension_fields:
                if row.get(field):
                    entry[field] = row.get(field)
            return entry

    return StreamingStockClosing


def _frappe_date_utils():
    from frappe.utils import add_days, flt

    return add_days, {"flt": flt}


def prepare_insertable_stock_closing_rows(entries: dict[Any, Any]) -> tuple[list[Any], int]:
    """Apply upstream valuation shaping once and return insertable rows in deterministic stream order."""
    sce_mod = get_stock_closing_module()
    insertable: list[Any] = []
    skipped = 0
    for key in entries:
        row = entries[key]
        if row.actual_qty == 0.0 and row.stock_value_difference == 0.0:
            skipped += 1
            continue
        if row.fifo_queue is not None:
            row.fifo_queue = json.dumps(row.fifo_queue)
        sce_mod.set_stock_value_and_valuation_rate(row)
        insertable.append(row)
    return insertable, skipped


def bulk_insert_stock_closing_rows(
    doc: Any,
    insertable_rows: Sequence[Any],
    dimensions: Iterable[Any],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    start_index: int = 1,
    log: Callable[..., None] = print,
) -> int:
    """Insert rows from ``start_index``; each row's ``idx`` is its deterministic stream position."""
    from erpnext.stock.utils import get_combine_datetime
    from frappe.model.naming import make_autoname
    from frappe.utils import now_datetime, nowtime

    columns = stock_closing_balance_columns(dimensions, scb_column_exists)
    created = 0
    started = time.monotonic()
    now = now_datetime()
    posting_time = nowtime()
    posting_datetime = get_combine_datetime(doc.to_date, posting_time)
    pending: list[list[Any]] = []

    def flush() -> None:
        nonlocal pending, created
        if not pending:
            return
        columns_sql = ", ".join(f"`{column}`" for column in columns)
        row_sql = "(" + ", ".join(["%s"] * len(columns)) + ")"
        sql = f"INSERT INTO `tabStock Closing Balance` ({columns_sql}) VALUES " + ", ".join([row_sql] * len(pending))
        params = [value for row in pending for value in row]
        frappe.db.sql(sql, params)
        created += len(pending)
        pending = []
        frappe.db.commit()
        total_written = start_index - 1 + created
        if total_written % 5000 < chunk_size:
            log("stock_closing_bulk_insert_progress", total_written, "elapsed", round(time.monotonic() - started, 2), flush=True)

    for position, row in enumerate(insertable_rows, 1):
        if position < start_index:
            continue
        values = {
            "name": make_autoname("hash", "Stock Closing Balance"),
            "creation": now,
            "modified": now,
            "modified_by": "Administrator",
            "owner": "Administrator",
            "docstatus": 0,
            "idx": position,
            "item_code": row.item_code,
            "warehouse": row.warehouse,
            "batch_no": row.get("batch_no"),
            "posting_date": doc.to_date,
            "posting_time": posting_time,
            "posting_datetime": posting_datetime,
            "actual_qty": row.actual_qty,
            "valuation_rate": row.valuation_rate,
            "stock_value": row.stock_value,
            "stock_value_difference": row.stock_value_difference,
            "company": doc.company,
            "stock_closing_entry": doc.name,
            "item_name": row.item_name,
            "item_group": row.item_group,
            "stock_uom": row.stock_uom,
            "inventory_dimension_key": row.get("inventory_dimension_key"),
            "fifo_queue": row.get("fifo_queue"),
            "_user_tags": None,
            "_comments": None,
            "_assign": None,
            "_liked_by": None,
        }
        for fieldname in dimension_fieldnames(dimensions):
            if fieldname not in values:
                values[fieldname] = row.get(fieldname)
        pending.append([values.get(column) for column in columns])
        if len(pending) >= chunk_size:
            flush()
    flush()
    total_written = start_index - 1 + created
    log("stock_closing_bulk_inserted_rows", total_written, "new_rows", created, "elapsed", round(time.monotonic() - started, 2), flush=True)
    return created


def count_stock_closing_balances(name: str) -> int:
    return int(frappe.db.count("Stock Closing Balance", {"stock_closing_entry": name}) or 0)


def normalized_identity_value(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def numeric_values_match(left: Any, right: Any, tolerance: float = 0.0000001) -> bool:
    try:
        return abs(float(left or 0) - float(right or 0)) <= tolerance
    except (TypeError, ValueError):
        return False


def stock_closing_row_identity(row: Any, dimensions: Iterable[Any]) -> tuple[str | None, ...]:
    fields = ["item_code", "warehouse", "batch_no", "inventory_dimension_key", *dimension_fieldnames(dimensions)]
    return tuple(normalized_identity_value(row.get(field)) for field in fields)


def resumable_start_index(
    doc: Any,
    insertable_rows: Sequence[Any],
    dimensions: Iterable[Any],
    log: Callable[..., None] = print,
) -> int:
    """Return the first deterministic row position that still needs insertion.

    Chunk inserts are atomic and row ``idx`` values are stream positions. A partial attempt is
    resumed only when its idx sequence is contiguous and its last row matches the deterministic
    stream at that position; legacy/corrupt partial rows are discarded and regenerated because
    the enclosing closing is not Completed and the caller holds the closing lock.
    """
    stats_rows = frappe.db.sql(
        """
        SELECT COUNT(*) AS row_count,
               COALESCE(MIN(`idx`), 0) AS min_idx,
               COALESCE(MAX(`idx`), 0) AS max_idx,
               COUNT(DISTINCT `idx`) AS distinct_idx_count
        FROM `tabStock Closing Balance`
        WHERE stock_closing_entry = %s
        """,
        (doc.name,),
        as_dict=True,
    )
    stats = stats_rows[0] if stats_rows else frappe._dict({"row_count": 0, "min_idx": 0, "max_idx": 0, "distinct_idx_count": 0})
    count = int(stats.row_count or 0)
    if count == 0:
        return 1

    contiguous = (
        int(stats.min_idx or 0) == 1
        and int(stats.max_idx or 0) == count
        and int(stats.distinct_idx_count or 0) == count
    )
    can_validate_tip = contiguous and 0 < count <= len(insertable_rows)
    if can_validate_tip:
        expected = insertable_rows[count - 1]
        existing_rows = frappe.db.sql(
            "SELECT * FROM `tabStock Closing Balance` WHERE stock_closing_entry = %s AND `idx` = %s LIMIT 1",
            (doc.name, count),
            as_dict=True,
        )
        if existing_rows:
            existing = existing_rows[0]
            identity_matches = stock_closing_row_identity(existing, dimensions) == stock_closing_row_identity(expected, dimensions)
            numeric_matches = all(
                numeric_values_match(existing.get(field), expected.get(field))
                for field in ("actual_qty", "valuation_rate", "stock_value", "stock_value_difference")
            )
            if identity_matches and numeric_matches:
                log("resuming_stock_closing_insert_stream", doc.name, "existing_rows", count, flush=True)
                return count + 1

    log("discarding_unresumable_partial_stock_closing_rows", doc.name, count, flush=True)
    doc.remove_stock_closing()
    frappe.db.commit()
    return 1


def expected_stock_closing_balance_count(doc: Any) -> int:
    streaming_cls = build_streaming_stock_closing_class()
    closing = streaming_cls(doc.company, str(doc.from_date), str(doc.to_date))
    entries = closing.get_stock_closing_entries()
    return sum(
        1
        for key in entries
        if not (entries[key].actual_qty == 0.0 and entries[key].stock_value_difference == 0.0)
    )


def verify_completed_balance_count(doc: Any) -> tuple[int, int]:
    actual = count_stock_closing_balances(doc.name)
    expected = expected_stock_closing_balance_count(doc)
    if actual != expected:
        raise StockClosingStateError(
            f"Completed Stock Closing Entry {doc.name} has {actual} balance rows but the deterministic stream expects {expected}; refusing to delete or reuse a corrupt completed closing"
        )
    return actual, expected


def create_submitted_stock_closing(company: str, cutoff: date, log: Callable[..., None] = print) -> str:
    sce_mod = get_stock_closing_module()
    original_enqueue = sce_mod.StockClosingEntry.enqueue_job

    def suppress_enqueue(self):
        log("background_closing_enqueue_suppressed_for_synchronous_streaming", self.name, flush=True)

    sce_mod.StockClosingEntry.enqueue_job = suppress_enqueue
    try:
        doc = frappe.get_doc({"doctype": "Stock Closing Entry", "company": company, "to_date": cutoff})
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        frappe.db.commit()
        log("created_submitted_stock_closing", doc.name, doc.from_date, doc.to_date, flush=True)
        return doc.name
    except Exception as exc:
        frappe.db.rollback()
        raise StockClosingError(f"Could not create or submit Stock Closing Entry for {company} through {cutoff}: {type(exc).__name__}: {exc}") from exc
    finally:
        sce_mod.StockClosingEntry.enqueue_job = original_enqueue


def generate_streaming_stock_closing(
    doc: Any,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    log: Callable[..., None] = print,
) -> dict[str, int | str]:
    from frappe.utils import now_datetime

    sce_mod = get_stock_closing_module()
    if doc.docstatus != 1:
        raise StockClosingStateError(f"{doc.name} is not submitted")
    log("streaming_generation_start", doc.name, doc.from_date, doc.to_date, flush=True)
    doc.db_set("status", "In Progress")
    frappe.db.commit()
    started_at = now_datetime()
    try:
        streaming_cls = build_streaming_stock_closing_class()
        closing = streaming_cls(doc.company, str(doc.from_date), str(doc.to_date))
        entries = closing.get_stock_closing_entries()
        log("stock_closing_aggregated_entry_keys", len(entries), flush=True)
        insertable_rows, skipped = prepare_insertable_stock_closing_rows(entries)
        start_index = resumable_start_index(doc, insertable_rows, closing.inv_dimensions, log)
        created = bulk_insert_stock_closing_rows(
            doc,
            insertable_rows,
            closing.inv_dimensions,
            chunk_size=chunk_size,
            start_index=start_index,
            log=log,
        )

        if not sce_mod.is_submitted_closing_entry(doc.name, for_update=True):
            doc.remove_stock_closing()
            doc.db_set("status", "Cancelled")
            frappe.db.commit()
            raise StockClosingStateError(f"{doc.name} was cancelled during generation")

        sce_mod.lock_company_for_closing(doc.company)
        is_outdated = doc.has_ledger_changes_since(started_at) or doc.has_pending_reposts_now()
        if is_outdated:
            doc.db_set("status", "Outdated")
            frappe.db.commit()
            raise StockClosingStateError(f"{doc.name} became outdated during generation")
        doc.db_set("status", "Completed")
        frappe.db.commit()
    except Exception as exc:
        try:
            frappe.db.rollback()
            current_status = frappe.db.get_value("Stock Closing Entry", doc.name, "status")
            if current_status not in {"Cancelled", "Outdated"}:
                doc.db_set("status", "Failed")
                frappe.db.commit()
        except Exception:
            pass
        if isinstance(exc, StockClosingError):
            raise
        raise StockClosingError(f"Streaming generation failed for {doc.name}: {type(exc).__name__}: {exc}") from exc

    rows = count_stock_closing_balances(doc.name)
    log("streaming_generation_complete", doc.name, "Completed", "balance_rows", rows, flush=True)
    return {"name": doc.name, "status": "Completed", "balance_rows": rows, "created": created, "skipped_zero_rows": skipped}


def recover_existing_closing(row: dict[str, Any], log: Callable[..., None] = print) -> Any:
    name = str(row["name"])
    doc = frappe.get_doc("Stock Closing Entry", name)
    if doc.docstatus != 1:
        raise StockClosingStateError(f"{name} is not submitted and cannot be recovered by the streaming seeder")
    if str(row.get("status")) == "Completed":
        raise StockClosingStateError(f"Refusing to recover completed closing {name}")
    partial_rows = count_stock_closing_balances(name)
    if partial_rows:
        log("preserving_partial_stock_closing_rows_for_resume_validation", name, partial_rows, flush=True)
    doc.db_set("status", "Queued")
    frappe.db.commit()
    return doc


def ensure_stock_closing(
    company: str,
    to_date: date | datetime | str,
    *,
    lock_timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS,
    abandoned_after_seconds: int = DEFAULT_ABANDONED_AFTER_SECONDS,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    dry_run: bool = False,
    verify_completed: bool = True,
    log: Callable[..., None] = print,
) -> dict[str, Any]:
    """Create, verify, or safely recover one unscoped closing synchronously.

    The function never enqueues ERPNext's background long job. Interruption leaves either no
    rows (before chunk commits), a contiguous partial row prefix that can be resumed under the
    same GET_LOCK, or a Completed closing whose balance count is verified before reuse.
    """
    require_frappe()
    cutoff = coerce_stock_closing_date(to_date)
    with mysql_stock_closing_lock(company, cutoff, lock_timeout_seconds):
        assert_no_blocking_reposts(company, cutoff)
        rows = get_stock_closing_rows(company)
        action, row, reason = inspect_stock_closing_rows(rows, cutoff, abandoned_after_seconds)
        if action == "error":
            raise StockClosingConflictError(reason)
        if action == "active":
            raise StockClosingConflictError(f"Refusing to recover {row.get('name')} because it may be live: {reason}")

        plan = {"company": company, "to_date": cutoff.isoformat(), "planned_action": action, "reason": reason}
        if row:
            plan["name"] = row.get("name")
            plan["status"] = row.get("status")
        if dry_run:
            plan["dry_run"] = True
            return plan

        if action == "create":
            name = create_submitted_stock_closing(company, cutoff, log)
            doc = frappe.get_doc("Stock Closing Entry", name)
        elif action == "recover":
            log("recovering_stock_closing", row.get("name"), reason, flush=True)
            doc = recover_existing_closing(row, log)
        else:
            doc = frappe.get_doc("Stock Closing Entry", str(row["name"]))
            if verify_completed:
                actual, expected = verify_completed_balance_count(doc)
                log("verified_completed_stock_closing", doc.name, "balance_rows", actual, flush=True)
            return {
                "name": doc.name,
                "status": "Completed",
                "action": "reuse",
                "balance_rows": count_stock_closing_balances(doc.name),
            }

        if not doc.from_date or not doc.to_date:
            raise StockClosingStateError(f"{doc.name} lacks a complete closing date range")
        assert_no_overlapping_closings(
            company,
            coerce_stock_closing_date(doc.from_date),
            coerce_stock_closing_date(doc.to_date),
            doc.name,
        )
        result = generate_streaming_stock_closing(doc, chunk_size=chunk_size, log=log)
        result["action"] = "generated"
        return result


def create_sequential_stock_closings(
    company: str,
    cutoffs: Sequence[date | datetime | str],
    *,
    dry_run: bool = False,
    log: Callable[..., None] = print,
) -> dict[str, str]:
    ordered_cutoffs = sorted({coerce_stock_closing_date(cutoff) for cutoff in cutoffs})
    if not ordered_cutoffs:
        raise ValueError("at least one stock closing cutoff is required")
    closing_names: dict[str, str] = {}
    for cutoff in ordered_cutoffs:
        log(f"Ensuring streaming Stock Closing Entry through {cutoff}", flush=True)
        result = ensure_stock_closing(company, cutoff, dry_run=dry_run, log=log)
        if dry_run:
            closing_names[cutoff.isoformat()] = str(result.get("name") or f"dry-run:{result['planned_action']}")
        else:
            closing_names[cutoff.isoformat()] = str(result["name"])
            log(f"Stock Closing Entry {result['name']} through {cutoff} completed", flush=True)
    return closing_names
