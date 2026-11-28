#!/usr/bin/env python3
"""Dev-only independent ordered stock-oracle verifier.

Runs inside a disposable bench/oracle container. It re-executes unmodified ERPNext
Gross Profit as the pinned actors and compares columns plus ordered rows against the
cached response artifacts. It never trusts cache-key metadata or capture observations.
"""
import argparse, json, os, sys, time
try:
    import resource
except ImportError:  # Windows offline tests; available inside the Linux oracle container
    resource = None
from pathlib import Path


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(value):
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_frappe(frappe, value):
    return json.loads(frappe.as_json(value))


def authenticate(frappe, user, password):
    from frappe.utils.password import Auth, passlibctx
    rows = (frappe.qb.from_(Auth).select(Auth.name, Auth.password)
            .where((Auth.doctype == "User") & (Auth.name == user) & (Auth.fieldname == "password") & (Auth.encrypted == 0))
            .limit(1).run(as_dict=True))
    if not rows or not passlibctx.verify(password, rows[0].password):
        raise PermissionError(f"pinned actor authentication failed for {user}")
    frappe.set_user(rows[0].name)
    return rows[0].name


def cached_parts(path):
    payload = load_json(path)
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("result"), list):
        raise ValueError(f"cached artifact lacks message.result: {path}")
    return {"columns": message.get("columns"), "rows": message["result"]}


def type_shape(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return ["list", [type_shape(item) for item in value]]
    if isinstance(value, dict):
        return ["dict", {str(key): type_shape(value[key]) for key in sorted(value, key=str)}]
    return type(value).__name__


def project_ordered_page(rows, page, page_length):
    start = int(page) * int(page_length)
    end = start + int(page_length)
    return list(rows[start:end]), start, end


def compare_exact(label, live_columns, live_rows, cache_file):
    cached = cached_parts(cache_file)
    live_json = canonical([live_columns, live_rows])
    cached_json = canonical([cached["columns"], cached["rows"]])
    value_ordered_equal = live_json == cached_json
    type_exact_equal = type_shape([live_columns, live_rows]) == type_shape([cached["columns"], cached["rows"]])
    ordered_equal = value_ordered_equal and type_exact_equal
    live_digest = sha256_text(live_json)
    cached_digest = sha256_text(cached_json)
    mismatch = None
    if not ordered_equal:
        mismatch = {
            "columns_equal": canonical(live_columns) == canonical(cached["columns"]),
            "value_ordered_equal": value_ordered_equal,
            "type_exact_equal": type_exact_equal,
            "row_count_live": len(live_rows), "row_count_cached": len(cached["rows"]),
            "first_ordered_row_difference": next((i for i, pair in enumerate(zip(live_rows, cached["rows"])) if canonical(pair[0]) != canonical(pair[1])), min(len(live_rows), len(cached["rows"])) if len(live_rows) != len(cached["rows"]) else None),
        }
    return {"label": label, "cache_file": str(cache_file), "ordered_exact_equal": ordered_equal,
            "value_ordered_equal": value_ordered_equal, "type_exact_equal": type_exact_equal,
            "live_sha256": live_digest, "cached_sha256": cached_digest,
            "live_row_count": len(live_rows), "cached_row_count": len(cached["rows"]), "mismatch": mismatch}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--credentials-env", default="MANAGER_ORACLE_CREDENTIALS_JSON")
    args = ap.parse_args()
    req = load_json(args.request)
    cache = Path(args.cache_dir).resolve()
    output = Path(args.output).resolve()
    creds = json.loads(os.environ[args.credentials_env])
    os.chdir("/home/frappe/frappe-bench/sites")
    import frappe
    frappe.init(site=req["site"])
    frappe.connect()
    from frappe.desk.query_report import run
    window = req["report_windows"]["year_to_date"]
    workload = req["manager_workload"]
    results = []
    try:
        for actor, group in (("summary_warehouse", "Warehouse"), ("summary_monthly", "Monthly")):
            c = creds[actor]
            user = authenticate(frappe, c.get("user") or req["source"]["actors"][actor]["user"], c["password"])
            filters = {"company": req.get("company") or "Khit Me Distribution", "from_date": window["from_date"], "to_date": window["to_date"], "group_by": group}
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0
            started = time.monotonic()
            out = run("Gross Profit", filters=json.dumps(filters, separators=(",", ":")), ignore_prepared_report=True)
            elapsed = time.monotonic() - started
            after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0
            message = normalize_frappe(frappe, out)
            matches = sorted((cache / "oracle-responses").glob(f"summary-{group.lower()}-*.bin")) if (cache / "oracle-responses").is_dir() else sorted(cache.glob(f"summary-{group.lower()}-*.bin"))
            if not matches:
                raise FileNotFoundError(f"no cached summary artifact for {group}")
            r = compare_exact(f"summary-{group}", message.get("columns"), message.get("result"), matches[0])
            r.update({"actor": actor, "actor_user": user, "window": dict(window), "execution_seconds": round(elapsed, 3), "maxrss_kib_before": before, "maxrss_kib_after": after})
            results.append(r)

        actor = "detail_reader"
        c = creds[actor]
        user = authenticate(frappe, c.get("user") or req["source"]["actors"][actor]["user"], c["password"])
        filters = {"company": req.get("company") or "Khit Me Distribution", "from_date": window["from_date"], "to_date": window["to_date"], "group_by": "Invoice"}
        started = time.monotonic()
        out = run("Gross Profit", filters=json.dumps(filters, separators=(",", ":")), ignore_prepared_report=True)
        elapsed = time.monotonic() - started
        # The stock generator returns the complete ordered result. Normalize only
        # the pages being compared to avoid making full-year JSON / parsed copies.
        rows = out["result"]
        source_count = len(rows)
        import manager_offload as mo
        detail_full_rows_sha256 = mo.canonical_json_streaming_sha256([out["columns"], rows], mo.frappe_json_handler())
        columns = normalize_frappe(frappe, out["columns"])
        page_len = int(workload["detail_page_length"])
        page_count = int(workload["detail_page_count"])
        if page_count <= 0 or page_len <= 0:
            raise ValueError("detail comparison requires positive page count and size")
        prefix = rows[:page_count * page_len]
        del rows, out
        for page in range(page_count):
            projected, projection_start, projection_end = project_ordered_page(prefix, page, page_len)
            part = normalize_frappe(frappe, projected)
            matches = sorted((cache / "oracle-responses").glob(f"detail-page-{page}-*.bin")) if (cache / "oracle-responses").is_dir() else sorted(cache.glob(f"detail-page-{page}-*.bin"))
            if not matches:
                raise FileNotFoundError(f"no cached detail artifact for page {page}")
            r = compare_exact(f"detail-page-{page}", columns, part, matches[0])
            r.update({"actor": actor, "actor_user": user, "window": dict(window), "detail_source_row_count": source_count, "projection": {"page": page, "start": projection_start, "end": projection_end, "page_length": page_len, "rows_in_page": len(projected), "projection_exact_equal": r["ordered_exact_equal"]}, "execution_seconds": round(elapsed, 3) if page == 0 else 0, "derivation_checked": "independent full ordered stock detail result projected separately, then compared with exact types"})
            results.append(r)
        verdict = {"schema_version": "independent-stock-oracle-verification-v1", "passed": all(r["ordered_exact_equal"] for r in results), "scope": "year_to_date Warehouse/Monthly summaries and first two ordered detail pages", "window": dict(window), "stock_detail_rows_sha256": detail_full_rows_sha256, "stock_detail_row_count": source_count, "results": results}
    finally:
        frappe.destroy()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(verdict, f, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps({"passed": verdict["passed"], "checks": len(results), "output": str(output)}, sort_keys=True))
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
