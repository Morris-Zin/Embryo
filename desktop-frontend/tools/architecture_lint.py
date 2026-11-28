#!/usr/bin/env python3
"""Architectural guardrails for the native migration workspace."""
from __future__ import annotations

import json
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility for minimal verification runners.
    tomllib = None
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DEPENDENCY_PARTS = ("tauri", "electron", "webview", "wry", "tao", "tiny_http", "axum")
REQUIRED_EVIDENCE_KINDS = {
    "reference-screenshot",
    "native-screenshot",
    "visual-comparison",
    "interaction-trace",
    "business-outcome",
    "performance",
}


def iter_cargo_manifests():
    yield ROOT / "Cargo.toml"
    yield from sorted(ROOT.glob("crates/*/Cargo.toml"))


def manifest_dependency_names(manifest: Path) -> set[str]:
    text = manifest.read_text(encoding="utf-8")
    if tomllib is not None:
        parsed = tomllib.loads(text)
        dependency_names = set()
        for section in ("dependencies", "dev-dependencies", "build-dependencies", "workspace.dependencies"):
            current = parsed
            for part in section.split("."):
                current = current.get(part, {})
            dependency_names.update(current.keys())
        for target in parsed.get("target", {}).values():
            dependency_names.update(target.get("dependencies", {}).keys())
        return dependency_names

    dependency_names = set()
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]").strip().strip("'\"")
            continue
        if "=" not in line:
            continue
        dependency = line.split("=", 1)[0].strip().strip("'\"")
        direct_sections = {"dependencies", "dev-dependencies", "build-dependencies"}
        section_parts = [part.strip().strip("'\"") for part in section.split(".")]
        if section in direct_sections or section.endswith(".dependencies"):
            dependency_names.add(dependency)
        elif len(section_parts) == 2 and section_parts[0] in direct_sections:
            dependency_names.add(section_parts[-1])
        elif len(section_parts) >= 2 and section_parts[-2] == "dependencies":
            dependency_names.add(section_parts[-1])
    return dependency_names


def lint_dependencies() -> list[str]:
    errors = []
    for manifest in iter_cargo_manifests():
        for dependency in manifest_dependency_names(manifest):
            lowered = dependency.lower()
            if any(part in lowered for part in FORBIDDEN_DEPENDENCY_PARTS):
                errors.append(f"{manifest}: forbidden browser/loopback dependency '{dependency}'")
    lock = ROOT / "Cargo.lock"
    if lock.exists():
        lock_text = lock.read_text(encoding="utf-8", errors="ignore").lower()
        for part in FORBIDDEN_DEPENDENCY_PARTS:
            if f'name = "{part}' in lock_text or f'name = "{part}"' in lock_text:
                errors.append(f"{lock}: forbidden dependency '{part}' appears in lockfile")
    return errors


def lint_registry() -> list[str]:
    registry_path = ROOT / "registry" / "screens.json"
    errors = []
    if not registry_path.exists():
        return [f"missing registry {registry_path}; run discover.py"]
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry.get("framework") != "slint":
        errors.append("registry framework must remain 'slint' until framework qualification explicitly changes it")
    seen = set()
    for screen in registry.get("screens", []):
        screen_id = screen.get("id", "<missing>")
        if screen_id in seen:
            errors.append(f"duplicate registry screen id {screen_id}")
        seen.add(screen_id)
        acceptance = screen.get("acceptance", {})
        if acceptance.get("visual_max_failed_pixel_ratio") != 0.0:
            errors.append(f"{screen_id}: visual acceptance tolerance was weakened")
        for key in (
            "require_text_parity",
            "require_accessibility_parity",
            "require_keyboard_parity",
            "require_business_outcome_parity",
        ):
            if acceptance.get(key) is not True:
                errors.append(f"{screen_id}: acceptance criterion {key} was weakened")
        if screen.get("status") in {"verified", "regression-locked"}:
            kinds = {evidence.get("kind") for evidence in screen.get("evidence", [])}
            missing = sorted(REQUIRED_EVIDENCE_KINDS - kinds)
            if missing:
                errors.append(f"{screen_id}: marked {screen.get('status')} without evidence kinds {missing}")
    return errors


def main() -> int:
    errors = lint_dependencies() + lint_registry()
    if errors:
        for error in errors:
            print(f"architecture-lint: {error}", file=sys.stderr)
        return 1
    print("architecture-lint: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
