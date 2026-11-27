#!/usr/bin/env python3
"""Authoritative ERPNext business-outcome comparison harness.

The harness verifies server behavior first and then requires equivalent native evidence.
It intentionally fails when the native authoritative adapter is absent.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def post_method(opener: urllib.request.OpenerDirector, base_url: str, endpoint: str, payload: dict[str, Any]) -> tuple[int, Any]:
    url = f"{base_url.rstrip('/')}/api/method/{endpoint}"
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode())
        except Exception:
            body = error.read().decode(errors="replace")
        return error.code, body


def login_opener(base_url: str, username: str, password: str) -> urllib.request.OpenerDirector:
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    if not password:
        return opener
    data = json.dumps({"usr": username, "pwd": password}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/method/login",
        data=data,
        headers={"content-type": "application/json"},
    )
    opener.open(request, timeout=20).read()
    csrf_token = next((cookie.value for cookie in jar if cookie.name == "csrf_token"), None)
    if not csrf_token:
        import re

        boot_html = opener.open(f"{base_url.rstrip('/')}/app", timeout=20).read().decode(errors="replace")
        match = re.search(r"(?:frappe\.csrf_token|window\.csrf_token)\s*=\s*[\"']([^\"']+)[\"']", boot_html)
        csrf_token = match.group(1) if match else None
    if csrf_token:
        opener.addheaders = [("X-Frappe-CSRF-Token", csrf_token)]
    return opener


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18086")
    parser.add_argument("--username", default="Administrator")
    parser.add_argument("--password", default="")
    parser.add_argument("--fixture", required=True, help="JSON sale fixture with warehouse/customer/price_list/items")
    parser.add_argument("--native-outcome", default=str(ROOT / "evidence" / "native-business-outcome.json"))
    parser.add_argument("--native-probe", help="optional native business-probe executable invoked with the same client_voucher_id")
    parser.add_argument("--operation", choices=["sale", "transfer"], default="sale")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    client_voucher_id = "migration-verify-" + uuid.uuid4().hex[:20]
    if args.operation == "transfer":
        payload = {
            "client_voucher_id": client_voucher_id,
            "source_warehouse": fixture["warehouse"],
            "target_warehouse": fixture["target_warehouse"],
            "transfer_mode": fixture.get("transfer_mode", "direct"),
            "outgoing_stock_entry": fixture.get("outgoing_stock_entry"),
            "items": fixture["items"],
            "printer_type": fixture.get("printer_type", "voucher"),
        }
        endpoint = "khitmee_ops.api.vouchers.submit_transfer"
    else:
        payload = {
            "client_voucher_id": client_voucher_id,
            "warehouse": fixture["warehouse"],
            "customer": fixture["customer"],
            "price_list": fixture["price_list"],
            "items": fixture["items"],
            "payment_mode": fixture.get("payment_mode"),
            "printer_type": fixture.get("printer_type", "voucher"),
            "override_rate": False,
        }
        endpoint = "khitmee_ops.api.vouchers.submit_sale"
    if args.native_probe:
        probe_result = subprocess.run(
            [
                args.native_probe,
                "--base-url", args.base_url,
                "--username", args.username,
                "--password", args.password,
                "--operation", args.operation,
                "--fixture", args.fixture,
                "--client-voucher-id", client_voucher_id,
                "--outcome", args.native_outcome,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe_result.returncode != 0:
            # Keep the harness strict: absence or adapter failure is reported below as missing/invalid evidence.
            pass
    opener = login_opener(args.base_url, args.username, args.password)
    first_status, first = post_method(opener, args.base_url, endpoint, payload)
    second_status, second = post_method(opener, args.base_url, endpoint, payload)

    failures = []
    first_message = first.get("message", first) if isinstance(first, dict) else first
    second_message = second.get("message", second) if isinstance(second, dict) else second
    first_voucher = first_message.get("voucher_no") if isinstance(first_message, dict) else None
    second_voucher = second_message.get("voucher_no") if isinstance(second_message, dict) else None
    if first_status != 200 or not first_voucher:
        failures.append(f"authoritative first submit failed with HTTP {first_status}: {str(first)[:300]}")
    if second_status != 200 or not second_voucher:
        failures.append(f"authoritative replay failed with HTTP {second_status}: {str(second)[:300]}")
    if first_voucher and second_voucher and first_voucher != second_voucher:
        failures.append(f"idempotent replay created mismatched vouchers {first_voucher} and {second_voucher}")

    native_path = Path(args.native_outcome)
    native = json.loads(native_path.read_text(encoding="utf-8")) if native_path.exists() else None
    if native is None:
        failures.append("native authoritative business outcome evidence is missing")
    else:
        if native.get("voucher_no") != first_voucher:
            failures.append("native voucher_no does not match authoritative ERPNext voucher_no")
        if native.get("used_fixture_substitution"):
            failures.append("native outcome used fixture substitution rather than ERPNext submission")

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "passed": not failures,
        "failures": failures,
        "authoritative": {
            "firstStatus": first_status,
            "secondStatus": second_status,
            "voucherNo": first_voucher,
            "replayVoucherNo": second_voucher,
        },
        "nativeOutcomePath": str(native_path),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failures": failures}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
