import json
import sys

import click

import frappe
from frappe.commands import pass_context

from khitmee_ops.setup.indexes import setup_database_objects


@click.command("khitmee-setup-indexes")
@click.option("--rebuild-item-search", is_flag=True, help="Enable and rebuild the SQLite FTS5 item index")
@click.option("--stock-closing-date", help="Create or refresh the daily stock closing snapshot for this date")
@click.option("--company", help="Optional company scope for the daily stock closing snapshot")
@pass_context
def khitmee_setup_indexes(context, rebuild_item_search=False, stock_closing_date=None, company=None):
	"""Create Khitmee composite indexes and optional performance helper data."""
	for site in context.sites:
		frappe.init(site)
		frappe.connect()
		try:
			result = setup_database_objects(
				rebuild_item_search=rebuild_item_search,
				stock_closing_date=stock_closing_date,
				company=company,
			)
			frappe.db.commit()
			created = ", ".join(result["created_indexes"]) or "none (already present)"
			click.echo(f"{site}: created indexes: {created}")
			if rebuild_item_search:
				click.echo(f"{site}: item search index enabled and rebuilt")
			if result["daily_stock_closing_rows"] is not None:
				click.echo(f"{site}: daily stock closing rows: {result['daily_stock_closing_rows']}")
		finally:
			frappe.destroy()


@click.command("khitmee-export-profit")
@click.option("--company", required=True, help="Company scope for the profit export")
@click.option("--from-date", required=True, help="First posting date to export")
@click.option("--to-date", required=True, help="Last posting date to export")
@click.option("--group-by", default="Invoice", show_default=True, help="Fast Gross Profit grouping")
@click.option("--warehouse", help="Optional warehouse scope")
@click.option("--user", required=True, help="ERPNext user whose permissions bind the export")
@click.option("--run-sync", is_flag=True, help="Run the export worker synchronously after enqueueing")
@pass_context
def khitmee_export_profit(
	context,
	company,
	from_date,
	to_date,
	group_by="Invoice",
	warehouse=None,
	user=None,
	run_sync=False,
):
	"""Enqueue a secured Fast Gross Profit CSV export."""
	for site in context.sites:
		frappe.init(site)
		frappe.connect()
		try:
			from khitmee_ops.profit_summary.export_service import (
				enqueue_profit_csv_export_for_user,
				run_profit_csv_export,
			)

			filters = {
				"company": company,
				"from_date": from_date,
				"to_date": to_date,
				"group_by": group_by,
			}
			if warehouse:
				filters["warehouse"] = warehouse
			result = enqueue_profit_csv_export_for_user(filters, user, source="cli")
			if run_sync:
				run_profit_csv_export(result["export_log_name"])
			frappe.db.commit()
			click.echo(
				f"{site}: export {result['export_log_name']} status={result['status']} "
				f"deduplicated={int(result['deduplicated'])}"
			)
		finally:
			frappe.destroy()


@click.command("khitmee-check-profit-parity")
@click.option("--company", required=True, help="Company scope for oracle parity checks")
@click.option("--from-date", required=True, help="First posting date to check")
@click.option("--to-date", required=True, help="Last posting date to check")
@click.option(
	"--group-by",
	multiple=True,
	default=("Warehouse", "Monthly"),
	show_default=True,
	help="Grouping to verify; repeat for multiple groupings",
)
@click.option("--sample-days", type=int, help="Optional number of days to sample")
@pass_context
def khitmee_check_profit_parity(
	context,
	company,
	from_date,
	to_date,
	group_by=("Warehouse", "Monthly"),
	sample_days=None,
):
	"""Run the independent Fast Gross Profit oracle parity check."""
	for site in context.sites:
		frappe.init(site)
		frappe.connect()
		try:
			from khitmee_ops.profit_summary.parity_check import run_profit_parity_check

			result = run_profit_parity_check(
				company,
				from_date,
				to_date,
				group_by_list=tuple(group_by) or ("Warehouse", "Monthly"),
				sample_days=sample_days,
			)
		finally:
			frappe.destroy()
		click.echo(json.dumps(result, indent=2, default=str))
		sys.exit(result["exit_code"])


@click.command("khitmee-reconcile-profit-summary")
@click.option("--company", required=True, help="Company scope")
@click.option("--from-date", required=True, help="First posting date to reconcile")
@click.option("--to-date", required=True, help="Last posting date to reconcile")
@pass_context
def khitmee_reconcile_profit_summary(context, company, from_date, to_date):
	"""Cheap stored profit summary qty/amount check against submitted invoice lines."""
	exit_code = 0
	for site in context.sites:
		frappe.init(site)
		frappe.connect()
		try:
			from khitmee_ops.profit_summary.reconcile import reconcile_summary_against_ledger

			drifts = reconcile_summary_against_ledger(company, from_date, to_date)
		finally:
			frappe.destroy()
		click.echo(json.dumps({"site": site, "drift_count": len(drifts), "drifts": drifts[:200]}, indent=2))
		exit_code = exit_code or (1 if drifts else 0)
	sys.exit(exit_code)


commands = [
	khitmee_setup_indexes,
	khitmee_export_profit,
	khitmee_check_profit_parity,
	khitmee_reconcile_profit_summary,
]
