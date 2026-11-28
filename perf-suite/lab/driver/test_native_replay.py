import copy
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import native_replay as replay
import posting_evidence as evidence
import perf_parity
from test_posting_evidence import fixture_data


def document_events():
    events,_,image=fixture_data("src",lost=False)
    item={"item_code":"A","qty":2,"uom":"Each"}
    for e in events:
        e["basket_hash"]=replay.basket_hash([item])
        if e["event"]=="voucher_start":
            inputs={"company":"Co","currency":"USD","items":[item.copy()]}
            e.update(warehouse="W",target_warehouse="T",supplier="Supplier",replay_input=inputs)
            if e["type"]=="sale":
                inputs.update(customer="Customer",price_list="Retail",payment_mode="Cash")
            if e["type"].startswith("purchase"):
                inputs["items"][0]["rate"]=4.00
                inputs["posting_date"]="2026-11-27"
    req={"seed":11,"fixture_sha256":"e"*64,"baseline_commit":"4"*40}
    return events,{"request":req,"arm":{"namespace":"orig","fixture_sha256":req["fixture_sha256"]},"image":image}


class NativeReplayTest(unittest.TestCase):
    def test_extraction_rejects_missing_inputs_swapped_schedule_and_mismatched_oracle(self):
        ev,before=document_events()
        incomplete=copy.deepcopy(ev)
        del incomplete[0]["replay_input"]
        with self.assertRaisesRegex(ValueError,"missing original"):
            replay.extract(incomplete,before,"2026-11-27")
        ids=[e["client_voucher_id"] for e in ev if e["event"]=="voucher_complete"]
        with self.assertRaisesRegex(ValueError,"precede"):
            replay.extract(ev,before,"2026-11-27",ids[::-1])
        altered=copy.deepcopy(ev)
        altered[0]["replay_input"]["items"][0]["qty"]=3
        with self.assertRaisesRegex(ValueError,"fingerprint"):
            replay.extract(altered,before,"2026-11-27")
        with self.assertRaisesRegex(ValueError,"day"):
            replay.extract(ev,before,"2026-11-28")

    def test_ledger_order_checks_real_per_item_sequence(self):
        names={"Sales Invoice":{"I1":"C1","I2":"C2"}}
        row=dict(item_code="A",warehouse="W",actual_qty="-1",voucher_type="Sales Invoice",posting_datetime="2026-11-27")
        rows=[dict(row,creation="01",voucher_no="I1"),dict(row,creation="02",voucher_no="I2")]
        ref=evidence.ledger_order({evidence.SLE:rows},names)
        self.assertEqual(ref,evidence.ledger_order({evidence.SLE:rows[::-1]},names))
        changed=copy.deepcopy(rows)
        changed[0]["creation"]="03"
        self.assertNotEqual(ref,evidence.ledger_order({evidence.SLE:changed},names))
        changed[0]["creation"]="02"
        with self.assertRaisesRegex(ValueError,"ambiguous"):
            evidence.ledger_order({evidence.SLE:changed},names)

    def test_verified_source_reuses_a_compatible_partial_order(self):
        ev,before=document_events()
        ids=[e["client_voucher_id"] for e in ev if e["event"]=="voucher_complete"]
        source_order=[ids[2],ids[3],ids[1],ids[0]]
        after={"arm":before["arm"],"image":before["image"],"completed":len(ids),
               "completed_ids_sha256":replay.sha(sorted(ids)),"compatible_serial_order":source_order,
               "ledger_order":{"method":"posted-sle-per-warehouse-item-v1","movements":3,"streams":2,"sha256":"c"*64}}
        plan=replay.extract(ev,before,"2026-11-27",after=after)
        self.assertEqual([r["client_voucher_id"] for r in plan["operations"]],source_order)
        self.assertEqual(plan["expected_ledger_order"],after["ledger_order"])
        after["compatible_serial_order"]=None
        with self.assertRaisesRegex(ValueError,"compatible serial schedule"):
            replay.extract(ev,before,"2026-11-27",after=after)

    def test_native_document_builders_run_all_four_types_as_named_actor(self):
        ev,before=document_events()
        plan=replay.extract(ev,before,"2026-11-27")
        self.assertIsNone(plan["expected_ledger_order"])
        state=SimpleNamespace(docs=[],commits=0,identity=None,logins=[])
        class Row(dict):
            def __getattr__(self,key): return self[key]
            def __setattr__(self,key,val): self[key]=val
        class Doc:
            def __init__(self,data):
                self.__dict__.update(data)
                self.items=[Row(i,name=f"detail{n}") for n,i in enumerate(self.items)]
                self.name=self.doctype+"-1"
                self.owner=state.identity
                self.docstatus=0
                self.flags=SimpleNamespace()
                self.payments=[]
                self.rounded_total=self.grand_total=8
            def append(self,field,value): getattr(self,field).append(Row(value))
            def set_missing_values(self): pass
            def calculate_taxes_and_totals(self): pass
            def insert(self): state.docs.append(self)
            def submit(self): self.docstatus=1
        frappe=ModuleType("frappe")
        auth=ModuleType("frappe.utils.password")
        auth.check_password=lambda name,pw: state.logins.append((name,pw))
        frappe.utils=SimpleNamespace(nowdate=lambda:"date",nowtime=lambda:"time")
        frappe.init=lambda **kwargs:None
        frappe.connect=frappe.destroy=lambda:None
        frappe.conf=SimpleNamespace(db_host="db")
        frappe.db=SimpleNamespace(commit=lambda:setattr(state,"commits",state.commits+1),rollback=lambda:None,exists=lambda *args:True)
        frappe.set_user=lambda actor:setattr(state,"identity",actor)
        frappe.get_doc=lambda data,*args:SimpleNamespace(company="Co",is_group=False) if args else Doc(data)
        with patch.dict(sys.modules,{"frappe":frappe,"frappe.utils.password":auth}), \
             patch.object(replay,"build_classic_sale",perf_parity.build_classic_sale,create=True), \
             patch.object(replay,"build_classic_transfer",perf_parity.build_classic_transfer,create=True):
            events=replay.post_native(plan,{"cashier@example.invalid":"test-only"})
        self.assertEqual(state.commits,4)
        self.assertEqual([d.docstatus for d in state.docs],[1]*4)
        self.assertTrue(all(d.owner=="cashier@example.invalid" for d in state.docs))
        self.assertEqual(state.docs[-1].items[0]["pr_detail"],state.docs[-2].items[0]["name"])
        self.assertTrue(all(e["submit_method"].endswith("Document.submit") for e in events if e["event"]=="voucher_complete"))


if __name__=="__main__":
    unittest.main()
