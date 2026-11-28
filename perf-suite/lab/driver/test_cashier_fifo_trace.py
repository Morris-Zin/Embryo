from decimal import Decimal
import copy
import json
from pathlib import Path
import unittest

from replay_cashier_fifo import conservation_verdict, consume, matches_opening


def retained_trace(index):
    """Only the opening predecessor and subsequent entries are needed for conservation."""
    path = Path(__file__).with_name("cashier-fifo-observations.json")
    observation = json.loads(path.read_bytes())["runs"][index]
    trace = {"warehouse":"Dist Hub 1 - KMD", "item_settings":observation["item_settings"], "ledger":[], "bins":[]}
    before, after = copy.deepcopy(trace), copy.deepcopy(trace)
    sales = {}
    for sku in sorted(observation["items"]):
        result = observation["items"][sku]
        for snapshot, key in ((before,"opening_bin"), (after,"closing_bin")):
            snapshot["bins"].append(dict(result[key]))
            snapshot["ledger"].append(dict(result["opening_predecessor"]))
        for r in result["ordered_new_entries"]:
            entry = dict(r["entry"])
            after["ledger"].append(entry)
            sales[entry["client_voucher_id"]] = {"id":entry["client_voucher_id"], "voucher":entry["voucher_no"],
                                                "invoice":{"currency":"USD"}}
    return {"before":{"db":{"fifo_trace":before}}, "after":{"db":{"fifo_trace":after, "sales":list(sales.values())}}}


class FifoTraceTest(unittest.TestCase):
    def test_positive_layers_are_consumed_in_order(self):
        self.assertEqual(consume([[Decimal(2), Decimal(3)], [Decimal(7), Decimal(5)]], Decimal(4)),
                         [[Decimal(5), Decimal(5)]])
        with self.assertRaisesRegex(ValueError, "exceeds"):
            consume([[Decimal(1), Decimal(8)]], Decimal(2))

    def test_joint_state_distinguishes_overlapping_issues_from_adjacent_ones(self):
        opening = {"qty_after_transaction":"9", "stock_value":"41", "stock_queue":"[[2,3],[7,5]]"}
        first = {"actual_qty":"-2", "qty_after_transaction":"7", "stock_value":"35",
                 "stock_value_difference":"-6", "stock_queue":"[[7,5]]"}
        other = {"actual_qty":"-4", "qty_after_transaction":"5", "stock_value":"25",
                 "stock_value_difference":"-16", "stock_queue":"[[5,5]]"}
        self.assertTrue(matches_opening(opening, first))
        self.assertTrue(matches_opening(opening, other))
        self.assertFalse(matches_opening(first, other))
        other["stock_queue"] = "[[5,8]]"
        self.assertFalse(matches_opening(opening, other))

    def test_retained_same_opening_branch_is_rejected_and_conserving_arm_passes(self):
        broken = conservation_verdict(retained_trace(0))
        self.assertFalse(broken["passed"])
        self.assertEqual(set(broken["bins"]), {"KM-00101","KM-00039"})
        for row in broken["bins"].values():
            self.assertEqual(Decimal(row["quantity_gaps"]["closing_queue_minus_bin"]), 7)
        self.assertTrue(conservation_verdict(retained_trace(1))["passed"])

    def test_changed_closing_inventory_value_or_quantity_fails_independently(self):
        for field, drift in (("stock_value","0.01"), ("actual_qty","0.000001")):
            with self.subTest(field=field):
                receipt = retained_trace(1)
                closing = receipt["after"]["db"]["fifo_trace"]["bins"][0]
                closing[field] = str(Decimal(closing[field])+Decimal(drift))
                self.assertFalse(conservation_verdict(receipt)["passed"])


if __name__ == "__main__":
    unittest.main()
