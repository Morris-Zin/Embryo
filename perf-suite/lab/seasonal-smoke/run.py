#!/usr/bin/env python3
"""Offer the seasonal sales subgroup for one minute against a fresh guest arm."""
import argparse
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
PERF = HERE.parents[1]
SSH = ["ssh", "-F", "/var/lib/khitmee-experiment-guest/identity/ssh_config", "-o", "BatchMode=yes"]
SMOKE_FIXTURE = "ccbecd846f6fe9bba96a83a6815859c3385bb795cf8a69f3e62135191ecfb61c"
K6 = Path("/var/lib/khitmee-experiment-guest/seasonal-cashier/k6")
K6_SHA = "ae93e9a5782805b8c816cd2997dbf9af448df56bd7733a4ea7d2d4e6632e9410"


@contextmanager
def monitor(namespace, enable, ssh=SSH, host="khitmee-lab"):
    report = {}
    if not enable:
        yield report
        return
    path = PERF / "lab/telemetry/telemetry_profiler.py"
    source = ("import sys, types\nm=types.ModuleType('telemetry_profiler')\nsys.modules[m.__name__]=m\n"
              + f"exec({path.read_text()!r},m.__dict__)\n" + (HERE / "window.py").read_text() + f"\nobserve({namespace!r})\n")
    bootstrap = "sudo -n python3 -u -B -c 'import sys,base64;exec(base64.b64decode(sys.stdin.buffer.readline()))'"
    with subprocess.Popen(ssh + [host, bootstrap], text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        try:
            proc.stdin.write(base64.b64encode(source.encode()).decode() + "\n")
            proc.stdin.flush()
            if not select.select([proc.stdout], [], [], 30)[0] or "ready" not in json.loads(proc.stdout.readline()):
                raise RuntimeError("guest telemetry did not reach start boundary")
            host_ready = datetime.now(timezone.utc).isoformat()
            yield report
            host_stop = datetime.now(timezone.utc).isoformat()
            data, _ = proc.communicate("stop\n", timeout=60)
            if proc.returncode:
                raise RuntimeError("guest telemetry collection failed")
            report.update(json.loads(data), sampler_sha256=hashlib.sha256(source.encode()).hexdigest(),
                          supervisor_ready_utc=host_ready, supervisor_stop_utc=host_stop)
        finally:
            if proc.poll() is None:
                proc.terminate()


def target(namespace, run, op, ssh=SSH, host="khitmee-lab", fixture=SMOKE_FIXTURE):
    observer = (HERE.parent / "driver/cashier_fifo_observer.py").read_text()
    audit = observer + "\n" + (HERE / "audit.py").read_text()
    src = "AUDIT = " + repr(audit) + "\n" + (HERE / "target.py").read_text()
    return json.loads(subprocess.check_output(ssh + [host, "sudo", "-n", "python3", "-B", "-", namespace, run, op, fixture],
                                              input=src.encode(), timeout=120))


def evaluate(events, before, after):
    starts = {e["id"]: e for e in events if e["type"] == "arrival"}
    requests = {e["id"]: e for e in events if e["type"] == "submit"}
    confirmed = {e["id"]: e for e in events if e["type"] == "confirmed"}
    failed = {e["id"]: e for e in events if e["type"] == "failed"}
    actual = {r["id"]: r for r in after["db"]["sales"]}
    errors = []
    for id_, sale in actual.items():
        request = requests.get(id_)
        if not request or not sale["invoice"] or sale["invoice"]["docstatus"] != 1:
            errors.append([id_, "unexpected or unsubmitted invoice"])
            continue
        wanted = [(sku, Decimal(str(qty)), request["warehouse"]) for sku, qty in request["items"]]
        posted = [(row["item_code"], Decimal(row["stock_qty"]), row["warehouse"]) for row in sale["items"]]
        sle = sorted((r["item_code"], -Decimal(r["qty"]), r["warehouse"]) for r in sale["stock"])
        gl = sale["gl"]
        if posted != wanted or sorted(posted) != sle:
            errors.append([id_, "invoice lines or SLE differ from requested stock quantities"])
        if not gl["entries"] or Decimal(gl["debit"]) != Decimal(gl["credit"]) or Decimal(gl["debit"]) <= 0:
            errors.append([id_, "GL entries absent or unbalanced"])
        if id_ in confirmed and confirmed[id_]["voucher"] != sale["voucher"]:
            errors.append([id_, "confirmation names different persisted voucher"])
    if len(actual) != len(after["db"]["sales"]) or len(set(r["voucher"] for r in actual.values())) != len(actual):
        errors.append(["run", "duplicate client ID or invoice"])
    delta = {k: v - before["db"]["counts"][k] for k, v in after["db"]["counts"].items()}
    if delta["Sales Invoice"] != len(actual) or delta["Khitmee Idempotency Log"] != len(actual):
        errors.append(["run", "invoice or idempotency delta differs from matched vouchers"])
    if before["services"] != after["services"]:
        errors.append(["run", "container restarted or changed"])
    groups = {}
    for group, offered in (("small", 60), ("medium", 16), ("large", 4)):
        ids = {k for k, v in starts.items() if v["group"] == group}
        confirmed_ids = ids & confirmed.keys()
        timings = sorted(confirmed[i]["duration_ms"] for i in confirmed_ids)
        groups[group] = {"offered": offered, "started": len(ids), "submitted": len(ids & requests.keys()),
                         "confirmed_within_minute": sum(confirmed[i]["elapsed_ms"] <= 60000 for i in confirmed_ids),
                         "confirmed_including_drain": len(confirmed_ids), "persisted": len(ids & actual.keys()),
                         "p95_confirmed_ms": timings[math.ceil(0.95 * len(timings)) - 1] if timings else None}
    summary = next((e for e in events if e["type"] == "summary"), {})
    return {"groups": groups, "k6": summary, "db_changes": delta, "financial_mismatches": errors,
            "failures": dict(Counter(e["reason"] for e in failed.values())),
            "confirmed_missing_from_db": sorted(confirmed.keys() - actual.keys()),
            "committed_without_confirmation": sorted(actual.keys() - confirmed.keys()),
            "started_without_terminal_event": sorted(starts.keys() - confirmed.keys() - failed.keys())}


def collect(namespace, run, output, *, ssh=SSH, host="khitmee-lab", k6=K6, k6_sha=K6_SHA, fixture=SMOKE_FIXTURE,
            seed=20240601, strict=False, instrument=True):
    if (not re.fullmatch(r"khitmee-arm-[a-f0-9]{16}", namespace) or not re.fullmatch(r"[0-9a-f]{64}", fixture)
            or output.exists() or host != "khitmee-lab"):
        raise ValueError("expected a disposable arm, pinned fixture, pinned guest and new evidence path")
    if hashlib.sha256(k6.read_bytes()).hexdigest() != k6_sha:
        raise ValueError("external k6 binary hash mismatch")
    if not re.fullmatch(r"[a-f0-9]{12}", run):
        raise ValueError("expected a 12-character lowercase hexadecimal run ID")
    setup = target(namespace, run, "setup", ssh, host, fixture)
    before = target(namespace, run, "audit", ssh, host, fixture)
    if before["db"]["smoke_idempotency_rows"]:
        raise ValueError("cashier probe requires a fresh arm")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    forward = ssh + ["-o", "ExitOnForwardFailure=yes", "-N", "-L", f"127.0.0.1:{port}:127.0.0.1:18086", host]
    with tempfile.TemporaryDirectory() as tmp, subprocess.Popen(forward) as tunnel:
        try:
            for _ in range(100):
                if tunnel.poll() is not None:
                    raise RuntimeError("failed to bind pinned guest tunnel")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("timed out opening guest tunnel")
            tree = Path(tmp)
            shutil.copytree(PERF / "k6/lib", tree / "k6/lib")
            staged = tree / "lab/seasonal-smoke/cashier.js"
            staged.parent.mkdir(parents=True)
            shutil.copyfile(HERE / "cashier.js", staged)
            hashes = {str(p.relative_to(tree)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in tree.rglob("*.js")}
            script_sources = {name: (tree / name).read_text() for name in hashes}
            manifest = tree / "manifest.json"
            seed_data = setup.pop("manifest")
            manifest.write_text(json.dumps(seed_data))
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8",
                   "CASHIER_RUN": run, "CASHIER_SEED": str(seed), "CASHIER_STRICT": str(int(strict)),
                   "TARGET_URL": f"http://127.0.0.1:{port}", "PERF_USER": "Administrator",
                   "PERF_PASSWORD": setup.pop("password"), "SEED_MANIFEST_PATH": str(manifest)}
            with monitor(namespace, instrument, ssh, host) as telemetry:
                proc = subprocess.run([str(k6), "run", "--quiet", "--no-usage-report", "--log-output=stdout",
                                       "--log-format=raw", str(staged)], env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, timeout=255, text=True)
            events = [json.loads(line.removeprefix("CASHIER ")) for line in proc.stdout.splitlines() if line.startswith("CASHIER ")]
            diagnostics = [line[:500] for line in proc.stdout.splitlines() if not line.startswith("CASHIER ")][-15:]
        finally:
            tunnel.terminate()
    after = target(namespace, run, "audit", ssh, host, fixture)
    result = evaluate(events, before, after)
    evidence = {"scope": "one-minute sale submissions only, one shared Administrator session; 50 ms simulated RTT",
                "run": run, "seed": seed, "setup": setup, "k6_sha256": k6_sha, "script_sha256": hashes,
                "script_sources": script_sources, "exit_code": proc.returncode,
                "offered": {"10_lines": 60, "50_lines": 16, "200_lines": 4}, "before": before, "after": after,
                "events": events, "driver_diagnostics": diagnostics, "result": result}
    if telemetry:
        evidence["telemetry"] = telemetry
    with output.open("xb") as out:
        out.write(gzip.compress(json.dumps(evidence, separators=(",", ":")).encode(), mtime=0))
    return evidence, seed_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("namespace")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", help="repeat a prior basket sequence on a fresh restore")
    parser.add_argument("--telemetry", action="store_true", help="bracket k6 with guest resource and SQL snapshots")
    args = parser.parse_args()
    os.umask(0o077)
    evidence, _ = collect(args.namespace, args.run_id or secrets.token_hex(6), args.output, instrument=args.telemetry)
    print(json.dumps({"result": evidence["result"], "driver_diagnostics": evidence["driver_diagnostics"],
                      "evidence": str(args.output)}, indent=2))
    if evidence["exit_code"] or not evidence["events"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
