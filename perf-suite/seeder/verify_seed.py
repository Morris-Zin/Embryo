#!/usr/bin/env python3
"""Validate the seeded Khitmee benchmark dataset without mutating it."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from seed_dataset import BENCH_SITE, assert_safe_site, connect_site, validate_manifest_schema

try:
	import frappe
except ImportError:
	frappe = None


def scalar(query: str, values: Sequence[Any] = ()) -> Any:
	rows = frappe.db.sql(query, values)
	return rows[0][0] if rows else None


def load_manifest(path: str | None) -> tuple[dict[str, Any], str]:
	candidates = []
	if path:
		candidates.append(Path(path))
	else:
		candidates.append(Path(__file__).with_name("seed_manifest.json"))
		if frappe is not None and getattr(frappe, "local", None) and getattr(frappe.local, "site_path", None):
			candidates.append(Path(frappe.get_site_path("private", "files")) / "seed_manifest.json")
	for candidate in candidates:
		if candidate.exists():
			return json.loads(candidate.read_text(encoding="utf-8")), str(candidate)
	raise FileNotFoundError("seed_manifest.json not found; pass --manifest")


def check_table_counts(manifest: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
	counts = manifest.get("counts", {})
	final = manifest.get("final_vouchers", {})
	item_summary = manifest.get("item_summary", {})
	item_count = item_summary.get("count") or len(manifest.get("items", []))
	bundle_count = len(item_summary.get("bundle_items", []))
	expected_minimums = {
		"tabItem": item_count + bundle_count,
		"tabItem Barcode": item_count + bundle_count,
		"tabUOM Conversion Detail": item_count + bundle_count,
		"tabItem Price": item_count + bundle_count,
		"tabCustomer": len(manifest.get("customers", [])),
		"tabSupplier": len(manifest.get("suppliers", [])),
		"tabProduct Bundle": bundle_count,
		"tabSales Invoice": counts.get("tabSales Invoice", 0) + len(final.get("sales", [])) + len(final.get("returns", [])),
		"tabSales Invoice Item": counts.get("tabSales Invoice Item", 0) + len(final.get("sales", [])) + len(final.get("returns", [])),
		"tabStock Entry": counts.get("tabStock Entry", 0) + len(final.get("transfers", [])),
		"tabStock Ledger Entry": counts.get("tabStock Ledger Entry", 0),
		"tabGL Entry": counts.get("tabGL Entry", 0),
		"tabPayment Ledger Entry": counts.get("tabPayment Ledger Entry", 0),
		"tabBin": item_count * len(manifest.get("warehouses", {})),
		"tabStock Closing Entry": 1,
	}
	for table, minimum in expected_minimums.items():
		actual = scalar(f"SELECT COUNT(*) FROM `{table}`") or 0
		if actual < minimum:
			errors.append(f"{table}: expected at least {minimum}, found {actual}")
		else:
			print(f"PASS count {table}: {actual} >= {minimum}")
	bundle_children = scalar("SELECT COUNT(*) FROM `tabProduct Bundle Item`") or 0
	if bundle_children < bundle_count * 2:
		warnings.append(f"tabProduct Bundle Item count looks low: {bundle_children}")


def check_payment_mode(company: str, errors: list[str]) -> None:
	row = frappe.db.sql(
		"SELECT type, enabled FROM `tabMode of Payment` WHERE name='Cash'"
	)
	if not row:
		errors.append("Mode of Payment 'Cash' is missing")
		return
	if row[0][0] != "Cash" or not row[0][1]:
		errors.append(f"Mode of Payment 'Cash' has wrong type/enabled: {row[0]}")
	mapped = scalar(
		"SELECT COUNT(*) FROM `tabMode of Payment Account` WHERE parent='Cash' AND company=%s AND default_account LIKE %s",
		(company, "Cash%"),
	) or 0
	if not mapped:
		errors.append(f"Mode of Payment 'Cash' has no account mapping for {company}")
	else:
		print(f"PASS Mode of Payment 'Cash' enabled with {mapped} company account mapping(s)")


def check_bins(company: str, errors: list[str]) -> None:
	negative = scalar(
		"SELECT COUNT(*) FROM `tabBin` WHERE company=%s AND (actual_qty < -0.000001 OR stock_value < -0.01)",
		(company,),
	) or 0
	if negative:
		errors.append(f"{negative} Bin rows have negative quantity or stock value")
	else:
		print("PASS Bin quantities and stock values are non-negative")
	rows = frappe.db.sql(
		"""
		SELECT b.item_code, b.warehouse, b.actual_qty, b.stock_value,
			(SELECT s.qty_after_transaction FROM `tabStock Ledger Entry` s
				WHERE s.item_code=b.item_code AND s.warehouse=b.warehouse AND s.is_cancelled=0
				ORDER BY s.posting_datetime DESC, s.creation DESC, s.name DESC LIMIT 1) AS sle_qty,
			(SELECT s.stock_value FROM `tabStock Ledger Entry` s
				WHERE s.item_code=b.item_code AND s.warehouse=b.warehouse AND s.is_cancelled=0
				ORDER BY s.posting_datetime DESC, s.creation DESC, s.name DESC LIMIT 1) AS sle_value
		FROM `tabBin` b
		WHERE b.company=%s
		ORDER BY b.name
		LIMIT 500
		""",
		(company,),
	)
	mismatches = []
	for item_code, warehouse, bin_qty, bin_value, sle_qty, sle_value in rows:
		bin_qty = float(bin_qty or 0)
		bin_value = float(bin_value or 0)
		if sle_qty is None:
			if abs(bin_qty) > 0.000001 or abs(bin_value) > 0.01:
				mismatches.append((item_code, warehouse, bin_qty, bin_value, None, None))
			continue
		if abs(bin_qty - float(sle_qty)) > 0.000001 or abs(bin_value - float(sle_value or 0)) > 0.02:
			mismatches.append((item_code, warehouse, bin_qty, bin_value, float(sle_qty), float(sle_value or 0)))
	if mismatches:
		errors.append(f"Bin/SLE mismatch in {len(mismatches)} sampled rows; first: {mismatches[0]}")
	else:
		print("PASS sampled Bin.actual_qty/stock_value match latest SLE")


def check_fifo_samples(company: str, errors: list[str]) -> None:
	negative = scalar(
		"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE company=%s AND is_cancelled=0 AND (qty_after_transaction < -0.000001 OR stock_value < -0.01)",
		(company,),
	) or 0
	if negative:
		errors.append(f"{negative} Stock Ledger Entries end with negative quantity or stock value")
	else:
		print("PASS Stock Ledger Entries remain non-negative")
	rows = []
	for direction in ("ASC", "DESC"):
		rows.extend(
			frappe.db.sql(
				f"""
				SELECT name, qty_after_transaction, stock_value, stock_queue
				FROM `tabStock Ledger Entry`
				WHERE company=%s AND is_cancelled=0
				ORDER BY posting_datetime {direction}, creation {direction}, name {direction}
				LIMIT 150
				""",
				(company,),
			)
		)
	bad = []
	for name, qty_after, stock_value, stock_queue in rows:
		try:
			queue = json.loads(stock_queue or "[]")
			queue_qty = sum(float(row[0]) for row in queue)
			queue_value = sum(float(row[0]) * float(row[1]) for row in queue)
			value_tol = max(0.05, abs(float(qty_after or 0)) * 0.001)
			if abs(queue_qty - float(qty_after or 0)) > 0.0001 or abs(queue_value - float(stock_value or 0)) > value_tol:
				bad.append((name, qty_after, stock_value, queue_qty, queue_value))
		except (TypeError, ValueError, IndexError, json.JSONDecodeError) as exc:
			bad.append((name, f"invalid stock_queue: {exc}"))
	if bad:
		errors.append(f"FIFO stock_queue mismatch in {len(bad)} sampled SLEs; first: {bad[0]}")
	else:
		print("PASS sampled FIFO stock_queue totals match SLE quantity and stock value")


def voucher_balance_check(voucher_type: str, names: list[str], errors: list[str]) -> None:
	if not names:
		return
	placeholders = ",".join(["%s"] * len(names))
	rows = frappe.db.sql(
		f"""
		SELECT voucher_no, COUNT(*), SUM(debit)-SUM(credit)
		FROM `tabGL Entry`
		WHERE voucher_type=%s AND voucher_no IN ({placeholders}) AND is_cancelled=0
		GROUP BY voucher_no
		""",
		[voucher_type, *names],
	)
	gl_rows = {row[0]: (int(row[1]), float(row[2] or 0)) for row in rows}
	missing = [name for name in names if name not in gl_rows]
	empty = [name for name in names if name in gl_rows and gl_rows[name][0] <= 0]
	unbalanced = [name for name in names if name in gl_rows and abs(gl_rows[name][1]) > 0.02]
	if missing:
		errors.append(f"{voucher_type} sample has vouchers with NO GL rows at all: {missing[0]} (+{len(missing) - 1} more)")
	elif empty:
		errors.append(f"{voucher_type} sample has vouchers with zero GL rows: {empty[0]}")
	elif unbalanced:
		name = unbalanced[0]
		errors.append(f"{voucher_type} sample has unbalanced GL vouchers: {name} off by {gl_rows[name][1]}")
	else:
		print(f"PASS all {len(names)} sampled {voucher_type} vouchers have >0 GL rows and balance")


def check_payment_ledger(names: list[str], errors: list[str]) -> None:
	if not names:
		return
	placeholders = ",".join(["%s"] * len(names))
	invoices = frappe.db.sql(
		f"SELECT name, grand_total, is_return FROM `tabSales Invoice` WHERE name IN ({placeholders})",
		names,
	)
	ple_rows = frappe.db.sql(
		f"""
		SELECT voucher_no, COUNT(*), SUM(amount)
		FROM `tabPayment Ledger Entry`
		WHERE voucher_type='Sales Invoice' AND voucher_no IN ({placeholders}) AND delinked=0
		GROUP BY voucher_no
		""",
		names,
	)
	ple_map = {row[0]: (int(row[1]), float(row[2] or 0)) for row in ple_rows}
	missing = []
	mismatched = []
	for name, grand_total, _is_return in invoices:
		entry = ple_map.get(name)
		if not entry or entry[0] <= 0:
			missing.append(name)
			continue
		if abs(abs(entry[1]) - abs(float(grand_total or 0))) > 0.02:
			mismatched.append((name, float(grand_total or 0), entry[1]))
	if missing:
		errors.append(f"{len(missing)} sampled Sales Invoices have no delinked=0 Payment Ledger Entry; first: {missing[0]}")
	elif mismatched:
		errors.append(f"Payment Ledger Entry amount mismatch in {len(mismatched)} sampled invoices; first: {mismatched[0]}")
	else:
		print(f"PASS all {len(invoices)} sampled Sales Invoices have matching Payment Ledger Entries")


def check_gl_balance(company: str, errors: list[str]) -> None:
	sales = [row[0] for row in frappe.db.sql("SELECT name FROM `tabSales Invoice` WHERE company=%s ORDER BY posting_date DESC, name DESC LIMIT 150", (company,))]
	stock = [row[0] for row in frappe.db.sql("SELECT name FROM `tabStock Entry` WHERE company=%s ORDER BY posting_date DESC, name DESC LIMIT 150", (company,))]
	voucher_balance_check("Sales Invoice", sales, errors)
	voucher_balance_check("Stock Entry", stock, errors)
	check_payment_ledger(sales, errors)


def check_item_search(errors: list[str]) -> None:
	db_path = os.path.join(frappe.get_site_path(), "item_search.db")
	if not os.path.exists(db_path):
		errors.append(f"item search index missing: {db_path}")
	else:
		print(f"PASS item search index exists: {db_path}")
	enabled = frappe.get_single_value("Global Defaults", "enable_item_search_index")
	if not enabled:
		errors.append("Global Defaults.enable_item_search_index is not enabled")
	else:
		print("PASS Global Defaults.enable_item_search_index is enabled")


def run_verify(args: argparse.Namespace) -> int:
	assert_safe_site(args.site, args.allow_non_bench_site)
	connect_site(args.site)
	manifest, manifest_path = load_manifest(args.manifest)
	errors = validate_manifest_schema(manifest)
	warnings: list[str] = []
	if errors:
		print(f"FAIL manifest schema at {manifest_path}: {errors[0]}")
	else:
		print(f"PASS manifest schema: {manifest_path}")
	if manifest.get("company") != args.company and not errors:
		warnings.append(f"manifest company {manifest.get('company')!r} differs from --company {args.company!r}")
	if not errors:
		check_table_counts(manifest, errors, warnings)
		check_payment_mode(args.company, errors)
		check_bins(args.company, errors)
		check_fifo_samples(args.company, errors)
		check_gl_balance(args.company, errors)
		check_item_search(errors)
	for warning in warnings:
		print(f"WARN {warning}")
	if errors:
		for error in errors:
			print(f"FAIL {error}")
		return 1
	print("VERIFY SEED PASS")
	return 0


def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Verify the disposable Khitmee ERPNext benchmark seed")
	parser.add_argument("--site", default=BENCH_SITE)
	parser.add_argument("--allow-non-bench-site", action="store_true")
	parser.add_argument("--company", default="Khit Me Distribution")
	parser.add_argument("--manifest")
	return parser


def main(argv: Sequence[str] | None = None) -> int:
	args = build_arg_parser().parse_args(argv)
	try:
		return run_verify(args)
	except Exception as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		if os.environ.get("SEEDER_DEBUG"):
			raise
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
