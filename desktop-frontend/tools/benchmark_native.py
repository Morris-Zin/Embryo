#!/usr/bin/env python3
"""Native pilot performance qualification harness.

Measures what can be measured directly on the host. Unmeasured dimensions remain explicit
failures in the JSON report instead of being omitted or fabricated.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def process_memory_bytes(pid: int, handle: int | None = None) -> int | None:
    if sys.platform == "win32":
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        close_handle = False
        process_handle = handle
        if not process_handle:
            PROCESS_QUERY_INFORMATION = 0x0400
            PROCESS_VM_READ = 0x0010
            process_handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            close_handle = True
        if not process_handle:
            return None
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        try:
            if ctypes.windll.psapi.GetProcessMemoryInfo(process_handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        finally:
            if close_handle:
                ctypes.windll.kernel32.CloseHandle(process_handle)
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def mcp_ready(port: int) -> bool:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bench", "version": "0"}},
    }).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=payload, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=0.25) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def benchmark_environment(rows: int, invoice_rows: int, port: int) -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("KHITMEE_ERP_"):
            env.pop(key)
    env["KHITMEE_CATALOG_ROWS"] = str(rows)
    env["KHITMEE_INVOICE_ROWS"] = str(invoice_rows)
    env["SLINT_MCP_PORT"] = str(port)
    env.setdefault("SLINT_EMIT_DEBUG_INFO", "1")
    return env


def measure_run(executable: Path, rows: int, invoice_rows: int, port: int, hold_seconds: float) -> dict[str, Any]:
    env = benchmark_environment(rows, invoice_rows, port)
    started = time.perf_counter()
    process = subprocess.Popen([str(executable)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    startup_ms = None
    deadline = started + 15
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            break
        if mcp_ready(port):
            startup_ms = (time.perf_counter() - started) * 1000
            break
        time.sleep(0.025)
    time.sleep(hold_seconds)
    memory_bytes = process_memory_bytes(process.pid, getattr(process, "_handle", None))
    result: dict[str, Any] = {
        "rows": rows,
        "invoiceRows": invoice_rows,
        "coldStartupMs": startup_ms,
        "idleWorkingSetBytes": memory_bytes,
        "exitCodeBeforeStop": process.poll(),
    }
    if startup_ms is None:
        result["error"] = "application did not expose the Slint MCP server within 15 seconds"
    process.kill()
    stdout, stderr = process.communicate(timeout=5)
    result["stdoutTail"] = stdout.decode(errors="replace")[-1000:]
    result["stderrTail"] = stderr.decode(errors="replace")[-1000:]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-startup-ms", type=float, default=1500.0)
    parser.add_argument("--max-idle-memory-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--rows", default="10000,100000")
    parser.add_argument("--invoice-rows", type=int, default=10000)
    args = parser.parse_args()

    executable = Path(args.executable)
    runs = []
    for index, rows_text in enumerate(args.rows.split(",")):
        rows = int(rows_text.strip())
        runs.append(measure_run(executable, rows, args.invoice_rows, 49380 + index, 1.0))

    failures = []
    for run in runs:
        if run.get("error"):
            failures.append(f"{run['rows']} rows: {run['error']}")
        if run.get("coldStartupMs") is None or run["coldStartupMs"] > args.max_startup_ms:
            failures.append(f"{run['rows']} rows: cold startup missing or above {args.max_startup_ms}ms")
        if run.get("idleWorkingSetBytes") is None or run["idleWorkingSetBytes"] > args.max_idle_memory_bytes:
            failures.append(f"{run['rows']} rows: idle memory missing or above {args.max_idle_memory_bytes}")

    # These dimensions require interactive MCP timing and platform/network probes. They are
    # explicit pending checks rather than silently skipped passes.
    pending = [
        "frame latency under scrolling",
        "p95 input latency",
        "allocation counts/rates",
        "network traffic against ERPNext",
        "barcode scanning device latency",
        "printing latency",
        "low-end Windows hardware validation",
    ]
    report = {
        "schemaVersion": 1,
        "generatedAt": now_iso(),
        "passed": not failures and not pending,
        "failures": failures + [f"pending required measurement: {item}" for item in pending],
        "measuredGatePassed": not failures,
        "pendingRequiredMeasurements": pending,
        "runs": runs,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failures": failures, "output": str(output)}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
