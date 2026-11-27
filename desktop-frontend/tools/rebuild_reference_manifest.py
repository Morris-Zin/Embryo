#!/usr/bin/env python3
"""Rebuild web-reference manifest metadata for already captured evidence directories."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def reference_id(route: str) -> str:
    return route.strip("/").replace("/", "-").replace("_", "-").replace("#", "--") or "root"


def main() -> int:
    evidence = ROOT / "evidence" / "web-reference"
    registry = load_json(ROOT / "registry" / "screens.json", {})
    routes_by_id = {}
    for screen in registry.get("screens", []):
        route = screen.get("source_route", "")
        if route:
            routes_by_id.setdefault(reference_id(route), route)
            routes_by_id.setdefault(screen.get("id", ""), route)

    entries = []
    for screen_dir in sorted(path for path in evidence.iterdir() if path.is_dir()):
        captures = []
        if (screen_dir / "reference.png").exists():
            captures.append(("initial", screen_dir))
        captures.extend((child.name, child) for child in sorted(screen_dir.iterdir()) if child.is_dir() and (child / "reference.png").exists())
        for state, directory in captures:
            screenshot = directory / "reference.png"
            dom = load_json(directory / "dom.json", {})
            trace = load_json(directory / "trace.json", {})
            actions = [event.get("action") for event in trace.get("events", []) if event.get("type") == "capture-action"]
            document = next((request for request in trace.get("requests", []) if request.get("resourceType") == "document"), {})
            entries.append({
                "route": routes_by_id.get(screen_dir.name),
                "id": screen_dir.name,
                "httpStatus": document.get("status"),
                "screenshot": str(screenshot.relative_to(evidence)).replace("\\", "/"),
                "screenshotSha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                "controls": len(dom.get("controls", [])),
                "title": dom.get("title", ""),
                "h1": dom.get("h1", ""),
                "state": state,
                "actions": actions,
                "recoveredFromExistingCapture": True,
            })

    manifest = {
        "schemaVersion": 1,
        "manifestRebuiltAt": datetime.now(timezone.utc).isoformat(),
        "viewport": {"width": 1180, "height": 760, "deviceScaleFactor": 1},
        "routes": entries,
        "authentication": "recovered metadata; original capture authentication unchanged",
    }
    output = evidence / "manifest.json"
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"entries": len(entries), "path": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
