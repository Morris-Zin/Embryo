"""Replay a cashier receipt and produce a redacted profile for the experiment loop."""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path

from run import evaluate


def profile(path):
    raw = path.read_bytes()
    receipt = json.loads(gzip.decompress(raw))
    result = evaluate(receipt["events"], receipt["before"], receipt["after"])
    if result != receipt["result"]:
        raise ValueError("cashier evidence does not replay")
    groups = result["groups"]
    by_id = {e["id"]: e["group"] for e in receipt["events"] if e["type"] == "arrival"}
    confirmations = [e for e in receipt["events"] if e["type"] == "confirmed"]
    ranks = []
    for metric, measure in (("confirmation_p95_ms", "duration_ms"), ("client_http_duration_p95_ms", "http_ms")):
        entries = []
        for name in groups:
            values = sorted(e[measure] for e in confirmations if by_id[e["id"]] == name)
            if values:
                entries.append({"target": name, "value": values[math.ceil(0.95 * len(values)) - 1], "samples": len(values)})
        ranks.append({"metric": metric, "unit": "ms", "entries": sorted(entries, key=lambda e: -e["value"])})
    result_profile = {"schema": "khitmee.cashier-discovery/v1", "status": "scoped", "workload": "one-minute-sales-subgroup",
            "seasonal_qualified": False, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "fixture_sha256": receipt["setup"]["state"]["fixture_sha256"], "groups": groups,
            "scope": receipt["scope"], "db_changes": result["db_changes"],
            "lost_confirmations": len(result["confirmed_missing_from_db"]) + len(result["committed_without_confirmation"]),
            "financial_mismatches": len(result["financial_mismatches"]), "rankings": ranks,
            "coverage": {"measured": ["submit-to-confirmation", "HTTP request duration", "database outcome"],
                         "missing": ["HTTP waiting subphase", "server-only latency", "CPU", "peak memory", "SQL timings",
                                     "block I/O", "lock waits"]},
            "hypotheses": [{"confidence": "low", "objective": "large_sale_p95_ms",
                            "statement": "Investigate the 200-line sale path under concurrent smaller arrivals.",
                            "evidence": ranks[0]["entries"],
                            "next_checks": ["Capture per-voucher SQL, lock waits, backend CPU and queue time during the same arrival mix before selecting a code change."]}]}
    if "telemetry" in receipt:
        t = receipt["telemetry"]
        top_sql = [r for r in t["sql"]["statements"]["top_by_time"] if r["count_delta"]]
        db = t["sql"]
        kernel = t["kernel"]
        start, end = kernel["before"], kernel["after"]
        report = {"duration_seconds": t["window"]["measured_duration_seconds"],
                  "resource_coverage": t["coverage"], "observer_status": t["status"],
                  "cpu_core_seconds": (end["busy_ticks"] - start["busy_ticks"]) / kernel["clock_ticks_per_second"],
                  "peak_memory_bytes": kernel["memory_peak_sampled_bytes"],
                  "disk_bytes_read": end["read_bytes"] - start["read_bytes"],
                  "disk_bytes_written": end["written_bytes"] - start["written_bytes"],
                  "site_digest_calls": sum(r["count_delta"] for r in top_sql),
                  "site_sql_seconds": sum(r["time_ms_delta"] for r in top_sql) / 1000,
                  "lock_waits": db["lock_waits"],
                  "site_digest_top_by_time": [{k: row.get(k) for k in (
                      "digest", "digest_text", "count_delta", "time_ms_delta", "rows_examined_delta", "rows_sent_delta")}
                      for row in top_sql[:8]]}
        result_profile["resource_window"] = report
        result_profile["coverage"] = {"measured": ["cashier wall and HTTP waiting time", "container CPU and throttling",
                "sampled container and guest memory", "guest and container block I/O", "site SQL digest counters",
                "instance-wide InnoDB row locks", "database outcome"],
            "missing": ["per-voucher CPU/SQL/locks", "queue time within HTTP waiting", "server-only latency"]}
        result_profile["rankings"].extend({"metric": r["metric"], "unit": r["unit"], "entries": r["entries"][:8]}
                                          for r in t["rankings"])
        wait_by_group = {}
        for name in groups:
            samples = sorted(e["http_waiting_ms"] for e in confirmations if by_id[e["id"]] == name)
            if samples:
                wait_by_group[name] = samples[math.ceil(0.95 * len(samples)) - 1]
        result_profile["http_waiting_p95_ms"] = wait_by_group
        result_profile["hypotheses"] = t["hypotheses"]
        if top_sql:
            top = report["site_digest_top_by_time"][0]
            next_checks = ["Trace the leading statement with request identities during a paired 200-line sale trial. "
                           "Compare confirmed latency, query count and SQL time while preserving stock and GL postings."]
            if "tabSeries" in str(top["digest_text"]) and "FOR UPDATE" in str(top["digest_text"]):
                next_checks = ["Measure the interval from invoice naming-series acquisition to transaction commit "
                               "for 10-, 50- and 200-line invoices. Capture requesting and blocking connection IDs.",
                               "Test moving independently verifiable work ahead of that acquisition. Compare lock-hold duration, "
                               "waiter time and cashier p95 while preserving the existing numbering, rollback, permissions and posting semantics."]
            result_profile["hypotheses"].insert(0, {"objective": "large_sale_p95_ms", "confidence": "low",
                "statement": "Test whether the leading SQL digest contributes to slow concurrent cashier confirmations.",
                "evidence": [top, report["lock_waits"], ranks[0]["entries"][0]], "next_checks": next_checks})
    return result_profile


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("receipt", type=Path)
    print(json.dumps(profile(args.parse_args().receipt), indent=2, allow_nan=False))
