#!/usr/bin/env python3
"""Discover Khitmee web screens, controls, routes, design tokens and API references.

The scanner is intentionally static and deterministic. It does not infer migration success:
status transitions require independent verification evidence written by verify.py or a human.
"""
from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "apps" / "khitmee_ops" / "khitmee_ops"
REGISTRY_PATH = REPO_ROOT / "desktop-frontend" / "registry" / "screens.json"
OVERRIDES_PATH = REPO_ROOT / "desktop-frontend" / "registry" / "screen-overrides.json"
CONTROL_TAGS = {"input", "select", "button", "textarea", "a"}
ENDPOINT_RE = re.compile(r"khitmee_ops\.api\.[A-Za-z0-9_.]+")
TOKEN_RE = re.compile(r"--([a-zA-Z0-9-]+)\s*:\s*([^;]+);")
KEY_RE = re.compile(r"<kbd>([^<]+)</kbd>", re.IGNORECASE)


class ScreenHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.h1 = ""
        self.controls: list[dict[str, Any]] = []
        self._capture: str | None = None
        self._capture_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag in {"title", "h1"}:
            self._capture = tag
            self._capture_text = []
        if tag in CONTROL_TAGS:
            control = {
                "control_type": tag,
                "id": attrs_dict.get("id", ""),
                "label": attrs_dict.get("aria-label") or attrs_dict.get("placeholder") or attrs_dict.get("value") or "",
                "role": attrs_dict.get("role", ""),
                "keyboard": [],
            }
            if control["id"] or control["label"] or control["role"]:
                self.controls.append(control)

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._capture_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if self._capture == tag:
            text = " ".join(part for part in self._capture_text if part).strip()
            if tag == "title":
                self.title = text
            elif tag == "h1" and not self.h1:
                self.h1 = text
            self._capture = None
            self._capture_text = []


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def discover_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    for css in sorted((WEB_ROOT / "public" / "css").glob("*.css")):
        for name, value in TOKEN_RE.findall(css.read_text(encoding="utf-8")):
            tokens[name] = " ".join(value.split())
    return tokens


def endpoint_contract(endpoint: str) -> dict[str, Any]:
    mutation = any(part in endpoint for part in ("submit", "transfer", "create", "update", "delete"))
    permissions = ["authenticated-counter-user"]
    if mutation:
        permissions.append("ERPNext authoritative mutation permission")
    return {
        "endpoint": endpoint,
        "authority": "ERPNext",
        "required_permissions": permissions,
        "mutation": mutation,
    }


def scan_screen(html_path: Path) -> dict[str, Any]:
    parser = ScreenHTMLParser()
    text = html_path.read_text(encoding="utf-8")
    parser.feed(text)
    route = "/" + html_path.stem
    scripts = re.findall(r'src="([^">]+\.js)', text)
    endpoints: set[str] = set()
    for script in scripts:
        asset_name = script.split("/")[-1].split("?")[0]
        for js_path in (WEB_ROOT / "public" / "js").glob(asset_name):
            endpoints.update(ENDPOINT_RE.findall(js_path.read_text(encoding="utf-8")))
    endpoints.update(ENDPOINT_RE.findall(text))

    keys = sorted(set(KEY_RE.findall(text)))
    for control in parser.controls:
        if control["id"] == "item-search":
            control["keyboard"] = ["F2"]
        elif control["id"] == "submit-print":
            control["keyboard"] = ["F9"]
    if keys and not any(control["keyboard"] for control in parser.controls):
        parser.controls.append({"control_type": "keyboard", "id": "page-shortcuts", "label": ",".join(keys), "role": "shortcut", "keyboard": keys})

    screen_id = {
        "khitmee_sales": "sales-voucher",
        "khitmee_transfer": "stock-transfer",
    }.get(html_path.stem, html_path.stem.replace("khitmee_", "").replace("_", "-"))
    return {
        "id": screen_id,
        "title": parser.h1 or parser.title or html_path.stem,
        "source_route": route,
        "status": "discovered",
        "dependencies": sorted(Path(script).name for script in scripts),
        "controls": parser.controls,
        "api_contracts": [endpoint_contract(endpoint) for endpoint in sorted(endpoints)],
        "native_component": "",
        "owner_scope": f"screens/{screen_id}",
        "evidence": [
            {
                "kind": "static-source-scan",
                "path": str(html_path.relative_to(REPO_ROOT)).replace("\\", "/"),
                "sha256": sha256(html_path),
                "note": "Deterministic source evidence; not visual parity evidence.",
            }
        ],
        "failures": [],
        "acceptance": {
            "visual_max_failed_pixel_ratio": 0.0,
            "require_text_parity": True,
            "require_accessibility_parity": True,
            "require_keyboard_parity": True,
            "require_business_outcome_parity": True,
            "max_startup_regression_ratio": 1.0,
            "max_idle_memory_bytes": 268435456,
            "max_p95_input_latency_ms": 50.0,
        },
    }


def merge_override(screen: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(screen)
    for key, value in override.items():
        if key in {"controls", "api_contracts", "evidence", "failures", "dependencies"}:
            existing = list(merged.get(key, []))
            for item in value:
                if item not in existing:
                    existing.append(item)
            merged[key] = existing
        elif key == "acceptance":
            acceptance = dict(merged.get("acceptance", {}))
            acceptance.update(value)
            merged[key] = acceptance
        else:
            merged[key] = value
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(REGISTRY_PATH))
    args = parser.parse_args()

    screens = [scan_screen(path) for path in sorted(WEB_ROOT.glob("www/khitmee_*.html"))]
    overrides = load_json(OVERRIDES_PATH, {"screens": {}, "additional_screens": [], "shared_component_scopes": {}, "experiments": []})
    by_id = {}
    for screen in screens:
        by_id[screen["id"]] = merge_override(screen, overrides.get("screens", {}).get(screen["id"], {}))
    for screen in overrides.get("additional_screens", []):
        by_id[screen["id"]] = merge_override(screen, overrides.get("screens", {}).get(screen["id"], {}))

    registry = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "framework": "slint",
        "design_tokens": discover_tokens(),
        "screens": [by_id[key] for key in sorted(by_id)],
        "shared_component_scopes": overrides.get("shared_component_scopes", {}),
        "experiments": overrides.get("experiments", []),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"discovered {len(screens)} source screens; registry has {len(registry['screens'])} entries: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
