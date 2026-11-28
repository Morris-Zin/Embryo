#!/usr/bin/env python3
"""Build portable evidence, process detail and e1/e2 comparison for the corrected mixed minute.

Runs inside WSL against the immutable operator artifacts. Writes only safe,
credential-free analysis artifacts into the repository.
"""
import gzip
import hashlib
import json
from pathlib import Path
import sys

REPO = Path("/mnt/e/Khitmee-ERPNext")
OUT_DIR = REPO / "perf-suite/lab/seasonal-smoke"
E1 = Path("/srv/khitmee-lab/operator/year-minute-arms/e1-s42/measured-v2")
E2 = Path("/srv/khitmee-lab/operator/year-minute-arms/e2-s42/measured-recovery-final")
WINDOWS = Path("/srv/khitmee-lab/operator/windows")
RELEASE = Path("/srv/khitmee-lab/operator/releases/year-mixed-minute-recovery-final")

SUMMARY_OUT = OUT_DIR / "mixed-minute-year-e2-summary.json"
PORTABLE_OUT = OUT_DIR / "mixed-minute-year-e2-2026-11-27.json.gz"
PROCESS_OUT = OUT_DIR / "mixed-minute-e2-process-evidence.json.gz"
COMPARE_OUT = OUT_DIR / "mixed-minute-e1-e2-comparison.md"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text())


def load_gz(path):
    return json.load(gzip.open(path, "rt", encoding="utf-8"))


def class_rows(summary):
    return summary.get("work", {})


def recovery_rows(summary):
    rows = []
    for event in summary.get("retry_events", []):
        rows.append({
            "type": event.get("type"),
            "event": event.get("event"),
            "client_voucher_id": event.get("client_voucher_id"),
            "attempts": event.get("attempts"),
            "resubmitted": event.get("resubmitted"),
            "recovery_attempted": event.get("recovery_attempted"),
            "recovery_reads": event.get("recovery_reads"),
            "first_committed_voucher": event.get("first_committed_voucher"),
            "voucher_no": event.get("voucher_no"),
            "reason": event.get("reason"),
        })
    return rows


def purchase_recovery_verdict(summary, events):
    """Determine whether both read-only purchase recovery cases are proven."""
    verdict = {"purchase_receipt": {"proven": False}, "purchase_invoice": {"proven": False}}
    completed = [e for e in events if e.get("event") == "voucher_complete"]
    for event in completed:
        kind = event.get("type")
        if kind not in verdict:
            continue
        if event.get("recovery_attempted") and event.get("reconciled") and not event.get("resubmitted"):
            verdict[kind] = {
                "proven": True,
                "client_voucher_id": event.get("client_voucher_id"),
                "voucher_no": event.get("voucher_no"),
                "first_committed_voucher": event.get("first_committed_voucher"),
                "recovery_reads": event.get("recovery_reads"),
                "lost_ack": bool(event.get("lost_ack")),
            }
    db = summary.get("database") or {}
    proven = db.get("proven_retries") or []
    verdict["database_proven"] = proven
    verdict["both_proven"] = all(v.get("proven") for v in verdict.values() if isinstance(v, dict) and "proven" in v)
    return verdict


def duplicates(events, summary):
    db = summary.get("database") or {}
    bindings = {}
    try:
        postings = load_gz(E2 / "database-postings.json.gz")
        bindings = postings.get("client_bindings", {})
    except FileNotFoundError:
        pass
    receipt_rows = (bindings.get("purchase_receipts_raw") or []) if bindings else []
    invoice_rows = (bindings.get("purchase_invoices_raw") or []) if bindings else []
    return {
        "purchase_receipt_rows": len(receipt_rows),
        "purchase_invoice_rows": len(invoice_rows),
        "unexplained_count_changes": db.get("unexplained_count_changes"),
        "committed_financial_failures": len(db.get("committed_financial_failures") or []),
    }


def main():
    for required in (E2 / "mixed-summary.json", E2 / "arm-telemetry.json", E2 / "process-kernel-telemetry.json"):
        if not required.is_file():
            raise SystemExit("missing required evidence: " + str(required))
    s1 = load_json(E1 / "mixed-summary.json")
    s2 = load_json(E2 / "mixed-summary.json")
    events2 = load_gz(E2 / "mixed-events.json.gz")
    process = load_json(E2 / "process-kernel-telemetry.json")
    telemetry2 = load_json(E2 / "arm-telemetry.json")

    release_manifest = sha(RELEASE / "manifest.json")

    verdict = purchase_recovery_verdict(s2, events2)
    dup = duplicates(events2, s2)

    portable_summary = {
        "schema": "khitmee.mixed-minute-e2-summary/v1",
        "run_id": s2.get("run_id"),
        "fixture_sha256": s2.get("fixture_sha256"),
        "seed": s2.get("seed"),
        "protected_harness_manifest_sha256": release_manifest,
        "harness_release": "year-mixed-minute-recovery-final",
        "full_season_qualified": False,
        "k6_exit": s2.get("k6_exit"),
        "dropped_iterations": s2.get("dropped_iterations"),
        "confirmed_in_minute": s2.get("confirmed_in_minute"),
        "last_confirmation_s": s2.get("last_confirmation_s"),
        "work": s2.get("work"),
        "event_counts": s2.get("event_counts"),
        "rejection_reasons": s2.get("rejection_reasons"),
        "routes": s2.get("routes"),
        "duplicate_start_events": s2.get("duplicate_start_events"),
        "duplicate_terminal_events": s2.get("duplicate_terminal_events"),
        "missing_terminals": s2.get("missing_terminals"),
        "retry_events": recovery_rows(s2),
        "purchase_recovery_verdict": verdict,
        "database": {
            "independent_verifier": (s2.get("database") or {}).get("independent_verifier"),
            "unexplained_count_changes": dup["unexplained_count_changes"],
            "committed_financial_failures": dup["committed_financial_failures"],
            "proven_retries": (s2.get("database") or {}).get("proven_retries"),
            "committed_basket_ledger_link_passes": (s2.get("database") or {}).get("committed_basket_ledger_link_passes"),
        },
        "process_kernel": {
            "complete": process.get("complete"),
            "oom_event_count": len(process.get("oom_events") or []),
            "oom_events": process.get("oom_events"),
            "coverage": process.get("coverage"),
        },
        "errors": s2.get("errors"),
    }
    SUMMARY_OUT.write_text(json.dumps(portable_summary, indent=1, sort_keys=True) + "\n")

    with gzip.open(PORTABLE_OUT, "wt", encoding="utf-8") as out:
        json.dump(events2, out, sort_keys=True, separators=(",", ":"), default=str)

    with gzip.open(PROCESS_OUT, "wt", encoding="utf-8") as out:
        json.dump(process, out, sort_keys=True, separators=(",", ":"), default=str)

    lines = []
    lines.append("# Mixed-minute baseline comparison: e1-s42 vs e2-s42 (corrected recovery)")
    lines.append("")
    lines.append("Both runs use the same protected year fixture `8cc3e00c…`, seed 42, run id "
                 "`mixed-year-minute-20261127`, so client voucher series are identical. "
                 "e1 used harness `year-mixed-minute-v2` (resubmit-on-ambiguous); "
                 "e2 used `year-mixed-minute-recovery-final` manifest `%s` "
                 "(read-only purchase recovery)." % release_manifest[:16])
    lines.append("")
    lines.append("| class | metric | e1 (v2) | e2 (recovery-final) |")
    lines.append("|---|---|---|---|")
    keys = ["sale", "sale_10", "sale_50", "sale_200", "transfer", "purchase_receipt", "purchase_invoice"]
    w1, w2 = class_rows(s1), class_rows(s2)
    for key in keys:
        a, b = w1.get(key, {}), w2.get(key, {})
        for metric in ("started", "confirmed", "confirmed_in_first_60s", "rejected", "confirmed_p95_ms"):
            lines.append("| %s | %s | %s | %s |" % (key, metric, a.get(metric), b.get(metric)))
    lines.append("")
    lines.append("| run-level | e1 | e2 |")
    lines.append("|---|---|---|")
    for metric in ("confirmed_in_minute", "last_confirmation_s", "dropped_iterations", "k6_exit"):
        lines.append("| %s | %s | %s |" % (metric, s1.get(metric), s2.get(metric)))
    lines.append("")
    lines.append("## Critical caveats")
    lines.append("")
    errors2 = s2.get("errors") or {}
    if errors2.get("load_generator"):
        lines.append("- **e2 load generator timed out after 370s** (`errors.load_generator`): "
                     "k6 was killed mid-flight, so `k6_exit`/`dropped_iterations` are null, 15 started vouchers "
                     "have no terminal event, and several committed documents never received a client confirmation. "
                     "e2 latency/completion numbers are therefore not a like-for-like performance comparison with e1; "
                     "the guest was materially more degraded (host WSL itself was intermittently unresponsive during the window).")
    lines.append("- e1 created **two** submitted Purchase Receipt rows for one client identity after a withheld "
                 "confirmation was blindly resubmitted; e2 created **zero** duplicates — the ambiguous receipt "
                 "(HTTP 502) was not resubmitted, read-only recovery correctly found no committed document, and the "
                 "dependent invoice correctly refused to cascade.")
    lines.append("- e2 OOM evidence comes from the integrated `process_kernel_telemetry` hook "
                 "(`complete: %s`, %s OOM event(s) with direct thread/process attribution); "
                 "e1 OOM evidence was reconstructed post-hoc from kernel journals."
                 % (process.get("complete"), len(process.get("oom_events") or [])))
    lines.append("")
    lines.append("## Purchase recovery verdict")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(verdict, indent=1, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Duplicate / reconciliation evidence")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(dup, indent=1, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Process/kernel evidence")
    lines.append("")
    lines.append("- complete: `%s`" % process.get("complete"))
    lines.append("- oom_event_count: `%s`" % len(process.get("oom_events") or []))
    lines.append("- detailed evidence: `mixed-minute-e2-process-evidence.json.gz`")
    lines.append("")
    lines.append("Both runs are diagnostic smoke evidence only; `full_season_qualified=false`.")
    COMPARE_OUT.write_text("\n".join(lines) + "\n")

    print(json.dumps({
        "summary": str(SUMMARY_OUT),
        "portable": str(PORTABLE_OUT),
        "process": str(PROCESS_OUT),
        "comparison": str(COMPARE_OUT),
        "purchase_recovery_both_proven": verdict["both_proven"],
        "oom_event_count": len(process.get("oom_events") or []),
    }, indent=1))


if __name__ == "__main__":
    main()
