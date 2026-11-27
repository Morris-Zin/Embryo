#!/usr/bin/env python3
"""Prepare isolated-ERP Sales Invoice detail fixtures idempotently."""
import http.cookiejar
import json
import os
import urllib.parse
import urllib.request
import urllib.error

base = os.environ.get("ERPNEXT_BASE_URL", "http://127.0.0.1:18086").rstrip("/")
password = os.environ.get("ERPNEXT_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
if not password:
    raise SystemExit("ERPNEXT_PASSWORD is required")
username = os.environ.get("ERPNEXT_USERNAME", "Administrator")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def call(path, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"content-type": "application/json"})
    try:
        return json.load(opener.open(req, timeout=30))
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(f"{path} HTTP {error.code}: {body[:700]}")

call("/api/method/login", {"usr": username, "pwd": password})
csrf = next((cookie.value for cookie in jar if cookie.name == "csrf_token"), None)
if csrf:
    opener.addheaders = [("X-Frappe-CSRF-Token", csrf)]

resource = "/api/resource/" + urllib.parse.quote("Sales Invoice")
marker = "migration-detail-multi-item-v1"
query = resource + "?" + urllib.parse.urlencode({
    "filters": json.dumps([["remarks", "like", f"%{marker}%"]]),
    "fields": json.dumps(["name", "docstatus"]),
    "limit_page_length": 5,
})
existing = json.load(opener.open(base + query)).get("data", [])
if existing:
    name = existing[0]["name"]
else:
    payload = {
        "doctype": "Sales Invoice",
        "customer": "KM-CUST-0001",
        "company": "Khit Me Distribution",
        "posting_date": "2026-11-27",
        "posting_time": "10:30:00",
        "due_date": "2026-12-11",
        "currency": "USD",
        "conversion_rate": 1,
        "selling_price_list": "Standard Selling",
        "price_list_currency": "USD",
        "plc_conversion_rate": 1,
        "update_stock": 1,
        "set_warehouse": "Dist Hub 1 - KMD",
        "remarks": marker,
        "items": [
            {"item_code": "KM-00001", "qty": 1, "rate": 18.46, "warehouse": "Dist Hub 1 - KMD"},
            {"item_code": "KM-00002", "qty": 2, "rate": 0.53, "warehouse": "Dist Hub 1 - KMD"},
            {"item_code": "KM-00003", "qty": 3, "rate": 3.06, "warehouse": "Dist Hub 1 - KMD"},
        ],
    }
    created = call(resource, payload)
    name = created["data"]["name"]
    call(resource + "/" + urllib.parse.quote(name), {"docstatus": 1}, "PUT")
print(name)
