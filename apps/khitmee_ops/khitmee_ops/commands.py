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


commands = [khitmee_setup_indexes]
