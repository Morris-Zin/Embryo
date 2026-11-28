import unittest

from run import evaluate


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.events = [{"type": "arrival", "id": "x", "group": "large"},
                       {"type": "submit", "id": "x", "warehouse": "Hub", "items": [["SKU", 2]]},
                       {"type": "confirmed", "id": "x", "voucher": "INV", "duration_ms": 1710, "elapsed_ms": 60001}]
        self.before = {"db": {"counts": {"Sales Invoice": 10, "Khitmee Idempotency Log": 3}}, "services": {}}
        self.sale = {"id": "x", "voucher": "INV", "invoice": {"docstatus": 1},
                     "items": [{"item_code": "SKU", "stock_qty": "2.0", "warehouse": "Hub"}],
                     "stock": [{"item_code": "SKU", "qty": "-2", "warehouse": "Hub"}],
                     "gl": {"entries": 2, "debit": "12.50", "credit": "12.5"}}
        self.after = {"db": {"counts": {"Sales Invoice": 11, "Khitmee Idempotency Log": 4}, "sales": [self.sale]},
                      "services": {}}

    def test_confirmation_in_drain_and_independent_stock_check(self):
        r = evaluate(self.events, self.before, self.after)
        self.assertEqual(r["financial_mismatches"], [])
        self.assertEqual(r["groups"]["large"]["confirmed_within_minute"], 0)
        self.assertEqual(r["groups"]["large"]["confirmed_including_drain"], 1)
        self.sale["stock"][0]["qty"] = "-1"
        self.assertTrue(evaluate(self.events, self.before, self.after)["financial_mismatches"])

    def test_lost_confirmation_is_distinct_from_posting(self):
        r = evaluate(self.events[:-1], self.before, self.after)
        self.assertEqual(r["committed_without_confirmation"], ["x"])


if __name__ == "__main__":
    unittest.main()
