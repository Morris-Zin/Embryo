"""Read committed rows from the isolated arm and join them to a pinned stock capture."""

import base64
from collections import defaultdict
from decimal import Decimal
import gzip
import graphlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[2] / "tools"), str(HERE.parents[1] / "seasonal")]
import financial_verifier as finance
import perf_parity as parity
import verify_run as seasonal

TYPES = seasonal.EXPECTED_DOCTYPE
DOC_FIELDS = {
    "sale": "customer company posting_date currency grand_total rounded_total paid_amount change_amount outstanding_amount net_total total_taxes_and_charges discount_amount update_stock is_pos",
    "transfer": "company posting_date stock_entry_type total_amount total_outgoing_value total_incoming_value",
    "purchase_receipt": "supplier company posting_date currency grand_total rounded_total net_total total_taxes_and_charges discount_amount",
    "purchase_invoice": "supplier company posting_date currency grand_total rounded_total outstanding_amount net_total total_taxes_and_charges discount_amount bill_no update_stock",
}
GL = "GL Entry"
SLE = "Stock Ledger Entry"
PLE = "Payment Ledger Entry"
IDEM = "Khitmee Idempotency Log"
PAYMENT = "Sales Invoice Payment"
CHILD = {kind: seasonal.CHILD_TABLE[doctype][3:] for kind, doctype in TYPES.items()}
FIELDS = {}
for kind, doctype in TYPES.items():
    FIELDS[doctype] = ("name", "owner", "docstatus", *DOC_FIELDS[kind].split())
    FIELDS[doctype] += ("remarks",)
    FIELDS[CHILD[kind]] = ("name", "parent", "parenttype", "idx", "uom", "conversion_factor",
                          *(finance.TRANSFER_ITEM_ROW_FIELDS if kind == "transfer" else finance.SALE_ITEM_ROW_FIELDS))
FIELDS[CHILD["purchase_invoice"]] += ("purchase_receipt", "pr_detail")
FIELDS[PAYMENT] = ("name", "parent", "parenttype", "idx", *finance.PAYMENT_ROW_FIELDS)
FIELDS[GL] = ("name", "voucher_type", "voucher_no", "company", "posting_date", *finance.GL_ROW_FIELDS[:-1],
              "against_voucher", "voucher_detail_no")
FIELDS[SLE] = ("name", "voucher_type", "voucher_no", "company", "posting_date", *finance.SLE_ROW_FIELDS[:-1],
               "voucher_detail_no", "posting_datetime", "creation")
FIELDS[PLE] = ("name", "voucher_type", "voucher_no", "account", "account_type", "party_type", "party",
               "company", "posting_date", "account_currency", "amount", "amount_in_account_currency",
               "against_voucher_type", "against_voucher_no", "delinked")
FIELDS[IDEM] = ("name", "idempotency_key", "request_fingerprint", "reference_doctype", "reference_name")
BEFORE = "posting-before.json"
SCHEMA = "khitmee-stock-posting-capture-v1"


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_json(path):
    def unique(pairs):
        result = dict(pairs)
        need(len(result) == len(pairs), "duplicate object key in posting evidence")
        return result
    path = Path(path)
    with path.open("rb") as raw:
        if raw.peek(2)[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=raw) as stream:
                return json.load(stream, object_pairs_hook=unique)
        return json.load(raw, object_pairs_hook=unique)


def literal(text):
    # Hex literals avoid differences in server backslash/ANSI quoting modes.
    return "CONVERT(X'" + text.encode().hex() + "' USING utf8mb4)"


def members(column, values):
    return column + " IN (" + ",".join(literal(v) for v in sorted(values)) + ")" if values else "FALSE"


def rows_predicate(table, names):
    for kind, doctype in TYPES.items():
        if table == doctype:
            return members("name", names[kind])
        if table == CHILD[kind] or (kind == "sale" and table == PAYMENT):
            return "(parenttype=" + literal(doctype) + " AND " + members("parent", names[kind]) + ")"
    doctype_field, name_field = ("reference_doctype", "reference_name") if table == IDEM else ("voucher_type", "voucher_no")
    return "(" + " OR ".join("(" + doctype_field + "=" + literal(dt) + " AND " + members(name_field, names[k]) + ")"
                             for k, dt in TYPES.items()) + ")"


def sql_capture(names):
    statements = ["SET TRANSACTION READ ONLY", "START TRANSACTION WITH CONSISTENT SNAPSHOT"]
    for table, fields in FIELDS.items():
        values = ",".join("CAST(`" + field + "` AS CHAR)" for field in fields)
        selected = rows_predicate(table, names) if names is not None else "FALSE"
        # A count and four exact 64-bit limb sums from SHA256 cover the full historical
        # row multiset. The complement includes even an orphan with NULL voucher_no.
        limbs = [f"COALESCE(SUM(CAST(CONV(SUBSTRING(h,{start},16),16,10) AS DECIMAL(30,0))),0)"
                 for start in (1, 17, 33, 49)]
        statements.append(f"SELECT JSON_ARRAY('inventory',{literal(table)},COUNT(*),"
                          + ",".join("CAST(" + x + " AS CHAR)" for x in limbs)
                          + f") FROM (SELECT SHA2(JSON_ARRAY({values}),256) h FROM `tab{table}`"
                          + f" WHERE COALESCE(({selected}),0)=0) historical")
        if names is not None:
            statements.append(f"SELECT JSON_ARRAY('row',{literal(table)},{values}) FROM `tab{table}` WHERE {selected}")
    bin_fields = ",".join("CAST(`" + f + "` AS CHAR)" for f in ("item_code", "warehouse", *parity.BIN_FIELDS))
    statements += [f"SELECT JSON_ARRAY('bin',{bin_fields}) FROM `tabBin` ORDER BY BINARY item_code,BINARY warehouse",
                   "ROLLBACK"]
    return ";\n".join(statements) + ";\n"


GUEST_QUERY = r"""
import tempfile, gzip, shutil
project,_ = validate_telemetry_window(message)
info = inspect(project + "-db-1")
require(info["Config"]["Labels"]["com.docker.compose.project"] == project
        and any(m.get("Name") == project+"-db" and m.get("Destination")=="/var/lib/mysql" for m in info["Mounts"]),
        "posting database identity mismatch")
site = Path("/var/lib/docker/volumes")/(project+"-sites")/"_data"/SITE/"site_config.json"
db = json.loads(site.read_bytes())["db_name"]
require(re.fullmatch("[_A-Za-z0-9]+",db),"invalid isolated site database")
with tempfile.TemporaryFile() as raw, io.BytesIO() as packed:
    proc = subprocess.run(["docker","exec","-i",project+"-db-1","sh","-c",
                           'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mariadb --quick --batch --raw --skip-column-names -uroot "$1"',
                           "posting-evidence",db], input=message["sql"].encode(), stdout=raw, stderr=subprocess.PIPE,
                          timeout=1200)
    require(proc.returncode == 0,"isolated read-only posting query failed: " +
            next((line.split(" at line ")[0][:18] for line in proc.stderr.decode(errors="replace").splitlines()
                  if line.startswith("ERROR ")), "client exit "+str(proc.returncode)))
    raw.seek(0)
    with gzip.GzipFile(fileobj=packed,mode="wb",mtime=0) as out:
        shutil.copyfileobj(raw,out)
    print(json.dumps({"db_container":info["Id"],"gz":base64.b64encode(packed.getvalue()).decode()}))
"""


def database_image(target, message, names):
    from seasonal_driver import remote
    from seasonal_telemetry import _guest_script
    result = remote(target, dict(message, sql=sql_capture(names)), _guest_script(),
                    GUEST_QUERY, timeout=1290)
    inv, rows = {}, defaultdict(list)
    bins = hashlib.sha256()
    bin_count = 0
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(result["gz"], validate=True))) as data:
        for line in data:
            tag, *record = json.loads(line)
            if tag == "bin":
                bins.update(canonical(record) + b"\n")
                bin_count += 1
            elif tag == "inventory":
                table, *values = record
                need(table in FIELDS and table not in inv and len(values) == 5, "invalid financial inventory")
                inv[table] = values
            else:
                need(tag == "row" and record[0] in FIELDS, "unknown financial row")
                table, *values = record
                rows[table].append(dict(zip(FIELDS[table], values, strict=True)))
    need(set(inv) == set(FIELDS) and bin_count > 0, "incomplete financial inventory")
    return {"inventory": inv, "bins": {"count": bin_count, "sha256": bins.hexdigest()},
            "db_container": result["db_container"]}, rows


def capture_before(req, target, message):
    from seasonal_driver import artifact
    need(req["fixture_sha256"] == message["fixture_sha256"], "posting fixture mismatch")
    image, _ = database_image(target, message, None)
    return artifact(Path(req["artifacts_dir"]), BEFORE, {"request": req, "arm": message, "image": image})


def index_events(events):
    groups = {key: {} for key in ("voucher_start", "voucher_complete", "voucher_reject", "voucher_unknown")}
    names = {kind: {} for kind in TYPES}
    for event in events:
        category = event.get("event")
        if category not in groups:
            continue
        client = event["client_voucher_id"]
        kind = event["type"]
        need(re.fullmatch(r"K6S-[A-Za-z0-9_.:-]{1,139}", client) and kind in TYPES
             and client not in groups[category], "invalid or repeated voucher event")
        groups[category][client] = event
        if category == "voucher_complete":
            name = event["voucher_no"]
            need(isinstance(name, str) and 0 < len(name) <= 140 and name not in names[kind],
                 "missing or reused committed voucher")
            names[kind][name] = client
    started = groups["voucher_start"]
    done = groups["voucher_complete"]
    rejected = groups["voucher_reject"]
    unknown = groups["voucher_unknown"]
    terminal_sets = (set(done), set(rejected), set(unknown))
    need(done and set(started) == set().union(*terminal_sets) and not (set(done) & (set(rejected) | set(unknown)))
         and not (set(rejected) & set(unknown)), "unaccounted started voucher")
    for cid, event in {**done, **rejected, **unknown}.items():
        need(all(event.get(k) == started[cid].get(k) for k in ("type","line_count","actor_user","basket_hash")),
             "start and outcome identity disagree")
    need(all(e.get("phase") == "overload" for e in list(rejected.values()) + list(unknown.values())),
         "unexplained rejected or unknown voucher")
    return done, names


def group_rows(rows):
    indexed = {}
    for table, fields in FIELDS.items():
        by = defaultdict(list)
        for row in rows.get(table, []):
            if table == IDEM:
                key = (row["reference_doctype"], row["reference_name"])
            elif table in (GL,SLE,PLE):
                key = (row["voucher_type"], row["voucher_no"])
            elif "parent" in fields:
                key = (row["parenttype"],row["parent"])
            else:
                key = (table,row["name"])
            by[key].append(row)
        indexed[table] = by
    return indexed


def without(row, *keys):
    return {k:v for k,v in row.items() if k not in keys}


def ledger_rows(rows, names, detail_ordinals):
    result = []
    for original in rows:
        row = without(original, "name", "voucher_type", "voucher_no", "posting_datetime", "creation")
        field = "against_voucher" if "against_voucher" in row else "against_voucher_no"
        if field in row:
            against = row[field]
            row[field] = names.get(row["against_voucher_type"], {}).get(against, against)
        if "voucher_detail_no" in row:
            row, = parity.ordinalize_voucher_detail_rows([row], detail_ordinals)
        result.append(row)
    return sorted(result, key=finance.canonical_json)


def ensure_inventory(before, after):
    need(before["db_container"] == after["db_container"], "disposable database changed")
    changed = sorted(k for k in FIELDS if before["inventory"][k] != after["inventory"][k])
    need(not changed, "unacknowledged posting or historical financial row change in " + ", ".join(changed))


def operation_data(event, indexed, name_map, bin_pin, *, native_stock=False):
    cid, kind, name = (event[k] for k in ("client_voucher_id", "type", "voucher_no"))
    doctype = TYPES[kind]
    key = (doctype,name)
    def rows(table):
        return indexed[table].get(key, [])
    parents = rows(doctype)
    need(len(parents) == 1 and parents[0]["docstatus"] == "1" and parents[0]["owner"] == event["actor_user"],
         "submitted document or posting actor mismatch for " + cid)
    doc = parents[0]
    items = sorted(rows(CHILD[kind]), key=lambda item: int(item["idx"]))
    need(items and len(items) == int(event["line_count"]) and
         [int(i["idx"]) for i in items] == list(range(1, len(items)+1)), "missing or duplicate item ordinal for " + cid)
    # A plain mapping has dict.items as an attribute; perf_parity expects Frappe's document.items child list.
    class Children:
        def __init__(self, entries):
            self.items = entries
        def get(self, field):
            return doc.get(field)
    children = Children(items)
    registry = parity.document_detail_registry(children)
    ordinals = {n:r["key"] for n,r in registry.items()}
    gl = ledger_rows(rows(GL),name_map,ordinals)
    sle = ledger_rows(rows(SLE),name_map,ordinals)
    payment_ledger = ledger_rows(rows(PLE),name_map,ordinals)
    parity.validate_gl_detail_links(gl,registry)
    parity.validate_sle_detail_links(sle,registry,children,doctype)
    debit, credit = (parity.exact_numeric_sum(gl,f) for f in ("debit","credit"))
    need(debit == credit and (gl or kind == "transfer"), "unbalanced or missing GL for " + cid)
    need(all(str(r["is_cancelled"]) == "0" for r in gl + sle), "cancelled ledger posting for " + cid)
    actual_qty = parity.exact_numeric_sum(sle,"actual_qty")
    need(sle if kind != "purchase_invoice" else not sle, "unexpected stock ledger coverage for " + cid)
    if kind == "transfer":
        need(actual_qty == 0, "unbalanced transfer for " + cid)
    if kind == "purchase_invoice":
        payable = [g for g in gl if g["party_type"] == "Supplier"]
        need(len(payable) == 1 and payable[0]["party"] == doc["supplier"]
             and Decimal(payable[0]["credit"]) > 0 and Decimal(payable[0]["debit"]) == 0,
             "missing or duplicated supplier payable for " + cid)
    normalized_items = []
    links = []
    for item in items:
        record = without(item,"name","parent","parenttype")
        if kind == "purchase_invoice":
            receipt = item["purchase_receipt"]
            receipt_cid = name_map[TYPES["purchase_receipt"]].get(receipt)
            expected_cid = cid.replace("-purchase_invoice-", "-purchase_receipt-")
            need(receipt_cid == expected_cid and cid != expected_cid, "invoice references an unexpected receipt")
            children_pr = indexed[CHILD["purchase_receipt"]][(TYPES["purchase_receipt"], receipt)]
            matching = [r for r in children_pr if r["name"] == item["pr_detail"]]
            need(len(matching) == 1 and all(matching[0][f] == item[f] for f in ("idx","item_code","qty","uom")),
                 "invoice references an incorrect receipt line")
            record.update(purchase_receipt=receipt_cid, pr_detail="item:" + matching[0]["idx"])
            links.append({"purchase_invoice":name,"purchase_receipt":receipt,"pr_detail":item["pr_detail"]})
        normalized_items.append(record)
    entries = rows(IDEM)
    if kind in ("sale","transfer") and not native_stock:
        need(len(entries) == 1 and entries[0]["idempotency_key"] == f"{event['actor_user']}:{kind}:{cid}"
             and bool(entries[0]["request_fingerprint"]), "missing or mismatched client reservation for " + cid)
    else:
        expected, = seasonal.parse_purchase_rows([[name, 1, doc["remarks"]]]).get(cid, [{}])
        need(expected.get("name") == name and (kind != "purchase_invoice" or doc["bill_no"] == cid),
             "purchase document client identity mismatch")
    doc_summary = {f:doc[f] for f in ("owner","docstatus",*DOC_FIELDS[kind].split())}
    # Payment Ledger has no standalone section in financial_verifier. Its sorted rows
    # remain in the document section for recursive exact comparison (including multiplicity).
    doc_summary["payment_ledger"] = payment_ledger
    snap = {"doc":doc_summary,"items":normalized_items,"gl":gl,"sle":sle,
            "gl_summary":{"count":len(gl),"debit_sum":str(debit),"credit_sum":str(credit)},
            "sle_summary":{"count":len(sle),"actual_qty_sum":str(actual_qty),
                           "stock_value_difference_sum":str(parity.exact_numeric_sum(sle,"stock_value_difference"))},
            "bin":bin_pin}
    if kind == "sale":
        snap["payments"] = [without(p,"name","parent","parenttype") for p in rows(PAYMENT)]
        need(snap["payments"], "missing POS tender for " + cid)
    retry = None
    if event.get("lost_ack") and not native_stock:
        first = event.get("first_committed_voucher")
        purchase = kind in {"purchase_receipt", "purchase_invoice"}
        base_proof = first == name and event.get("retried") is True and event.get("retry_confirmed") is True
        if purchase:
            replay_proof = (
                int(event.get("attempts", 0)) == 1
                and event.get("resubmitted") is False
                and event.get("recovery_attempted") is True
                and event.get("reconciled") is True
                and int(event.get("recovery_reads", 0)) >= 1
            )
        else:
            replay_proof = int(event.get("attempts", 0)) >= 2 and event.get("resubmitted") is True
        need(base_proof and replay_proof, "missing original committed retry confirmation")
        retry = {"mode":finance.COMMITTED_RETRY,"client_voucher_id":cid,"first_voucher":first,"retry_voucher":name,
                 "documents":[{"name":name,"docstatus":int(doc["docstatus"])}],"gl_entries":rows(GL),
                 "stock_entries":rows(SLE),"count_deltas":{"documents":1,"gl_entries":len(gl),"stock_entries":len(sle)},
                 "idempotency_entries":entries,"links":links}
    return snap, retry


def ledger_order(raw, names, predecessors=None):
    groups = defaultdict(list)
    for r in raw[SLE]:
        cid = names[r["voucher_type"]][r["voucher_no"]]
        groups[(r["warehouse"],r["item_code"])].append((r["posting_datetime"],r["creation"],cid,r["actual_qty"]))
    streams = []
    for key,entries in sorted(groups.items()):
        need(all(row[0] and row[1] for row in entries) and len({x[:2] for x in entries}) == len(entries),
             "ambiguous stock ledger order for " + str(key))
        sequence = [[r[2],r[3]] for r in sorted(entries)]
        streams.append([*key,sequence])
        if predecessors is not None:
            for old,new in zip(sequence, sequence[1:]):
                if old[0] != new[0]:
                    predecessors[new[0]].add(old[0])
    return {"method":"posted-sle-per-warehouse-item-v1","streams":len(streams),"movements":len(raw[SLE]),
            "sha256":hashlib.sha256(canonical(streams)).hexdigest()}


def collect_run(req, target, message, events):
    before = read_json(Path(req["artifacts_dir"]) / BEFORE)
    need(before["request"] == req and before["arm"] == message, "pre-run financial inventory belongs to another arm")
    done, names = index_events(events)
    after, raw = database_image(target, message, names)
    ensure_inventory(before["image"], after)
    bins = {"scope":"whole-run","before":before["image"]["bins"],"after":after["bins"]}
    name_map = {TYPES[kind]:v for kind,v in names.items()}
    predecessors = {cid:set() for cid in sorted(done)}
    for cid,event in done.items():
        if event["type"] == "purchase_invoice":
            receipt = cid.replace("-purchase_invoice-", "-purchase_receipt-")
            need(receipt != cid and receipt in done, "missing original ordered purchase receipt")
            predecessors[cid].add(receipt)
    ordering = ledger_order(raw,name_map,predecessors)
    try:
        serial_order = list(graphlib.TopologicalSorter({k:sorted(v) for k,v in predecessors.items()}).static_order())
    except graphlib.CycleError:
        serial_order = None
    from seasonal_driver import artifact
    artifact(Path(req["artifacts_dir"]), "posting-after.json", {"arm":message,"image":after,"bin":bins,
             "completed":len(done),"candidate_row_counts":{t:len(raw.get(t,[])) for t in FIELDS},
             "ledger_order":ordering,"compatible_serial_order":serial_order,
             "completed_ids_sha256":hashlib.sha256(canonical(sorted(done))).hexdigest()})
    return done, group_rows(raw), name_map, bins, ordering


CONTROLLERS = tuple(
    f"{module}/doctype/{slug}/{slug}.py" for module,slug in (
        ("accounts","sales_invoice"), ("accounts","purchase_invoice"),
        ("stock","stock_entry"), ("stock","purchase_receipt")))


def stock_controller_evidence(target,message,contract):
    from seasonal_driver import remote
    from seasonal_telemetry import _guest_script
    need(set(contract["controllers"]) == set(CONTROLLERS) and re.fullmatch(r"[a-f0-9]{40}", contract["upstream_revision"])
         and re.fullmatch(r"sha256:[a-f0-9]{64}",contract["backend_image_id"]), "invalid stock source pin")
    script = _guest_script() + """
project,_ = validate_telemetry_window(message)
backend = project + "-backend-1"
info = inspect(backend)
require(info["Config"]["Labels"]["com.docker.compose.project"] == project,"unexpected stock backend")
paths = message["controllers"]
root = BENCH + "/apps/erpnext/erpnext/"
sums = command("docker","exec",backend,"sha256sum",*[root+p for p in paths]).decode().splitlines()
print(json.dumps({"backend_image_id":info["Image"],"controllers":dict(zip(paths,[s.split()[0] for s in sums],strict=True))}))
"""
    actual = remote(target,dict(message,controllers=list(CONTROLLERS)),script,suffix="")
    need(actual == {k:contract[k] for k in ("backend_image_id","controllers")},
         "installed stock image or document controllers differ from the upstream pin")
    return actual


def capture_stock_reference(req, target, message, events, stock_contract, output_path, *, expected_order=None):
    """Export from a fresh native ERPNext replay, given an operator-pinned upstream source contract."""
    need(req["arm"] == "baseline" and req["commit"] == req["baseline_commit"], "native reference needs a clean baseline")
    source = stock_controller_evidence(target,message,stock_contract)
    done,indexed,names,bins,order = collect_run(req,target,message,events)
    if expected_order is not None:
        need(order == expected_order, "native run did not reproduce recorded economic ledger order")
    operations = {}
    for cid,event in sorted(done.items()):
        need(event.get("submit_method") in ("frappe.desk.form.save.savedocs","frappe.model.document.Document.submit"),
             "missing native stock submit trace")
        snap,_ = operation_data(event,indexed,names,bins,native_stock=True)
        operations[cid] = {"actor_user":event["actor_user"], "basket_hash":event["basket_hash"],
                           "kind":event["type"],"snapshot":snap}
    ref = {"schema":SCHEMA,"fixture_sha256":req["fixture_sha256"],"seed":req["seed"],
           "baseline_commit":req["baseline_commit"],"namespace":message["namespace"],
           "upstream_revision":stock_contract["upstream_revision"],"stock_source":source,"bin":bins,"operations":operations,
           "order":order,"order_scope":"source-ledger-matched" if expected_order is not None else "explicit-serial-diagnostic"}
    path = Path(output_path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with temporary.open("wb") as file, gzip.GzipFile(fileobj=file,mode="wb",mtime=0) as zipped:
            with io.TextIOWrapper(zipped,encoding="utf-8") as text:
                json.dump(ref,text,sort_keys=True,separators=(",",":"),allow_nan=False)
        os.link(temporary,path)
    finally:
        temporary.unlink()
    from khitmee_lab_adapter import file_hash
    return {"path":str(path.resolve()),"sha256":file_hash(path)}


def capture(req, target, message, events, reference_path):
    """The caller supplies its previously digest-checked, operator-owned stock oracle path."""
    ref = read_json(reference_path)
    need(ref["schema"] == SCHEMA and ref["fixture_sha256"] == req["fixture_sha256"] == message["fixture_sha256"]
         and ref["seed"] == req["seed"] and ref["baseline_commit"] == req["baseline_commit"],
         "reference format, fixture, revision or seed mismatch")
    need(re.fullmatch(r"khitmee-arm-[0-9a-f]{16}",ref["namespace"]) and ref["namespace"] != message["namespace"]
         and re.fullmatch(r"[0-9a-f]{40}",ref["upstream_revision"]), "independent stock source identity missing")
    source = ref["stock_source"]
    need(re.fullmatch(r"sha256:[a-f0-9]{64}",source["backend_image_id"])
         and set(source["controllers"]) == set(CONTROLLERS)
         and all(re.fullmatch(r"[a-f0-9]{64}",h) for h in source["controllers"].values()), "incomplete stock source pin")
    if req.get("workload") == "seasonal-v1":
        need(ref.get("order_scope") == "source-ledger-matched", "diagnostic posting order cannot qualify a full seasonal run")
    done,indexed,name_map,bins,order = collect_run(req,target,message,events)
    need(ref["order"] == order, "financial posting order differs from the stock reference")
    need(ref["bin"] == bins, "stock and candidate whole-run Bin snapshots differ")
    stock = ref["operations"]
    need(isinstance(stock, dict) and done.keys() <= stock.keys(), "stock capture is missing completed vouchers")
    operations = []
    for cid,event in sorted(done.items()):
        expected = stock[cid]
        need(expected["kind"] == event["type"] and expected["basket_hash"] == event["basket_hash"]
             and expected["actor_user"] == event["actor_user"] and expected["snapshot"]["bin"] == bins,
             "wrong stock input or snapshot for " + cid)
        snap, retry = operation_data(event,indexed,name_map,bins)
        op = {"client_voucher_id":cid,"kind":event["type"],
              "mode":finance.COMMITTED_RETRY if retry else finance.COMMITTED_POSTING,
              "reference":{"source":"stock-db:"+ref["namespace"],"snapshot":expected["snapshot"]},
              "candidate":{"source":"arm-db:"+message["namespace"],"snapshot":snap}}
        if retry:
            op["retry"] = retry
        operations.append(op)
    return {"fixture_id":req["fixture_sha256"],"upstream_revision":ref["upstream_revision"],
            "candidate_revision":req["commit"],"permissions":{"actors":sorted({e["actor_user"] for e in done.values()})},
            "seed":req["seed"],"operations":operations}
