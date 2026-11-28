"""Paired, one-minute cashier sales screen on an adapter-restored guest arm."""
from collections import Counter, defaultdict
import copy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

import cashier_profile
from run import collect, evaluate

HERE = Path(__file__).resolve().parent
WORKLOAD = "cashier-sales-minute-v1"
GROUPS = {"small": (60, 10), "medium": (16, 50), "large": (4, 200)}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def by_id(events, kind):
    rows = [r for r in events if r["type"] == kind]
    result = {r["id"]: r for r in rows}
    require(len(rows) == len(result), "duplicate " + kind + " event")
    return result


def workload(receipt):
    run_id = receipt["run"]
    expected = {f"SCS-{run_id}-{group}-{seq}": (group, lines)
                for group, (count, lines) in GROUPS.items() for seq in range(count)}
    events = receipt["events"]
    starts, requests, confirmations = (by_id(events, name) for name in ("arrival", "submit", "confirmed"))
    require(receipt["exit_code"] == 0 and not by_id(events, "failed"), "failed cashier request")
    require(expected.keys() == starts.keys() == requests.keys() == confirmations.keys(), "incomplete or unexpected cashier IDs")
    ignored = [e for e in events if e["type"] == "boundary_ignored"]
    require(len({e["group"] for e in ignored}) == len(ignored)
            and all(e["sequence"] == GROUPS[e["group"]][0] and e["elapsed_ms"] >= 59900 for e in ignored),
            "invalid scheduler boundary record")
    summaries = [e for e in events if e["type"] == "summary"]
    require(len(summaries) == 1 and summaries[0]["dropped"] == 0
            and summaries[0]["iterations"] == len(starts) + len(ignored), "load-generator count mismatch")
    require(len({c["voucher"] for c in confirmations.values()}) == len(expected), "duplicate confirmed invoice")
    for key, (group, lines) in expected.items():
        items = requests[key]["items"]
        require((starts[key]["group"], starts[key]["lineCount"]) == (group, lines)
                and len(items) == len({i[0] for i in items}) == lines, "basket class or distinct line count changed")
        require(all(Decimal(str(i[1])) > 0 for i in items), "nonpositive basket quantity")
        require(confirmations[key]["status"] == 200 and math.isfinite(confirmations[key]["duration_ms"])
                and confirmations[key]["duration_ms"] > 0, "missing successful measured confirmation")
    lost = [r for r in events if r["type"] == "retry" and r["lost_ack"]]
    witness = f"SCS-{run_id}-small-0"
    require(len(lost) == 1 and lost[0]["id"] == witness and
            lost[0]["original_voucher"] == confirmations[witness]["voucher"], "lost-confirmation replay returned a different invoice")
    return {"offered": len(expected), "started": len(starts), "completed": len(confirmations), "arrival_seconds": 60,
            "ignored_k6_boundary_iterations": len(ignored), "confirmed_retry_id": witness,
            "last_confirmation_elapsed_ms": max(e["elapsed_ms"] for e in confirmations.values())}


def number(n):
    return format(Decimal(str(n)).normalize(), "f") if n else "0"


def committed(receipt, seed):
    result = evaluate(receipt["events"], receipt["before"], receipt["after"])
    require(not (result["financial_mismatches"] or result["confirmed_missing_from_db"] or
                 result["committed_without_confirmation"] or result["started_without_terminal_event"]),
            "committed quantities, GL balance, idempotency, or confirmations differ")
    requests = by_id(receipt["events"], "submit")
    prices = {p["item_code"]: Decimal(str(p["rate"])) for p in seed["items"]}
    customer = seed.get("default_customer") or seed["customers"][0]
    if isinstance(customer, dict):
        customer = customer.get("name") or customer["customer"]
    per_sale, inventory, accounts = {}, defaultdict(lambda: [Decimal(0), Decimal(0)]), defaultdict(lambda: [Decimal(0), Decimal(0)])
    total_lines = ledger_lines = 0
    for sale in receipt["after"]["db"]["sales"]:
        key = sale["id"]
        request = requests[key]
        header = sale["invoice"]
        require(header["customer"] == customer and header["company"] == seed["company"] and header["currency"] == seed["currency"]
                and header["set_warehouse"] == request["warehouse"], "invoice party, currency, company or warehouse differs from fixture")
        total = Decimal(0)
        for item in sale["items"]:
            expected = prices[item["item_code"]] * Decimal(item["stock_qty"])
            require(Decimal(item["rate"]) == prices[item["item_code"]] and Decimal(item["base_net_amount"]) == expected,
                    "posted price or amount differs from seeded selling price")
            total += expected
        payable = Decimal(header["rounded_total"]) or total
        require(Decimal(header["grand_total"]) == total and Decimal(header["paid_amount"]) == payable
                and not Decimal(header["outstanding_amount"]), "invoice payment or total differs")
        per_sale[key] = {"warehouse": request["warehouse"], "basket": request["items"], "grand_total": number(total),
                         "paid_amount": number(payable), "currency": header["currency"], "customer": customer}
        per_sale[key]["stock_by_warehouse_item"] = sorted(
            [r["warehouse"], r["item_code"], number(r["qty"]), number(r["value_change"])] for r in sale["stock"])
        per_sale[key]["gl_by_account_party"] = sorted(
            [r["account"], r["party_type"] or "", r["party"] or "", number(r["debit"]), number(r["credit"])]
            for r in sale["accounts"])
        for row in sale["stock"]:
            acc = inventory[row["warehouse"], row["item_code"]]
            acc[0] += Decimal(row["qty"])
            acc[1] += Decimal(row["value_change"])
        gl_net = defaultdict(Decimal)
        for row in sale["accounts"]:
            debtor, creditor = Decimal(row["debit"]), Decimal(row["credit"])
            acc = accounts[row["account"], row["party_type"] or "", row["party"] or ""]
            acc[0] += debtor
            acc[1] += creditor
            gl_net[row["account"]] += debtor - creditor
        require(gl_net[seed["cash_account"]] == payable and gl_net[seed["accounts"]["income"]] == -total,
                "cash or income posting differs from invoice")
        total_lines += len(sale["items"])
        ledger_lines += int(sale["gl"]["entries"])
    require(result["db_changes"]["Sales Invoice Item"] == result["db_changes"]["Stock Ledger Entry"] == total_lines
            and result["db_changes"]["GL Entry"] == ledger_lines, "unexplained stock or GL rows")
    def normalized(records):
        return [[*key, *map(number, value)] for key, value in sorted(records.items())]
    return {"sales": per_sale, "stock_by_warehouse_item": normalized(inventory), "gl_by_account_party": normalized(accounts)}


def controls(receipt, seed):
    tests = []
    for name, edit in (
        ("posted price changed by one unit", lambda s: s["items"][0].update(rate=str(Decimal(s["items"][0]["rate"]) + Decimal("0.001")))),
        ("stock deducted from different warehouse", lambda s: s["stock"][0].update(warehouse="different")),
        ("unbalanced debit", lambda s: s["gl"].update(debit=str(Decimal(s["gl"]["debit"]) + Decimal("0.001")))),
        ("duplicate confirmation", None),
    ):
        sample = copy.deepcopy(receipt)
        if edit:
            edit(sample["after"]["db"]["sales"][0])
        else:
            sample["events"].append(next(r for r in sample["events"] if r["type"] == "confirmed"))
        try:
            if edit:
                committed(sample, seed)
            else:
                workload(sample)
        except ValueError:
            tests.append({"name": name, "expected_failure_detected": True})
        else:
            raise ValueError("cashier verifier missed negative control: " + name)
    return tests


def run(req, folder, target, message, app, capabilities):
    from seasonal_driver import adapter, artifact
    from replay_cashier_fifo import conservation_verdict
    require(req["workload"] == WORKLOAD and capabilities["project"] == message["namespace"] and
            capabilities["target"] == message["target"] and capabilities["site"] == "bench-loadtest.localhost"
            and capabilities["http_port"] == 18086 and "submit_sale" in capabilities["voucher_methods"],
            "wrong target or missing cashier endpoint")
    cfg = json.loads((HERE / "cashier.json").read_bytes())["k6"]
    raw = folder / "cashier-evidence.json.gz"
    run_id = hashlib.sha256(f'{WORKLOAD}/{req["fixture_sha256"]}/{req["seed"]}'.encode()).hexdigest()[:12]
    receipt, seed = collect(message["namespace"], run_id, raw,
                            ssh=["ssh", "-F", target["config"], "-o", "BatchMode=yes"], host=target["host"],
                            k6=Path(cfg["path"]).resolve(strict=True), k6_sha=cfg["sha256"], fixture=req["fixture_sha256"],
                            seed=req["seed"], strict=True, instrument=True)
    report = cashier_profile.profile(raw)
    profile_ref = artifact(folder, "cashier-profile.json", report)
    conservation = conservation_verdict(receipt)
    conservation_ref = artifact(folder, "cashier-fifo-conservation.json", conservation)
    require(conservation["passed"], "cashier FIFO stock quantity or value conservation failed")
    load = workload(receipt)
    load_ref = artifact(folder, "cashier-workload.json", load)
    signature = committed(receipt, seed)
    finance_ref = artifact(folder, "cashier-finance-signature.json", signature)
    negative = controls(receipt, seed)
    control_ref = artifact(folder, "cashier-controls.json", negative)
    rw = report["resource_window"]
    tel = receipt["telemetry"]
    require(rw["observer_status"] == "complete" and rw["site_digest_calls"] > 0 and
            not any(t["memory"]["oom_kill_delta"] or t["memory"]["oom_delta"] for t in tel["targets"].values()),
            "incomplete telemetry, counter discontinuity or OOM")
    rt = receipt["result"]
    metrics = {"cpu_core_seconds": rw["cpu_core_seconds"], "peak_memory_bytes": rw["peak_memory_bytes"],
               "sql_seconds": rw["site_sql_seconds"], "io_bytes": rw["disk_bytes_read"] + rw["disk_bytes_written"],
               "lock_wait_ms": rw["lock_waits"]["row_lock_time_ms_delta"]}
    for name, (_, size) in GROUPS.items():
        metrics[f"sale_{size}_p95_ms"] = rt["groups"][name]["p95_confirmed_ms"]
    latencies = sorted(r["duration_ms"] for r in receipt["events"] if r["type"] == "confirmed")
    metrics["sale_p95_ms"] = latencies[math.ceil(.95 * len(latencies)) - 1]
    require(all(type(v) in (int,float) and math.isfinite(v) and v >= 0 for v in metrics.values()), "invalid cashier metric")
    checks = [{"name": n, "passed": True} for n in ("invoice prices and payment", "requested stock and ledger quantities",
              "cash, income and balanced GL", "one posted invoice per key", "replay confirmed original invoice")]
    probes = [{"name": n, "gate": gate, "read_only": True, "verified": True, **ref} for n, gate, ref in (
        ("committed cashier audit", "finance_parity", finance_ref), ("FIFO bin conservation", "finance_parity", conservation_ref),
        ("cashier corruption controls", "mutation_controls", control_ref),
        ("external offered-work receipt", "workload_complete", load_ref), ("guest and SQL counters", "telemetry_complete", profile_ref))]
    return {"schema_version": adapter.HARNESS_EVIDENCE_SCHEMA, "identity": req, "target": message["target"], "app": app,
            "gates": {g: True for g in adapter.GATES}, "metrics": metrics, "seasonal_qualified": False,
            "counters": {k: 0 for k in adapter.COUNTER_FIELDS},
            "seasonal_result": {**load_ref, **load, "duration_seconds": 60, "complete": True, "workload": WORKLOAD},
            "finance_evidence": {**finance_ref, "read_only": True, "verified": True, "checks": checks,
                                 "fifo_conservation":conservation_ref},
            "mutation_controls": {**control_ref, "read_only": True, "verified": True, "controls": negative},
            "telemetry": {**profile_ref, "complete": True}, "probes": probes}
