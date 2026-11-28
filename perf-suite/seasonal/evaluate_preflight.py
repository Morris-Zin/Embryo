#!/usr/bin/env python3
"""Fail-closed evaluator for the idle seasonal preflight scenario."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_run  # noqa: E402

REQUIRED_STEPS = [
    "login_boot_and_discovery",
    "sale",
    "transfer",
    "purchase_receipt",
    "purchase_invoice",
    "search",
    "warm_summary_warehouse",
    "warm_summary_monthly",
    "detail_page_1_and_deep_page",
    "unauthorized_detail_denied",
    "export_enqueue_status_download",
]


def evaluate_log(k6_log):
    events = verify_run.parse_events(k6_log)["events"]
    result = next((event for event in reversed(events) if event.get("event") == "preflight_result"), None)
    failures = []
    if result is None:
        failures.append("missing preflight_result event")
        results = {}
    else:
        results = result.get("results") or {}
        if result.get("passed") is not True:
            failures.append("preflight_result.passed is not true")
        for step in result.get("missing_steps") or []:
            failures.append(f"missing step: {step}")
        for step in result.get("failed_steps") or []:
            failures.append(f"failed step: {step}")
    for step in REQUIRED_STEPS:
        if results.get(step) is not True:
            failures.append(f"required step did not succeed: {step}")
    unauthorized_denials = [event for event in events if event.get("event") == "preflight_unauthorized_detail_denied"]
    if not unauthorized_denials:
        failures.append("missing unauthorized-detail denial evidence")
    status_events = [event for event in events if event.get("event") == "export_status"]
    if not status_events:
        failures.append("missing export status evidence")
    return {
        "passed": not failures,
        "required_steps": REQUIRED_STEPS,
        "results": results,
        "failures": failures,
        "preflight_result": result or {},
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--k6-log", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--json-output", required=True)
    args = parser.parse_args(argv)
    evaluation = evaluate_log(args.k6_log)
    lines = [
        "# Seasonal idle preflight scorecard",
        "",
        f"Verdict: {'GO' if evaluation['passed'] else 'NO-GO'}",
        "",
        "| Step | Result |",
        "|---|---|",
    ]
    for step in REQUIRED_STEPS:
        lines.append(f"| {step} | {'PASS' if evaluation['results'].get(step) is True else 'FAIL'} |")
    if evaluation["failures"]:
        lines.extend(["", "## Failures", ""] + [f"- {failure}" for failure in evaluation["failures"]])
    markdown = "\n".join(lines) + "\n"
    Path(args.output).write_text(markdown, encoding="utf-8")
    Path(args.json_output).write_text(json.dumps(evaluation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(markdown)
    return 0 if evaluation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
