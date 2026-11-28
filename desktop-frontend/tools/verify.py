#!/usr/bin/env python3
"""Conservative one-to-one migration verification orchestrator.

Evidence is state-based: each screen has an `initial` state stored directly in its
capture directory and may have named interaction states in child directories. Missing
or incomparable evidence is always a failure.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
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


def norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_role(role: str) -> str:
    normalized = norm_text(role).lower().replace("-", "").replace("_", "")
    equivalents = {
        "link": "button",
        "searchbox": "textinput",
        "radio": "radiobutton",
            "textbox": "textinput",
            "listbox": "list",
            "option": "listitem",
            "group": "groupbox",
    }
    return equivalents.get(normalized, normalized)


def web_role(control: dict[str, Any]) -> str:
    explicit = norm_text(control.get("role")).lower()
    if explicit:
        return explicit
    tag = norm_text(control.get("tag")).lower()
    if tag == "button":
        return "button"
    if tag == "a":
        return "link"
    if tag == "select":
        return "combobox"
    if tag == "textarea":
        return "textbox"
    if tag == "input":
        input_type = norm_text(control.get("inputType") or control.get("type")).lower()
        if input_type == "search":
            return "searchbox"
        if input_type == "radio":
            return "radio"
        if input_type == "checkbox":
            return "checkbox"
        return "textbox"
    return tag or "unknown"


def occurrence_keyed(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    result = {}
    for row in rows:
        base = (row["role"], row["label"])
        counts[base] += 1
        result[f"{base[0]}|{base[1]}|{counts[base]}"] = row
    return result


def web_controls(dom: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = []
    for control in dom.get("controls", []) if isinstance(dom, dict) else []:
        if not control.get("visible", True):
            continue
        fallback = control.get("ariaLabel") or ""
        if not fallback:
            if control.get("tag") in ("input", "textarea"):
                fallback = control.get("placeholder") or control.get("id") or control.get("text")
            else:
                fallback = control.get("text") or control.get("placeholder") or control.get("id")
        label = norm_text(control.get("accessibilityName") if control.get("hasAccessibilityName") else fallback)
        geometry = control.get("geometry") or {}
        rows.append({
            "role": canonical_role(web_role(control)),
            "label": label,
            "x": float(geometry.get("x", 0) or 0),
            "y": float(geometry.get("y", 0) or 0),
            "width": float(geometry.get("width", 0) or 0),
            "height": float(geometry.get("height", 0) or 0),
            "disabled": bool(control.get("disabled")),
            "expanded": control.get("ariaExpanded") or None,
            "value": norm_text(control.get("value") or (control.get("text") if control.get("tag") in ("input", "textarea") else "")),
        })
    return occurrence_keyed(rows)


def native_controls(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = []
    for element in tree.get("elements", []) if isinstance(tree, dict) else []:
        size = element.get("size") or {}
        if not size.get("width") or not size.get("height") or element.get("computedOpacity", 1) == 0:
            continue
        label = norm_text(element.get("accessibleLabel") or element.get("accessibleValue") or element.get("accessiblePlaceholder"))
        raw_role = norm_text(element.get("accessibleRole") or element.get("role")).lower()
        live_region = norm_text(element.get("accessibleLiveRegion") or element.get("liveRegion")).lower()
        role = "status" if raw_role in ("text", "statictext") and live_region and live_region != "off" else raw_role
        if raw_role in ("text", "statictext") and role != "status":
            continue
        role = canonical_role(role)
        if not label and role in ("unknown", "", "image", "list"):
            continue
        position = element.get("absolutePosition") or {}
        rows.append({
            "role": role or "unknown",
            "label": label,
            "x": float(position.get("x", 0) or 0),
            "y": float(position.get("y", 0) or 0),
            "width": float(size.get("width", 0) or 0),
            "height": float(size.get("height", 0) or 0),
            "disabled": element.get("accessibleEnabled") is not True,
            "expanded": element.get("accessibleExpanded") if element.get("accessibleExpanded") is not None else ("false" if canonical_role(element.get("accessibleRole") or "") == "combobox" else None),
            "value": norm_text(element.get("accessibleValue") or ""),
        })
    return occurrence_keyed(rows)


def compare_visual(reference_png: Path, native_png: Path, report_path: Path, threshold: float) -> dict[str, Any]:
    script = ROOT / "tools" / "compare_screenshots.py"
    result = subprocess.run(
        [sys.executable, str(script), str(reference_png), str(native_png), "--report", str(report_path), "--max-failed-pixel-ratio", str(threshold)],
        text=True,
        capture_output=True,
    )
    report = load_json(report_path, {"passed": False, "error": "comparison did not write a report"})
    report["exitCode"] = result.returncode
    if result.stderr.strip():
        report["stderr"] = result.stderr.strip()
    return report


def compare_structure(reference_dom: dict[str, Any], native_tree: dict[str, Any], tolerance: float) -> dict[str, Any]:
    reference = web_controls(reference_dom)
    native = native_controls(native_tree)
    missing = sorted(set(reference) - set(native))
    unexpected = sorted(set(native) - set(reference))
    geometry_failures = []
    for key in sorted(set(reference) & set(native)):
        left = reference[key]
        right = native[key]
        deltas = {axis: abs(left[axis] - right[axis]) for axis in ("x", "y", "width", "height")}
        if any(delta > tolerance for delta in deltas.values()):
            geometry_failures.append({"control": key, "reference": {axis: left[axis] for axis in deltas}, "native": {axis: right[axis] for axis in deltas}, "deltas": deltas})
        interactive = canonical_role(left["role"]) in {"button", "combobox", "textinput", "radiobutton", "checkbox", "slider", "spinbox", "switch", "tab"}
        if (interactive and left["disabled"] != right["disabled"]) or (left.get("expanded") is not None and left.get("expanded") != right.get("expanded")) or left.get("value") != right.get("value"):
            geometry_failures.append({"control": key, "stateMismatch": {"referenceDisabled": left["disabled"], "nativeDisabled": right["disabled"], "referenceExpanded": left.get("expanded"), "nativeExpanded": right.get("expanded"), "referenceValue": left.get("value"), "nativeValue": right.get("value")}})
    return {
        "passed": not missing and not unexpected and not geometry_failures,
        "referenceControlCount": len(reference),
        "nativeControlCount": len(native),
        "missingControls": missing,
        "unexpectedControls": unexpected,
        "geometryTolerancePx": tolerance,
        "geometryFailures": geometry_failures,
    }


def keyboard_check(screen_dir: Path, reference_dom: dict[str, Any]) -> dict[str, Any]:
    required = sorted(set(reference_dom.get("pageShortcuts", [])) | {key for control in reference_dom.get("controls", []) for key in control.get("keyboard", [])})
    path = screen_dir / "keyboard-results.json"
    data = load_json(path, None)
    outcomes = data.get("shortcuts", {}) if isinstance(data, dict) else {}
    missing = [key for key in required if outcomes.get(key, {}).get("passed") is not True]
    return {
        "passed": not missing,
        "requiredShortcuts": required,
        "missingBehavioralResults": missing,
        "path": str(path),
    }


def state_dirs(base: Path) -> dict[str, Path]:
    states = {}
    if (base / "reference.png").exists() or (base / "native.png").exists():
        states["initial"] = base
    if base.exists():
        for child in sorted(base.iterdir()):
            if child.is_dir() and ((child / "reference.png").exists() or (child / "native.png").exists()):
                states[child.name] = child
    return states


def compare_state(screen: dict[str, Any], state: str, reference_dir: Path, native_dir: Path, evidence_out: Path) -> dict[str, Any]:
    failures = []
    checks: dict[str, Any] = {}
    reference_png = reference_dir / "reference.png"
    native_png = native_dir / "native.png"
    reference_dom_path = reference_dir / "dom.json"
    native_tree_path = native_dir / "element-tree.json"
    required = {
        "reference-screenshot": reference_png,
        "native-screenshot": native_png,
        "reference-dom": reference_dom_path,
        "reference-trace": reference_dir / "trace.json",
        "native-element-tree": native_tree_path,
    }
    for kind, path in required.items():
        exists = path.exists()
        checks[kind] = {"passed": exists, "path": str(path)}
        if not exists:
            failures.append(f"missing {kind}: {path}")

    acceptance = screen.get("acceptance", {})
    if reference_png.exists() and native_png.exists():
        visual = compare_visual(reference_png, native_png, evidence_out / screen["id"] / state / "visual-comparison.json", float(acceptance.get("visual_max_failed_pixel_ratio", 0.0)))
        checks["visual-comparison"] = visual
        if not visual.get("passed"):
            failures.append("visual comparison failed or inputs were incomparable")

    reference_dom = load_json(reference_dom_path, {})
    native_tree = load_json(native_tree_path, {})
    if not reference_dom or not native_tree:
        checks["structure"] = {"passed": False, "error": "empty or missing structured evidence"}
        if reference_dom_path.exists() or native_tree_path.exists():
            failures.append("structured evidence is empty; structural checks cannot run")
    else:
        structure = compare_structure(reference_dom, native_tree, float(acceptance.get("geometry_tolerance_px", 0.0)))
        checks["structure"] = structure
        if not structure["passed"]:
            failures.append("control identity, text/accessibility semantics, geometry, or state comparison failed")
        keyboard = keyboard_check(native_dir, reference_dom)
        checks["keyboard"] = keyboard
        if acceptance.get("require_keyboard_parity", True) and not keyboard["passed"]:
            failures.append("behavioral keyboard evidence missing or failing")

    return {"state": state, "passed": not failures, "failures": failures, "checks": checks}


def evaluate_screen(screen: dict[str, Any], web_reference: Path, native_evidence: Path, evidence_out: Path, business_report: Path | None, performance_report: Path | None) -> dict[str, Any]:
    reference_id = screen["source_route"].strip("/").replace("/", "-").replace("#", "--") or "root"
    aliases = [reference_id, screen["source_route"].strip("/").replace("_", "-").replace("#", "--"), screen["id"]]
    reference_base = next((web_reference / alias for alias in aliases if (web_reference / alias).exists()), web_reference / reference_id)
    native_base = native_evidence / screen["id"]
    reference_states = state_dirs(reference_base)
    native_states = state_dirs(native_base)
    state_names = sorted(set(reference_states) | set(native_states) | {"initial"}, key=lambda value: (value != "initial", value))
    result = {"screen": screen["id"], "passed": False, "failures": [], "states": [], "checks": {}}
    if not state_names:
        result["failures"].append(f"no reference or native capture states found for {screen['id']}")
    for state in state_names:
        if state not in reference_states:
            state_result = {"state": state, "passed": False, "failures": ["reference state capture missing"], "checks": {}}
        elif state not in native_states:
            state_result = {"state": state, "passed": False, "failures": ["native state capture missing"], "checks": {}}
        else:
            state_result = compare_state(screen, state, reference_states[state], native_states[state], evidence_out)
        result["states"].append(state_result)
        result["failures"].extend(f"{state}: {failure}" for failure in state_result["failures"])

    screen_business_report = business_report or (ROOT / "evidence" / "business" / f"{screen['id']}-business.json")
    if not screen_business_report.exists():
        result["checks"]["business-outcome"] = {"passed": False, "error": "business outcome report missing"}
        result["failures"].append("business outcome evidence missing")
    else:
        business = load_json(screen_business_report, {})
        result["checks"]["business-outcome"] = business
        if not business.get("passed"):
            result["failures"].append("business outcome comparison failed")
    if performance_report is None or not performance_report.exists():
        result["checks"]["performance"] = {"passed": False, "error": "performance report missing"}
        result["failures"].append("performance evidence missing")
    else:
        performance = load_json(performance_report, {})
        result["checks"]["performance"] = performance
        if not performance.get("passed"):
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
    results = [evaluate_screen(screen, Path(args.web_reference), Path(args.native_evidence), Path(args.evidence_out), business_report, performance_report) for screen in screens]
    report = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "passed": bool(results) and all(item["passed"] for item in results),
        "results": results,
    }
    write_json(Path(args.evidence_out) / "verification-report.json", report)

    if args.update_registry:
        for screen in registry.get("screens", []):
            matched = next((item for item in results if item["screen"] == screen.get("id")), None)
            if matched and matched["passed"]:
                screen["failures"] = []
                if screen.get("status") != "regression-locked":
                    screen["status"] = "verified"
            elif matched:
                screen["failures"] = matched["failures"]
        write_json(registry_path, registry)

    print(json.dumps({"passed": report["passed"], "report": str(Path(args.evidence_out) / "verification-report.json"), "screens": len(results)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
