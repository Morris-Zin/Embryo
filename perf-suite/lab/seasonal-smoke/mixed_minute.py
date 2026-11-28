"""One externally timed, named-actor mixed minute on a clean restored year arm."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/"driver"))
import posting_evidence as posting
import native_replay
import process_kernel_telemetry as process_kernel
import seasonal_acceptance as acceptance
import seasonal_driver as driver
import seasonal_telemetry as telemetry
import verify_run as verifier

FIXTURE = "0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd"
K6 = "ae93e9a5782805b8c816cd2997dbf9af448df56bd7733a4ea7d2d4e6632e9410"
K6_WINDOWS = "6b68dbae8de12ae761da8722f1a182ee3ff47fe68e829b89e74a2796ded83fcb"
ACTORS = "aff3b15acb83d6699162fff77dd32d3815f1177a6dfdfdf403208df1cef48633"
MANIFEST = "1811b91867d80e5afe8d44f43d5816efa9f171940cc368a97a9c9fab393d9334"
OFFERED = {"sale":80,"transfer":10,"purchase_receipt":5,"purchase_invoice":5}
BASE_SEED = "42"
RUN_ID = "mixed-year-minute-20261127"


def write(path, obj):
    with gzip.open(path,"xt",encoding="utf-8") if path.suffix==".gz" else path.open("x",encoding="utf-8") as out:
        json.dump(obj,out,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)


class ArmDB:
    def __init__(self,target,message):
        self.target,self.message=target,message

    def query(self,sql):
        if not sql.lstrip().upper().startswith("SELECT ") or ";" in sql:
            raise ValueError("invalid diagnostic SQL")
        program=telemetry._guest_script()+r"""
project,_=validate_telemetry_window(message)
label=inspect(project+"-db-1")
require(any(m.get("Name")==project+"-db" and m.get("Destination")=="/var/lib/mysql" for m in label["Mounts"]),
        "wrong database volume")
site=Path("/var/lib/docker/volumes")/(project+"-sites")/"_data"/SITE/"site_config.json"
db=json.loads(site.read_bytes())["db_name"]
require(re.fullmatch(r"[_A-Za-z0-9]+",db),"unexpected schema")
data=command("docker","exec","-i",project+"-db-1","sh","-c",
             'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mariadb -uroot -NB --raw "$1"',"verify",db,
             data=("START TRANSACTION READ ONLY;\n"+message["select"]+";\nROLLBACK;\n").encode(),
             timeout=180)
print(json.dumps(data.decode().splitlines()))
"""
        lines=driver.remote(self.target,dict(self.message,select=sql),program,suffix="",timeout=190)
        return [line.split("\t") for line in lines]

    def counts(self):
        sql="SELECT "+",".join(f"(SELECT COUNT(*) FROM `tab{t}`)" for t in posting.FIELDS)
        row,=self.query(sql)
        return dict(zip(posting.FIELDS,map(int,row),strict=True))


def load_events(path):
    events=[]
    for line in path.read_text(encoding="utf-8").splitlines():
        r=json.loads(line); msg=r.get("msg","")
        if isinstance(msg,str) and msg.startswith(acceptance.PREFIX):
            event=json.loads(msg[len(acceptance.PREFIX):])
            event["observed_utc"]=r["time"]
            events.append(event)
    return events


def quantile(values):
    return sorted(values)[math.ceil(.95*len(values))-1] if values else None


def score(events):
    starts=[e for e in events if e["event"]=="voucher_start"]
    done=[e for e in events if e["event"]=="voucher_complete"]
    rejected=[e for e in events if e["event"]=="voucher_reject"]
    unknown=[e for e in events if e["event"]=="voucher_unknown"]
    summary={}
    original={e["client_voucher_id"]:e for e in starts}
    stamps=[e["start_ms"] for e in events if e["event"]=="minute_clock"]
    zero=min(stamps) if stamps else None
    def elapsed(e):
        # k6 emits "at" with milliseconds. The outer logger can truncate to seconds.
        return round(datetime.fromisoformat(e["at"].replace("Z","+00:00")).timestamp()*1000)-zero
    for kind,offered in {**OFFERED,"sale_10":60,"sale_50":16,"sale_200":4}.items():
        def selected(e):
            return e["type"]==kind or (e["type"]=="sale" and kind=="sale_"+str(e["line_count"]))
        rows=[e for e in done if selected(e)]
        summary[kind]={"offered":offered,"started":sum(map(selected,starts)),"confirmed":len(rows),
                       "confirmed_in_first_60s":sum(0 <= elapsed(e) < 60000 for e in rows) if zero is not None else None,
                       "rejected":sum(map(selected,rejected)),
                       "confirmed_p95_ms":quantile([e["duration_ms"] for e in rows]),
                       "request_elapsed_p95_ms":quantile([e["server_duration_ms"] for e in rows]),
                       "max_confirmed_ms":max((e["duration_ms"] for e in rows),default=None)}
    ids=Counter(e["client_voucher_id"] for e in done+rejected+unknown)
    return {"scope":"one diagnostic open-arrival minute plus drain, year fixture",
            "scheduled_vouchers":100,"scheduled_searches":80,"offered_manager_requests":{"Warehouse":6,"Monthly":6,"detail":2},
            "start_ms":zero,"scenario_start_spread_ms":max(stamps)-min(stamps) if stamps else None,
            "work":summary,"started_unique":len(original),"duplicate_start_events":len(starts)-len(original),
            "duplicate_terminal_events":sum(c-1 for c in ids.values()),"missing_terminals":sorted(set(original)-set(ids)),
            "confirmed_in_minute":sum(0 <= elapsed(e) < 60000 for e in done) if zero is not None else None,
            "last_confirmation_s":max((round(elapsed(e)/1000,3) for e in done),default=None) if zero is not None else None,
            "event_counts":dict(Counter(e["event"] for e in events)),
            "rejection_reasons":[{"kind":k[0],"status":k[1],"reason":k[2],"count":n}
                for k,n in Counter((e["type"],e.get("status"),e.get("reason")) for e in rejected).items()],
            "retry_events":[{f:e.get(f) for f in ("type","event","client_voucher_id","first_committed_voucher",
                          "voucher_no","attempts","resubmitted","recovery_attempted","recovery_reads","reason")}
                          for e in done+rejected+unknown if e.get("lost_ack")],
            "routes":{k:{"count":len([e for e in events if e["event"]==k]),
                         "p95_ms":quantile([e["duration_ms"] for e in events if e["event"]==k and "duration_ms" in e])}
                      for k in ("search_complete","search_reject","manager_summary","manager_summary_reject",
                                "manager_detail","manager_detail_reject")}}


def audit(db, events, before, folder):
    by={k:{e["client_voucher_id"]:e for e in events if e["event"]==v}
        for k,v in (("started","voucher_start"),("completed","voucher_complete"),("rejected","voucher_reject"),("unknown","voucher_unknown"))}
    by["events"]=events
    by["config"]=next(e["config"] for e in events if e["event"]=="config")
    basic=verifier.verify(by,db)
    raw_base=verifier.rows_for_verification(db,by["config"]["run_prefix"])
    parsed=verifier.normalize_base_rows(by,raw_base)
    typed=verifier.collect_document_names(by,parsed)
    names={k:set(typed.get(dt,())) for k,dt in posting.TYPES.items()}
    raw={}
    for table,fields in posting.FIELDS.items():
        values=",".join("CAST(`"+f+"` AS CHAR)" for f in fields)
        data=db.query("SELECT JSON_ARRAY("+values+") FROM `tab"+table+"` WHERE "+posting.rows_predicate(table,names))
        raw[table]=[dict(zip(fields,json.loads(row[0]),strict=True)) for row in data]
    after=db.counts()
    counts={t:{"before":b,"after":after[t],"delta":after[t]-b,"attributed_to_client_documents":len(raw[t])}
            for t,b in before.items()}
    gaps={k:v for k,v in counts.items() if v["delta"]!=v["attributed_to_client_documents"]}
    write(folder/"database-postings.json.gz",{"rows":raw,"client_bindings":raw_base,"observed_counts":counts})
    name_map={dt:{e["voucher_no"]:cid for cid,e in by["completed"].items() if e["type"]==k}
              for k,dt in posting.TYPES.items()}
    indexed=posting.group_rows(raw)
    failures=[]
    proven=[]
    valid=Counter()
    for cid,e in by["completed"].items():
        try:
            snap,retry=posting.operation_data(e,indexed,name_map,{"scope":"independent posting check"})
            intended=by["started"][cid]["replay_input"]["items"]
            posted=snap["items"]
            if len(intended)!=len(posted) or any(p["item_code"]!=i["item_code"] or p["uom"]!=i["uom"]
                    or Decimal(p["qty"])!=Decimal(str(i["qty"])) or
                    ("rate" in i and Decimal(p["rate"])!=Decimal(str(i["rate"]))) for p,i in zip(posted,intended)):
                raise ValueError("committed basket differs from recorded inputs")
            valid[e["type"]]+=1
            if retry:
                verdict=posting.finance.reconcile_retry_evidence(retry,kind=e["type"])
                if not verdict["proves_committed_retry"]:
                    raise ValueError(str(verdict["differences"]))
                proven.append({"id":cid,"kind":e["type"],"voucher":e["voucher_no"]})
        except (ValueError,KeyError,TypeError,ArithmeticError,RuntimeError) as ex:
            failures.append({"id":cid,"kind":e["type"],"error":str(ex)})
    return {"independent_verifier":basic,"counts":counts,"unexplained_count_changes":gaps,
            "committed_basket_ledger_link_passes":dict(valid),"committed_financial_failures":failures,"proven_retries":proven}


def run(args):
    os.umask(0o077)
    cfg=json.loads(args.context.read_bytes())
    request,target,message=native_replay.arm(cfg)
    if message["fixture_sha256"]!=FIXTURE or str(request["seed"])!=BASE_SEED or request["arm"]!="baseline":
        raise ValueError("unexpected year fixture or host")
    actors,manifest=(acceptance.pinned_file({"path":str(path),"sha256":digest}) for path,digest in
                     ((args.actors,ACTORS),(args.manifest,MANIFEST)))
    windows_k6=str(args.k6).lower().endswith(".exe")
    k6=acceptance.pinned_file({"path":str(args.k6),"sha256":K6_WINDOWS if windows_k6 else K6})
    folder=args.folder
    folder.mkdir(exist_ok=False)
    role_env=json.loads(actors.read_bytes())
    reasons={}
    db=ArmDB(target,message)
    counts=db.counts()
    cashier=role_env["PERF_USER"]
    actor_counts=db.query("SELECT "+",".join(f"(SELECT COUNT(*) FROM `tab{dt}` WHERE owner={posting.literal(cashier)})"
                                               for dt in posting.TYPES.values()))
    if any(int(v) for v in actor_counts[0]):
        raise ValueError("cashier financial rows already present on restored arm")
    setup={"fixture_sha256":FIXTURE,"run_id":RUN_ID,"seed":BASE_SEED,"target":message,"initial_counts":counts,
           "actor_counts_before":actor_counts[0],"arm_label":args.arm_label,
           "manager_target_url":str(args.manager_target or ""),"manager_target_split":bool(args.manager_target),"cashier_only":bool(args.cashier_only),
           "generator_vus":{"vouchers":88,"searches":80,"contention":1,"managers":"6+6+2","note":"original one-minute schedule headroom restored with memory-efficient shared generator context"},
           "script_hashes":{
               str(p.relative_to(HERE.parents[1])):hashlib.sha256(p.read_bytes()).hexdigest() for p in
               [HERE/"mixed_minute.js",HERE.parents[1]/"k6/scenarios/seasonal.js",*sorted((HERE.parents[1]/"k6/lib").glob("*.js"))]},
           "manifest_sha256":MANIFEST,"k6_sha256":K6_WINDOWS if windows_k6 else K6,"k6_platform":"windows-amd64" if windows_k6 else "linux-amd64","actor_config_sha256":ACTORS}
    write(folder/"mixed-provenance.json",setup)
    env={k:os.environ[k] for k in ("HOME","LANG","PATH") if k in os.environ}
    seed=json.loads(manifest.read_bytes()); end=seed["report_window"]["to_date"]
    env.update(role_env,SEED_MANIFEST_PATH=str(manifest),SEASONAL_PROFILE="smoke",SEASONAL_BASELINE_ONLY="1",SEASONAL_REQUIRE_RETRY_EVIDENCE="1",
               SITE_NAME="bench-loadtest.localhost",SEASONAL_ENABLE_MANAGERS=("0" if args.cashier_only else "1"),SEASONAL_ENABLE_EXPORT="0",
               RUN_ID=RUN_ID,SEASONAL_SEED=BASE_SEED,REPORT_TO_DATE=end,REPORT_FROM_DATE=end[:4]+"-01-01")

    if args.manager_target:
        env["SEASONAL_MANAGER_TARGET_URL"]=str(args.manager_target)
    log=folder/"k6-events.log"
    code=None
    telemetry_window={}
    process_window={}
    with acceptance.guest_tunnel(target,18086) as url:
        env["TARGET_URL"]=url
        # SQL digest coverage is useful on a diagnostic arm. A declared missing
        # sampler may not suppress the requested one and only workload minute.
        measurement=telemetry.measure(target,message,folder,interval=4)
        process_measurement=None
        try:
            telemetry_window=measurement.__enter__()
        except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as ex:
            reasons["telemetry_start"]=str(ex)
            measurement=None
        if measurement is not None:
            process_measurement=process_kernel.measure(target,message,folder,interval=1)
            try:
                process_window=process_measurement.__enter__()
            except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as ex:
                reasons["process_telemetry_start"]=str(ex)
                process_measurement=None
        try:
            with (folder/"k6-output.txt").open("xb") as output:
                if windows_k6:
                    def winpath(path):
                        text=str(path)
                        for prefix, drive in (("/mnt/e/", "E:"), ("/mnt/c/", "C:")):
                            if text.startswith(prefix):
                                return drive + "\\" + text[len(prefix):].replace("/", "\\")
                        raise ValueError(f"no Windows path mapping for {text}")
                    work_root=Path(os.environ.get("K6_WINDOWS_WORK_ROOT","/mnt/e/Khitmee-ERPNext/perf-suite/tmp/windows-k6"))
                    work=work_root/(RUN_ID+"-"+uuid.uuid4().hex[:12])
                    work.mkdir(parents=True,exist_ok=False)
                    windows_script=Path(os.environ.get("K6_WINDOWS_SCRIPT","/mnt/e/Khitmee-ERPNext/perf-suite/lab/seasonal-smoke/mixed_minute.js"))
                    staged_log=work/"k6-events.log"
                    staged_summary=work/"k6-summary.json"
                    shutil.copyfile(manifest,work/"seed_manifest.json")
                    env["SEED_MANIFEST_PATH"]=winpath(work/"seed_manifest.json")
                    args_k6=["/usr/bin/time","-v","-o",str(folder/"k6-resource.txt"),str(k6),"run","--quiet","--log-format=json","--log-output=file="+winpath(staged_log),
                             "--summary-export",winpath(staged_summary),winpath(windows_script)]
                    reasons["windows_k6_staging"]=str(work)
                    env["WSLENV"]=":".join(k for k in env if k not in ("HOME","LANG","PATH"))
                else:
                    args_k6=["/usr/bin/time","-v","-o",str(folder/"k6-resource.txt"),str(k6),"run","--quiet","--log-format=json","--log-output=file="+str(log),
                             "--summary-export",str(folder/"k6-summary.json"),str(HERE/"mixed_minute.js")]
                code=subprocess.run(args_k6,env=env,stdout=output,stderr=output,timeout=370).returncode
                if windows_k6:
                    if staged_log.is_file():
                        shutil.copyfile(staged_log,log)
                    if staged_summary.is_file():
                        shutil.copyfile(staged_summary,folder/"k6-summary.json")
        except subprocess.TimeoutExpired:
            reasons["load_generator"]="timed out after 370s"
            if windows_k6:
                if staged_log.is_file():
                    shutil.copyfile(staged_log,log)
                if staged_summary.is_file():
                    shutil.copyfile(staged_summary,folder/"k6-summary.json")
        finally:
            if process_measurement is not None:
                try:
                    process_measurement.__exit__(None,None,None)
                except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as ex:
                    reasons["process_telemetry_end"]=str(ex)
            if measurement is not None:
                try:
                    measurement.__exit__(None,None,None)
                except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as ex:
                    reasons["telemetry_end"]=str(ex)
    events=load_events(log) if log.is_file() else []
    summary=score(events)
    summary.update(k6_exit=code,fixture_sha256=FIXTURE,run_id=RUN_ID,seed=BASE_SEED,telemetry=telemetry_window,
                   process_kernel_telemetry=process_window,errors=reasons,full_season_qualified=False)
    k6_summary=folder/"k6-summary.json"
    if k6_summary.exists():
        summary["dropped_iterations"]=json.loads(k6_summary.read_bytes())["metrics"].get("dropped_iterations",{}).get("count",0)
    write(folder/"mixed-events.json.gz",events)
    if any(e["event"]=="voucher_start" for e in events):
        try:
            summary["database"]=audit(db,events,counts,folder)
        except (ValueError,KeyError,RuntimeError,TypeError,OSError,subprocess.SubprocessError) as ex:
            reasons["database"]=str(ex)
    write(folder/"mixed-summary.json",summary)
    print(json.dumps({k:summary.get(k) for k in ("k6_exit","work","confirmed_in_minute","last_confirmation_s",
         "dropped_iterations","rejection_reasons","routes","errors")}),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ("context","actors","manifest","k6","folder"):
        parser.add_argument("--"+arg,type=Path,required=True)
    parser.add_argument("--manager-target",default="")
    parser.add_argument("--arm-label",default="local-managers")
    parser.add_argument("--cashier-only",action="store_true")
    run(parser.parse_args())
