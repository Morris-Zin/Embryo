import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import posting_evidence as pe


def row(table, **values):
    return {field: values.get(field) for field in pe.FIELDS[table]}


def fixture_data(prefix, native=False, lost=True):
    actor = "cashier@example.invalid"
    events, raw = [], {table:[] for table in pe.FIELDS}
    for kind,doctype in pe.TYPES.items():
        cid = f"K6S-seed-mix-{kind}-m0-s0"
        name = prefix + kind
        purchase = kind in {"purchase_receipt", "purchase_invoice"}
        base = {"type":kind,"line_count":1,"actor_user":actor,"basket_hash":"same",
                "phase":"baseline","client_voucher_id":cid}
        events += [dict(base,event="voucher_start"),dict(base,event="voucher_complete",voucher_no=name,
                   attempts=1 if purchase else (2 if lost else 1), retried=lost, lost_ack=lost, retry_confirmed=lost,
                   recovery_attempted=lost and purchase, recovery_reads=2 if lost and purchase else 0,
                   reconciled=lost and purchase,
                   resubmitted=(lost and not purchase),
                   first_committed_voucher=name if lost else "", submit_method="frappe.desk.form.save.savedocs")]
        raw[doctype].append(row(doctype,name=name,owner=actor,docstatus="1",supplier="Supplier",
                                 bill_no=cid,remarks="Khitmee seasonal client_voucher_id="+cid))
        child = pe.CHILD[kind]
        quantity = "-2" if kind=="sale" else "2"
        raw[child].append(row(child,name=name+"-line",parent=name,parenttype=doctype,idx="1",item_code="A",
                              qty="2",stock_qty="2",rate="4.00",amount="8.00",warehouse="W",uom="Each",
                              s_warehouse="FROM",t_warehouse="TO",
                              purchase_receipt=prefix+"purchase_receipt",pr_detail=prefix+"purchase_receipt-line"))
        if kind!="transfer":
            for party,debit,credit in (("Supplier" if kind=="purchase_invoice" else None,"0.00","8.00"),(None,"8.00","0.00")):
                raw[pe.GL].append(row(pe.GL,name=name+"-gl-"+credit,voucher_no=name,voucher_type=doctype,
                                      party_type=party,party=party,debit=debit,credit=credit,is_cancelled="0"))
        if kind!="purchase_invoice":
            movements = [("FROM","-2"),("TO","2")] if kind=="transfer" else [("W",quantity)]
            for wh,qty in movements:
                raw[pe.SLE].append(row(pe.SLE,name=name+"-"+wh,voucher_type=doctype,voucher_no=name,
                                      voucher_detail_no=name+"-line",item_code="A",actual_qty=qty,
                                      posting_datetime="2026-11-27 00:00:00", creation="creation-"+kind,
                                      warehouse=wh,stock_value_difference="4.0000",is_cancelled="0"))
        if kind in ("sale","transfer") and not native:
            raw[pe.IDEM].append(row(pe.IDEM,reference_doctype=doctype,reference_name=name,
                                     idempotency_key=f"{actor}:{kind}:{cid}",request_fingerprint="hash"))
        if kind=="sale":
            raw[pe.PAYMENT].append(row(pe.PAYMENT,name=name+"-cash",parent=name,parenttype=doctype,idx="1",
                                      amount="8.00",mode_of_payment="Cash",account="Cash"))
    image = {"inventory":{t:[1,"1","2","3","4"] for t in pe.FIELDS},
             "bins":{"count":1,"sha256":"1"*64},"db_container":"db-"+prefix}
    return events,raw,image


class PostingEvidenceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ref = self.tmp / "stock.json.gz"
        self.contract = {"backend_image_id":"sha256:"+"b"*64,"controllers":{p:"f"*64 for p in pe.CONTROLLERS},
                         "upstream_revision":"e"*40}
        self.source = {k:v for k,v in self.contract.items() if k!="upstream_revision"}
        self.prepare("stock", True)
        with patch.object(pe,"stock_controller_evidence",return_value=self.source):
            pe.capture_stock_reference(self.req,{},self.msg,self.events,self.contract,self.ref)
        self.prepare("candidate",False)

    def prepare(self, role, native):
        folder = self.tmp / role
        folder.mkdir()
        self.req = {"arm":"baseline" if native else "candidate","commit":"a"*40,"baseline_commit":"a"*40,
                    "fixture_sha256":"2"*64,"seed":123,"artifacts_dir":str(folder)}
        self.msg = {"fixture_sha256":self.req["fixture_sha256"],"namespace":"khitmee-arm-"+("a" if native else "b")*16}
        self.events,self.rows,self.image = fixture_data(role,native=native,lost=not native)
        if hasattr(self,"db"):
            self.db.stop()
        self.db = patch.object(pe,"database_image", side_effect=lambda *args:(self.image,self.rows))
        self.db.start()
        self.addCleanup(self.db.stop)
        pe.capture_before(self.req,{},self.msg)

    def capture(self):
        after = Path(self.req["artifacts_dir"])/"posting-after.json"
        after.unlink(missing_ok=True)
        return pe.capture(self.req,{},self.msg,self.events,self.ref)

    def write_reference(self, data):
        with gzip.open(self.ref,"wt",encoding="utf-8") as out:
            json.dump(data,out,sort_keys=True,separators=(",",":"),allow_nan=False)

    def test_all_classes_independently_match_with_real_retry_shape(self):
        doc = self.capture()
        verdict = pe.finance.verify_evidence_document(doc)
        self.assertTrue(verdict["ok"],verdict)
        self.assertEqual([o["proves_committed_retry"] for o in verdict["operations"]],[True]*4)

    def test_ordinary_success_cannot_claim_retry(self):
        for e in self.events:
            e["lost_ack"] = e["retried"] = False
        doc = self.capture()
        self.assertTrue(pe.finance.verify_evidence_document(doc)["ok"])
        self.assertTrue(all(o["mode"]=="committed_posting" and "retry" not in o for o in doc["operations"]))

    def test_full_season_refuses_diagnostic_schedule(self):
        self.req["workload"]="seasonal-v1"
        with self.assertRaisesRegex(ValueError,"diagnostic posting order"):
            self.capture()

    def test_missing_receipt_link_refused(self):
        self.rows[pe.CHILD["purchase_invoice"]][0]["pr_detail"] = "foreign"
        with self.assertRaisesRegex(ValueError,"incorrect receipt line"):
            self.capture()

    def test_extra_rejected_post_is_detected_by_historical_inventory(self):
        original = self.events[0]
        new = dict(original,phase="overload",client_voucher_id="K6S-rejected")
        self.events += [new,dict(new,event="voucher_reject")]
        pe.index_events(self.events)
        self.image["inventory"]["Purchase Invoice"][0] += 1
        with self.assertRaisesRegex(ValueError,"unacknowledged posting"):
            self.capture()

    def test_duplicate_ack_and_wrong_replay_origin(self):
        with self.assertRaisesRegex(ValueError,"repeated voucher"):
            pe.index_events(self.events + [self.events[1]])
        self.events[1]["first_committed_voucher"] = "unrelated"
        with self.assertRaisesRegex(ValueError,"original committed retry"):
            self.capture()

    def test_oracle_catches_one_cent_corruption_and_same_arm_refused(self):
        self.rows[pe.PAYMENT][0]["amount"] = "8.01"
        self.assertFalse(pe.finance.verify_evidence_document(self.capture())["ok"])
        self.msg["namespace"] = "khitmee-arm-"+"a"*16
        with self.assertRaisesRegex(ValueError,"independent stock source"):
            self.capture()

    def test_reference_binding_and_duplicate_keys_fail_closed(self):
        original = pe.read_json(self.ref)
        for field,value in (("schema","wrong"),("fixture_sha256","3"*64),("seed",124),("baseline_commit","b"*40)):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                self.write_reference(changed)
                with self.assertRaisesRegex(ValueError,"reference format, fixture, revision or seed mismatch"):
                    self.capture()
        self.ref.write_text('{"schema":"x","schema":"x"}',encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"duplicate object key"):
            self.capture()

    def test_pre_run_inventory_is_required_and_arm_bound(self):
        path = Path(self.req["artifacts_dir"])/pe.BEFORE
        before = pe.read_json(path)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.capture()
        foreign = copy.deepcopy(before)
        foreign["arm"] = dict(foreign["arm"],namespace="khitmee-arm-"+"c"*16)
        path.write_text(json.dumps(foreign),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"pre-run financial inventory"):
            self.capture()

    def test_whole_run_bin_and_order_are_bound_to_reference(self):
        original = pe.read_json(self.ref)
        for field,reason in (("order","posting order"),("bin","Bin snapshots")):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = {}
                self.write_reference(changed)
                with self.assertRaisesRegex(ValueError,reason):
                    self.capture()

    def test_start_and_outcome_identity_mismatch_refused(self):
        self.events[1]["basket_hash"] = "different"
        with self.assertRaisesRegex(ValueError,"start and outcome identity disagree"):
            self.capture()

    def test_posting_actor_and_client_reservation_are_enforced(self):
        original = copy.deepcopy(self.rows)
        sale = pe.TYPES["sale"]
        self.rows[sale][0]["owner"] = "other@example.invalid"
        with self.assertRaisesRegex(ValueError,"posting actor mismatch"):
            self.capture()
        self.rows = copy.deepcopy(original)
        self.rows[pe.IDEM] = [r for r in self.rows[pe.IDEM] if r["reference_doctype"] != sale]
        with self.assertRaisesRegex(ValueError,"missing or mismatched client reservation"):
            self.capture()

    def test_sql_is_transactional_and_covers_every_table_even_empty_names(self):
        sql = pe.sql_capture({k:{} for k in pe.TYPES})
        self.assertTrue(sql.startswith("SET TRANSACTION READ ONLY;"))
        self.assertIn("START TRANSACTION WITH CONSISTENT SNAPSHOT",sql)
        self.assertTrue(sql.endswith("ROLLBACK;\n"))
        for t in pe.FIELDS:
            self.assertIn("`tab"+t+"`",sql)


class GuestBundleContractTest(unittest.TestCase):
    def test_stock_controller_query_uses_real_guest_guard(self):
        import seasonal_driver
        contract = {"controllers":{p:"f"*64 for p in pe.CONTROLLERS},"backend_image_id":"sha256:"+"b"*64,
                    "upstream_revision":"f"*40}
        seen = {k:v for k,v in contract.items() if k != "upstream_revision"}
        with patch.object(seasonal_driver,"remote",return_value=seen) as ssh:
            self.assertEqual(pe.stock_controller_evidence({}, {}, contract), seen)
            code=ssh.call_args.args[2]
            self.assertIn("def validate_telemetry_window(", code)
            compile(code,"<protected-stock-query>","exec")


if __name__ == "__main__":
    unittest.main()
