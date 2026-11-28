#!/usr/bin/env python3
"""External seasonal manager offload integration.

The module is intentionally dependency-free so the benchmark owner can run it outside the
measured host with only Python. It proxies exact oracle responses for static report windows,
caches them by principal and snapshot, measures concurrency, and fails closed for dynamic
windows whose cashier writes are not part of the verified snapshot.
"""

import argparse
import copy
import csv
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from urllib.parse import parse_qs, urlencode, urlparse

SCHEMA_VERSION = "manager-offload-v1"
MANAGER_DIR = Path(__file__).resolve().parent
REPO_ROOT = MANAGER_DIR.parents[3] if len(MANAGER_DIR.parents) > 3 else MANAGER_DIR
QUERY_METHOD = "frappe.desk.query_report.run"
SUMMARY_GROUPS = ("Warehouse", "Monthly")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class RequestError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_text(value):
    """canonical-json-v1 compatible with perf-suite/k6/evaluate_seasonal.py."""
    if value is None or isinstance(value, bool):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot be canonically hashed")
        if value == 0:
            return "0"
        if value.is_integer() and abs(value) <= 9007199254740991:
            return str(int(value))
        text = format(value, ".15f").rstrip("0").rstrip(".")
        return text if text not in {"", "-0"} else "0"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json_text(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(str(key), ensure_ascii=False, separators=(",", ":"))
            + ":"
            + canonical_json_text(value[key])
            for key in sorted(value, key=lambda item: str(item))
        ) + "}"
    raise TypeError(f"unsupported canonical JSON value type: {type(value).__name__}")


def canonical_json_sha256(value):
    return hashlib.sha256(canonical_json_text(value).encode("utf-8")).hexdigest()


def json_compatible_value(value, handler=None):
    """Return a JSON-compatible value using the same scalar conversion as Frappe/orjson.

    The recursion is intentional: it lets in-process capture normalize row objects without
    first serializing and reparsing a potentially hundreds-of-MB report payload.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_compatible_value(item, handler) for item in value]
    if isinstance(value, dict):
        return {str(key): json_compatible_value(item, handler) for key, item in value.items()}
    if handler is not None:
        converted = handler(value)
        if converted is not value:
            return json_compatible_value(converted, handler)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json_update(hasher, value, handler=None):
    """Incrementally hash canonical-json-v1 without materializing the full document."""
    if value is None or isinstance(value, bool):
        text = json.dumps(value, separators=(",", ":"))
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot be canonically hashed")
        if value == 0:
            text = "0"
        elif value.is_integer() and abs(value) <= 9007199254740991:
            text = str(int(value))
        else:
            text = format(value, ".15f").rstrip("0").rstrip(".")
            if text in {"", "-0"}:
                text = "0"
    elif isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(value, (list, tuple)):
        hasher.update(b"[")
        for index, item in enumerate(value):
            if index:
                hasher.update(b",")
            canonical_json_update(hasher, item, handler)
        hasher.update(b"]")
        return
    elif isinstance(value, dict):
        hasher.update(b"{")
        for index, key in enumerate(sorted(value, key=lambda item: str(item))):
            if index:
                hasher.update(b",")
            hasher.update(json.dumps(str(key), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            hasher.update(b":")
            canonical_json_update(hasher, value[key], handler)
        hasher.update(b"}")
        return
    else:
        if handler is None:
            raise TypeError(f"unsupported canonical JSON value type: {type(value).__name__}")
        converted = handler(value)
        if converted is value:
            raise TypeError(f"unsupported canonical JSON value type: {type(value).__name__}")
        canonical_json_update(hasher, converted, handler)
        return
    hasher.update(text.encode("utf-8"))


def canonical_json_streaming_sha256(value, handler=None):
    hasher = hashlib.sha256()
    canonical_json_update(hasher, value, handler)
    return hasher.hexdigest()


STOCK_CACHE_KIND = "query_report_stock_oracle_v2"
CACHE_INDEX_SCHEMA_VERSION = "manager-oracle-cache-index-v2"
STATIC_CACHE_VERIFICATION_SCHEMA = "stock-static-cache-verification-v1"
STOCK_DETAIL_VERIFICATION_SCHEMA = "stock-detail-double-verification-v1"
HYBRID_SERVING_VERIFICATION_SCHEMA = "hybrid-serving-verification-v1"


def sha256_bytes(body):
    return hashlib.sha256(body).hexdigest()


def json_dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def atomic_write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("xb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def atomic_write_json(path, value):
    atomic_write_bytes(path, (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def parse_iso_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"{field} must be an ISO YYYY-MM-DD date") from exc


def window_days(window):
    start = parse_iso_date(window.get("from_date"), "from_date")
    end = parse_iso_date(window.get("to_date"), "to_date")
    return start, end, (end - start).days + 1


def validate_trial_request(req):
    if not isinstance(req, dict):
        raise RequestError("trial request must be a JSON object")
    if req.get("schema_version") != SCHEMA_VERSION:
        raise RequestError(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("trial_id", "fixture_sha256", "site", "artifact_dir", "report_windows", "source", "oracle", "manager_workload", "integration", "freshness"):
        if field not in req:
            raise RequestError(f"missing required field {field}")
    if not isinstance(req.get("seed"), int) or req["seed"] < 0:
        raise RequestError("seed must be a non-negative integer")
    if not re_full_hash(req["fixture_sha256"]):
        raise RequestError("fixture_sha256 must be a lowercase SHA-256 hex digest")
    windows = req["report_windows"]
    for name in ("year_to_date", "full_year"):
        if name not in windows:
            raise RequestError(f"missing report_windows.{name}")
        start, end, _days = window_days(windows[name])
        if start > end:
            raise RequestError(f"report_windows.{name} is reversed")
    _start, _end, full_days = window_days(windows["full_year"])
    if full_days < 365:
        raise RequestError("full_year window must span at least 365 inclusive dates")
    source = req["source"]
    oracle = req["oracle"]
    for label, section in (("source", source), ("oracle", oracle)):
        base_url = str(section.get("base_url", ""))
        allowed = ("http://", "https://") if label == "source" else ("http://", "https://", "inprocess://")
        if not base_url.startswith(allowed):
            raise RequestError(f"{label}.base_url must be an HTTP URL" + (" or inprocess:// oracle transport" if label == "oracle" else ""))
    if oracle.get("erpnext_unmodified") is not True:
        raise RequestError("oracle.erpnext_unmodified must be true")
    if oracle.get("snapshot_kind") not in ("static-fixture", "synced-replica", "same-database-read-model"):
        raise RequestError("unsupported oracle.snapshot_kind")
    parse_iso_date(oracle.get("snapshot_cutoff_date"), "oracle.snapshot_cutoff_date")
    workload = req["manager_workload"]
    for field in ("summary_report", "detail_report", "export_enqueue_method", "export_download_method"):
        if not workload.get(field):
            raise RequestError(f"missing manager_workload.{field}")
    if not (1 <= int(workload.get("detail_page_count", 0)) <= 20):
        raise RequestError("detail_page_count must be between 1 and 20")
    if not (1 <= int(workload.get("detail_page_length", 0)) <= 500):
        raise RequestError("detail_page_length must be between 1 and 500")
    if "export_in_timed_load" in workload and not isinstance(workload["export_in_timed_load"], bool):
        raise RequestError("manager_workload.export_in_timed_load must be a boolean when present")
    integration = req["integration"]
    if integration.get("mode") not in ("split-manager-url", "reverse-proxy-shim"):
        raise RequestError("integration.mode is unsupported")
    if not (1 <= int(integration.get("listen_port", 0)) <= 65535):
        raise RequestError("integration.listen_port must be a TCP port")
    if integration["mode"] == "reverse-proxy-shim" and integration.get("owner_accepted_proxy_overhead") is not True:
        raise RequestError("reverse-proxy-shim requires owner_accepted_proxy_overhead=true")
    if "fail_closed_on_cache_miss" in integration and not isinstance(integration["fail_closed_on_cache_miss"], bool):
        raise RequestError("integration.fail_closed_on_cache_miss must be a boolean when present")
    if "oracle_timeout_seconds" in req and not (1 <= int(req["oracle_timeout_seconds"]) <= 3600):
        raise RequestError("oracle_timeout_seconds must be between 1 and 3600 when present")
    freshness = req["freshness"]
    if freshness.get("required_write_cutoff") not in ("pre-trial-static", "include-cashier-writes"):
        raise RequestError("freshness.required_write_cutoff is unsupported")
    return copy.deepcopy(req)


def re_full_hash(value):
    value = str(value or "")
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def artifact_dir_for(req):
    path = Path(req["artifact_dir"])
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def assess_freshness(req):
    oracle = req["oracle"]
    cutoff = parse_iso_date(oracle["snapshot_cutoff_date"], "oracle.snapshot_cutoff_date")
    windows = req["report_windows"]
    window_rows = []
    static_windows = True
    for name in ("year_to_date", "full_year"):
        _start, end, _days = window_days(windows[name])
        covered = end <= cutoff
        static_windows = static_windows and covered
        window_rows.append({
            "window": name,
            "from_date": windows[name]["from_date"],
            "to_date": windows[name]["to_date"],
            "oracle_snapshot_cutoff_date": oracle["snapshot_cutoff_date"],
            "covered_by_static_snapshot": covered,
        })
    required = req["freshness"]["required_write_cutoff"]
    if required == "include-cashier-writes":
        return {
            "decision": "parity-reject",
            "exact_static_snapshot": False,
            "reason": "trial requests require cashier writes, but no exact synchronized ERPNext write model has been verified",
            "windows": window_rows,
            "cashier_writes_included": False,
            "unsupported": ["include-cashier-writes"],
        }
    if not static_windows:
        return {
            "decision": "parity-reject",
            "exact_static_snapshot": False,
            "reason": "one or more report windows extend beyond the oracle snapshot cutoff",
            "windows": window_rows,
            "cashier_writes_included": False,
            "unsupported": ["report-window-after-snapshot-cutoff"],
        }
    return {
        "decision": "exact-static-window",
        "exact_static_snapshot": True,
        "reason": "all report windows end at or before the matched oracle snapshot cutoff; trial-day cashier writes cannot change these rows",
        "windows": window_rows,
        "cashier_writes_included": False,
        "unsupported": [],
    }


def git_ref_status():
    refs = {
        "pr7_reference": "62a584da81457eee9b33fd082e2edb193dedd8ca",
        "issue2_candidate_branch": "feat/daily-profit-summary-and-guardrails",
    }
    result = {}
    for name, ref in refs.items():
        try:
            commit = subprocess.check_output(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", ref + "^{commit}"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            ).strip()
            paths = subprocess.check_output(
                ["git", "-C", str(REPO_ROOT), "ls-tree", "-r", "--name-only", commit],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            ).splitlines()
            result[name] = {
                "ref": ref,
                "available": True,
                "commit": commit,
                "has_profit_summary": any(path.startswith("apps/khitmee_ops/khitmee_ops/profit_summary/") for path in paths),
                "has_offload_benchmark": any(path.startswith("perf-suite/offload/") for path in paths),
            }
        except (OSError, subprocess.SubprocessError):
            result[name] = {"ref": ref, "available": False, "has_profit_summary": False, "has_offload_benchmark": False}
    return result


def read_model_assessment(req):
    freshness = assess_freshness(req)
    refs = git_ref_status()
    return {
        "schema_version": "manager-read-model-assessment-v1",
        "generated_at": utc_now(),
        "trial_id": req["trial_id"],
        "freshness": freshness,
        "git_refs": refs,
        "applied_read_model": {
            "kind": "principal-and-snapshot-scoped exact oracle response cache",
            "applied": freshness["exact_static_snapshot"],
            "why": "It is byte-identical for a fixed static window, fixed actor/principal and matched oracle snapshot; it performs no financial estimation.",
        },
        "incremental_mariadb_read_models": {
            "pr7_additive_summary_status": "not-applied-to-erpnext-gross-profit",
            "issue2_candidate_status": "available-in-git-ref" if refs["issue2_candidate_branch"].get("has_profit_summary") else "unavailable-in-git-refs",
            "exactness_boundary": [
                "PR #7 measured canonical additive transport/read-model parity, not ERPNext Gross Profit semantics.",
                "Issue #2 candidate code is inspected as a git ref but is not treated as proven in this fixture without live oracle parity, permissions, repost/dirty-window and export evidence.",
                "Dynamic cashier writes, cancellations, returns, reposts, bundles and permission-visible populations require exact ERPNext semantics or a live oracle fallback."
            ],
        },
        "parity_reject_paths": [
            {"path": "linked and unlinked sales returns", "reason": "lightweight additive rows can reverse or duplicate the wrong contribution family"},
            {"path": "FIFO valuation and repost item valuation completion", "reason": "valuation depends on ordered stock ledger state and repost completion, not only additive invoice lines"},
            {"path": "cancellation, amendment and backdated vouchers", "reason": "modified-time upserts do not by themselves prove correct invalidation of every affected report date"},
            {"path": "product bundles and packed items", "reason": "stock ERPNext expands bundle lines through a separate semantic path"},
            {"path": "permission-filtered financial populations", "reason": "row visibility depends on the bound principal, user permissions, match conditions and export permission"},
            {"path": "precision, ordering and tie behavior", "reason": "financial output requires exact Decimal precision, stock ordering, cross-month ordering and tie semantics"},
            {"path": "current/dirty-day cashier writes", "reason": "the static oracle snapshot does not include trial writes; serving it as current would be stale"}
        ],
    }


class HttpResponse:
    def __init__(self, status, reason, headers, body):
        self.status = status
        self.reason = reason
        self.headers = list(headers)
        self.body = body

    def header(self, name):
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return ""

    def header_all(self, name):
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


def http_request(base_url, method, path, body=b"", headers=None, timeout=60):
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RequestError(f"unsupported base URL {base_url}")
    conn_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    conn = conn_class(parsed.hostname, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=dict(headers or {}))
        response = conn.getresponse()
        data = response.read()
        return HttpResponse(response.status, response.reason, response.getheaders(), data)
    finally:
        conn.close()


def api_path(method, query=""):
    return "/api/method/" + method + (("?" + query) if query else "")


def form_body(fields):
    return urlencode({key: value for key, value in fields.items() if value is not None}).encode("utf-8")


def parse_set_sid(headers):
    for header, value in headers:
        if header.lower() == "set-cookie":
            cookie = SimpleCookie()
            cookie.load(value)
            if "sid" in cookie:
                return cookie["sid"].value
    return ""


def cookie_sid(header_value):
    if not header_value:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(header_value)
    except Exception:
        return ""
    return cookie["sid"].value if "sid" in cookie else ""


INPROCESS_SESSIONS = {}
INPROCESS_FRAPPE = None


def inprocess_oracle(base_url):
    return str(base_url or "").startswith("inprocess://")


def inprocess_connect(site):
    global INPROCESS_FRAPPE
    if INPROCESS_FRAPPE is None:
        import frappe
        frappe.init(site=site)
        frappe.connect()
        INPROCESS_FRAPPE = frappe
    return INPROCESS_FRAPPE


def inprocess_response_payload(payload, status=200, headers=None):
    try:
        import orjson
        from frappe.utils.response import json_handler
        body = orjson.dumps(payload, default=json_handler)
    except Exception:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return HttpResponse(status, "OK" if status < 400 else "Error", headers or [("Content-Type", "application/json")], body)


def inprocess_login(base_url, user, password, site=None):
    try:
        frappe = inprocess_connect(site)
        from frappe.utils.password import Auth, passlibctx
        rows = (
            frappe.qb.from_(Auth)
            .select(Auth.name, Auth.password)
            .where((Auth.doctype == "User") & (Auth.name == user) & (Auth.fieldname == "password") & (Auth.encrypted == 0))
            .limit(1)
            .run(as_dict=True)
        )
        if not rows or not passlibctx.verify(password, rows[0].password):
            return inprocess_response_payload({"error": "Incorrect User or Password"}, 401), ""
        canonical_user = rows[0].name
        frappe.set_user(canonical_user)
        sid = "inprocess-" + uuid.uuid4().hex
        INPROCESS_SESSIONS[sid] = canonical_user
        return inprocess_response_payload({"message": "Logged In", "full_name": canonical_user}), sid
    except Exception as exc:
        return inprocess_response_payload({"error": type(exc).__name__}, 500), ""


def inprocess_call(method, fields, sid, site=None):
    frappe = inprocess_connect(site)
    user = INPROCESS_SESSIONS.get(sid)
    if not user:
        return inprocess_response_payload({"error": "in-process session missing"}, 401)
    frappe.set_user(user)
    try:
        frappe.local.form_dict = frappe._dict(fields or {})
        if method == QUERY_METHOD:
            from frappe.desk.query_report import run
            result = run(
                report_name=fields.get("report_name"),
                filters=fields.get("filters"),
                ignore_prepared_report=fields.get("ignore_prepared_report"),
            )
        else:
            function = frappe.get_attr(method)
            result = function(**fields)
        if hasattr(result, "get_data") and hasattr(result, "status_code"):
            body = result.get_data()
            headers = [(key, value) for key, value in result.headers.items()]
            return HttpResponse(result.status_code, getattr(result, "status", "OK"), headers, body)
        return inprocess_response_payload({"message": result})
    except Exception as exc:
        status = 500
        permission_error = getattr(frappe, "PermissionError", None)
        does_not_exist = getattr(frappe, "DoesNotExistError", None)
        if permission_error is not None and isinstance(exc, permission_error):
            status = 403
        elif does_not_exist is not None and isinstance(exc, does_not_exist):
            status = 404
        return inprocess_response_payload({"error": type(exc).__name__}, status)


def frappe_login(base_url, user, password, timeout=30, site=None):
    if inprocess_oracle(base_url):
        return inprocess_login(base_url, user, password, site=site)
    body = form_body({"usr": user, "pwd": password})
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if site:
        headers["X-Frappe-Site-Name"] = site
    response = http_request(
        base_url,
        "POST",
        "/api/method/login",
        body,
        headers,
        timeout=timeout,
    )
    return response, parse_set_sid(response.headers)


def frappe_post_method(base_url, method, fields, sid="", timeout=120, site=None):
    if inprocess_oracle(base_url):
        return inprocess_call(method, dict(fields), sid, site=site)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if sid:
        headers["Cookie"] = "sid=" + sid
    if site:
        headers["X-Frappe-Site-Name"] = site
    return http_request(base_url, "POST", api_path(method), form_body(fields), headers, timeout=timeout)


def frappe_get_method(base_url, method, query, sid="", timeout=180, site=None):
    if inprocess_oracle(base_url):
        fields = {key: values[-1] if values else "" for key, values in parse_qs(query, keep_blank_values=True).items()}
        return inprocess_call(method, fields, sid, site=site)
    headers = {"Accept": "*/*"}
    if sid:
        headers["Cookie"] = "sid=" + sid
    if site:
        headers["X-Frappe-Site-Name"] = site
    return http_request(base_url, "GET", api_path(method, query), b"", headers, timeout=timeout)


def json_body(response):
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def report_parts(response):
    data = json_body(response)
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return None
    columns = message.get("columns")
    rows = message.get("result")
    if not isinstance(rows, list):
        return None
    return {"columns": columns, "rows": rows, "message": message}


def report_digest(response):
    parts = report_parts(response)
    if parts is None:
        return "", 0
    return canonical_json_sha256([parts["columns"], parts["rows"]]), len(parts["rows"])


def extract_export_name(response):
    data = json_body(response)
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return ""
    if message.get("export_log_name") or message.get("name"):
        return str(message.get("export_log_name") or message.get("name"))
    nested = message.get("export_log")
    if isinstance(nested, dict):
        return str(nested.get("name") or nested.get("export_log_name") or "")
    return ""


def load_credentials(env_name, actors=None):
    if not env_name:
        raise RequestError("credential_env is required for live oracle capture")
    raw = os.environ.get(env_name)
    if not raw:
        raise RequestError(f"environment variable {env_name} is not set")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestError(f"environment variable {env_name} must contain JSON") from exc
    result = {}
    actors = actors or {}
    for actor, meta in actors.items():
        user = meta.get("user")
        entry = data.get(actor)
        if isinstance(entry, str):
            entry = {"user": user, "password": entry}
        if not isinstance(entry, dict) and user in data:
            value = data[user]
            entry = {"user": user, "password": value} if isinstance(value, str) else value
        if not isinstance(entry, dict) or not entry.get("password"):
            raise RequestError(f"credentials for actor {actor} are missing in {env_name}")
        entry_user = entry.get("user") or user
        if user and entry_user and str(entry_user).casefold() != str(user).casefold():
            raise RequestError(f"credentials for actor {actor} authenticate {entry_user}, but the trial request expects {user}")
        result[actor] = {"user": entry_user, "password": entry["password"]}
    if not result and isinstance(data, dict) and data.get("user") and data.get("password"):
        result["default"] = {"user": data["user"], "password": data["password"]}
    if not result:
        raise RequestError(f"no usable actor credentials found in {env_name}")
    return result


def cache_key(kind, user, payload):
    return hashlib.sha256(canonical_json_text({"kind": kind, "user": user or "", "payload": payload}).encode("utf-8")).hexdigest()


def canonical_filter_object(filters):
    if isinstance(filters, str):
        filters = json.loads(filters)
    if not isinstance(filters, dict):
        raise RequestError("filters must be a JSON object")
    return json.loads(json_dumps(filters))


def report_key_payload(report_name, filters, start=None, page_length=None, ignore_prepared_report=0):
    payload = {"report_name": report_name, "filters": canonical_filter_object(filters), "ignore_prepared_report": int(ignore_prepared_report or 0)}
    if start is not None:
        payload["start"] = int(start)
    if page_length is not None:
        payload["page_length"] = int(page_length)
    return payload


def save_response_artifact(folder, label, response):
    digest = sha256_bytes(response.body)
    name = f"{label}-{digest[:16]}.bin"
    path = folder / name
    atomic_write_bytes(path, response.body)
    return str(path.relative_to(folder.parent)) if path.is_relative_to(folder.parent) else str(path), digest


def observation_base(req, actor, user, event, group, window, cache):
    return {
        "observation_id": f"oracle/{event}/{group}/{len(window['to_date'])}",
        "event": event,
        "group": group,
        "actor": actor,
        "actor_user": user,
        "window_scope": window["scope"],
        "window_from_date": window["from_date"],
        "window_to_date": window["to_date"],
        "cache_key": cache,
        "hash_algorithm": "sha256",
    }


def expected_actor_permissions(req, actor):
    return list(((req["source"].get("actors") or {}).get(actor) or {}).get("expected_permissions") or [])


def capture_report(req, oracle, credentials, actor, group, window, response_dir):
    credential = credentials[actor]
    user = credential["user"]
    login_response, sid = frappe_login(oracle["base_url"], user, credential["password"], site=req.get("site"))
    login_data = json_body(login_response)
    base = {
        "actor": actor,
        "actor_user": user,
        "login_http_status": login_response.status,
        "login_ok": login_response.status == 200 and bool(sid),
        "login_message": str((login_data or {}).get("message") or "")[:120],
    }
    if not base["login_ok"]:
        return [dict(base, event="permission_reject", group=group, ok=False, reason="oracle login failed")], {}
    filters = {
        "company": req.get("company") or "Khit Me Distribution",
        "from_date": window["from_date"],
        "to_date": window["to_date"],
        "group_by": group,
    }
    started = time.monotonic()
    response = frappe_post_method(
        oracle["base_url"],
        QUERY_METHOD,
        {"report_name": req["manager_workload"].get("oracle_report", "Gross Profit"), "filters": json.dumps(filters, separators=(",", ":")), "ignore_prepared_report": 1},
        sid,
        timeout=int(req.get("oracle_timeout_seconds", 300)),
        site=req.get("site"),
    )
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    digest, rows = report_digest(response)
    key_payload = report_key_payload(req["manager_workload"]["summary_report"], filters, ignore_prepared_report=1)
    key = cache_key(STOCK_CACHE_KIND, user, key_payload)
    artifact_name = ""
    body_sha = sha256_bytes(response.body)
    if response.status == 200 and digest:
        artifact_name, body_sha = save_response_artifact(response_dir, "summary-" + group.lower(), response)
    obs = {
        **base,
        **observation_base(req, actor, user, "manager_summary", group, window, key),
        "observation_id": f"oracle/manager_summary/{group}/0",
        "http_status": response.status,
        "duration_ms": duration_ms,
        "row_count": rows,
        "sha256": digest,
        "result_sha256": digest,
        "response_sha256": body_sha,
        "response_artifact": artifact_name,
        "hash_canonicalization": "canonical-json-v1([columns,result])",
        "key_payload": key_payload,
        "ok": response.status == 200 and bool(digest) and rows > 0,
        "permission_evidence": {"login_ok": base["login_ok"], "report_http_status": response.status, "expected_permissions": expected_actor_permissions(req, actor)},
    }
    if not obs["ok"]:
        obs["event"] = "manager_summary_reject"
        obs["reason"] = "oracle did not return a synchronous non-empty report result"
    return [obs], {key: (response, key_payload) if obs["ok"] else None}


def frappe_json_handler():
    try:
        from frappe.utils.response import json_handler
        return json_handler
    except Exception:
        return None


def small_frappe_json_body(payload):
    handler = frappe_json_handler()
    try:
        import orjson
        return orjson.dumps(payload, default=handler) if handler else orjson.dumps(payload)
    except Exception:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=handler or str, allow_nan=False).encode("utf-8")


def capture_detail_inprocess(req, oracle, credentials, response_dir, sid):
    """Direct in-process detail capture without serializing/parsing the full source result.

    ERPNext still materializes its own report rows; this optimization removes the additional
    hundreds-of-MB response serialization, JSON parse, and full normalized copy that the
    generic HTTP-shaped path would otherwise make merely to retain two small pages.
    """
    actor = "detail_reader"
    user = credentials[actor]["user"]
    window = req["report_windows"]["year_to_date"]
    filters = {
        "company": req.get("company") or "Khit Me Distribution",
        "from_date": window["from_date"],
        "to_date": window["to_date"],
        "group_by": "Invoice",
    }
    workload = req["manager_workload"]
    page_count = int(workload["detail_page_count"])
    page_length = int(workload["detail_page_length"])
    observations = []
    cache_entries = {}
    page_digests = []
    status = 200
    error_type = ""
    started_at = time.monotonic()
    try:
        frappe = inprocess_connect(req.get("site"))
        frappe.set_user(user)
        frappe.local.form_dict = frappe._dict({
            "report_name": workload.get("oracle_report", "Gross Profit"),
            "filters": json.dumps(filters, separators=(",", ":")),
            "ignore_prepared_report": 1,
        })
        from frappe.desk.query_report import run
        result = run(
            report_name=workload.get("oracle_report", "Gross Profit"),
            filters=json.dumps(filters, separators=(",", ":")),
            ignore_prepared_report=1,
        )
    except Exception as exc:
        result = {"columns": [], "result": []}
        status = 500
        error_type = type(exc).__name__
    duration_ms = round((time.monotonic() - started_at) * 1000, 3)
    handler = frappe_json_handler()
    source_columns = result.get("columns") if isinstance(result, dict) else []
    source_rows = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), list) else []
    try:
        source_sha = canonical_json_streaming_sha256([source_columns or [], source_rows], handler) if status == 200 else ""
    except Exception:
        source_sha = ""
        status = 500
        error_type = error_type or "SourceHashError"

    source_complete = status == 200 and bool(source_sha)
    for page in range(page_count):
        start = page * page_length
        page_rows = source_rows[start:start + page_length] if status == 200 else []
        key_payload = report_key_payload(workload["detail_report"], filters, start, page_length, ignore_prepared_report=1)
        key = cache_key(STOCK_CACHE_KIND, user, key_payload)
        digest = ""
        artifact_name = ""
        body_sha = ""
        page_response = None
        if status == 200 and page_rows:
            digest = canonical_json_streaming_sha256([source_columns or [], page_rows], handler)
            message = dict(result)
            message["result"] = page_rows
            body = small_frappe_json_body({"message": message})
            page_response = HttpResponse(200, "OK", [("Content-Type", "application/json")], body)
            artifact_name, body_sha = save_response_artifact(response_dir, f"detail-page-{page}", page_response)
        page_digests.append(digest)
        ok = bool(page_response is not None and digest and 0 < len(page_rows) <= page_length and source_sha)
        obs = {
            **observation_base(req, actor, user, "manager_detail", "manager_detail", window, key),
            "observation_id": f"oracle/manager_detail/page-{page}",
            "page": page,
            "start": start,
            "page_length": page_length,
            "http_status": status,
            "duration_ms": duration_ms if page == 0 else 0,
            "first_50_duration_ms": duration_ms if page == 0 else None,
            "row_count": len(page_rows),
            "source_row_count": len(source_rows),
            "sha256": digest,
            "first_page_sha256": digest if page == 0 else "",
            "response_sha256": body_sha,
            "response_artifact": artifact_name,
            "oracle_source_result_sha256": source_sha,
            "oracle_source_columns_rows_sha256": source_sha,
            "oracle_source_row_count": len(source_rows) if source_complete else None,
            "oracle_source_complete": source_complete,
            "oracle_source_truncated": False if source_complete else None,
            "oracle_source_materialization": "in-process native result; full source was hashed incrementally and never serialized as one response body",
            "derivation": "exact contiguous row slice of one synchronous unmodified stock oracle detail result; only the requested page slice is serialized",
            "hash_canonicalization": "canonical-json-v1([columns,result]) streaming over native in-process values",
            "key_payload": key_payload,
            "ok": ok,
            "permission_evidence": {"login_ok": True, "report_http_status": status, "expected_permissions": expected_actor_permissions(req, actor)},
        }
        if error_type:
            obs["error_type"] = error_type
        if not ok:
            obs["event"] = "manager_detail_reject"
            obs["reason"] = "oracle detail page missing, empty, over page length, source hash unavailable, or invalid"
        observations.append(obs)
        cache_entries[key] = (page_response, key_payload) if ok else None
    for obs in observations:
        obs["all_pages_sha256"] = canonical_json_sha256(page_digests)
    return observations, cache_entries


def capture_detail(req, oracle, credentials, response_dir):
    actor = "detail_reader"
    credential = credentials[actor]
    user = credential["user"]
    login_response, sid = frappe_login(oracle["base_url"], user, credential["password"], site=req.get("site"))
    if login_response.status != 200 or not sid:
        return [dict(actor=actor, actor_user=user, event="permission_reject", group="manager_detail", ok=False, reason="oracle login failed")], {}
    if inprocess_oracle(oracle["base_url"]):
        return capture_detail_inprocess(req, oracle, credentials, response_dir, sid)
    window = req["report_windows"]["year_to_date"]
    filters = {
        "company": req.get("company") or "Khit Me Distribution",
        "from_date": window["from_date"],
        "to_date": window["to_date"],
        "group_by": "Invoice",
    }
    workload = req["manager_workload"]
    page_count = int(workload["detail_page_count"])
    page_length = int(workload["detail_page_length"])

    # Frappe's query_report.run API does not implement start/page_length pagination for
    # synchronous report execution. Capture the oracle's complete ordered detail result once,
    # then project exact contiguous row slices for the original seasonal page contract.
    started_at = time.monotonic()
    response = frappe_post_method(
        oracle["base_url"],
        QUERY_METHOD,
        {
            "report_name": workload.get("oracle_report", "Gross Profit"),
            "filters": json.dumps(filters, separators=(",", ":")),
            "ignore_prepared_report": 1,
        },
        sid,
        timeout=int(req.get("oracle_timeout_seconds", 300)),
        site=req.get("site"),
    )
    duration_ms = round((time.monotonic() - started_at) * 1000, 3)
    parts = report_parts(response)
    source_rows = parts["rows"] if parts is not None else []
    source_columns = parts["columns"] if parts is not None else []
    source_sha = sha256_bytes(response.body)
    ordered_sha = canonical_json_streaming_sha256([source_columns, source_rows]) if parts else ""
    observations = []
    cache_entries = {}
    page_digests = []
    for page in range(page_count):
        start = page * page_length
        page_rows = source_rows[start:start + page_length] if response.status == 200 and parts is not None else []
        key_payload = report_key_payload(workload["detail_report"], filters, start, page_length, ignore_prepared_report=1)
        key = cache_key(STOCK_CACHE_KIND, user, key_payload)
        digest = canonical_json_sha256([source_columns, page_rows]) if parts is not None and page_rows else ""
        artifact_name = ""
        body_sha = ""
        page_response = None
        if response.status == 200 and parts is not None and page_rows:
            message = dict(parts["message"])
            message["result"] = page_rows
            body = json.dumps({"message": message}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            page_response = HttpResponse(200, "OK", [("Content-Type", "application/json")], body)
            artifact_name, body_sha = save_response_artifact(response_dir, f"detail-page-{page}", page_response)
        page_digests.append(digest)
        ok = bool(page_response is not None and digest and 0 < len(page_rows) <= page_length)
        obs = {
            **observation_base(req, actor, user, "manager_detail", "manager_detail", window, key),
            "observation_id": f"oracle/manager_detail/page-{page}",
            "page": page,
            "start": start,
            "page_length": page_length,
            "http_status": response.status,
            "duration_ms": duration_ms if page == 0 else 0,
            "first_50_duration_ms": duration_ms if page == 0 else None,
            "row_count": len(page_rows),
            "source_row_count": len(source_rows),
            "sha256": digest,
            "first_page_sha256": digest if page == 0 else "",
            "response_sha256": body_sha,
            "response_artifact": artifact_name,
            "oracle_full_response_sha256": source_sha,
            "oracle_source_columns_rows_sha256": ordered_sha,
            "derivation": "exact contiguous row slice of one synchronous oracle detail result; Frappe query_report.run does not paginate start/page_length",
            "hash_canonicalization": "canonical-json-v1([columns,result])",
            "key_payload": key_payload,
            "ok": ok,
            "permission_evidence": {"login_ok": True, "report_http_status": response.status, "expected_permissions": expected_actor_permissions(req, actor)},
        }
        if not ok:
            obs["event"] = "manager_detail_reject"
            obs["reason"] = "oracle detail page missing, empty, over page length, or invalid"
        observations.append(obs)
        cache_entries[key] = (page_response, key_payload) if ok else None
    for obs in observations:
        obs["all_pages_sha256"] = canonical_json_sha256(page_digests)
    return observations, cache_entries


def capture_export(req, oracle, credentials, response_dir, skip_export=False):
    actor = "export_requester"
    user = credentials[actor]["user"]
    if skip_export:
        return [dict(actor=actor, actor_user=user, event="export_not_run", group="export_complete", ok=False, reason="export intentionally skipped for low-impact coordination smoke")], {}
    credential = credentials[actor]
    login_response, sid = frappe_login(oracle["base_url"], user, credential["password"], site=req.get("site"))
    if login_response.status != 200 or not sid:
        return [dict(actor=actor, actor_user=user, event="permission_reject", group="export_complete", ok=False, reason="oracle login failed")], {}
    window = req["report_windows"]["full_year"]
    filters = {
        "company": req.get("company") or "Khit Me Distribution",
        "from_date": window["from_date"],
        "to_date": window["to_date"],
        "group_by": "Invoice",
    }
    workload = req["manager_workload"]
    enqueue_payload = {"filters": canonical_filter_object(filters)}
    enqueue_key = cache_key("export_enqueue", user, enqueue_payload)
    started_at = time.monotonic()
    enqueue = frappe_post_method(
        oracle["base_url"],
        workload["export_enqueue_method"],
        {"filters": json.dumps(filters, separators=(",", ":"))},
        sid,
        timeout=30,
        site=req.get("site"),
    )
    enqueue_ms = round((time.monotonic() - started_at) * 1000, 3)
    export_name = extract_export_name(enqueue)
    enqueue_artifact = ""
    if enqueue.status == 200 and export_name:
        enqueue_artifact, _ = save_response_artifact(response_dir, "export-enqueue", enqueue)
    observations = [{
        **observation_base(req, actor, user, "export_enqueue", "export_complete", window, enqueue_key),
        "observation_id": "oracle/export_enqueue/0",
        "http_status": enqueue.status,
        "duration_ms": enqueue_ms,
        "export_log_name": export_name,
        "sha256": sha256_bytes(enqueue.body),
        "response_artifact": enqueue_artifact,
        "response_sha256": sha256_bytes(enqueue.body),
        "key_payload": enqueue_payload,
        "ok": enqueue.status == 200 and bool(export_name),
        "permission_evidence": {"login_ok": True, "enqueue_http_status": enqueue.status, "expected_permissions": expected_actor_permissions(req, actor)},
    }]
    cache_entries = {enqueue_key: (enqueue, enqueue_payload) if observations[0]["ok"] else None}
    if not observations[0]["ok"]:
        observations[0]["event"] = "export_reject"
        observations[0]["reason"] = "oracle export enqueue absent, forbidden, or returned no export_log_name"
        return observations, cache_entries

    deadline = time.monotonic() + int(req.get("export_timeout_seconds", 120))
    download = None
    status_seen = []
    while time.monotonic() < deadline:
        download = frappe_get_method(
            oracle["base_url"],
            workload["export_download_method"],
            urlencode({"export_log_name": export_name}),
            sid,
            timeout=180,
            site=req.get("site"),
        )
        status_seen.append(download.status)
        if download.status == 200:
            break
        if download.status != 409:
            break
        time.sleep(2)
    total_ms = round((time.monotonic() - started_at) * 1000, 3)
    download_key = cache_key("export_download", user, {"export_log_name": export_name})
    ok = download is not None and download.status == 200 and len(download.body) > 0
    artifact_name = ""
    if ok:
        artifact_name, _ = save_response_artifact(response_dir, "export-download", download)
    row_count = 0
    if ok:
        try:
            text = download.body.decode("utf-8-sig")
            row_count = max(0, sum(1 for row in csv.reader(StringIO(text))) - 1)
        except Exception:
            row_count = 0
    observations.append({
        **observation_base(req, actor, user, "export_complete", "export_complete", window, download_key),
        "observation_id": "oracle/export_complete/0",
        "http_status": download.status if download else 0,
        "duration_ms": total_ms,
        "export_log_name": export_name,
        "row_count": row_count,
        "body_bytes": len(download.body) if download else 0,
        "sha256": sha256_bytes(download.body) if download else "",
        "response_artifact": artifact_name,
        "key_payload": {"export_log_name": export_name},
        "download_statuses": status_seen,
        "hash_canonicalization": "raw-export-response-text-v1",
        "ok": ok and row_count > 0,
        "permission_evidence": {"login_ok": True, "download_http_status": download.status if download else 0, "expected_permissions": expected_actor_permissions(req, actor)},
    })
    cache_entries[download_key] = (download, {"export_log_name": export_name}) if observations[-1]["ok"] else None
    if not observations[-1]["ok"]:
        observations[-1]["event"] = "export_reject"
        observations[-1]["reason"] = "oracle export did not produce a non-empty CSV download"
    return observations, cache_entries


def oracle_report_name(req):
    return req["manager_workload"].get("oracle_report", "Gross Profit")


def cache_index_provenance(req):
    oracle = req["oracle"]
    return {
        "fixture_sha256": req["fixture_sha256"],
        "trial_id": req["trial_id"],
        "site": req["site"],
        "cache_kind": STOCK_CACHE_KIND,
        "oracle_report": oracle_report_name(req),
        "oracle": {
            "erpnext_unmodified": oracle.get("erpnext_unmodified"),
            "erpnext_version": oracle.get("erpnext_version"),
            "snapshot_kind": oracle.get("snapshot_kind"),
            "snapshot_cutoff_date": oracle.get("snapshot_cutoff_date"),
            "source_fixture_sha256": oracle.get("source_fixture_sha256"),
        },
    }


def write_cache_index(cache_entries, artifact_dir, req, verification=None):
    responses = {}
    for key, entry in cache_entries.items():
        if entry is None:
            continue
        response, payload = entry
        digest = sha256_bytes(response.body)
        name = f"cache-{digest[:16]}.bin"
        atomic_write_bytes(artifact_dir / "oracle-responses" / name, response.body)
        responses[key] = {
            "artifact": "oracle-responses/" + name,
            "sha256": digest,
            "status": response.status,
            "headers": [[k, v] for k, v in response.headers if k.lower() not in HOP_BY_HOP_HEADERS],
        }
    index = {
        "schema_version": CACHE_INDEX_SCHEMA_VERSION,
        "created_at": utc_now(),
        "provenance": cache_index_provenance(req),
        "responses": responses,
    }
    if verification:
        index["verification"] = verification
    atomic_write_json(artifact_dir / "oracle-cache-index.json", index)


def capture_oracle(req, skip_export=False):
    artifact_dir = artifact_dir_for(req)
    response_dir = artifact_dir / "oracle-responses"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # Capture evidence is immutable. Never overwrite a prior cache/capture in a shared
    # artifact directory: a failed capture must not destroy a previously qualified cache,
    # and a stock capture must not replace older custom/Fast evidence in place.
    for prior in (artifact_dir / "oracle-cache-index.json", artifact_dir / "oracle-capture.json"):
        if prior.exists():
            raise RequestError(f"artifact directory already contains immutable oracle evidence: {prior}; use a new capture run directory")
    response_dir.mkdir(parents=True, exist_ok=True)
    actors = req["source"].get("actors") or {}
    needed = {"summary_warehouse": actors.get("summary_warehouse", {}), "summary_monthly": actors.get("summary_monthly", {}), "detail_reader": actors.get("detail_reader", {}), "export_requester": actors.get("export_requester", {})}
    credentials = load_credentials(req["oracle"].get("credential_env") or req["source"].get("credential_env"), needed)
    oracle = req["oracle"]
    freshness = assess_freshness(req)
    observations = []
    cache_entries = {}
    verification = None
    if freshness["exact_static_snapshot"]:
        for group, actor in (("Warehouse", "summary_warehouse"), ("Monthly", "summary_monthly")):
            rows, cache = capture_report(req, oracle, credentials, actor, group, req["report_windows"]["year_to_date"], response_dir)
            observations.extend(rows)
            cache_entries.update(cache)
        rows, cache = capture_detail(req, oracle, credentials, response_dir)
        if req["integration"].get("fail_closed_on_cache_miss") and all(row.get("ok") for row in rows):
            verify_dir = artifact_dir / "detail-independent-repeat"
            verify_dir.mkdir()
            repeated, _ = capture_detail(req, oracle, credentials, verify_dir)
            def signature(row):
                return (row.get("cache_key"), row.get("sha256"),
                        row.get("oracle_source_columns_rows_sha256"))
            if (oracle_report_name(req) != "Gross Profit" or not rows
                    or not all(row.get("ok") for row in repeated)
                    or not all(re_full_hash(signature(row)[2]) for row in rows)
                    or list(map(signature, rows)) != list(map(signature, repeated))):
                raise RequestError("independent stock detail capture did not match")
            verification = {
                "schema_version": STOCK_DETAIL_VERIFICATION_SCHEMA,
                "detail_source_report": "Gross Profit",
                "stock_detail_rows_sha256": signature(rows[0])[2],
                "detail_stock_double_execution_ordered_hash_equal": True,
                "snapshot_id": req["fixture_sha256"],
                "detail_cache_keys": {str(row["page"]): {"cache_key": row["cache_key"],
                    "response_sha256": row["response_sha256"]} for row in rows},
            }
        observations.extend(rows)
        cache_entries.update(cache)
        rows, cache = capture_export(req, oracle, credentials, response_dir, skip_export=skip_export)
        observations.extend(rows)
        cache_entries.update(cache)
    else:
        observations.append({
            "event": "freshness_reject",
            "ok": False,
            "reason": freshness["reason"],
            "group": "freshness",
        })
    write_cache_index(cache_entries, artifact_dir, req, verification)
    capture = {
        "schema_version": "manager-oracle-capture-v1",
        "captured_at": utc_now(),
        "trial_id": req["trial_id"],
        "fixture_sha256": req["fixture_sha256"],
        "seed": req["seed"],
        "site": req["site"],
        "oracle": {key: value for key, value in oracle.items() if key != "credential_env"},
        "freshness": freshness,
        "observations": observations,
        "coverage": {
            "Warehouse": any(row.get("event") == "manager_summary" and row.get("group") == "Warehouse" and row.get("ok") for row in observations),
            "Monthly": any(row.get("event") == "manager_summary" and row.get("group") == "Monthly" and row.get("ok") for row in observations),
            "manager_detail": sum(1 for row in observations if row.get("event") == "manager_detail" and row.get("ok")) == int(req["manager_workload"]["detail_page_count"]),
            "export_complete": any(row.get("event") == "export_complete" and row.get("ok") for row in observations),
        },
        "secrets_included": False,
    }
    capture["exact oracle coverage"] = all(capture["coverage"].values())
    atomic_write_json(artifact_dir / "oracle-capture.json", capture)
    return capture


def parse_form_request(body, content_type):
    if "application/json" in (content_type or ""):
        data = json.loads(body.decode("utf-8") or "{}")
        return {
            key: json.dumps(value, separators=(",", ":"), ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            for key, value in data.items()
        }
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def request_cache_key(kind, user, method, fields, query):
    if kind == "query_report":
        filters = canonical_filter_object(fields.get("filters") or "{}")
        payload = report_key_payload(
            fields.get("report_name") or "",
            filters,
            int(fields["start"]) if fields.get("start") not in (None, "") else None,
            int(fields["page_length"]) if fields.get("page_length") not in (None, "") else None,
            int(fields.get("ignore_prepared_report") or 0),
        )
    elif kind == "export_enqueue":
        payload = {"filters": canonical_filter_object(fields.get("filters") or "{}")}
    elif kind == "export_download":
        payload = {"export_log_name": query.get("export_log_name", [""])[-1]}
    else:
        payload = {"method": method, "fields": fields, "query": query}
    return cache_key(STOCK_CACHE_KIND if kind == "query_report" else kind, user, payload), payload


def sliced_query_response(response, start, page_length):
    parts = report_parts(response)
    if parts is None:
        return response
    start = max(0, int(start))
    page_length = max(1, int(page_length))
    rows = parts["rows"][start:start + page_length]
    message = dict(parts["message"])
    message["result"] = rows
    body = json.dumps({"message": message}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return HttpResponse(200, "OK", [("Content-Type", "application/json")], body)


def response_event_for(kind, response):
    event = {"sha256": "", "row_count": 0}
    if kind == "query_report":
        digest, rows = report_digest(response)
        event.update({"sha256": digest, "row_count": rows, "hash_canonicalization": "canonical-json-v1([columns,result])"})
    elif kind in ("export_enqueue", "export_download"):
        event.update({"sha256": sha256_bytes(response.body), "body_bytes": len(response.body), "hash_canonicalization": "raw-export-response-text-v1" if kind == "export_download" else "raw-json-response-v1"})
    else:
        event.update({"sha256": sha256_bytes(response.body), "body_bytes": len(response.body)})
    return event


class Metrics:
    def __init__(self, artifact_dir):
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.artifact_dir = artifact_dir
        self.started_at = utc_now()
        self.total_requests = 0
        self.completed_requests = 0
        self.active_requests = 0
        self.max_active_requests = 0
        self.errors = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.freshness_rejects = 0
        self.endpoint_latency_ms = {}

    def begin(self):
        with self.lock:
            self.total_requests += 1
            self.active_requests += 1
            self.max_active_requests = max(self.max_active_requests, self.active_requests)
            active = self.active_requests
        self.write()
        return active

    def finish(self, endpoint, duration_ms, cache_state, error=False, freshness_reject=False):
        with self.lock:
            self.active_requests = max(0, self.active_requests - 1)
            self.completed_requests += 1
            self.errors += 1 if error else 0
            self.cache_hits += 1 if cache_state == "hit" else 0
            self.cache_misses += 1 if cache_state == "miss" else 0
            self.freshness_rejects += 1 if freshness_reject else 0
            self.endpoint_latency_ms.setdefault(endpoint, []).append(round(duration_ms, 3))
        self.write()

    def snapshot(self):
        with self.lock:
            latencies = {
                key: {
                    "count": len(values),
                    "max_ms": max(values),
                    "p95_ms": sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)] if values else None,
                }
                for key, values in self.endpoint_latency_ms.items()
            }
            return {
                "schema_version": "manager-service-metrics-v1",
                "started_at": self.started_at,
                "updated_at": utc_now(),
                "total_requests": self.total_requests,
                "completed_requests": self.completed_requests,
                "active_requests": self.active_requests,
                "max_active_requests": self.max_active_requests,
                "errors": self.errors,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "freshness_rejects": self.freshness_rejects,
                "endpoint_latency": latencies,
            }

    def write(self):
        snapshot = self.snapshot()
        with self.write_lock:
            atomic_write_json(self.artifact_dir / "service-metrics.json", snapshot)


class EventLog:
    def __init__(self, artifact_dir):
        self.path = artifact_dir / "external-events.jsonl"
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def write(self, event):
        event = dict(event)
        event.setdefault("ts", utc_now())
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json_dumps(event) + "\n")


def cache_index_rejection_reason(index, req):
    if not isinstance(index, dict):
        return "cache index is not an object"
    if index.get("schema_version") != CACHE_INDEX_SCHEMA_VERSION:
        return "cache index is missing the versioned stock-oracle schema; legacy/custom flat indexes are not eligible"
    provenance = index.get("provenance")
    if not isinstance(provenance, dict):
        return "cache index has no provenance"
    expected = cache_index_provenance(req)
    if provenance != expected:
        return "cache index provenance does not match the pinned fixture, oracle version/report, snapshot and stock cache contract"
    if not isinstance(index.get("responses"), dict):
        return "cache index responses are missing or invalid"
    return ""


def detail_static_cache_verified(index):
    """Return true only when detail cache bytes are backed by a double-run stock ordering proof.

    A direct stock capture is enough for small summary responses, but ordered Invoice detail is
    tie-sensitive. Detail replay therefore requires an explicit verification object proving that
    two genuine unmodified-stock executions produced the same full ordered rows hash and that the
    rejected candidate hash is different.
    """
    verification = (index or {}).get("verification") or {}
    stock_hash = verification.get("stock_detail_rows_sha256")
    candidate_hash = verification.get("candidate_detail_rows_sha256")
    schema = verification.get("schema_version")
    basic_proof = bool(
        schema in (STATIC_CACHE_VERIFICATION_SCHEMA, STOCK_DETAIL_VERIFICATION_SCHEMA, HYBRID_SERVING_VERIFICATION_SCHEMA)
        and verification.get("detail_source_report") == "Gross Profit"
        and verification.get("detail_stock_double_execution_ordered_hash_equal") is True
        and re_full_hash(stock_hash)
        and (schema == STOCK_DETAIL_VERIFICATION_SCHEMA or verification.get("candidate_detail_rejected") is True)
        and (schema == STOCK_DETAIL_VERIFICATION_SCHEMA or re_full_hash(candidate_hash))
        and (schema == STOCK_DETAIL_VERIFICATION_SCHEMA or stock_hash != candidate_hash)
        and verification.get("snapshot_id")
    )
    if not basic_proof:
        return False
    detail_bindings = verification.get("detail_cache_keys") or {}
    if schema in (STOCK_DETAIL_VERIFICATION_SCHEMA, HYBRID_SERVING_VERIFICATION_SCHEMA) and not detail_bindings:
        return False
    responses = (index or {}).get("responses") or {}
    for binding in detail_bindings.values():
        key = (binding or {}).get("cache_key")
        digest = (binding or {}).get("response_sha256")
        if not key or not re_full_hash(digest) or (responses.get(key) or {}).get("sha256") != digest:
            return False
    return True


def hybrid_serving_index(index):
    return ((index or {}).get("verification") or {}).get("schema_version") == HYBRID_SERVING_VERIFICATION_SCHEMA


def candidate_summary_cache_verified(index):
    """Return true when cached summary bytes are proven exact candidate-summary output.

    The proof must cover both eligible summary groups, include the pinned candidate commit,
    show exact stock/candidate ordered hashes, and show that the stock oracle itself was
    verified by two independent executions. Detail remains governed separately by
    detail_static_cache_verified().
    """
    verification = (index or {}).get("verification") or {}
    if verification.get("schema_version") != HYBRID_SERVING_VERIFICATION_SCHEMA:
        return False
    commit = str(verification.get("candidate_commit") or "")
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        return False
    if verification.get("summary_source_report") != "Fast Gross Profit":
        return False
    qualification = verification.get("summary_qualification") or {}
    summary_bindings = verification.get("summary_cache_keys") or {}
    responses = (index or {}).get("responses") or {}
    for group in SUMMARY_GROUPS:
        binding = summary_bindings.get(group) or {}
        key = binding.get("cache_key")
        digest = binding.get("response_sha256")
        if not key or not re_full_hash(digest) or (responses.get(key) or {}).get("sha256") != digest:
            return False
        row = qualification.get(group) or {}
        stock_hash = row.get("stock_rows_sha256")
        candidate_hash = row.get("candidate_rows_sha256")
        if not (
            row.get("exact_ordered_candidate_equal") is True
            and row.get("stock_double_execution_verified") is True
            and re_full_hash(stock_hash)
            and stock_hash == candidate_hash
        ):
            return False
    return bool(verification.get("snapshot_id"))


def read_validated_cache_index(artifact_dir, req):
    index_path = artifact_dir / "oracle-cache-index.json"
    if not index_path.is_file():
        return {}, "missing"
    try:
        index = load_json(index_path)
    except Exception:
        return {}, "cache index is not valid JSON"
    reason = cache_index_rejection_reason(index, req)
    if reason:
        return {}, reason
    return index["responses"], ""


def load_preseed_cache(artifact_dir, req):
    entries, rejection = read_validated_cache_index(artifact_dir, req)
    cache = {}
    if rejection:
        return cache
    for key, meta in entries.items():
        if not isinstance(meta, dict):
            continue
        artifact = meta.get("artifact")
        expected_sha = meta.get("sha256")
        if not isinstance(artifact, str) or not artifact.startswith("oracle-responses/") or ".." in Path(artifact).parts:
            continue
        if not isinstance(expected_sha, str) or not re_full_hash(expected_sha):
            continue
        path = artifact_dir / artifact
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected_sha:
            continue
        try:
            status = int(meta["status"])
            headers = [tuple(row) for row in meta.get("headers", [])]
        except Exception:
            continue
        cache[key] = HttpResponse(status, "OK", headers, path.read_bytes())
    return cache


def freshness_allows_request(req, filters):
    freshness = assess_freshness(req)
    if not freshness["exact_static_snapshot"]:
        return False, freshness["reason"]
    cutoff = parse_iso_date(req["oracle"]["snapshot_cutoff_date"], "oracle.snapshot_cutoff_date")
    try:
        to_date = parse_iso_date(filters.get("to_date"), "filters.to_date")
    except RequestError:
        return False, "manager request has no valid filters.to_date"
    if to_date > cutoff:
        return False, "manager request extends beyond the verified oracle snapshot cutoff"
    return True, ""


class OffloadServerState:
    def __init__(self, req):
        self.req = req
        self.artifact_dir = artifact_dir_for(req)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        _entries, self.cache_index_rejection = read_validated_cache_index(self.artifact_dir, req)
        self.cache_index = {}
        if not self.cache_index_rejection:
            try:
                self.cache_index = load_json(self.artifact_dir / "oracle-cache-index.json")
            except Exception:
                self.cache_index = {}
        self.detail_static_cache_verified = detail_static_cache_verified(self.cache_index)
        self.hybrid_serving_index = hybrid_serving_index(self.cache_index)
        self.candidate_summary_cache_verified = candidate_summary_cache_verified(self.cache_index)
        self.cache = load_preseed_cache(self.artifact_dir, req)
        self.cache_lock = threading.Lock()
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.metrics = Metrics(self.artifact_dir)
        self.events = EventLog(self.artifact_dir)
        self.manager_users = {
            meta.get("user", "").casefold()
            for meta in (req["source"].get("actors") or {}).values()
            if meta.get("user")
        }

    def session_user(self, cookie_header):
        sid = cookie_sid(cookie_header)
        if not sid:
            return "", ""
        with self.sessions_lock:
            row = self.sessions.get(sid, {})
        return sid, row.get("user", "")

    def remember_login(self, response, user, upstream):
        sid = parse_set_sid(response.headers)
        if sid:
            with self.sessions_lock:
                self.sessions[sid] = {"user": user, "upstream": upstream}
        return sid

    def cached(self, key):
        with self.cache_lock:
            return self.cache.get(key)

    def store_cache(self, key, response):
        with self.cache_lock:
            self.cache[key] = HttpResponse(response.status, response.reason, list(response.headers), response.body)


def make_handler(state):
    class OffloadHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "KhitmeeManagerOffload/1.0"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            self.handle_any()

        def do_POST(self):
            self.handle_any()

        def read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            return self.rfile.read(length) if length else b""

        def send_response_bytes(self, response, cache_state="bypass", route_policy=""):
            body = response.body
            self.send_response(response.status, response.reason)
            sent_content_type = False
            for key, value in response.headers:
                lower = key.lower()
                if lower in HOP_BY_HOP_HEADERS or lower in ("set-cookie",):
                    if lower == "set-cookie":
                        self.send_header(key, value)
                    continue
                if lower == "content-type":
                    sent_content_type = True
                self.send_header(key, value)
            if not sent_content_type:
                self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Khitmee-Offload-Cache", cache_state)
            self.send_header("X-Khitmee-Offload-Mode", state.req["integration"]["mode"])
            if route_policy:
                self.send_header("X-Khitmee-Offload-Route", route_policy)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def reject(self, status, reason, event_kind, endpoint, started, active, freshness_reject=False):
            payload = json.dumps({"error": reason, "manager_offload_reject": True}, separators=(",", ":")).encode("utf-8")
            response = HttpResponse(status, "Rejected", [("Content-Type", "application/json")], payload)
            duration = (time.monotonic() - started) * 1000
            self.send_response_bytes(response, "reject")
            state.events.write({
                "event": event_kind,
                "endpoint": endpoint,
                "status": status,
                "reason": reason,
                "cache": "reject",
                "duration_ms": round(duration, 3),
                "active_on_arrival": active,
                "freshness_reject": freshness_reject,
            })
            state.metrics.finish(endpoint, duration, "reject", error=True, freshness_reject=freshness_reject)

        def upstream_headers(self, include_body_headers=True):
            headers = {}
            for name in ("Accept", "Content-Type", "Cookie", "Host", "X-Frappe-CSRF-Token", "X-Frappe-Site-Name"):
                value = self.headers.get(name)
                if value and (include_body_headers or name not in ("Content-Type",)):
                    headers[name] = value
            if state.req.get("site"):
                headers["X-Frappe-Site-Name"] = state.req["site"]
            return headers

        def upstream_for_login(self, user):
            if state.req["integration"]["mode"] == "split-manager-url":
                return state.req["oracle"]["base_url"], "oracle"
            if user.casefold() in state.manager_users:
                return state.req["oracle"]["base_url"], "oracle"
            return state.req["source"]["base_url"], "source"

        def upstream_for_session_or_manager(self):
            _sid, user = state.session_user(self.headers.get("Cookie", ""))
            if user.casefold() in state.manager_users or state.req["integration"]["mode"] == "split-manager-url":
                return state.req["oracle"]["base_url"], "oracle"
            return state.req["source"]["base_url"], "source"

        def proxy_auth_bootstrap(self, started, active):
            if state.req["integration"]["mode"] == "split-manager-url":
                _sid, user = state.session_user(self.headers.get("Cookie", ""))
                if not user:
                    self.reject(403, "manager auth bootstrap requires an authenticated manager session", "auth_bootstrap_reject", "auth_bootstrap", started, active)
                    return
                if user.casefold() not in state.manager_users:
                    self.reject(403, "auth bootstrap principal is not a configured manager actor", "auth_bootstrap_reject", "auth_bootstrap", started, active)
                    return
            upstream, upstream_kind = self.upstream_for_session_or_manager()
            headers = self.upstream_headers(include_body_headers=False)
            headers.setdefault("Accept", self.headers.get("Accept", "text/html,application/xhtml+xml"))
            response = http_request(upstream, self.command, self.path, b"", headers, timeout=30)
            duration = (time.monotonic() - started) * 1000
            self.send_response_bytes(response, "bypass")
            state.events.write({
                "event": "auth_bootstrap",
                "endpoint": "auth_bootstrap",
                "path": self.path.split("?", 1)[0],
                "upstream": upstream_kind,
                "status": response.status,
                "duration_ms": round(duration, 3),
                "active_on_arrival": active,
            })
            state.metrics.finish("auth_bootstrap", duration, "bypass", error=response.status >= 500)

        def handle_login(self, body, started, active):
            try:
                fields = parse_form_request(body, self.headers.get("Content-Type", ""))
            except Exception:
                self.reject(400, "login form could not be parsed", "login_reject", "login", started, active)
                return
            user = fields.get("usr") or fields.get("user") or ""
            upstream, upstream_kind = self.upstream_for_login(user)
            login_headers = self.upstream_headers()
            login_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            login_headers.setdefault("Accept", "application/json")
            response = http_request(
                upstream,
                "POST",
                "/api/method/login",
                body,
                login_headers,
                timeout=30,
            )
            if response.status == 200:
                state.remember_login(response, user, upstream_kind)
            duration = (time.monotonic() - started) * 1000
            self.send_response_bytes(response, "bypass")
            state.events.write({
                "event": "login",
                "endpoint": "login",
                "user": user,
                "upstream": upstream_kind,
                "status": response.status,
                "duration_ms": round(duration, 3),
                "active_on_arrival": active,
            })
            state.metrics.finish("login", duration, "bypass", error=response.status >= 400)

        def proxy_nonmanager(self, body, started, active):
            headers = self.upstream_headers()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            response = http_request(state.req["source"]["base_url"], self.command, self.path, body, headers, timeout=120)
            duration = (time.monotonic() - started) * 1000
            self.send_response_bytes(response, "bypass")
            state.events.write({
                "event": "proxied_nonmanager",
                "endpoint": "nonmanager",
                "path": self.path.split("?", 1)[0],
                "status": response.status,
                "duration_ms": round(duration, 3),
                "active_on_arrival": active,
            })
            state.metrics.finish("nonmanager", duration, "bypass", error=response.status >= 500)

        def handle_manager(self, body, started, active):
            parsed = urlparse(self.path)
            method = parsed.path[len("/api/method/"):]
            workload = state.req["manager_workload"]
            if method == QUERY_METHOD:
                kind = "query_report"
            elif method == workload["export_enqueue_method"]:
                kind = "export_enqueue"
            elif method == workload["export_download_method"]:
                kind = "export_download"
            else:
                self.reject(404, "not a configured seasonal manager method", "manager_method_reject", "manager", started, active)
                return
            try:
                fields = parse_form_request(body, self.headers.get("Content-Type", "")) if self.command == "POST" else {}
                query = parse_qs(parsed.query, keep_blank_values=True)
                sid, user = state.session_user(self.headers.get("Cookie", ""))
                key, key_payload = request_cache_key(kind, user, method, fields, query)
            except Exception as exc:
                self.reject(400, f"manager request could not be canonically interpreted: {exc}", "manager_request_reject", kind, started, active)
                return

            if not user or user.casefold() not in state.manager_users:
                self.reject(403, "manager login required", "session_reject", kind, started, active)
                return
            identity = http_request(state.req["oracle"]["base_url"], "GET",
                "/api/method/frappe.auth.get_logged_user", headers={
                    "Cookie": "sid=" + sid, "X-Frappe-Site-Name": state.req["site"],
                    "Accept": "application/json"}, timeout=30)
            try:
                payload = json.loads(identity.body) if identity.status == 200 else None
            except (ValueError, UnicodeError):
                payload = None
            if not isinstance(payload, dict) or payload.get("message") != user:
                self.reject(403, "oracle session is no longer valid", "session_reject", kind, started, active)
                return

            filters = {}
            try:
                if fields.get("filters"):
                    filters = canonical_filter_object(fields["filters"])
            except Exception:
                filters = {}
            allowed, freshness_reason = (True, "")
            if kind in ("query_report", "export_enqueue"):
                allowed, freshness_reason = freshness_allows_request(state.req, filters)
            if not allowed:
                self.reject(409, freshness_reason, "freshness_reject", kind, started, active, freshness_reject=True)
                return

            group_by = filters.get("group_by")
            detail_query = kind == "query_report" and group_by == "Invoice"
            summary_query = kind == "query_report" and group_by in SUMMARY_GROUPS
            route_policy = "configured-method"
            if kind == "query_report":
                if detail_query:
                    route_policy = "stock-detail-static-cache" if state.detail_static_cache_verified else "stock-detail-live-oracle"
                elif summary_query:
                    route_policy = "candidate-summary-static-cache" if state.candidate_summary_cache_verified else "stock-summary-static-cache"
                else:
                    self.reject(409, "query report route is not an eligible Warehouse/Monthly summary or ordered Invoice detail route", "route_policy_reject", kind, started, active)
                    return

            detail_cache_allowed = not detail_query or state.detail_static_cache_verified
            summary_cache_allowed = not summary_query or not state.hybrid_serving_index or state.candidate_summary_cache_verified
            cached = state.cached(key) if detail_cache_allowed and summary_cache_allowed else None
            if cached is not None:
                response = cached
                cache_state = "hit"
            elif not state.req["integration"].get("fail_closed_on_cache_miss"):
                if detail_query:
                    route_policy = "stock-detail-live-oracle"
                elif summary_query:
                    route_policy = "stock-summary-live-oracle"
                headers = self.upstream_headers()
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                headers.setdefault("Accept", self.headers.get("Accept", "*/*"))
                upstream_body = body
                upstream_path = self.path
                paginated_query = False
                if kind == "query_report" and self.command == "POST":
                    upstream_fields = dict(fields)
                    # Never invoke the rejected Fast detail implementation on the oracle. Manager
                    # workload names are external aliases; the only detail backend permitted by
                    # policy is the unmodified stock report.
                    upstream_fields["report_name"] = oracle_report_name(state.req)
                    if fields.get("start") not in (None, "") and fields.get("page_length") not in (None, ""):
                        paginated_query = True
                        upstream_fields.pop("start", None)
                        upstream_fields.pop("page_length", None)
                    upstream_body = form_body(upstream_fields)
                    upstream_path = parsed.path
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                response = http_request(
                    state.req["oracle"]["base_url"],
                    self.command,
                    upstream_path,
                    upstream_body,
                    headers,
                    timeout=300,
                )
                cache_state = "miss"
                cacheable = False
                if kind == "query_report" and response.status == 200 and report_parts(response) is not None:
                    cacheable = not detail_query and not state.hybrid_serving_index
                    if paginated_query:
                        response = sliced_query_response(response, fields["start"], fields["page_length"])
                elif kind == "export_enqueue" and response.status == 200 and extract_export_name(response):
                    cacheable = True
                elif kind == "export_download" and response.status == 200 and response.body:
                    cacheable = True
                if cacheable:
                    state.store_cache(key, response)
            else:
                self.reject(409, "no exact cached oracle response for this principal, window, and pagination key", "cache_miss_reject", kind, started, active)
                return
            duration = (time.monotonic() - started) * 1000
            self.send_response_bytes(response, cache_state, route_policy)
            event = {
                "event": "external_response",
                "endpoint": kind,
                "cache_key": key,
                "key_payload": key_payload,
                "user": user,
                "status": response.status,
                "cache": cache_state,
                "duration_ms": round(duration, 3),
                "active_on_arrival": active,
                "freshness": "exact-static-window",
            }
            event.update(response_event_for(kind, response))
            state.events.write(event)
            state.metrics.finish(kind, duration, cache_state, error=response.status >= 500)

        def handle_health(self, started, active):
            payload = json.dumps({
                "schema_version": "manager-service-health-v1",
                "ok": True,
                "preseed_cache_entries": len(state.cache),
                "cache_index_rejection": state.cache_index_rejection,
                "cache_contract": {"schema_version": CACHE_INDEX_SCHEMA_VERSION, "kind": STOCK_CACHE_KIND},
                "route_policy": {
                    "summaries": "exact qualified candidate Warehouse/Monthly cache when hybrid proof is present; otherwise stock-oracle summary cache only",
                    "detail": "independently verified native stock Invoice pages; strict cache misses return 409; live stock requires miss forwarding enabled",
                    "hybrid_serving_index": state.hybrid_serving_index,
                    "detail_static_cache_verified": state.detail_static_cache_verified,
                    "candidate_summary_cache_verified": state.candidate_summary_cache_verified,
                    "rejected_fast_detail_serving": True,
                },
                "freshness": assess_freshness(state.req),
            }, separators=(",", ":")).encode("utf-8")
            response = HttpResponse(200, "OK", [("Content-Type", "application/json")], payload)
            duration = (time.monotonic() - started) * 1000
            self.send_response_bytes(response, "local")
            state.metrics.finish("health", duration, "local", error=False)

        def handle_any(self):
            started = time.monotonic()
            active = state.metrics.begin()
            body = self.read_body()
            endpoint = "nonmanager"
            try:
                if self.command == "GET" and self.path.split("?", 1)[0] == "/__manager_offload_health":
                    self.handle_health(started, active)
                    return
                if self.path.split("?", 1)[0] == "/api/method/login":
                    endpoint = "login"
                    self.handle_login(body, started, active)
                    return
                if self.command == "GET" and self.path.split("?", 1)[0].rstrip("/") in ("/app", "/desk"):
                    endpoint = "auth_bootstrap"
                    self.proxy_auth_bootstrap(started, active)
                    return
                if self.path.startswith("/api/method/"):
                    parsed_method = self.path.split("?", 1)[0][len("/api/method/"):]
                    manager_methods = {
                        QUERY_METHOD: "query_report",
                        state.req["manager_workload"]["export_enqueue_method"]: "export_enqueue",
                        state.req["manager_workload"]["export_download_method"]: "export_download",
                    }
                    if parsed_method in manager_methods:
                        endpoint = manager_methods[parsed_method]
                        self.handle_manager(body, started, active)
                        return
                if state.req["integration"]["mode"] == "reverse-proxy-shim":
                    self.proxy_nonmanager(body, started, active)
                else:
                    self.reject(404, "split-manager-url mode only serves configured manager endpoints", "nonmanager_reject", "nonmanager", started, active)
            except (OSError, http.client.HTTPException, TimeoutError) as exc:
                self.reject(502, f"upstream connection failed: {type(exc).__name__}", "upstream_error", endpoint, started, active)

    return OffloadHandler


def serve(req):
    artifact_dir = artifact_dir_for(req)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    state = OffloadServerState(req)
    host = state.req["integration"]["listen_host"]
    port = int(state.req["integration"]["listen_port"])
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    atomic_write_json(artifact_dir / "service-ready.json", {
        "schema_version": "manager-service-ready-v1",
        "started_at": utc_now(),
        "pid": os.getpid(),
        "listen": f"http://{host}:{port}",
        "mode": state.req["integration"]["mode"],
        "preseed_cache_entries": len(state.cache),
        "cache_index_rejection": state.cache_index_rejection,
        "cache_contract": {"schema_version": CACHE_INDEX_SCHEMA_VERSION, "kind": STOCK_CACHE_KIND},
        "freshness": assess_freshness(req),
        "shutdown_command": f"terminate pid {os.getpid()} after the coordinated trial window",
    })
    def terminate(signum, _frame):
        raise SystemExit(128 + int(signum))

    previous_handlers = {}
    for signum in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if signum is not None:
            previous_handlers[signum] = signal.signal(signum, terminate)
    exit_code = 0
    try:
        try:
            server.serve_forever(poll_interval=0.2)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
    finally:
        state.metrics.write()
        atomic_write_json(artifact_dir / "service-stopped.json", {
            "schema_version": "manager-service-stopped-v1",
            "stopped_at": utc_now(),
            "pid": os.getpid(),
            "exit_code": exit_code,
            "metrics": state.metrics.snapshot(),
        })
        server.server_close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    if exit_code:
        raise SystemExit(exit_code)


def load_events(path):
    events = []
    if not path.is_file():
        return events
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    return events


def verify(req):
    artifact_dir = artifact_dir_for(req)
    capture_path = artifact_dir / "oracle-capture.json"
    if not capture_path.is_file():
        raise RequestError("oracle-capture.json is missing; run capture-oracle first")
    capture = load_json(capture_path)
    events = load_events(artifact_dir / "external-events.jsonl")
    metrics_path = artifact_dir / "service-metrics.json"
    metrics = load_json(metrics_path) if metrics_path.is_file() else {}
    external_by_key = {}
    failures = []
    export_endpoints = {"export_enqueue", "export_download"}
    manager_endpoints = export_endpoints | {"query_report", "manager", "upstream"}
    for event in events:
        endpoint = event.get("endpoint")
        status = event.get("status")
        if endpoint == "export_download" and event.get("event") == "external_response" and status == 409:
            # A pending poll is followed by a separately required successful download.
            continue
        if endpoint in manager_endpoints and (status != 200 or event.get("cache") == "reject"):
            failures.append({"event": event.get("event"), "endpoint": endpoint, "status": status})
        if event.get("event") == "external_response" and event.get("cache_key"):
            external_by_key.setdefault(event["cache_key"], []).append(event)
    comparisons = []
    covered = set()
    for obs in capture.get("observations", []):
        if not obs.get("ok") or not obs.get("cache_key"):
            continue
        key = obs["cache_key"]
        rows = external_by_key.get(key, [])
        matched = bool(rows)
        exact = matched and bool(obs.get("sha256")) and all(
            row.get("status") == 200 and row.get("sha256") == obs["sha256"] for row in rows
        )
        group = obs.get("group")
        if exact:
            covered.add("manager_detail" if group == "manager_detail" else group)
        comparisons.append({
            "observation_id": obs.get("observation_id"),
            "group": group,
            "cache_key": key,
            "oracle_sha256": obs.get("sha256"),
            "external_observations": len(rows),
            "external_sha256_values": sorted({row.get("sha256", "") for row in rows}),
            "cache_states": sorted({row.get("cache", "") for row in rows}),
            "exact": exact,
            "status": "exact" if exact else ("missing" if not matched else "mismatch"),
        })
    detail_needed = int(req["manager_workload"]["detail_page_count"])
    detail_exact = sum(1 for row in comparisons if row["group"] == "manager_detail" and row["exact"])
    export_in_timed_load = bool(req["manager_workload"].get("export_in_timed_load", True))
    required = {"Warehouse", "Monthly"}
    if export_in_timed_load:
        required.add("export_complete")
    coverage_ok = required <= covered and detail_exact == detail_needed
    timed = [row for row in comparisons if export_in_timed_load or row["group"] != "export_complete"]
    all_exact = bool(timed) and all(row["exact"] for row in timed)
    unsupported = []
    for obs in capture.get("observations", []):
        if not obs.get("ok"):
            unsupported.append({"event": obs.get("event"), "group": obs.get("group"), "reason": obs.get("reason", "oracle observation not OK")})
    export_events = {"export_enqueue", "export_complete", "export_reject", "export_not_run"}
    timed_unsupported = [row for row in unsupported if export_in_timed_load or row.get("event") not in export_events and row.get("group") != "export_complete"]
    export_unsupported = [row for row in unsupported if row.get("event") in export_events or row.get("group") == "export_complete"]
    export_rows = [row for row in comparisons if row["group"] == "export_complete"]
    export_exact = bool(export_rows) and all(row["exact"] for row in export_rows)
    timed_failures = [row for row in failures if export_in_timed_load or row["endpoint"] not in export_endpoints]
    export_capability_passed = export_exact and not export_unsupported and not any(
        row["endpoint"] in export_endpoints for row in failures
    )
    measured_passed = coverage_ok and all_exact and not timed_unsupported and not timed_failures
    verdict = {
        "schema_version": "manager-parity-verdict-v1",
        "generated_at": utc_now(),
        "trial_id": req["trial_id"],
        "passed": measured_passed and (export_capability_passed if export_in_timed_load else True),
        "measured_scope": "summaries-and-two-page-detail" if not export_in_timed_load else "summaries-detail-and-export",
        "measured_scope_passed": measured_passed,
        "export_in_timed_load": export_in_timed_load,
        "export_capability_passed": export_capability_passed,
        "coverage_ok": coverage_ok,
        "all_oracle_observations_exact": bool(comparisons) and all(row["exact"] for row in comparisons),
        "all_timed_observations_exact": all_exact,
        "detail_pages_exact": detail_exact,
        "detail_pages_required": detail_needed,
        "covered_groups": sorted(covered),
        "comparisons": comparisons,
        "parity_rejects": unsupported,
        "timed_parity_rejects": timed_unsupported,
        "manager_request_failures": failures,
        "timed_request_failures": timed_failures,
        "export_parity_rejects": export_unsupported,
        "freshness": capture.get("freshness"),
        "metrics": {
            "max_active_requests": metrics.get("max_active_requests"),
            "completed_requests": metrics.get("completed_requests"),
            "cache_hits": metrics.get("cache_hits"),
            "cache_misses": metrics.get("cache_misses"),
            "freshness_rejects": metrics.get("freshness_rejects"),
            "endpoint_latency": metrics.get("endpoint_latency"),
        },
        "evidence_boundary": "Exact means byte-identical verified response content for the fixed actor, window and oracle snapshot. It does not promote additive financial estimates to ERPNext parity.",
    }
    export_capability_path = artifact_dir / "export-capability.json"
    if export_capability_path.is_file():
        verdict["export_capability_artifact"] = "export-capability.json"
    atomic_write_json(artifact_dir / "parity-verdict.json", verdict)
    return verdict


def sync_status(req):
    artifact_dir = artifact_dir_for(req)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    assessment = read_model_assessment(req)
    atomic_write_json(artifact_dir / "freshness-verdict.json", assessment)
    return assessment


def validate_command(req):
    artifact_dir = artifact_dir_for(req)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    freshness = assess_freshness(req)
    result = {
        "schema_version": "manager-request-validation-v1",
        "validated_at": utc_now(),
        "valid": True,
        "trial_id": req["trial_id"],
        "artifact_dir": str(artifact_dir),
        "freshness": freshness,
        "warnings": [],
    }
    if req["integration"]["mode"] == "reverse-proxy-shim":
        result["warnings"].append("reverse-proxy-shim changes the cashier network path; owner acceptance is required and recorded in the request")
    if not freshness["exact_static_snapshot"]:
        result["warnings"].append("freshness decision is parity-reject; service will fail closed for dynamic financial windows")
    atomic_write_json(REPO_ROOT / "perf-suite/lab/offload-followup/manager/outbox/request-validation.json", result)
    atomic_write_json(artifact_dir / "request-validation.json", result)
    return result


def command_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-request", "capture-oracle", "serve", "verify", "sync-status"))
    parser.add_argument("--request", required=True, help="trial request JSON")
    parser.add_argument("--skip-export", action="store_true", help="capture summaries/detail but do not trigger the potentially expensive full-year export")
    args = parser.parse_args(argv)
    req = validate_trial_request(load_json(args.request))
    if args.command == "validate-request":
        result = validate_command(req)
    elif args.command == "capture-oracle":
        result = capture_oracle(req, skip_export=args.skip_export)
    elif args.command == "serve":
        serve(req)
        return 0
    elif args.command == "verify":
        result = verify(req)
    else:
        result = sync_status(req)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 1 if args.command == "verify" and not result["passed"] else 0


def main():
    try:
        return command_main()
    except (RequestError, OSError, json.JSONDecodeError, http.client.HTTPException) as exc:
        print(f"manager_offload failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
