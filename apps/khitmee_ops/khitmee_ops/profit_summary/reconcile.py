"""Cheap ledger reconciliation for the stored daily profit summary.

The stock Gross Profit oracle costs minutes to hours per year of data.  This
check costs two grouped SQL scans (owned by ``store.get_reconcile_inputs``):
for every clean (posting_date, warehouse) it compares the stored Set A
normal / unlinked-return qty and selling amount with submitted invoice line
sums, excluding that day's Set B replay invoices.  It cannot validate buying
amounts (that is the oracle's job), but it catches any builder defect that
inflates, drops or misattributes invoice lines -- such as the in-place stock
group fold that once copied whole-day totals onto one line.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TypedDict

try:
	import frappe
except Exception:  # pragma: no cover - importable on a plain host Python
	frappe = None

RECONCILE_LOOKBACK_DAYS = 35


class Drift(TypedDict):
	posting_date: str
	warehouse: str
	bucket: str
	field: str
	stored: str
	ledger: str


def _dec(value) -> Decimal:
	return Decimal(str(value if value is not None else 0))


def compare_rows(stored_rows, ledger_rows, clean_days, *, qty_tolerance="0.01", amount_tolerance="0.001") -> list[Drift]:
	"""Pure comparison of stored summary rows with ledger sums on clean days."""
	qty_tol = Decimal(qty_tolerance)
	amount_tol = Decimal(amount_tolerance)
	clean = {str(day) for day in clean_days}

	stored = {}
	for row in stored_rows:
		day = str(row.get("posting_date"))
		warehouse = str(row.get("warehouse") or "")
		stored[(day, warehouse, "normal")] = (_dec(row.get("normal_qty")), _dec(row.get("normal_base_amount")))
		stored[(day, warehouse, "unlinked_return")] = (
			_dec(row.get("unlinked_return_qty")),
			_dec(row.get("unlinked_return_base_amount")),
		)

	ledger = {}
	for row in ledger_rows:
		key = (str(row.get("posting_date")), str(row.get("warehouse") or ""), str(row.get("bucket")))
		ledger[key] = (_dec(row.get("qty")), _dec(row.get("base_amount")))

	drifts: list[Drift] = []
	zero = (Decimal(0), Decimal(0))
	for key in sorted(set(stored) | set(ledger)):
		if key[0] not in clean:
			continue
		stored_qty, stored_amount = stored.get(key, zero)
		ledger_qty, ledger_amount = ledger.get(key, zero)
		for field, left, right, tol in (
			("qty", stored_qty, ledger_qty, qty_tol),
			("base_amount", stored_amount, ledger_amount, amount_tol),
		):
			if abs(left - right) > tol:
				drifts.append(
					{
						"posting_date": key[0],
						"warehouse": key[1],
						"bucket": key[2],
						"field": field,
						"stored": str(left),
						"ledger": str(right),
					}
				)
	return drifts


def reconcile_summary_against_ledger(company: str, from_date: str, to_date: str) -> list[Drift]:
	from khitmee_ops.profit_summary import store

	clean_days, stored_rows, ledger_rows = store.get_reconcile_inputs(company, from_date, to_date)
	if not clean_days:
		return []
	return compare_rows(stored_rows, ledger_rows, clean_days)


def nightly_reconcile() -> int:
	"""Scheduler entry: reconcile the trailing window, log and re-dirty drifted days.

	Re-dirtying makes transient drift self-heal on the next dirty-slice run;
	a builder defect drifts again every night and stays visible in Error Log.
	"""
	if frappe is None:
		raise RuntimeError("profit summary reconciliation requires Frappe")
	from frappe.utils import add_days, getdate, today

	from khitmee_ops.profit_summary import store

	to_date = add_days(getdate(today()), -1)
	from_date = add_days(to_date, -(RECONCILE_LOOKBACK_DAYS - 1))
	drift_count = 0
	for (company,) in frappe.db.sql("SELECT `name` FROM `tabCompany` ORDER BY `name`"):
		drifts = reconcile_summary_against_ledger(company, str(from_date), str(to_date))
		if not drifts:
			continue
		drift_count += len(drifts)
		days = sorted({drift["posting_date"] for drift in drifts})
		frappe.log_error(
			title=f"Profit summary drift: {company}",
			message=frappe.as_json({"days": days, "drifts": drifts[:200]}),
		)
		store.bump_dirty_days_ordered((company, day) for day in days)
	frappe.db.commit()
	return drift_count
