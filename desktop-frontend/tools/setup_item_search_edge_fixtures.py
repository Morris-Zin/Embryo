#!/usr/bin/env python3
"""Prepare isolated-ERP item-search edge fixtures idempotently."""
import http.cookiejar
import json
import os
import urllib.parse
import urllib.request

base = os.environ.get("ERPNEXT_BASE_URL", "http://127.0.0.1:18086").rstrip("/")
password = os.environ.get("ERPNEXT_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
if not password:
    raise SystemExit("ERPNEXT_PASSWORD is required")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def request(path, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    return json.load(opener.open(req))

opener.open(
    urllib.request.Request(
        base + "/api/method/login",
        data=urllib.parse.urlencode({"usr": os.environ.get("ERPNEXT_USERNAME", "Administrator"), "pwd": password}).encode(),
    )
)

resource = "/api/resource/" + urllib.parse.quote("Item Price")
query = resource + "?" + urllib.parse.urlencode(
    {
        "filters": json.dumps([
            ["item_code", "=", "KM-00005"],
            ["customer", "=", "KM-CUST-0002"],
            ["price_list", "=", "Standard Selling"],
        ]),
        "fields": json.dumps(["name"]),
    }
)
rows = opener.open(base + query).read()
rows = json.loads(rows).get("data", [])
price_payload = {
    "doctype": "Item Price",
    "item_code": "KM-00005",
    "price_list": "Standard Selling",
    "selling": 1,
    "customer": "KM-CUST-0002",
    "price_list_rate": 11.11,
    "currency": "USD",
}
if rows:
    request(resource + "/" + urllib.parse.quote(rows[0]["name"]), {"price_list_rate": 11.11}, "PUT")
else:
    request(resource, price_payload)

localized_name = "Phsar Select Baby Care 00327 မြန်မာ"
request("/api/resource/" + urllib.parse.quote("Item") + "/KM-00327", {"item_name": localized_name}, "PUT")
print("item-search edge fixtures ready")
