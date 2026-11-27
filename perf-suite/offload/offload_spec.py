"""Shared table and query definitions for the database offload proof of concept."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    duckdb_type: str


@dataclass(frozen=True)
class TableSpec:
    key: str
    source_name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: str = "name"

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def select_sql(self, where: str | None = None) -> str:
        column_list = ", ".join(f"`{column.name}`" for column in self.columns)
        sql = f"SELECT {column_list} FROM `{self.source_name}`"
        if where:
            sql += f" WHERE {where}"
        return sql

    def duckdb_columns_expression(self) -> str:
        entries = ", ".join(f"'{column.name}': '{column.duckdb_type}'" for column in self.columns)
        return "{" + entries + "}"


VARCHAR = "VARCHAR"
DECIMAL_18_6 = "DECIMAL(18,6)"
DECIMAL_20_6 = "DECIMAL(20,6)"


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "sales_invoice",
        "tabSales Invoice",
        (
            ColumnSpec("name", VARCHAR),
            ColumnSpec("modified", "TIMESTAMP"),
            ColumnSpec("docstatus", "TINYINT"),
            ColumnSpec("company", VARCHAR),
            ColumnSpec("posting_date", "DATE"),
            ColumnSpec("posting_time", "TIME"),
            ColumnSpec("customer", VARCHAR),
            ColumnSpec("customer_name", VARCHAR),
            ColumnSpec("is_return", "TINYINT"),
            ColumnSpec("return_against", VARCHAR),
            ColumnSpec("update_stock", "TINYINT"),
            ColumnSpec("is_opening", VARCHAR),
            ColumnSpec("is_debit_note", "TINYINT"),
            ColumnSpec("total_qty", DECIMAL_18_6),
            ColumnSpec("base_net_total", DECIMAL_20_6),
            ColumnSpec("base_grand_total", DECIMAL_20_6),
            ColumnSpec("currency", VARCHAR),
            ColumnSpec("conversion_rate", DECIMAL_18_6),
        ),
    ),
    TableSpec(
        "sales_invoice_item",
        "tabSales Invoice Item",
        (
            ColumnSpec("name", VARCHAR),
            ColumnSpec("modified", "TIMESTAMP"),
            ColumnSpec("parent", VARCHAR),
            ColumnSpec("idx", "INTEGER"),
            ColumnSpec("item_code", VARCHAR),
            ColumnSpec("item_name", VARCHAR),
            ColumnSpec("item_group", VARCHAR),
            ColumnSpec("brand", VARCHAR),
            ColumnSpec("qty", DECIMAL_18_6),
            ColumnSpec("stock_qty", DECIMAL_18_6),
            ColumnSpec("base_net_rate", DECIMAL_20_6),
            ColumnSpec("base_net_amount", DECIMAL_20_6),
            ColumnSpec("warehouse", VARCHAR),
            ColumnSpec("incoming_rate", DECIMAL_20_6),
            ColumnSpec("actual_qty", DECIMAL_18_6),
            ColumnSpec("cost_center", VARCHAR),
            ColumnSpec("docstatus", "TINYINT"),
        ),
    ),
    TableSpec(
        "stock_ledger_entry",
        "tabStock Ledger Entry",
        (
            ColumnSpec("name", VARCHAR),
            ColumnSpec("modified", "TIMESTAMP"),
            ColumnSpec("item_code", VARCHAR),
            ColumnSpec("warehouse", VARCHAR),
            ColumnSpec("posting_date", "DATE"),
            ColumnSpec("posting_time", "TIME"),
            ColumnSpec("posting_datetime", "TIMESTAMP"),
            ColumnSpec("voucher_type", VARCHAR),
            ColumnSpec("voucher_no", VARCHAR),
            ColumnSpec("voucher_detail_no", VARCHAR),
            ColumnSpec("actual_qty", DECIMAL_18_6),
            ColumnSpec("qty_after_transaction", DECIMAL_18_6),
            ColumnSpec("incoming_rate", DECIMAL_20_6),
            ColumnSpec("outgoing_rate", DECIMAL_20_6),
            ColumnSpec("valuation_rate", DECIMAL_20_6),
            ColumnSpec("stock_value", DECIMAL_20_6),
            ColumnSpec("stock_value_difference", DECIMAL_20_6),
            ColumnSpec("company", VARCHAR),
            ColumnSpec("is_cancelled", "TINYINT"),
        ),
    ),
    TableSpec(
        "packed_item",
        "tabPacked Item",
        (
            ColumnSpec("name", VARCHAR),
            ColumnSpec("modified", "TIMESTAMP"),
            ColumnSpec("parenttype", VARCHAR),
            ColumnSpec("parent", VARCHAR),
            ColumnSpec("parent_item", VARCHAR),
            ColumnSpec("item_code", VARCHAR),
            ColumnSpec("warehouse", VARCHAR),
            ColumnSpec("qty", DECIMAL_18_6),
            ColumnSpec("rate", DECIMAL_20_6),
            ColumnSpec("parent_detail_docname", VARCHAR),
            ColumnSpec("docstatus", "TINYINT"),
        ),
    ),
    TableSpec(
        "item",
        "tabItem",
        (
            ColumnSpec("name", VARCHAR),
            ColumnSpec("modified", "TIMESTAMP"),
            ColumnSpec("item_code", VARCHAR),
            ColumnSpec("item_name", VARCHAR),
            ColumnSpec("item_group", VARCHAR),
            ColumnSpec("brand", VARCHAR),
            ColumnSpec("is_stock_item", "TINYINT"),
            ColumnSpec("disabled", "TINYINT"),
        ),
    ),
    TableSpec(
        "warehouse",
        "tabWarehouse",
        (
            ColumnSpec("name", VARCHAR),
            ColumnSpec("modified", "TIMESTAMP"),
            ColumnSpec("warehouse_name", VARCHAR),
            ColumnSpec("company", VARCHAR),
            ColumnSpec("disabled", "TINYINT"),
            ColumnSpec("lft", "INTEGER"),
            ColumnSpec("rgt", "INTEGER"),
        ),
    ),
)

TABLES_BY_KEY = {table.key: table for table in TABLE_SPECS}


@dataclass(frozen=True)
class CanonicalQuery:
    key: str
    title: str
    mariadb_sql: str
    duckdb_sql: str
    order_sensitive: bool = True


_INVOICE_JOIN_MARIADB = """
FROM `tabSales Invoice` AS si
JOIN `tabSales Invoice Item` AS sii ON sii.parent = si.name
WHERE si.company = %s
  AND si.docstatus = 1
  AND si.is_opening != 'Yes'
  AND si.posting_date BETWEEN %s AND %s
"""

_INVOICE_JOIN_DUCKDB = """
FROM sales_invoice AS si
JOIN sales_invoice_item AS sii ON sii.parent = si.name
WHERE si.company = ?
  AND si.docstatus = 1
  AND si.is_opening != 'Yes'
  AND si.posting_date BETWEEN ? AND ?
"""

CANONICAL_QUERIES: tuple[CanonicalQuery, ...] = (
    CanonicalQuery(
        "invoice_fact_scan",
        "One-row aggregate over the invoice fact join",
        """
SELECT
  COUNT(*) AS line_count,
  COUNT(DISTINCT si.name) AS invoice_count,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
"""
        + _INVOICE_JOIN_MARIADB,
        """
SELECT
  COUNT(*) AS line_count,
  COUNT(DISTINCT si.name) AS invoice_count,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
"""
        + _INVOICE_JOIN_DUCKDB,
    ),
    CanonicalQuery(
        "daily_invoice_summary",
        "Daily financial aggregate by warehouse",
        """
SELECT
  si.posting_date,
  sii.warehouse,
  COUNT(DISTINCT si.name) AS invoice_count,
  COUNT(*) AS line_count,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
"""
        + _INVOICE_JOIN_MARIADB
        + "GROUP BY si.posting_date, sii.warehouse\nORDER BY si.posting_date, sii.warehouse",
        """
SELECT
  si.posting_date,
  sii.warehouse,
  COUNT(DISTINCT si.name) AS invoice_count,
  COUNT(*) AS line_count,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
"""
        + _INVOICE_JOIN_DUCKDB
        + "GROUP BY si.posting_date, sii.warehouse\nORDER BY si.posting_date, sii.warehouse",
    ),
    CanonicalQuery(
        "monthly_item_margin",
        "Monthly item-level margin scan",
        """
SELECT
  DATE_FORMAT(si.posting_date, '%%Y-%%m') AS month,
  sii.item_code,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
"""
        + _INVOICE_JOIN_MARIADB
        + "GROUP BY DATE_FORMAT(si.posting_date, '%%Y-%%m'), sii.item_code\nORDER BY month, sii.item_code",
        """
SELECT
  strftime(si.posting_date, '%Y-%m') AS month,
  sii.item_code,
  SUM(sii.stock_qty) AS qty,
  SUM(sii.base_net_amount) AS selling_amount,
  SUM(sii.stock_qty * sii.incoming_rate) AS buying_amount_estimate,
  SUM(sii.base_net_amount - (sii.stock_qty * sii.incoming_rate)) AS gross_profit_estimate
"""
        + _INVOICE_JOIN_DUCKDB
        + "GROUP BY strftime(si.posting_date, '%Y-%m'), sii.item_code\nORDER BY month, sii.item_code",
    ),
    CanonicalQuery(
        "inventory_balance",
        "Inventory quantity/value aggregate from stock ledger rows",
        """
SELECT
  warehouse,
  item_code,
  SUM(actual_qty) AS qty,
  SUM(stock_value_difference) AS stock_value
FROM `tabStock Ledger Entry`
WHERE company = %s
  AND is_cancelled = 0
  AND posting_date <= %s
GROUP BY warehouse, item_code
ORDER BY warehouse, item_code
""",
        """
SELECT
  warehouse,
  item_code,
  SUM(actual_qty) AS qty,
  SUM(stock_value_difference) AS stock_value
FROM stock_ledger_entry
WHERE company = ?
  AND is_cancelled = 0
  AND posting_date <= ?
GROUP BY warehouse, item_code
ORDER BY warehouse, item_code
""",
    ),
)

QUERIES_BY_KEY = {query.key: query for query in CANONICAL_QUERIES}


def query_parameters(query_key: str, company: str, from_date: str, to_date: str) -> tuple[Any, ...]:
    if query_key == "inventory_balance":
        return (company, to_date)
    return (company, from_date, to_date)


def normalize_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value.normalize()
    return value


def rows_equal(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
    if len(left) != len(right):
        return False
    for lval, rval in zip(left, right):
        if isinstance(lval, Decimal) or isinstance(rval, Decimal):
            try:
                if Decimal(str(lval)) != Decimal(str(rval)):
                    return False
                continue
            except Exception:
                return False
        if lval != rval:
            return False
    return True


NULL_TOKEN = "\\N"


def format_timedelta(value: Any) -> str:
    total_seconds = int(value.total_seconds())
    micros = value.microseconds
    sign = "-" if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    suffix = f".{micros:06d}".rstrip("0") if micros else ""
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}{suffix}"


def json_default(value: Any) -> str:
    from datetime import date, datetime, time
    from decimal import Decimal
    from datetime import timedelta

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return format_timedelta(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
