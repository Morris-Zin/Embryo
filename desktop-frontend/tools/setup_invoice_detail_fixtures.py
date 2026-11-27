#!/usr/bin/env python3
"""Prepare isolated-ERP Sales Invoice detail fixtures idempotently."""
import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request

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
        raise RuntimeError(f"{path} HTTP {error.code}: {body[:900]}")

call("/api/method/login", {"usr": username, "pwd": password})
csrf = next((cookie.value for cookie in jar if cookie.name == "csrf_token"), None)
if csrf:
    opener.addheaders = [("X-Frappe-CSRF-Token", csrf)]

resource = "/api/resource/" + urllib.parse.quote("Sales Invoice")

def find_invoice(marker):
    query = resource + "?" + urllib.parse.urlencode({
        "filters": json.dumps([["remarks", "like", f"%{marker}%"]]),
        "fields": json.dumps(["name", "docstatus"]),
        "limit_page_length": 5,
    })
    rows = json.load(opener.open(base + query)).get("data", [])
    return rows[0]["name"] if rows else None

def ensure_invoice(marker, items, submit=True):
    if existing := find_invoice(marker):
        return existing
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
            {"item_code": item_code, "qty": qty, "rate": rate, "warehouse": "Dist Hub 1 - KMD"}
            for item_code, qty, rate in items
        ],
    }
    created = call(resource, payload)
    name = created["data"]["name"]
    if submit:
        call(resource + "/" + urllib.parse.quote(name), {"docstatus": 1}, "PUT")
    return name

multi_name = ensure_invoice(
    "migration-detail-multi-item-v1",
    [("KM-00001", 1, 18.46), ("KM-00002", 2, 0.53), ("KM-00003", 3, 3.06)],
)
draft_name = ensure_invoice(
    "migration-detail-draft-v1",
    [("KM-00001", 1, 18.46)],
    submit=False,
)
print(json.dumps({"multi_item": multi_name, "draft": draft_name}))
