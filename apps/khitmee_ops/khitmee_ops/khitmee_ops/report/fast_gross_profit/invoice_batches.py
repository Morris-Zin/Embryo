"""Bounded Invoice report traversal shared by detail pages and CSV export."""

from datetime import date

from . import fast_gross_profit as fast

INVOICE_PAGE_SIZE = 50
INVOICE_KEY_BATCH_SIZE = 128
INVOICE_BATCH_ROWS = 2_000
MAX_SINGLE_FAMILY_ROWS = 5_000


def _invoice_keys(filters, invoice_ids, *, count_rows=False):
	from pypika.enums import Order
	from pypika.functions import Cast, Coalesce, Count, Sum

	si = fast.frappe.qb.DocType("Sales Invoice")
	selector = fast.FastGrossProfitGenerator.__new__(fast.FastGrossProfitGenerator)
	selector.filters = fast._copy_report_filters(filters)
	selector._scoped_invoice_ids = fast._normalise_invoice_ids(invoice_ids)
	selector._warehouse_scope = None
	query = selector._upstream_prepare_invoice_query()
	# Preserve the stock joins, active filters and match conditions. Project only
	# the columns needed to locate the next set of invoice trees.
	query._selects = []
	query._orderbys = []
	posting_time = Coalesce(si.posting_time, "00:00:00")
	name_key = Cast(si.name, "BINARY")
	query = query.select(si.name, posting_time.as_("time_key"), si.return_against)
	main_query = query.distinct().where(fast.invoice_phase_filter(si, 1, filters))
	if count_rows:
		sii = fast.frappe.qb.DocType("Sales Invoice Item")
		packed = fast.frappe.qb.DocType("Packed Item")
		packed_count = (
			fast.frappe.qb.from_(packed)
			.select(Count(packed.name))
			.where(
				(packed.parent == si.name) & (packed.parenttype == "Sales Invoice")
				& (packed.parent_item == sii.item_code) & (packed.docstatus == 1)
			)
		)
		rendered_rows = 1 + Count(sii.name) + Sum(packed_count)
		query = query.select(rendered_rows.as_("rendered_rows")).groupby(si.name, posting_time, si.return_against)
	else:
		query = query.distinct()
	ordered = query.orderby(posting_time, order=Order.desc).orderby(name_key, order=Order.asc)
	last_day = date.fromisoformat(fast.posting_date_sort_value(filters["from_date"]))
	first_day = date.fromisoformat(fast.posting_date_sort_value(filters["to_date"]))
	for phase in (1, 0):
		for ordinal in range(first_day.toordinal(), last_day.toordinal() - 1, -1):
			day = date.fromordinal(ordinal)
			daily = ordered.where((si.posting_date == day) & fast.invoice_phase_filter(si, phase, filters))
			cursor = None
			while True:
				page = daily
				if cursor is not None:
					page = page.where(
						(posting_time < cursor.time_key)
						| ((posting_time == cursor.time_key) & (name_key > Cast(cursor.name, "BINARY")))
					)
				rows = page.limit(INVOICE_KEY_BATCH_SIZE).run(as_dict=True)
				absorbed = set()
				if phase == 0 and (originals := {row.return_against for row in rows if row.return_against}):
					absorbed = {row.name for row in main_query.where(si.name.isin(sorted(originals))).run(as_dict=True)}
				for row in rows:
					if row.return_against not in absorbed:
						yield phase, row.name, int(row.get("rendered_rows") or 0)
				if len(rows) < INVOICE_KEY_BATCH_SIZE:
					break
				cursor = rows[-1]


def _render_invoice_batch(filters, phase, names, *, export_scope=None):
	row_limit = MAX_SINGLE_FAMILY_ROWS if len(names) == 1 else INVOICE_BATCH_ROWS
	options = {} if export_scope is None else {"export_scope": export_scope}
	try:
		columns, data = fast.run_detail_gross_profit(
			filters, invoice_ids=names, invoice_phase=phase, invoice_row_limit=row_limit, **options
		)
	except fast.InvoiceBatchTooLarge:
		if len(names) == 1:
			raise fast.frappe.ValidationError(
				f"Profit invoice family exceeds the {MAX_SINGLE_FAMILY_ROWS}-row limit: {names[0]}"
			) from None
	else:
		data.pop()  # Batch Total is replaced by the export total or omitted for detail pages.
		yield columns, data
		return
	middle = len(names) // 2
	yield from _render_invoice_batch(filters, phase, names[:middle], export_scope=export_scope)
	yield from _render_invoice_batch(filters, phase, names[middle:], export_scope=export_scope)


def iter_invoice_detail_batches(filters, *, invoice_ids=None, page=None, export_scope=None):
	"""Yield complete invoice trees in the report's two-phase ordering."""
	filters = fast._copy_report_filters(filters)
	fast.validate_date_range(filters)
	keys = _invoice_keys(filters, invoice_ids, count_rows=page is not None)
	if page is not None:
		yield from _read_invoice_page(filters, page, keys, invoice_ids)
		return
	yield from _render_selected_invoices(filters, keys, export_scope=export_scope)


def _render_selected_invoices(filters, keys, *, export_scope=None):
	names, phase = [], 1
	for next_phase, name, _ in keys:
		if names and (next_phase != phase or len(names) >= INVOICE_KEY_BATCH_SIZE):
			yield from _render_invoice_batch(filters, phase, names, export_scope=export_scope)
			names = []
		names.append(name)
		phase = next_phase
	if names:
		yield from _render_invoice_batch(filters, phase, names, export_scope=export_scope)


def _read_invoice_page(filters, page, keys, invoice_ids):
	skip = (page - 1) * INVOICE_PAGE_SIZE
	selected = []
	remaining = INVOICE_PAGE_SIZE
	front_trim = 0
	for key in keys:
		row_count = key[2]
		if skip >= row_count:
			skip -= row_count
			continue
		if not selected:
			front_trim = skip
		selected.append(key)
		remaining -= row_count - skip
		skip = 0
		if remaining <= 0:
			break

	slice_remaining = INVOICE_PAGE_SIZE
	for columns, data in _render_selected_invoices(filters, selected):
		front = min(front_trim, len(data))
		front_trim -= front
		rows = data[front : front + slice_remaining]
		slice_remaining -= len(rows)
		if rows:
			yield columns, rows

	# The stock Total is part of the final slice. Recompute the grand total in
	# forward row order using bounded batches, without retaining earlier pages.
	if remaining > 0 and skip == 0:
		base_amount = buying_amount = 0
		columns = None
		for batch_columns, rows in iter_invoice_detail_batches(filters, invoice_ids=invoice_ids):
			columns = batch_columns
			for row in rows:
				if row.get("indent") == 1:
					base_amount += row.get("selling_amount") or 0.0
					buying_amount += row.get("buying_amount") or 0.0
		yield columns or invoice_detail_columns(filters), [fast.invoice_total_row(filters, base_amount, buying_amount)]


def invoice_detail_columns(filters):
	columns, _ = fast.run_detail_gross_profit(filters, invoice_ids=())
	return columns


def read_invoice_page_number(value):
	try:
		page = int(str(value))
		if not 1 <= page <= 1_000_000_000:
			raise ValueError
	except (TypeError, ValueError):
		raise fast.frappe.ValidationError("Invoice page must be a positive whole number") from None
	return page
