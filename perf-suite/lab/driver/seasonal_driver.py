#!/usr/bin/env python3
"""Run the trusted report screen or check seasonal readiness over the guest's pinned SSH identity."""
import argparse
import base64
from collections import defaultdict
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
ROOT = LAB.parents[1]
sys.path[:0] = [str(LAB/"environment"), str(LAB/"diagnostics"), str(ROOT/"tools")]
import financial_verifier
import khitmee_lab_adapter as adapter
import smoke


SCREEN = "smoke-gross-profit-shop1-last-seed-day-by-item-v1"
SEASONAL = "seasonal-v1"
# Recorded from the independently run stock module on the original synthetic fixture.
STOCK_RESULT = "88bac0a4969de3fba8ae56b512237e13151460a450716a225f98194545aa3202"
STOCK_SOURCE = "fff7460f8e9609ab89a27f96c26af903c18ec6564b60e08702ad672d0bc11cce"
APP_MODULE = "apps/khitmee_ops/khitmee_ops/khitmee_ops/report/fast_gross_profit/fast_gross_profit.py"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",",":"), allow_nan=False).encode()


def artifact(folder, name, data):
    raw=canonical(data)+b"\n"
    with (folder/name).open("xb") as out:
        out.write(raw)
    return {"artifact":name,"sha256":sha(raw)}


def remote(target, message, script="", suffix="main(message)", timeout=360):
    source=(HERE/"arm_guest.py").read_text()+f"\nmessage=json.loads({canonical(message).decode()!r})\n"+script+"\n"+suffix+"\n"
    argv=["ssh","-T","-F",target["config"],target["host"],"sudo -n python3 -IB -"]
    done=subprocess.run(argv,input=source.encode(),capture_output=True,timeout=timeout)
    if done.returncode:
        # Guest driver errors include only operator-selected context. Never echo Compose env or arbitrary app errors.
        errors=done.stderr.decode(errors="replace").splitlines()
        detail=next((x for x in reversed(errors) if x.startswith(
            ("ValueError:","RuntimeError:","NameError:","KeyError:","TypeError:","ModuleNotFoundError:","AttributeError:"))),
                    "guest process failed ("+next((x.partition(':')[0] for x in reversed(errors) if 'Error:' in x),"unknown error")+")")
        raise ValueError(adapter.sanitize_text(detail,240))
    return json.loads(done.stdout)


def arm_source(req,folder):
    store=folder.parents[2]
    key=(store/"BASELINE").read_text().strip()
    require(adapter.SHA256_RE.fullmatch(key),"invalid supervisor baseline pointer")
    pinned=(store/"objects"/key).read_bytes()
    require(sha(pinned)==key,"baseline manifest digest changed")
    config=json.loads(pinned)
    for field in ("baseline_commit","workload","fixture_sha256"):
        require(config[field]==req[field],"store pin mismatch "+field)
    repo=config["repo"]
    def git(*argv):
        return subprocess.check_output(["git","-C",repo,*argv],stderr=subprocess.DEVNULL)
    if req["arm"]=="candidate":
        patch=git("diff","--binary","--no-ext-diff",req["baseline_commit"],req["commit"])
        require(sha(patch)==req["patch_sha256"],"candidate patch digest mismatch")
    else:
        require(req["baseline_commit"]==req["commit"],"baseline commit mismatch")
    archive=git("archive","--format=tar",req["commit"],"apps/khitmee_ops")
    report_hash = sha(git("show",req["commit"]+":"+APP_MODULE)) if req["workload"] == SCREEN else None
    return archive,report_hash


def readiness(target):
    result=remote(target,{"op":"readiness"})
    result["workload"]=SEASONAL
    reasons=[]
    if result["fixture_scale"]=="smoke":
        reasons.append("protected restore contains smoke history and catalog")
    if not result["full_year_export"]:
        reasons.append("installed application lacks the full-year export download")
    result["observed_blockers"]=reasons
    result["seasonal_qualified"]=False
    return result


def check_screen(raw,expected_source):
    stock=raw["stock"]
    fields=("columns","rows","filters","site","seed_manifest_sha256","currency","precision")
    require(sha(canonical({k:stock[k] for k in fields}))==STOCK_RESULT,"stock rows differ from independent fixture oracle")
    require(stock["module_sha256"]==STOCK_SOURCE,"upstream module digest mismatch")
    require(len(raw["fast"])==20,"incomplete repeat count")
    comparisons=[]
    for arm in raw["fast"]:
        window=arm["guest_window"]
        require(arm["module_sha256"]==expected_source,"loaded candidate module differs from pinned Git source")
        require(not window["missing"] and not window["counter_resets"] and not window["identity_changes"],"incomplete cgroup coverage")
        require(window["statement_digest"]["supported"] and window["statement_digest"]["top_by_time"],"SQL digest coverage unavailable")
        comparison=smoke.compare(stock,arm)
        require(comparison["matches"],"financial report differs from upstream")
        comparisons.append(comparison)
    return comparisons


def report_priorities(measurements):
    totals = defaultdict(float)
    counts = defaultdict(int)
    for measurement in measurements:
        for row in measurement["top_sql_templates"]:
            totals[row["template"]] += row["total_ms"]
            counts[row["template"]] += row["count"]
    entries = [{"template":template,"observed_ms":total,"observed_calls":counts[template]}
               for template,total in sorted(totals.items(), key=lambda entry: -entry[1])]
    rankings = [{"metric":"report_sql_template_ms","unit":"milliseconds","entries":entries,
                 "scope":"Templates retained in the per-invocation top SQL list; totals are lower bounds."}]
    hypotheses = []
    if entries:
        hypotheses.append({"objective":"sql_seconds","evidence":entries[0],
                           "question":"Can the highest-time traced template do less work with identical report output?"})
    return {"rankings":rankings,"hypotheses":hypotheses}


def screen_run(req,folder,target,message,app,expected_source):
    before=remote(target,dict(message,op="counts"))
    probe=smoke.bundled_module("perf_profile",ROOT/"tools/perf_profile.py")+(LAB/"diagnostics/probe.py").read_text()
    script=(smoke.bundled_module("telemetry_profiler",LAB/"telemetry/telemetry_profiler.py")
            +smoke.bundled_module("guest_probe",LAB/"diagnostics/guest.py")+(HERE/"screen_guest.py").read_text())
    def invocation(implementation):
        return remote(target,message,script,f"screen_once(message, {probe!r}, {implementation!r})")
    # The external supervisor issues each arrival. The guest only observes the single requested call.
    stock=invocation("stock")
    calls=[invocation("fast") for _ in range(20)]
    raw={"stock":stock["observation"],"fast":[c["observation"] for c in calls],"target":stock["target"],
         "wall_seconds":sum(c["wall_seconds"] for c in calls),
         "windows":[{k:v for k,v in c.items() if k!="observation"} for c in calls]}
    raw_ref=artifact(folder,"report-raw.json",raw)
    after=remote(target,dict(message,op="counts"))
    count_ref=artifact(folder,"persistence.json",{"before":before,"after":after})
    require(before==after and not any(v[3] for v in after["services"].values()),"posting counts or service identity changed")
    comparisons=check_screen(raw,expected_source)
    finance_ref=artifact(folder,"report-parity.json",comparisons)
    controls=financial_verifier.run_negative_controls(precision=raw["stock"]["precision"]["currency"])
    require(controls["ok"],"financial verifier missed a mutation")
    bad=copy.deepcopy(raw["fast"][0])
    field=next(i for i,c in enumerate(bad["columns"]) if c.get("fieldname")=="gross_profit")
    bad["rows"][0][field]=str(financial_verifier.Decimal(str(bad["rows"][0][field]))+financial_verifier.Decimal("0.001"))
    require(not smoke.compare(raw["stock"],bad)["matches"],"live report corruption escaped the comparison")
    control_ref=artifact(folder,"mutation-controls.json",{"verifier":controls,"corrupted_report_caught":True})
    windows=[a["guest_window"] for a in raw["fast"]]
    samples=[v for c in calls for v in c["memory_samples"]]
    require(samples,"missing whole-guest memory samples")
    measurements=[a["measurements"] for a in raw["fast"]]
    artifact(folder,"report-priorities.json",report_priorities(measurements))
    latency=sorted(m["wall_ns"]/1e6 for m in measurements)
    metrics={"cpu_core_seconds":sum((c["resource_end"]["cpu_ticks"]-c["resource_start"]["cpu_ticks"])
                                     /c["ticks_per_second"] for c in calls),
             "peak_memory_bytes":max(samples+[c[k]["memory_bytes"] for c in calls for k in ("resource_start","resource_end")]),
             "sql_seconds":sum(m["sql_ms"] for m in measurements)/1000,
             "io_bytes":sum(c["resource_end"]["disk_bytes"]-c["resource_start"]["disk_bytes"] for c in calls),
             "lock_wait_ms":sum(m["mariadb_global_deltas"]["Innodb_row_lock_time"] for m in measurements),
             "summary_p95_ms":latency[math.ceil(.95*len(latency))-1]}
    require(all(math.isfinite(v) and v>=0 for v in metrics.values()),"counter reset or invalid metric")
    oom=sum(t["memory"]["oom_kill_delta"] for w in windows for t in w["targets"].values())
    require(oom==0,"OOM kill during report screen")
    telemetry_ref=artifact(folder,"screen-telemetry.json",{"metrics":metrics,"raw":raw_ref,"lifecycle":count_ref})
    workload_ref=artifact(folder,"screen-load.json",{"offered":20,"completed":len(latency),"wall_ms":latency,
                                                      "duration_seconds":raw["wall_seconds"]})
    probes=[{"name":name,"gate":gate,"read_only":True,"verified":True,**ref} for name,gate,ref in (
        ("exact gross profit fields","finance_parity",finance_ref),("financial controls","mutation_controls",control_ref),
        ("serial report invocations","workload_complete",workload_ref),("guest counters","telemetry_complete",telemetry_ref))]
    return {"schema_version":adapter.HARNESS_EVIDENCE_SCHEMA,"identity":req,"target":raw["target"],"app":app,
            "gates":{g:True for g in adapter.GATES},"metrics":metrics,
            "counters":{"oom_kills":oom,"restarts":0,"dropped":20-len(latency),"duplicate_postings":0,"lost_vouchers":0},
            "seasonal_result":{**workload_ref,"complete":True,"workload":SCREEN,"offered":20,"started":20,
                               "completed":20,"duration_seconds":raw["wall_seconds"]},
            "finance_evidence":{**finance_ref,"read_only":True,"verified":True,"checks":[{"name":"stock-visible rows","passed":True}]},
            "telemetry":{**telemetry_ref,"complete":True},
            "mutation_controls":{**control_ref,"read_only":True,"verified":True,
                                 "controls":[{"name":c["name"],"expected_failure_detected":c["caught"]} for c in controls["controls"]]},
            "probes":probes}


def execute(envelope):
    require(envelope["schema_version"]==adapter.HARNESS_REQUEST_SCHEMA,"unsupported harness request")
    req=adapter.validate_request(envelope["request"])
    folder=Path(envelope["artifacts_dir"]).resolve(strict=True)
    require(folder==Path.cwd().resolve() and str(folder)==req["artifacts_dir"],"artifact directory disagreement")
    require(req["workload"] in envelope["fixture"]["manifest"]["workloads"],"workload unsupported by restored fixture")
    require(req["workload"] in (SCREEN,adapter.CASHIER_SCREEN,SEASONAL),"unknown workload")
    provision=envelope["provision"]
    require(provision["namespace"]==adapter.arm_namespace(req) and provision["fixture_sha256"]==req["fixture_sha256"]
            and provision["restored_from_immutable_fixture"] is True,"provision identity mismatch")
    target=envelope["environment"]["ssh"]
    require(target["host"]=="khitmee-lab","unexpected SSH host")
    message={"namespace":provision["namespace"],"fixture_sha256":req["fixture_sha256"],"target":envelope["environment"]["target"]}
    archive,expected_source=arm_source(req,folder)
    app={"source_sha256":sha(archive),"commit":req["commit"],"patch_sha256":req["patch_sha256"]}
    try:
        details=remote(target,dict(message,op="deploy",source=base64.b64encode(archive).decode(),source_sha256=sha(archive)))
        artifact(folder,"deployed.json",details)
        if req["workload"]==SCREEN:
            return screen_run(req,folder,target,message,app,expected_source)
        capabilities=remote(target,dict(message,op="capabilities"))
        if req["workload"] == adapter.CASHIER_SCREEN:
            sys.path.insert(0, str(LAB / "seasonal-smoke"))
            import supervisor_arm
            return supervisor_arm.run(req, folder, target, message, app, capabilities)
        artifact(folder,"seasonal-readiness.json",capabilities)
        import seasonal_acceptance
        return seasonal_acceptance.run(req,folder,target,message,app,capabilities)
    finally:
        original_error = sys.exc_info()[1]
        try:
            cleanup = remote(target,dict(message,op="cleanup"))
        except (OSError, ValueError, subprocess.SubprocessError) as cleanup_error:
            artifact(folder,"driver-cleanup.json",{"stopped":False,"error":str(cleanup_error)})
            if original_error is None:
                raise
            print("Additional cleanup failure: " + str(cleanup_error), file=sys.stderr)
        else:
            artifact(folder,"driver-cleanup.json",cleanup)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness",action="store_true")
    parser.add_argument("--ssh-config",default=adapter.DEFAULT_SSH_CONFIG)
    args=parser.parse_args()
    try:
        if args.readiness:
            result=readiness({"config":args.ssh_config,"host":"khitmee-lab"})
        else:
            result=execute(json.load(sys.stdin))
        print(json.dumps(result,allow_nan=False))
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError,adapter.Refusal) as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(2)
