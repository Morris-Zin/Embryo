"""Synthetic acceptance controls. These exercise validation without running a guest."""
from decimal import Decimal
import unittest

from supervisor_arm import GROUPS, committed, controls, workload


def example():
    run_id = "fedcba012345"
    events, sales = [], []
    seed = {"company": "Bench", "customers": ["Customer"], "currency": "USD", "cash_account": "Cash",
            "accounts": {"income": "Sales"}, "items": [{"item_code": str(i), "rate": 2} for i in range(200)]}
    for group, (count, size) in GROUPS.items():
        for i in range(count):
            key = f"SCS-{run_id}-{group}-{i}"
            voucher = "INV-" + key
            amount = str(2 * size)
            events.extend([{"type": "arrival", "id": key, "group": group, "lineCount": size},
                           {"type": "submit", "id": key, "warehouse": "Hub", "items": [[str(j), 1] for j in range(size)]},
                           {"type": "confirmed", "id": key, "voucher": voucher, "duration_ms": 10, "elapsed_ms": 100, "status": 200}])
            sales.append({"id": key, "voucher": voucher, "invoice": {"docstatus": 1, "company": "Bench", "customer": "Customer",
                          "currency": "USD", "set_warehouse": "Hub", "grand_total": amount, "rounded_total": amount,
                          "paid_amount": amount, "outstanding_amount": "0"},
                          "items": [{"item_code": str(j), "stock_qty": "1", "rate": "2", "base_net_amount": "2", "warehouse":"Hub"}
                                    for j in range(size)],
                          "stock": [{"item_code": str(j), "qty": "-1", "value_change": "-1", "warehouse": "Hub"} for j in range(size)],
                          "gl": {"entries": 2, "debit": amount, "credit": amount},
                          "accounts": [{"account": "Cash", "party_type": None, "party": None, "debit": amount, "credit": "0"},
                                       {"account": "Sales", "party_type": None, "party": None, "debit": "0", "credit": amount}]})
    first = sales[0]
    events.extend([{"type": "retry", "id": first["id"], "lost_ack": True, "original_voucher": first["voucher"]},
                   {"type": "boundary_ignored", "group": "small", "sequence": 60, "elapsed_ms": 60001},
                   {"type": "summary", "iterations": 81, "dropped": 0}])
    counts = {"Sales Invoice": 80, "Sales Invoice Item": 2200, "Stock Ledger Entry": 2200, "GL Entry": 160,
              "Khitmee Idempotency Log": 80}
    before = {"services": {}, "db": {"sales": [], "counts": dict.fromkeys(counts, 0)}}
    after = {"services": {}, "db": {"sales": sales, "counts": counts}}
    return {"events": events, "exit_code": 0, "run": run_id, "before": before, "after": after}, seed


class SupervisorTest(unittest.TestCase):
    def test_exact_count_control_and_order_independent_signature(self):
        r, seed = example()
        self.assertEqual(workload(r)["completed"], 80)
        sig = committed(r, seed)
        self.assertEqual(len(sig["sales"]), 80)
        self.assertEqual(len(controls(r, seed)), 4)
        r["after"]["db"]["sales"].reverse()
        self.assertEqual(committed(r, seed), sig)

    def test_extra_committed_or_mismatched_retry_refused(self):
        r, _ = example()
        retry = next(e for e in r["events"] if e["type"] == "retry")
        retry["original_voucher"] = "Wrong invoice"
        with self.assertRaises(ValueError):
            workload(r)
        r, seed = example()
        r["after"]["db"]["counts"]["Sales Invoice"] += 1
        with self.assertRaises(ValueError):
            committed(r, seed)

    def test_valuation_drift_changes_financial_signature(self):
        r, seed = example()
        sig = committed(r, seed)
        stock = r["after"]["db"]["sales"][0]["stock"][0]
        stock["value_change"] = str(Decimal(stock["value_change"]) - Decimal("0.001"))
        self.assertNotEqual(sig, committed(r, seed))

    def test_swapped_invoice_costs_preserve_totals_but_fail_paired_signature(self):
        r, seed = example()
        a,b = r["after"]["db"]["sales"][:2]
        for sale,cost in ((a,"1"),(b,"2")):
            sale["stock"][0]["value_change"] = "-" + cost
            sale["accounts"].extend([
                {"account":"COGS","debit":cost,"credit":"0","party_type":None,"party":None},
                {"account":"Inventory","credit":cost,"debit":"0","party_type":None,"party":None}])
            sale["gl"]["entries"] += 2
            for f in ("debit","credit"):
                sale["gl"][f] = str(Decimal(sale["gl"][f])+Decimal(cost))
        r["after"]["db"]["counts"]["GL Entry"] += 4
        initial = committed(r,seed)
        a["stock"][0],b["stock"][0] = b["stock"][0],a["stock"][0]
        a["accounts"][-2:],b["accounts"][-2:] = b["accounts"][-2:],a["accounts"][-2:]
        a["gl"],b["gl"] = b["gl"],a["gl"]
        changed = committed(r,seed)
        self.assertEqual(initial["stock_by_warehouse_item"], changed["stock_by_warehouse_item"])
        self.assertEqual(initial["gl_by_account_party"], changed["gl_by_account_party"])
        self.assertNotEqual(initial["sales"][a["id"]], changed["sales"][a["id"]])
        self.assertNotEqual(initial["sales"][b["id"]], changed["sales"][b["id"]])


if __name__ == "__main__":
    unittest.main()
