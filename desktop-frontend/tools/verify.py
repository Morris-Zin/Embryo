#!/usr/bin/env python3
"""Screen migration verification orchestrator.

This script is deliberately conservative: missing or incomparable evidence is a failure,
never a skipped pass. It can update registry statuses only after all acceptance dimensions
pass with reproducible artifacts.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    return str(value or "")


def compare_visual(reference_png: Path, native_png: Path, report_path: Path, threshold: float) -> dict[str, Any]:
    script = ROOT / "tools" / "compare_screenshots.py"
    result = subprocess.run(
        [sys.executable, str(script), str(reference_png), str(native_png), "--report", str(report_path), "--max-failed-pixel-ratio", str(threshold)],
        text=True,
        capture_output=True,
    )
    report = load_json(report_path, {"passed": False, "error": "comparison did not write a report"})
    report["exitCode"] = result.returncode
    if result.stdout.strip():
        report["stdout"] = result.stdout.strip()
    if result.stderr.strip():
        report["stderr"] = result.stderr.strip()
    return report


def evaluate_screen(
    screen: dict[str, Any],
    web_reference: Path,
    native_evidence: Path,
    evidence_out: Path,
    business_report: Path | None,
    performance_report: Path | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "screen": screen["id"],
        "passed": False,
        "failures": [],
        "checks": {},
    }

    reference_id = screen["source_route"].strip("/").replace("/", "-").replace("#", "--") or "root"
    reference_dir = web_reference / reference_id
    # The sales route captures as khitmee-sales while registry uses sales-voucher.
    aliases = [reference_id, screen["source_route"].strip("/").replace("_", "-"), screen["id"]]
    existing_reference = next((web_reference / alias for alias in aliases if (web_reference / alias / "reference.png").exists()), reference_dir)
    reference_png = existing_reference / "reference.png"
    native_png = native_evidence / "native.png"
    reference_dom = load_json(existing_reference / "dom.json", {})
    native_tree = load_json(native_evidence / "element-tree.json", {})

    required_files = {
        "reference-screenshot": reference_png,
        "native-screenshot": native_png,
        "reference-dom": existing_reference / "dom.json",
        "reference-trace": existing_reference / "trace.json",
        "native-element-tree": native_evidence / "element-tree.json",
    }
    for kind, path in required_files.items():
        exists = path.exists()
        result["checks"][kind] = {"passed": exists, "path": str(path)}
        if not exists:
            result["failures"].append(f"missing {kind}: {path}")

    if reference_png.exists() and native_png.exists():
        visual_report = compare_visual(
            reference_png,
            native_png,
            evidence_out / screen["id"] / "visual-comparison.json",
            float(screen.get("acceptance", {}).get("visual_max_failed_pixel_ratio", 0.0)),
        )
        result["checks"]["visual-comparison"] = visual_report
        if not visual_report.get("passed"):
            result["failures"].append("visual comparison failed or inputs were incomparable")

    if reference_dom and native_tree:
        reference_text = flatten_text(reference_dom.get("bodyText", "")) + " " + flatten_text(reference_dom.get("controls", []))
        native_text = flatten_text(native_tree)
        title = reference_dom.get("h1") or reference_dom.get("title") or screen.get("title", "")
        title_probe = str(title).split()[0] if title else ""
        text_passed = bool(title_probe and title_probe.casefold() in native_text.casefold())
        result["checks"]["text-probe"] = {"passed": text_passed, "probe": title_probe}
        if not text_passed:
            result["failures"].append(f"native tree does not contain reference title probe {title_probe!r}")

        reference_roles = {control.get("role") for control in reference_dom.get("controls", []) if control.get("role")}
        native_roles = set()
        for element in native_tree.get("elements", []) if isinstance(native_tree, dict) else []:
            accessible = element.get("accessible", {}) if isinstance(element, dict) else {}
            role = accessible.get("role") or element.get("accessibleRole") or element.get("role")
            if role:
                native_roles.add(str(role))
        missing_roles = sorted(reference_roles - native_roles)
        role_passed = not missing_roles
        result["checks"]["accessibility-role-probe"] = {"passed": role_passed, "missingRoles": missing_roles, "nativeRoles": sorted(native_roles)}
        if not role_passed:
            result["failures"].append(f"native accessibility roles missing {missing_roles}")

        required_shortcuts = sorted({key for control in reference_dom.get("controls", []) for key in control.get("keyboard", [])})
        shortcut_probe_passed = all(shortcut in native_text for shortcut in required_shortcuts)
        result["checks"]["keyboard-probe"] = {"passed": shortcut_probe_passed, "requiredShortcuts": required_shortcuts}
        if not shortcut_probe_passed:
            result["failures"].append(f"native evidence does not expose required shortcuts {required_shortcuts}")

    if business_report is None or not business_report.exists():
        result["checks"]["business-outcome"] = {"passed": False, "error": "business outcome report missing"}
        result["failures"].append("business outcome evidence missing")
    else:
        business = load_json(business_report, {})
        passed = bool(business.get("passed"))
        result["checks"]["business-outcome"] = business
        if not passed:
            result["failures"].append("business outcome comparison failed")

    if performance_report is None or not performance_report.exists():
        result["checks"]["performance"] = {"passed": False, "error": "performance report missing"}
        result["failures"].append("performance evidence missing")
    else:
        performance = load_json(performance_report, {})
        passed = bool(performance.get("passed"))
        result["checks"]["performance"] = performance
        if not passed:
            result["failures"].append("performance qualification failed")

    result["passed"] = not result["failures"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default=str(ROOT / "registry" / "screens.json"))
    parser.add_argument("--web-reference", default=str(ROOT / "evidence" / "web-reference"))
    parser.add_argument("--native-evidence", default=str(ROOT / "evidence" / "native"))
    parser.add_argument("--evidence-out", default=str(ROOT / "evidence" / "verification"))
    parser.add_argument("--business-report")
    parser.add_argument("--performance-report")
    parser.add_argument("--screen", action="append", default=[])
    parser.add_argument("--update-registry", action="store_true")
    args = parser.parse_args()

    registry_path = Path(args.registry)
    registry = load_json(registry_path)
    if registry is None:
        print(f"missing registry {registry_path}; run discover.py", file=sys.stderr)
        return 2
    selected = set(args.screen)
    screens = [screen for screen in registry.get("screens", []) if not selected or screen.get("id") in selected]
    business_report = Path(args.business_report) if args.business_report else None
    performance_report = Path(args.performance_report) if args.performance_report else None

    results = [
        evaluate_screen(screen, Path(args.web_reference), Path(args.native_evidence), Path(args.evidence_out), business_report, performance_report)
        for screen in screens
    ]
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "passed": bool(results) and all(result["passed"] for result in results),
        "results": results,
    }
    report_path = Path(args.evidence_out) / "verification-report.json"
    write_json(report_path, report)

    if args.update_registry:
        for screen in registry.get("screens", []):
            matching = next((result for result in results if result["screen"] == screen.get("id")), None)
            if matching is None:
                continue
            if matching["passed"]:
                screen["status"] = "verified" if screen.get("status") != "regression-locked" else "regression-locked"
                screen["failures"] = []
            else:
                screen["failures"] = sorted(set(screen.get("failures", []) + matching["failures"]))
        write_json(registry_path, registry)

    print(json.dumps({"passed": report["passed"], "screens": len(results), "report": str(report_path)}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
