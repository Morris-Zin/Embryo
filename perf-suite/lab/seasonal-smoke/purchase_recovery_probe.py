#!/usr/bin/env python3
"""Low-load targeted probe driver: prove positive read-only purchase recovery.

Runs the witness purchase_cycle through the production cascade on a freshly
restored year arm, then independently audits the database. Split evidence:
the withheld confirmation is deliberate benchmark behavior at trivial load,
not a genuine overload lost-acknowledgement witness.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "driver"))
sys.path.insert(0, str(HERE))
import mixed_minute as mm
import seasonal_acceptance as acceptance
import native_replay

RUN_ID = "purchase-recovery-probe-20261127"


def write(path, obj):
    with gzip.open(path, "xt", encoding="utf-8") if path.suffix == ".gz" else path.open("x", encoding="utf-8") as out:
        json.dump(obj, out, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def completion(events, kind):
    rows = [e for e in events if e.get("event") == "voucher_complete" and e.get("type") == kind]
    return rows[0] if rows else None


def run(args):
    os.umask(0o077)
    cfg = json.loads(args.context.read_bytes())
    request, target, message = native_replay.arm(cfg)
    if message["fixture_sha256"] != mm.FIXTURE or str(request["seed"]) != mm.BASE_SEED or request["arm"] != "baseline":
        raise ValueError("unexpected year fixture or host")
    actors = acceptance.pinned_file({"path": str(args.actors), "sha256": mm.ACTORS})
    manifest = acceptance.pinned_file({"path": str(args.manifest), "sha256": mm.MANIFEST})
    k6 = acceptance.pinned_file({"path": str(args.k6), "sha256": mm.K6})
    folder = args.folder
    folder.mkdir(exist_ok=False)
    role_env = json.loads(actors.read_bytes())
    seed = json.loads(manifest.read_bytes())
    end = seed["report_window"]["to_date"]
    env = {k: os.environ[k] for k in ("HOME", "LANG", "PATH") if k in os.environ}
    env.update(role_env, SEED_MANIFEST_PATH=str(manifest), SEASONAL_PROFILE="smoke",
               SEASONAL_REQUIRE_RETRY_EVIDENCE="1", SEASONAL_ENABLE_MANAGERS="0", SEASONAL_ENABLE_EXPORT="0",
               SITE_NAME="bench-loadtest.localhost", RUN_ID=RUN_ID, SEASONAL_SEED=mm.BASE_SEED,
               REPORT_TO_DATE=end, REPORT_FROM_DATE=end[:4] + "-01-01")
    log = folder / "probe-events.log"
    code = None
    reasons = {}
    with acceptance.guest_tunnel(target, 18086) as url:
        env["TARGET_URL"] = url
        try:
            with (folder / "probe-output.txt").open("xb") as output:
                args_k6 = [str(k6), "run", "--quiet", "--log-format=json", "--log-output=file=" + str(log),
                           "--summary-export", str(folder / "probe-k6-summary.json"), str(HERE / "purchase_recovery_probe.js")]
                code = subprocess.run(args_k6, env=env, stdout=output, stderr=output, timeout=300).returncode
        except subprocess.TimeoutExpired:
            reasons["load_generator"] = "timed out after 300s"
    events = mm.load_events(log) if log.is_file() else []
    write(folder / "probe-events.json.gz", events)

    verdict = {"schema": "khitmee.purchase-recovery-probe/v1", "run_id": RUN_ID, "k6_exit": code, "errors": reasons}
    ok = True
    for kind in ("purchase_receipt", "purchase_invoice"):
        event = completion(events, kind)
        case = {"completed": bool(event)}
        if event:
            same = event.get("first_committed_voucher") and event.get("first_committed_voucher") == event.get("voucher_no")
            case.update({
                "client_voucher_id": event.get("client_voucher_id"),
                "voucher_no": event.get("voucher_no"),
                "first_committed_voucher": event.get("first_committed_voucher"),
                "resubmitted": bool(event.get("resubmitted")),
                "recovery_attempted": bool(event.get("recovery_attempted")),
                "recovery_reads": event.get("recovery_reads"),
                "reconciled": bool(event.get("reconciled")),
                "retry_confirmed": bool(event.get("retry_confirmed")),
                "same_voucher_recovered": bool(same),
            })
            case["proven"] = bool(case["recovery_attempted"] and case["reconciled"] and not case["resubmitted"]
                                  and case["same_voucher_recovered"] and case["retry_confirmed"])
        else:
            case["proven"] = False
        ok = ok and case["proven"]
        verdict[kind] = case
    verdict["both_cases_proven"] = ok

    db = mm.ArmDB(target, message)
    if any(e.get("event") == "voucher_start" for e in events):
        prefix = next((e["config"]["run_prefix"] for e in events if e.get("event") == "config" and e.get("config")), None)
        if prefix:
            verifier = mm.verifier
            like = verifier.sql_like_contains(prefix)
            receipts = db.query(
                "SELECT name,docstatus,REPLACE(REPLACE(COALESCE(remarks,''),'\\t',' '),'\\n',' ') "
                "FROM `tabPurchase Receipt` WHERE remarks LIKE " + like)
            invoices = db.query(
                "SELECT name,docstatus,bill_no FROM `tabPurchase Invoice` WHERE bill_no LIKE " + like)
            audit = {
                "receipt_rows": receipts,
                "invoice_rows": invoices,
                "receipt_count": len(receipts),
                "invoice_count": len(invoices),
                "exactly_one_each": len(receipts) == 1 and len(invoices) == 1,
                "all_rows_submitted": all(str(r[1]) in {"1", "1.0"} for r in receipts + invoices),
            }
            if len(invoices) == 1:
                links = db.query(
                    "SELECT COUNT(*),SUM(COALESCE(purchase_receipt,'')=''),SUM(COALESCE(pr_detail,'')='') "
                    "FROM `tabPurchase Invoice Item` WHERE parent=" + verifier.sql_string(invoices[0][0]))
                audit["invoice_item_link_row"] = links[0] if links else None
                if links and links[0]:
                    audit["all_invoice_rows_linked"] = int(float(links[0][1] or 0)) == 0 and int(float(links[0][2] or 0)) == 0
            by = {k: {e["client_voucher_id"]: e for e in events if e["event"] == v}
                  for k, v in (("started", "voucher_start"), ("completed", "voucher_complete"),
                               ("rejected", "voucher_reject"), ("unknown", "voucher_unknown"))}
            by["events"] = events
            by["config"] = next((e["config"] for e in events if e.get("event") == "config"), {})
            if by["config"]:
                try:
                    audit["independent_verifier"] = verifier.verify(by, db)
                except (ValueError, KeyError, RuntimeError, TypeError) as ex:
                    audit["independent_verifier_error"] = str(ex)
            verdict["database_audit"] = audit
    verdict["split_evidence_caveat"] = ("The withheld first confirmation is deliberate benchmark witness behavior "
                                        "at trivial load, not a genuine overload lost-acknowledgement witness.")
    audit = verdict.get("database_audit") or {}
    verifier_result = audit.get("independent_verifier") or {}
    proof_ok = bool(
        verdict["both_cases_proven"]
        and audit.get("exactly_one_each")
        and audit.get("all_rows_submitted")
        and audit.get("all_invoice_rows_linked")
        and verifier_result.get("passed")
        and not verifier_result.get("defects")
    )
    verdict["proof_complete"] = proof_ok
    if not proof_ok:
        verdict["proof_failure"] = "missing positive recovery proof, exact database shape, or clean verifier result; failing closed"
    write(folder / "probe-summary.json", verdict)
    print(json.dumps(verdict, indent=1, sort_keys=True))
    if not proof_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("context", "actors", "manifest", "k6", "folder"):
        parser.add_argument("--" + arg, type=Path, required=True)
    run(parser.parse_args())
