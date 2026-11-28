"""Run the installed seasonal k6 workload against the freshly restored arm."""
from collections import Counter
from contextlib import contextmanager
from datetime import date, timedelta
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
PREFIX = "KHITMEE_SEASONAL_EVENT "
POSTING_KINDS = {"sale", "transfer", "purchase_receipt", "purchase_invoice"}


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def pinned_file(ref):
    from khitmee_lab_adapter import file_hash
    path = Path(ref["path"]).resolve(strict=True)
    need(file_hash(path) == ref["sha256"], "seasonal input changed: " + path.name)
    return path


@contextmanager
def guest_tunnel(target, port):
    need(target["host"] == "khitmee-lab" and port == 18086, "expected isolated guest frontend")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        local = sock.getsockname()[1]
    with subprocess.Popen(["ssh", "-TN", "-F", target["config"], "-o", "ExitOnForwardFailure=yes",
                           "-L", f"127.0.0.1:{local}:127.0.0.1:{port}", target["host"]]) as proc:
        try:
            for _ in range(80):
                need(proc.poll() is None, "SSH forwarding failed")
                try:
                    with socket.create_connection(("127.0.0.1", local), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise ValueError("timed out opening isolated guest frontend")
            yield f"http://127.0.0.1:{local}"
        finally:
            proc.terminate()


def p95(values):
    need(values and all(type(v) in (float, int) and math.isfinite(v) and v >= 0 for v in values),
         "missing or invalid route latency")
    return sorted(values)[math.ceil(.95 * len(values)) - 1]


def score(events):
    configs = [e["config"] for e in events if e["event"] == "config"]
    need(len(configs) == 1 and configs[0]["profile"] == "acceptance", "expected full acceptance workload")
    config = configs[0]
    need(config["managers_enabled"] and config["export_enabled"], "manager workload disabled")
    segments = {s["name"]: s for s in config["phases"]}
    for name, seconds, scale in (("baseline", 3600, 1), ("margin", 900, 1.25), ("recovery", 120, 1)):
        need((segments[name]["duration_seconds"], segments[name]["scale"]) == (seconds, scale),
             "unexpected duration or offered rate for " + name)
    committed = [e for e in events if e["event"] == "voucher_complete"]
    starts = [e for e in events if e["event"] == "voucher_start"]
    ids = Counter(e["client_voucher_id"] for e in committed)
    rejected = [e for e in events if e["event"] == "voucher_reject"]
    unknown = [e for e in events if e["event"] == "voucher_unknown"]
    rejects = Counter(e["client_voucher_id"] for e in rejected)
    unknowns = Counter(e["client_voucher_id"] for e in unknown)
    need(all(e.get("phase") == "overload" for e in rejected + unknown), "voucher rejection or unknown outcome outside overload")
    terminals = ids + rejects + unknowns
    need(ids and max(terminals.values()) == 1 and terminals == Counter(e["client_voucher_id"] for e in starts),
         "duplicate or unconfirmed submissions")
    need(len({(e["type"], e["voucher_no"]) for e in committed}) == len(ids), "voucher reused across requests")
    need(any(e["event"] == "export_complete" for e in events), "full-year export never completed")
    latency = {}
    for segment in ("baseline", "margin", "recovery"):
        window = [e for e in events if e.get("segment") == segment]
        count = segments[segment]["duration_seconds"] // 60
        done = [e for e in window if e["event"] == "voucher_complete"]
        for minute in range(count):
            minute_docs = [e for e in done if e["minute"] == minute]
            shops = {e["warehouse"] for e in minute_docs if e["contention"] and e["type"] == "sale"}
            need(len(shops) == 6, "missing six-shop contention burst")
        for kind, field in (("sale","sale"), ("transfer","transfer"), ("purchase_receipt","receipt"),
                            ("purchase_invoice","purchase_invoice")):
            latency.setdefault(field + "_p95_ms", []).append(p95([e["duration_ms"] for e in done if e["type"] == kind]))
        latency.setdefault("large_sale_p95_ms", []).append(p95([e["duration_ms"] for e in done
                                                              if e["type"] == "sale" and e["line_count"] == 200]))
        for event, field, key, minimum in (("search_complete","search","duration_ms",count*80),
                                           ("manager_detail","detail","first_50_duration_ms",count*2)):
            values = [e[key] for e in window if e["event"] == event]
            need(len(values) >= minimum, "missing " + event + " arrivals")
            latency.setdefault(field + "_p95_ms", []).append(p95(values))
        for grouping in ("Warehouse", "Monthly"):
            queries = [e for e in window if e["event"] == "manager_summary" and e["group_by"] == grouping]
            need(len(queries) >= count*6, "missing manager refreshes for " + grouping)
            latency.setdefault("summary_p95_ms", []).append(p95([e["duration_ms"] for e in queries if e["warm"]]))
    return {key:max(values) for key,values in latency.items()}, committed, config["total_duration_seconds"]


def read_events(path):
    events = []
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            record = json.loads(line)
            msg = record.get("msg", "")
            if msg.startswith(PREFIX):
                events.append(json.loads(msg[len(PREFIX):]))
    return events


def compare_manager_outputs(reference, req, events):
    need(reference["fixture_sha256"] == req["fixture_sha256"] and reference["seed"] == req["seed"],
         "manager oracle uses a different fixture or workload seed")
    observed = [e for e in events if e.get("observation_id")]
    observations = {e["observation_id"]: e for e in observed}
    need(len(observations) == len(observed), "duplicate manager observation ID")
    covered = set()
    for case in reference["observations"]:
        event = observations.get(case["observation_id"])
        need(event and case["snapshot"] and event["event"] == case["event"], "missing manager oracle observation")
        group = event.get("group_by") or event["event"]
        hash_field = {"manager_summary": "result_sha256", "manager_detail": "all_pages_sha256",
                      "export_complete": "sha256"}[event["event"]]
        digest = event.get(hash_field)
        need(group == case["group"] and len(case["sha256"]) == 64 and digest == case["sha256"],
             "stock report mismatch at " + case["observation_id"])
        covered.add(group)
    need({"Warehouse", "Monthly", "manager_detail", "export_complete"} <= covered,
         "stock oracle must cover both summaries, paged detail, and downloaded CSV")


def workload_checks(events, summary):
    from evaluate_seasonal import evaluate_events
    return evaluate_events(events, summary)


def run(req, folder, target, message, app, capabilities):
    from seasonal_driver import LAB, adapter, artifact, financial_verifier
    need(capabilities["fixture_scale"] != "smoke", "requires a full-year seasonal fixture")
    need(capabilities["full_year_export"], "requires requester-bound full-year CSV export")
    path = HERE / "seasonal.json"
    need(path.is_file(), "install seasonal.json in the pinned harness with fixture, k6, actors and stock references")
    cfg = json.loads(path.read_bytes())
    need(cfg["fixture_sha256"] == req["fixture_sha256"], "wrong seasonal fixture")
    k6, manifest, actor_config = (pinned_file(cfg[key]) for key in ("k6", "seed_manifest", "actors"))
    window = capabilities["report_window"]
    start = date.fromisoformat(window["from_date"])
    end = date.fromisoformat(cfg["as_of"])
    need((date.fromisoformat(window["to_date"]) - start).days >= 364 and start <= end - timedelta(days=364),
         "seasonal fixture needs at least 365 days of seeded history")
    reference = pinned_file(cfg["references"][str(req["seed"])])
    report_reference = json.loads(pinned_file(cfg["manager_oracles"][str(req["seed"])]).read_bytes())
    from posting_evidence import capture, capture_before
    from seasonal_telemetry import measure
    sys.path.insert(0, str(LAB.parent / "k6"))
    actor_env = json.loads(actor_config.read_bytes())
    need(all(isinstance(v,str) and (k.startswith(("PERF_","SEASONAL_MANAGER_","SEASONAL_CASHIER_")))
             for k,v in actor_env.items()), "unexpected actor settings")
    prefixes = ["PERF"] + ["SEASONAL_MANAGER_" + role for role in ("WAREHOUSE", "MONTHLY", "DETAIL", "EXPORT")]
    need(all(actor_env.get(p+"_USER") and actor_env.get(p+"_PASSWORD") for p in prefixes),
         "cashier, report reader or export credentials missing")
    users = [actor_env[p+"_USER"].casefold() for p in prefixes]
    need("administrator" not in users and len(set(users)) == len(prefixes),
         "expected distinct cashier, warehouse reader, monthly reader, detail reader and export requester")
    env = {k:os.environ[k] for k in ("PATH","HOME","LANG") if k in os.environ}
    env.update(actor_env, RUN_ID=f"season-{req['seed']}", SEASONAL_SEED=str(req["seed"]),
               SEASONAL_PROFILE="acceptance", SITE_NAME=capabilities["site"], SEED_MANIFEST_PATH=str(manifest),
               REPORT_TO_DATE=end.isoformat(), REPORT_FROM_DATE=end.replace(month=1, day=1).isoformat(),
               EXPORT_FROM_DATE=(end-timedelta(days=364)).isoformat(),
               SEASONAL_EXPORT_ENQUEUE_METHOD="khitmee_ops.profit_summary.export_service.enqueue_profit_csv_export",
               SEASONAL_EXPORT_DOWNLOAD_METHOD="khitmee_ops.profit_summary.export_service.download_profit_csv_export")
    summary_path = folder/"k6-summary.json"
    with guest_tunnel(target, capabilities["http_port"]) as url, tempfile.TemporaryDirectory() as tmp:
        env["TARGET_URL"] = url
        log = Path(tmp)/"k6.log"
        capture_before(req, target, message)
        with measure(target, message, folder) as telemetry:
            proc = subprocess.run([str(k6), "run", "--quiet", "--log-format=json", "--log-output=file="+str(log),
                                   "--summary-export", str(summary_path), str(LAB.parent/"k6/scenarios/seasonal.js")],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3*3600)
        events = read_events(log)
        artifact(folder,"seasonal-events.json",events)
        need(proc.returncode in (0,99), "k6 setup or execution failed")
    summary = json.loads(summary_path.read_bytes())
    dropped = summary["metrics"].get("dropped_iterations",{}).get("count",0)
    need(dropped == 0, "load generator dropped scheduled arrivals")
    decision = workload_checks(events,summary)
    workload_ref = artifact(folder,"seasonal-verdict.json",decision)
    metrics, posted, duration = score(events)
    evidence = capture(req,target,message,events,reference)
    need(evidence["fixture_id"] == req["fixture_sha256"], "committed finance fixture mismatch")
    expected = {e["client_voucher_id"] for e in posted}
    operations = evidence["operations"]
    need(len(operations) == len(expected) and {o["client_voucher_id"] for o in operations} == expected
         and all("reference" in o and "candidate" in o for o in operations), "incomplete stock comparison")
    finance = financial_verifier.verify_evidence_document(evidence)
    finance_ref = artifact(folder,"seasonal-finance-verdict.json",finance)
    need(finance["ok"], "one or more independent posting comparisons failed")
    compare_manager_outputs(report_reference, req, events)
    retries = {e["client_voucher_id"] for e in posted if e.get("lost_ack")}
    proven = {op["client_voucher_id"] for op in finance["operations"] if op["proves_committed_retry"]}
    need({e["type"] for e in posted if e["client_voucher_id"] in retries} == POSTING_KINDS
         and retries <= proven, "missing committed lost-confirmation replay in a voucher class")
    controls = financial_verifier.run_negative_controls()
    control_ref = artifact(folder,"seasonal-mutations.json",controls)
    need(controls["ok"], "financial mutation missed")
    need(decision["passed"] is True, "seasonal workload, recovery or financial report check failed")
    telemetry_ref = {k:telemetry[k] for k in ("artifact","sha256")}
    probes = [{"name":name, "gate":gate, "read_only":True, "verified":True, **ref}
              for name,gate,ref in (("committed independent comparisons","finance_parity",finance_ref),
                                   ("financial mutations","mutation_controls",control_ref),
                                   ("offered seasonal arrivals","workload_complete",workload_ref),
                                   ("whole guest and SQL counters","telemetry_complete",telemetry_ref))]
    # evaluate_events matches the observed per-minute shape to an exact
    # endpoint-bounded k6 schedule, and rejects dropped iterations.
    schedule = decision["arrival_schedule"]
    need(schedule and all(row["matched"] for row in schedule), "no matched seasonal arrival schedule")
    offered = sum(sum(row["shape_totals"].values()) for row in schedule)
    started = sum(row["started"] for row in schedule)
    need(offered == started, "some scheduled vouchers did not start")
    return {"schema_version":adapter.HARNESS_EVIDENCE_SCHEMA,"identity":req,"target":message["target"],"app":app,
            "gates":{gate:True for gate in adapter.GATES},"metrics":{**metrics,**telemetry["metrics"]},
            "counters":{**telemetry["counters"],"dropped":dropped,"lost_vouchers":0,"duplicate_postings":0},
            "seasonal_result":{**workload_ref,"complete":True,"workload":req["workload"],
                               "offered":offered,"started":started,
                               "completed":len(expected),"duration_seconds":duration},
            "finance_evidence":{**finance_ref,"read_only":True,"verified":True,
                                "checks":[{"name":"committed stock and retry comparison","passed":True}]},
            "telemetry":{**telemetry_ref,"complete":True},"probes":probes,
            "mutation_controls":{**control_ref,"verified":True,"read_only":True,
                                 "controls":[{"name":c["name"],"expected_failure_detected":c["caught"]}
                                             for c in controls["controls"]]}}
