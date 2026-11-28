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
        "limit_page_length": 20,
    })
    rows = json.load(opener.open(base + query)).get("data", [])
    return rows[0]["name"] if rows else None

def find_draft_invoice(marker):
    query = resource + "?" + urllib.parse.urlencode({
        "filters": json.dumps([["remarks", "like", f"%{marker}%"], ["docstatus", "=", 0]]),
        "fields": json.dumps(["name", "docstatus"]),
        "limit_page_length": 1,
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
multi_draft_name = ensure_invoice(
    "migration-detail-multi-draft-v1",
    [("KM-00001", 1, 18.46), ("KM-00002", 2, 0.53), ("KM-00003", 3, 3.06)],
    submit=False,
)

def ensure_editable_draft(side):
    marker = f"migration-detail-editable-{side}-v2"
    import time
    unique_marker = f"{marker}-{time.time_ns()}"
    return ensure_invoice(unique_marker, [("KM-00001", 1, 18.46)], submit=False)

def ensure_paid_invoice(marker):
    if existing := find_invoice(marker):
        return existing
    payload = {
        "doctype": "Sales Invoice",
        "customer": "KM-CUST-0001",
        "company": "Khit Me Distribution",
        "posting_date": "2026-11-28",
        "posting_time": "10:30:00",
        "due_date": "2026-11-28",
        "currency": "USD",
        "conversion_rate": 1,
        "selling_price_list": "Standard Selling",
        "price_list_currency": "USD",
        "plc_conversion_rate": 1,
        "update_stock": 1,
        "set_warehouse": "Dist Hub 1 - KMD",
        "is_pos": 1,
        "remarks": marker,
        "items": [
            {"item_code": "KM-00001", "qty": 1, "rate": 18.46, "warehouse": "Dist Hub 1 - KMD"}
        ],
        "payments": [{"mode_of_payment": "Cash", "amount": 18.46}],
    }
    created = call(resource, payload)
    name = created["data"]["name"]
    call(resource + "/" + urllib.parse.quote(name), {"docstatus": 1}, "PUT")
    fetched = json.load(opener.open(base + resource + "/" + urllib.parse.quote(name)))["data"]
    if fetched.get("status") != "Paid" or float(fetched.get("outstanding_amount") or 0) != 0:
        raise RuntimeError(f"paid fixture {name} did not reach Paid/zero-outstanding state")
    return name

paid_name = ensure_paid_invoice("migration-detail-paid-v1")

def ensure_overdue_invoice(marker):
    if existing := find_invoice(marker):
        return existing
    name = ensure_invoice(marker, [("KM-00001", 1, 18.46)], submit=True)
    import subprocess
    for expression in [
        f"frappe.db.set_value('Sales Invoice','{name}','due_date','2026-11-26')",
        f"frappe.db.sql(\"update `tabPayment Schedule` set due_date='2026-11-26' where parent='{name}'\")",
        f"frappe.db.set_value('Sales Invoice','{name}','status','Overdue')",
    ]:
        result = subprocess.run(
            ["docker", "exec", "khitmee-perf-backend-1", "bench", "--site", "bench-loadtest.localhost", "execute", expression],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"overdue fixture setup failed: {result.stderr[:500] or result.stdout[:500]}")
    fetched = json.load(opener.open(base + resource + "/" + urllib.parse.quote(name)))["data"]
    if fetched.get("status") != "Overdue":
        raise RuntimeError(f"overdue fixture {name} did not read back as Overdue")
    return name

overdue_name = ensure_overdue_invoice("migration-detail-overdue-v1")

web_draft = ensure_editable_draft("web")
native_draft = ensure_editable_draft("native")
def ensure_editable_multi_draft(side):
    import time
    unique_marker = f"migration-detail-multi-editable-{side}-v1-{time.time_ns()}"
    return ensure_invoice(
        unique_marker,
        [("KM-00001", 1, 18.46), ("KM-00002", 2, 0.53), ("KM-00003", 3, 3.06)],
        submit=False,
    )

web_multi_draft = ensure_editable_multi_draft("web")
native_multi_draft = ensure_editable_multi_draft("native")

def ensure_submitted_pair_invoice(side):
    import time
    unique_marker = f"migration-detail-cancel-{side}-v1-{time.time_ns()}"
    return ensure_invoice(unique_marker, [("KM-00001", 1, 18.46)], submit=True)

web_submitted = ensure_submitted_pair_invoice("web")
native_submitted = ensure_submitted_pair_invoice("native")

print(json.dumps({"multi_item": multi_name, "draft": draft_name, "multi_draft": multi_draft_name, "paid": paid_name, "overdue": overdue_name, "web_draft": web_draft, "native_draft": native_draft, "web_multi_draft": web_multi_draft, "native_multi_draft": native_multi_draft, "web_submitted": web_submitted, "native_submitted": native_submitted}))
