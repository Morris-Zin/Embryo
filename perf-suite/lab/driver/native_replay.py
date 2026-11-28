#!/usr/bin/env python3
"""Extract recorded seasonal requests and issue a native stock replay on one fresh baseline arm."""

import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess
import sys

import posting_evidence as evidence
from posting_evidence import need, canonical
import perf_parity

SCHEMA = "khitmee-native-replay-plan-v1"


def sha(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def read_pinned(path, expected):
    from khitmee_lab_adapter import file_hash
    need(file_hash(path) == expected, "pinned input changed: " + path.name)
    return evidence.read_json(path)


def stock_source(checkout,revision,image):
    need(re.fullmatch("[0-9a-f]{40}",revision) and re.fullmatch("sha256:[0-9a-f]{64}",image),
         "expected full upstream revision and backend image digest")
    hashes = {}
    for name in evidence.CONTROLLERS:
        result = subprocess.run(["git","-C",str(checkout),"show",revision+":erpnext/"+name],
                                capture_output=True,check=True)
        hashes[name] = hashlib.sha256(result.stdout).hexdigest()
    return {"upstream_revision":revision,"backend_image_id":image,"controllers":hashes}


def basket_hash(items):
    text = "|".join(f"{r['item_code']}:{format(Decimal(str(r['qty'])).normalize(),'f')}:{r['uom']}" for r in items)
    def fnv(s):
        value = 2166136261
        data = s.encode("utf-16-le")
        for index in range(0,len(data),2):
            value = ((value ^ int.from_bytes(data[index:index+2],"little")) * 16777619) & 0xffffffff
        return f"{value:08x}"
    return fnv(text) + fnv("second:"+text)


def opening(image):
    return sha({"inventory":image["inventory"],"bins":image["bins"]})


def replay_row(cid,start):
    inputs = start.get("replay_input")
    need(isinstance(inputs,dict),"missing original request input for "+cid)
    kind = start["type"]
    need(kind in evidence.TYPES,"unknown native posting kind")
    purchase = kind.startswith("purchase")
    fields = {"company","currency","items"}
    fields |= {"customer","price_list","payment_mode"} if kind=="sale" else {"posting_date"} if purchase else set()
    need(set(inputs)==fields and all(isinstance(inputs[f],str) and inputs[f] for f in fields-{"items"}),
         "unexpected or missing request field for "+cid)
    items = inputs["items"]
    need(isinstance(items,list) and len(items) == start["line_count"] > 0, "line count mismatch for "+cid)
    for item in items:
        need(set(item)==({"item_code","uom","qty","rate"} if purchase else {"item_code","uom","qty"})
             and all(isinstance(item[f],str) and item[f] for f in ("item_code","uom")), "invalid purchase/sale line")
        for f in (("qty","rate") if purchase else ("qty",)):
            value = Decimal(str(item[f]))
            need(value.is_finite() and value>0, "invalid decimal in recorded request")
    need(basket_hash(items)==start["basket_hash"], "recorded input differs from basket fingerprint "+cid)
    actor = start.get("actor_user")
    need(isinstance(actor,str) and actor and actor.casefold() not in ("administrator","guest"), "missing restricted actor")
    need(isinstance(start["warehouse"],str) and start["warehouse"], "missing warehouse")
    out = {"client_voucher_id":cid,"type":kind,"actor_user":actor,"basket_hash":start["basket_hash"],
           "line_count":len(items),"phase":start["phase"],"warehouse":start["warehouse"],"input":inputs}
    if kind == "transfer":
        need(start["target_warehouse"] and start["warehouse"] != start["target_warehouse"], "missing transfer destination")
        out["target_warehouse"] = start["target_warehouse"]
    if purchase:
        need(start["supplier"],"missing purchase supplier")
        out["supplier"] = start["supplier"]
    return out


def extract(events, before, posting_date, ordered_ids=None, after=None):
    done,_ = evidence.index_events(events)
    starts = {e["client_voucher_id"]:e for e in events if e.get("event")=="voucher_start"}
    if after is None:
        order = [cid for cid in starts if cid in done]
        source = "external start event order"
    else:
        order = after.get("compatible_serial_order")
        need(order is not None, "observed ledger dependencies lack a compatible serial schedule")
        source = "observed per-item ledger and receipt dependencies"
    if ordered_ids is not None:
        order = ordered_ids
        source = "explicit list"
    need(isinstance(order,list) and len(order)==len(set(order))==len(done) and set(order)==set(done),
         "explicit order must contain each committed client ID once")
    need(len(order)<86400, "serial replay needs a distinct posting second inside the declared day")
    req = before["request"]
    need(before["arm"]["fixture_sha256"]==req["fixture_sha256"],"unbound original inventory")
    stamp = datetime.combine(date.fromisoformat(posting_date),time())
    records, prior = [], set()
    for slot,cid in enumerate(order):
        record = replay_row(cid,starts[cid])
        kind = record["type"]
        if kind.startswith("purchase"):
            need(record["input"]["posting_date"]==posting_date, "ordered replay day differs from original purchase date")
        if kind=="purchase_invoice":
            receipt = cid.replace("-purchase_invoice-","-purchase_receipt-")
            need(receipt != cid and receipt in prior, "linked receipt must precede the invoice")
            record["receipt_client_id"] = receipt
        record["posting_time"] = (stamp+timedelta(seconds=slot)).time().isoformat()
        prior.add(cid)
        records.append(record)
    if after is not None:
        need(after["arm"]==before["arm"] and after["image"]["inventory"]==before["image"]["inventory"]
             and after["image"]["db_container"]==before["image"]["db_container"] and after["completed"]==len(order)
             and after["completed_ids_sha256"]==sha(sorted(done)), "original closing receipt does not describe the input set")
        need(after["ledger_order"]["method"]=="posted-sle-per-warehouse-item-v1"
             and after["ledger_order"]["movements"]>0,"missing observed economic posting order")
    return {"schema":SCHEMA,"fixture_sha256":req["fixture_sha256"],"seed":req["seed"],"baseline_commit":req["baseline_commit"],
            "source_namespace":before["arm"]["namespace"],"opening_sha256":opening(before["image"]),"posting_date":posting_date,
            "order_source":source,
            "expected_ledger_order":after["ledger_order"] if after is not None else None,"operations":records}


def post_native(plan,passwords):
    """Executed inside the disposable benchmark backend with trusted stock builders."""
    import frappe
    from frappe.utils.password import check_password
    frappe.init(site="bench-loadtest.localhost",sites_path="/home/frappe/frappe-bench/sites")
    frappe.connect()
    if frappe.conf.db_host != "db":
        raise ValueError("unexpected stock replay DB host")
    logged_in = set()
    receipts = {}
    events = []
    try:
        for op in plan["operations"]:
            cid,actor,kind,inputs = (op[f] for f in ("client_voucher_id","actor_user","type","input"))
            if actor not in logged_in:
                check_password(actor,passwords[actor])
                logged_in.add(actor)
            frappe.set_user(actor)
            marker = "Khitmee seasonal client_voucher_id="+cid
            fixed = {"posting_date":plan["posting_date"],"posting_time":op["posting_time"],"set_posting_time":1,"remarks":marker}
            # Normal ERP permissions and stock document validation run under the recorded actor.
            class StockDocument:
                utils = frappe.utils
                @staticmethod
                def get_doc(data):
                    return frappe.get_doc(dict(data,**fixed))
            class Inputs:
                @staticmethod
                def _validate_items(rows):
                    return rows
                @staticmethod
                def _validate_warehouse(name,label):
                    wh = frappe.get_doc("Warehouse",name)
                    if wh.company != inputs["company"] or wh.is_group:
                        raise ValueError("wrong native warehouse")
                    return wh
                @staticmethod
                def _validate_sale_inputs(customer,price_list,payment_mode):
                    for dt,n in (("Customer",customer),("Price List",price_list),("Mode of Payment",payment_mode)):
                        if not frappe.db.exists(dt,n):
                            raise ValueError("missing native sale input")
                @staticmethod
                def _sales_invoice_payable_total(doc):
                    return doc.rounded_total or doc.grand_total

            tags = {f:op[f] for f in ("client_voucher_id","type","basket_hash","line_count","actor_user","phase")}
            events.append(dict(tags,event="voucher_start"))
            if kind=="sale":
                doc = build_classic_sale(StockDocument, Inputs, dict(inputs,warehouse=op["warehouse"]))()
            elif kind=="transfer":
                doc = build_classic_transfer(StockDocument,Inputs,dict(inputs,source=op["warehouse"],target=op["target_warehouse"]))()
            else:
                invoice = kind=="purchase_invoice"
                receipt = receipts[op["receipt_client_id"]] if invoice else None
                items = []
                for index,line in enumerate(inputs["items"]):
                    row = dict(line,warehouse=op["warehouse"])
                    if invoice:
                        ref = receipt["items"][index]
                        if ref["item_code"] != line["item_code"] or ref["uom"] != line["uom"]:
                            raise ValueError("ordered purchase lines differ from the native receipt")
                        row.update(purchase_receipt=receipt["name"],pr_detail=ref["name"])
                    items.append(row)
                data = dict(doctype="Purchase Invoice" if invoice else "Purchase Receipt", supplier=op["supplier"],
                            company=inputs["company"], currency=inputs["currency"], conversion_rate=1, items=items)
                if invoice:
                    data.update(bill_no=cid,bill_date=plan["posting_date"],due_date=plan["posting_date"],update_stock=0)
                else:
                    data["set_warehouse"] = op["warehouse"]
                doc = StockDocument.get_doc(data)
                doc.insert()
                doc.submit()
                if not invoice:
                    receipts[cid] = {"name":doc.name,"items":[{f:r.get(f) for f in ("name","item_code","uom")} for r in doc.items]}
            if doc.docstatus!=1 or doc.owner!=actor or str(doc.posting_date)!=plan["posting_date"]:
                raise ValueError("native posted document identity/date mismatch")
            frappe.db.commit()
            events.append(dict(tags,event="voucher_complete",voucher_no=doc.name,attempts=1,lost_ack=False,
                               retried=False,submit_method="frappe.model.document.Document.submit"))
        return events
    finally:
        frappe.db.rollback()
        frappe.destroy()


def arm(envelope):
    from seasonal_driver import adapter
    req = adapter.validate_request(envelope["request"])
    target = envelope["environment"]["ssh"]
    provision = envelope["provision"]
    message = {"target":envelope["environment"]["target"],"fixture_sha256":req["fixture_sha256"],"namespace":provision["namespace"]}
    need(req["fixture_sha256"]==provision["fixture_sha256"]
         and provision["restored_from_immutable_fixture"] is True and provision["namespace"]==adapter.arm_namespace(req)
         and target["host"]=="khitmee-lab","expected isolated disposable arm")
    return req,target,message


def validate_plan(plan):
    need(plan.get("schema")==SCHEMA and isinstance(plan.get("operations"),list) and plan["operations"],
         "empty or unsupported stock plan")
    seen=set()
    stamp=datetime.combine(date.fromisoformat(plan["posting_date"]),time())
    for index,record in enumerate(plan["operations"]):
        cid=record["client_voucher_id"]
        need(isinstance(cid,str) and re.fullmatch(r"K6S-[A-Za-z0-9_.:-]{1,139}",cid) and cid not in seen,
             "invalid replay client ID")
        seen.add(cid)
        source=dict(record,replay_input=record["input"])
        rebuilt=replay_row(cid,source)
        if record["type"]=="purchase_invoice":
            receipt=cid.replace("-purchase_invoice-","-purchase_receipt-")
            need(receipt!=cid and receipt in seen,"missing earlier purchase receipt in stock plan")
            rebuilt["receipt_client_id"]=receipt
        rebuilt["posting_time"]=(stamp+timedelta(seconds=index)).time().isoformat()
        need(rebuilt==record,"invalid stock replay fields or chronology")
    if plan.get("expected_ledger_order") is not None:
        order=plan["expected_ledger_order"]
        need(order["method"]=="posted-sle-per-warehouse-item-v1" and order["movements"]>0 and order["streams"]>0
             and re.fullmatch("[0-9a-f]{64}",order["sha256"]),"invalid source ordering pin")


def run(envelope,plan,passwords,stock_contract,output):
    from seasonal_driver import artifact,remote
    from seasonal_telemetry import _guest_script
    req,target,message = arm(envelope)
    validate_plan(plan)
    need(req["arm"]=="baseline" and req["commit"]==req["baseline_commit"], "expected independent clean baseline")
    need(plan["schema"]==SCHEMA and all(plan[f]==req[f] for f in ("seed","fixture_sha256","baseline_commit"))
         and plan["source_namespace"]!=message["namespace"],"stock replay does not match the independent source")
    need(not output.exists() and set(passwords)=={r["actor_user"] for r in plan["operations"]}
         and all(isinstance(p,str) and p for p in passwords.values()),"missing restricted actor or output already exists")
    evidence.stock_controller_evidence(target,message,stock_contract)
    evidence.capture_before(req,target,message)
    folder = Path(req["artifacts_dir"])
    before = evidence.read_json(folder/evidence.BEFORE)
    need(opening(before["image"])==plan["opening_sha256"], "fresh arm financial opening differs from recorded source")
    script = "\n".join(inspect.getsource(f) for f in (perf_parity.build_classic_sale,perf_parity.build_classic_transfer,post_native))
    guest = _guest_script() + """
project,_ = validate_telemetry_window(message)
backend = project + "-backend-1"
require(inspect(backend)["Config"]["Labels"]["com.docker.compose.project"] == project,"wrong stock backend")
source = "import json\\n" + message["program"] + "\\n"
source += "plan,passwords = json.loads(" + repr(json.dumps([message["plan"],message["auth"]])) + ")\\n"
source += "print('NATIVE_STOCK_EVENTS ' + json.dumps(post_native(plan,passwords)))\\n"
out = command("docker","exec","-i","-w",BENCH+"/sites",backend,BENCH+"/env/bin/python","-IB","-",data=source.encode(),timeout=18000)
lines = [line[20:] for line in out.splitlines() if line.startswith(b"NATIVE_STOCK_EVENTS ")]
require(len(lines)==1,"incomplete native stock event trace")
print(lines[0].decode())
"""
    events = remote(target,dict(message,program=script,plan=plan,auth=passwords),guest,suffix="",timeout=18100)
    artifact(folder,"native-stock-events.json",events)
    pin = evidence.capture_stock_reference(req,target,message,events,stock_contract,output,
                                          expected_order=plan["expected_ledger_order"])
    artifact(folder,"native-stock-reference.json",dict(pin,plan_sha256=sha(plan),source_order=plan["order_source"]))
    return pin


def main():
    from seasonal_driver import adapter
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest="command",required=True)
    pin=subs.add_parser("pin-stock")
    pin.add_argument("--upstream-checkout",type=Path,required=True)
    pin.add_argument("--upstream-revision",required=True)
    pin.add_argument("--backend-image-id",required=True)
    pin.add_argument("--output",type=Path,required=True)
    for key in ("source-before","source-after"):
        observe = subs.add_parser(key)
        observe.add_argument("--envelope",type=Path,required=True)
        if key=="source-after":
            observe.add_argument("--events",type=Path,required=True)
    extract_cmd=subs.add_parser("extract")
    for key in ("events","source-before","output"):
        extract_cmd.add_argument("--"+key,type=Path,required=True)
    extract_cmd.add_argument("--source-after",type=Path)
    extract_cmd.add_argument("--ordered-ids",type=Path)
    extract_cmd.add_argument("--posting-date",required=True)
    replay_cmd=subs.add_parser("run")
    for key in ("envelope","plan","actor-passwords","stock-source","output"):
        replay_cmd.add_argument("--"+key,type=Path,required=True)
    for key in ("plan","stock-source"):
        replay_cmd.add_argument("--"+key+"-sha256",required=True)
    args=parser.parse_args()
    if args.command=="pin-stock":
        contract=stock_source(args.upstream_checkout,args.upstream_revision,args.backend_image_id)
        with args.output.open("xb") as dest:
            dest.write(canonical(contract)+b"\n")
        print(json.dumps({"path":str(args.output.resolve()),"sha256":adapter.file_hash(args.output)}))
    elif args.command in ("source-before", "source-after"):
        req,target,message = arm(evidence.read_json(args.envelope))
        if args.command=="source-before":
            print(json.dumps(evidence.capture_before(req,target,message)))
        else:
            evidence.collect_run(req,target,message,evidence.read_json(args.events))
            print(json.dumps({"artifact":"posting-after.json"}))
    elif args.command=="extract":
        plan=extract(evidence.read_json(args.events),evidence.read_json(args.source_before),args.posting_date,
                     evidence.read_json(args.ordered_ids) if args.ordered_ids else None,
                     evidence.read_json(args.source_after) if args.source_after else None)
        with args.output.open("xb") as dest:
            dest.write(canonical(plan)+b"\n")
        print(json.dumps({"path":str(args.output.resolve()),"sha256":adapter.file_hash(args.output),
                          "order_source":plan["order_source"],"operations":len(plan["operations"])}))
    else:
        pin=run(evidence.read_json(args.envelope),read_pinned(args.plan,args.plan_sha256),evidence.read_json(args.actor_passwords),
                read_pinned(args.stock_source,args.stock_source_sha256),args.output)
        print(json.dumps(pin))


if __name__=="__main__":
    try:
        main()
    except (ValueError,KeyError,OSError,subprocess.SubprocessError) as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(2)
