#!/usr/bin/env python3
"""Compare native startup latency and idle memory between two executables."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from benchmark_native import measure_run, now_iso


def summarize(runs: list[dict], binary: str) -> dict:
    selected = [run for run in runs if run["binary"] == binary and run.get("coldStartupMs") is not None]
    startup = [run["coldStartupMs"] for run in selected]
    memory = [run["idleWorkingSetBytes"] for run in selected if run.get("idleWorkingSetBytes") is not None]
    return {
        "startupMs": startup,
        "p50StartupMs": statistics.median(startup) if startup else None,
        "meanStartupMs": statistics.mean(startup) if startup else None,
        "idleMemoryBytes": memory,
        "p50IdleMemoryBytes": statistics.median(memory) if memory else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-executable", required=True)
    parser.add_argument("--candidate-executable", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--invoice-rows", type=int, default=10000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--hold-seconds", type=float, default=0.1)
    parser.add_argument("--min-improvement-pct", type=float, default=30.0)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")

    binaries = {
        "base": Path(args.base_executable),
        "candidate": Path(args.candidate_executable),
    }
    port = 49600
    for path in binaries.values():
        measure_run(path, args.rows, args.invoice_rows, port, 0.05)
        port += 1

    runs = []
    for repeat in range(args.repeats):
        ordered_binaries = list(binaries.items()) if repeat % 2 == 0 else list(reversed(tuple(binaries.items())))
        for binary, path in ordered_binaries:
            result = measure_run(path, args.rows, args.invoice_rows, port, args.hold_seconds)
            port += 1
            result["binary"] = binary
            result["repeat"] = repeat + 1
            result.pop("stdoutTail", None)
            result.pop("stderrTail", None)
            runs.append(result)

    summary = {binary: summarize(runs, binary) for binary in binaries}
    base_p50 = summary["base"]["p50StartupMs"]
    candidate_p50 = summary["candidate"]["p50StartupMs"]
    improvement = None
    if base_p50 and candidate_p50 is not None:
        improvement = (base_p50 - candidate_p50) / base_p50 * 100
    failures = []
    for binary in binaries:
        measured = len(summary[binary]["startupMs"])
        if measured != args.repeats:
            failures.append(f"{binary} produced {measured}/{args.repeats} complete startup measurements")
    if improvement is None:
        failures.append("startup comparison did not produce complete measurements")
    elif improvement < args.min_improvement_pct:
        failures.append(
            f"candidate p50 startup improvement {improvement:.2f}% is below {args.min_improvement_pct:.2f}%"
        )
    report = {
        "schemaVersion": 1,
        "generatedAt": now_iso(),
        "rows": args.rows,
        "invoiceRows": args.invoice_rows,
        "repeats": args.repeats,
        "summary": summary,
        "p50StartupImprovementPct": improvement,
        "passed": not failures,
        "failures": failures,
        "runs": runs,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "output": str(output), "p50StartupImprovementPct": improvement}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
