import unittest

from khitmee_ops.profit_summary.reconcile import compare_rows


def stored(day, warehouse, qty, amount, ur_qty=0, ur_amount=0):
	return {
		"posting_date": day,
		"warehouse": warehouse,
		"normal_qty": qty,
		"normal_base_amount": amount,
		"unlinked_return_qty": ur_qty,
		"unlinked_return_base_amount": ur_amount,
	}


def ledger(day, warehouse, bucket, qty, amount):
	return {"posting_date": day, "warehouse": warehouse, "bucket": bucket, "qty": qty, "base_amount": amount}


class TestProfitSummaryReconcile(unittest.TestCase):
	def test_matching_rows_have_no_drift(self):
		drifts = compare_rows(
			[stored("2026-11-26", "Shop 1", "1958", "41590.55", "-5", "-120.5")],
			[
				ledger("2026-11-26", "Shop 1", "normal", "1958.000000000", "41590.550000000"),
				ledger("2026-11-26", "Shop 1", "unlinked_return", "-5", "-120.5"),
			],
			["2026-11-26"],
		)
		self.assertEqual(drifts, [])

	def test_inflated_group_fold_row_is_reported(self):
		# The 2026-11-26 regression: one line carried the whole day's totals.
		drifts = compare_rows(
			[stored("2026-11-26", "Shop 1", "30287", "596492.45")],
			[ledger("2026-11-26", "Shop 1", "normal", "1958", "41590.55")],
			["2026-11-26"],
		)
		self.assertEqual({d["field"] for d in drifts}, {"qty", "base_amount"})
		self.assertTrue(all(d["bucket"] == "normal" and d["warehouse"] == "Shop 1" for d in drifts))

	def test_missing_stored_warehouse_and_dirty_days(self):
		drifts = compare_rows(
			[],
			[
				ledger("2026-11-25", "Shop 2", "normal", "3", "30"),
				ledger("2026-11-27", "Shop 2", "normal", "9", "90"),
			],
			["2026-11-25"],
		)
		self.assertEqual(len(drifts), 2)
		self.assertTrue(all(d["posting_date"] == "2026-11-25" for d in drifts))

	def test_sub_precision_rounding_is_tolerated(self):
		drifts = compare_rows(
			[stored("2026-11-24", "Shop 1", "10", "100.0004")],
			[ledger("2026-11-24", "Shop 1", "normal", "10", "100.0000")],
			["2026-11-24"],
		)
		self.assertEqual(drifts, [])


if __name__ == "__main__":
	unittest.main()
