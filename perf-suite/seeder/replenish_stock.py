#!/usr/bin/env python3
"""Safely top up benchmark warehouse stock from the seed manifest.

The module is importable without Frappe so unit tests can exercise the pure planning and
safety policy offline. Live execution is intentionally self-contained because it is normally
piped into the backend container's bench Python interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import frappe
except ImportError:  # pure-Python tests do not require Frappe
    frappe = None


BENCH_SITE = "bench-loadtest.localhost"
DEFAULT_CONTAINER_MANIFEST = "/tmp/khitmee-seeder/seed_manifest.json"
DEFAULT_BATCH_SIZE = 200
PROTECTED_SITES = frozenset(
    {
        "khitmee.localhost",
        "khitmee.com",
        "www.khitmee.com",
        "erp.khitmee.com",
        "prod.khitmee.localhost",
        "production.khitmee.localhost",
        "dev.khitmee.localhost",
        "stage.khitmee.localhost",
        "staging.khitmee.localhost",
    }
)


class ReplenishmentError(RuntimeError):
    pass


class ReplenishmentSafetyError(ReplenishmentError):
    pass


def normalized_site_name(site: str | None) -> str:
    return str(site or "").strip().lower().rstrip(".")


def is_protected_site(site: str | None) -> bool:
    normalized = normalized_site_name(site)
    return normalized in PROTECTED_SITES or normalized.endswith(".khitmee.localhost")


def assert_safe_replenishment_site(
    site: str,
    allow_non_bench_site: bool = False,
    actual_site: str | None = None,
) -> None:
    requested = normalized_site_name(site)
    if not requested:
        raise ReplenishmentSafetyError("A non-empty ERPNext site name is required")
    candidates = {requested}
    if actual_site is not None:
        candidates.add(normalized_site_name(actual_site))
    for candidate in candidates:
        if is_protected_site(candidate):
            raise ReplenishmentSafetyError(
                f"Refusing to touch protected live/dev site {candidate!r}. Use {BENCH_SITE} for benchmark replenishment."
            )
    if requested != BENCH_SITE and not allow_non_bench_site:
        raise ReplenishmentSafetyError(
            f"Refusing to replenish non-benchmark site {site!r}. Pass --allow-non-bench-site only for an explicit disposable non-live site."
        )
    if actual_site is not None and normalized_site_name(actual_site) != requested:
        raise ReplenishmentSafetyError(
            f"Connected site {actual_site!r} does not match requested replenishment site {site!r}; refusing to continue."
        )


def require_frappe() -> None:
    if frappe is None:
        raise RuntimeError("frappe is not importable; run inside the bench Python environment")


def warehouse_role(warehouse: str) -> str:
    lowered = str(warehouse or "").lower()
    return "distribution" if "dist" in lowered or "hub" in lowered else "retail"


def compute_replenishment_plan(
    manifest: dict[str, Any],
    live_bins: dict[tuple[str, str], float],
    *,
    min_hub_hot_qty: float = 1500.0,
    hub_hot_rank: int = 50,
    min_shop_hot_qty: float = 400.0,
    shop_hot_rank: int = 50,
) -> dict[str, Any]:
    """Compute warehouse/item top-ups without mutating or depending on the manifest object."""
    by_warehouse: dict[str, list[dict[str, Any]]] = {}
    hot_summary = {
        "distribution": {
            "hot_rank": int(hub_hot_rank),
            "min_hot_qty": float(min_hub_hot_qty),
            "hot_items": 0,
            "hot_deficits": 0,
            "hot_deficit_qty": 0.0,
        },
        "retail": {
            "hot_rank": int(shop_hot_rank),
            "min_hot_qty": float(min_shop_hot_qty),
            "hot_items": 0,
            "hot_deficits": 0,
            "hot_deficit_qty": 0.0,
        },
    }
    total_deficits = 0

    in_stock_items = manifest.get("in_stock_items") or {}
    for warehouse, items in in_stock_items.items():
        role = warehouse_role(warehouse)
        hot_rank = int(hub_hot_rank) if role == "distribution" else int(shop_hot_rank)
        min_hot_qty = float(min_hub_hot_qty) if role == "distribution" else float(min_shop_hot_qty)
        warehouse_rows: list[dict[str, Any]] = []
        for index, item in enumerate(items or []):
            item_code = str(item.get("item_code") or "")
            if not item_code:
                continue
            seed_qty = float(item.get("qty") or 0.0)
            is_hot = index < hot_rank
            target_qty = max(seed_qty, min_hot_qty) if is_hot else seed_qty
            live_qty = float(live_bins.get((warehouse, item_code), 0.0) or 0.0)
            if is_hot:
                hot_summary[role]["hot_items"] += 1
            if live_qty >= target_qty:
                continue
            deficit = round(target_qty - live_qty, 6)
            row = {
                "item_code": item_code,
                "qty": deficit,
                "target_qty": round(target_qty, 6),
                "live_qty": round(live_qty, 6),
                "uom": str(item.get("uom") or "Nos"),
            }
            warehouse_rows.append(row)
            total_deficits += 1
            if is_hot:
                hot_summary[role]["hot_deficits"] += 1
                hot_summary[role]["hot_deficit_qty"] = round(hot_summary[role]["hot_deficit_qty"] + deficit, 6)
        if warehouse_rows:
            by_warehouse[str(warehouse)] = warehouse_rows

    return {
        "total_deficits": total_deficits,
        "by_warehouse": by_warehouse,
        "hot_summary": hot_summary,
    }


def resolve_sites_path(site: str) -> str:
    cwd = Path(os.getcwd())
    if (cwd / site).exists():
        return str(cwd)
    bench_sites = Path("/home/frappe/frappe-bench/sites")
    return str(bench_sites)


def connect_site(site: str) -> None:
    require_frappe()
    if (
        getattr(frappe.local, "initialised", False)
        and getattr(frappe.local, "site", None) == site
        and getattr(frappe.local, "db", None)
    ):
        return
    frappe.init(site=site, sites_path=resolve_sites_path(site), force=True)
    frappe.connect()


def connect_and_assert_site(site: str, allow_non_bench_site: bool = False) -> str:
    assert_safe_replenishment_site(site, allow_non_bench_site)
    connect_site(site)
    actual_site = getattr(frappe.local, "site", None)
    assert_safe_replenishment_site(site, allow_non_bench_site, actual_site=actual_site)
    return str(actual_site)


def load_manifest(path: str | None) -> tuple[dict[str, Any], str]:
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(Path(DEFAULT_CONTAINER_MANIFEST))
        if frappe is not None and getattr(frappe, "local", None) and getattr(frappe.local, "site_path", None):
            candidates.append(Path(frappe.get_site_path("private", "files")) / "seed_manifest.json")
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8")), str(candidate)
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"seed_manifest.json not found; tried {tried}")


def fetch_live_bins() -> dict[tuple[str, str], float]:
    rows = frappe.db.sql(
        "SELECT warehouse, item_code, actual_qty FROM `tabBin`",
        as_dict=True,
    )
    return {
        (str(row.get("warehouse")), str(row.get("item_code"))): float(row.get("actual_qty") or 0.0)
        for row in rows or []
    }


def iter_plan_rows(plan: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for warehouse, rows in (plan.get("by_warehouse") or {}).items():
        for row in rows:
            yield warehouse, row


def positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def latest_sle_valuation_rate(item_code: str, warehouse: str) -> float | None:
    scoped_rows = frappe.db.sql(
        """
        SELECT valuation_rate
        FROM `tabStock Ledger Entry`
        WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
          AND valuation_rate IS NOT NULL AND valuation_rate > 0
        ORDER BY posting_datetime DESC, creation DESC, name DESC
        LIMIT 1
        """,
        (item_code, warehouse),
    )
    if scoped_rows:
        return positive_float(scoped_rows[0][0])
    rows = frappe.db.sql(
        """
        SELECT valuation_rate
        FROM `tabStock Ledger Entry`
        WHERE item_code=%s AND is_cancelled=0
          AND valuation_rate IS NOT NULL AND valuation_rate > 0
        ORDER BY posting_datetime DESC, creation DESC, name DESC
        LIMIT 1
        """,
        (item_code,),
    )
    return positive_float(rows[0][0]) if rows else None


def item_fallback_valuation_rate(item_code: str) -> float | None:
    meta = frappe.get_meta("Item")
    for fieldname in ("valuation_rate", "standard_rate"):
        if not meta.has_field(fieldname):
            continue
        rate = positive_float(frappe.db.get_value("Item", item_code, fieldname))
        if rate is not None:
            return rate
    return None


def resolve_valuation_rates(plan: dict[str, Any]) -> dict[tuple[str, str], float]:
    rates: dict[tuple[str, str], float] = {}
    for warehouse, row in iter_plan_rows(plan):
        item_code = row["item_code"]
        key = (warehouse, item_code)
        if key in rates:
            continue
        rates[key] = latest_sle_valuation_rate(item_code, warehouse) or item_fallback_valuation_rate(item_code) or 1.0
    return rates


def hot_bin_snapshots(
    manifest: dict[str, Any],
    live_bins: dict[tuple[str, str], float],
    *,
    min_hub_hot_qty: float = 1500.0,
    hub_hot_rank: int = 50,
    min_shop_hot_qty: float = 400.0,
    shop_hot_rank: int = 50,
    limit_per_warehouse: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for warehouse, items in (manifest.get("in_stock_items") or {}).items():
        role = warehouse_role(warehouse)
        hot_rank = int(hub_hot_rank) if role == "distribution" else int(shop_hot_rank)
        min_hot_qty = float(min_hub_hot_qty) if role == "distribution" else float(min_shop_hot_qty)
        rows: list[dict[str, Any]] = []
        for index, item in enumerate((items or [])[:hot_rank]):
            if len(rows) >= limit_per_warehouse:
                break
            item_code = str(item.get("item_code") or "")
            if not item_code:
                continue
            seed_qty = float(item.get("qty") or 0.0)
            rows.append(
                {
                    "item_code": item_code,
                    "hot_index": index,
                    "live_qty": round(float(live_bins.get((warehouse, item_code), 0.0) or 0.0), 6),
                    "target_qty": round(max(seed_qty, min_hot_qty), 6),
                    "uom": str(item.get("uom") or "Nos"),
                }
            )
        if rows:
            snapshots[str(warehouse)] = rows
    return snapshots


def chunks(rows: Sequence[dict[str, Any]], size: int) -> Iterable[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def company_stock_entry_defaults(company: str) -> dict[str, str | None]:
    stock_adjustment = frappe.db.get_value("Company", company, "stock_adjustment_account")
    cost_center = frappe.db.get_value("Company", company, "cost_center")
    if not stock_adjustment:
        raise ReplenishmentError(f"Company {company!r} has no stock_adjustment_account for Material Receipt valuation")
    return {"expense_account": stock_adjustment, "cost_center": cost_center}


def post_replenishment_stock_entries(
    manifest: dict[str, Any],
    plan: dict[str, Any],
    valuation_rates: dict[tuple[str, str], float],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    from frappe.utils import nowdate, nowtime

    company = str(manifest.get("company") or "")
    if not company:
        raise ReplenishmentError("seed manifest does not contain a company")
    defaults = company_stock_entry_defaults(company)
    vouchers: list[dict[str, Any]] = []
    posting_date = nowdate()
    posting_time = nowtime()

    for warehouse, rows in (plan.get("by_warehouse") or {}).items():
        for batch in chunks(rows, batch_size):
            item_rows = []
            for row in batch:
                item_code = row["item_code"]
                item_rows.append(
                    {
                        "item_code": item_code,
                        "qty": row["qty"],
                        "uom": row.get("uom") or "Nos",
                        "t_warehouse": warehouse,
                        "basic_rate": valuation_rates.get((warehouse, item_code), 1.0),
                        "expense_account": defaults["expense_account"],
                        "cost_center": defaults["cost_center"],
                    }
                )
            doc = frappe.get_doc(
                {
                    "doctype": "Stock Entry",
                    "stock_entry_type": "Material Receipt",
                    "purpose": "Material Receipt",
                    "company": company,
                    "posting_date": posting_date,
                    "posting_time": posting_time,
                    "set_posting_time": 1,
                    "to_warehouse": warehouse,
                    "remarks": "Khitmee disposable benchmark stock replenishment",
                    "items": item_rows,
                }
            )
            doc.flags.ignore_permissions = True
            doc.insert(ignore_permissions=True)
            doc.submit()
            if int(doc.docstatus or 0) != 1:
                raise ReplenishmentError(f"Stock Entry {doc.name} did not reach docstatus=1")
            vouchers.append({"name": doc.name, "warehouse": warehouse, "item_count": len(item_rows)})
    return vouchers


def verify_plan_applied(
    plan: dict[str, Any], live_after: dict[tuple[str, str], float]
) -> list[dict[str, Any]]:
    shortfalls = []
    for warehouse, row in iter_plan_rows(plan):
        actual = float(live_after.get((warehouse, row["item_code"]), 0.0) or 0.0)
        if actual < float(row["target_qty"]):
            shortfalls.append(
                {
                    "warehouse": warehouse,
                    "item_code": row["item_code"],
                    "target_qty": row["target_qty"],
                    "actual_qty": round(actual, 6),
                }
            )
    return shortfalls


def verify_completed_stock_closings() -> dict[str, Any]:
    rows = frappe.db.sql(
        """
        SELECT name, docstatus, status
        FROM `tabStock Closing Entry`
        WHERE name LIKE 'CBAL-%'
        ORDER BY name
        """,
        as_dict=True,
    )
    bad = [
        {"name": row.get("name"), "docstatus": row.get("docstatus"), "status": row.get("status")}
        for row in rows or []
        if int(row.get("docstatus") or 0) != 1 or str(row.get("status") or "") != "Completed"
    ]
    if bad:
        raise ReplenishmentError(f"Stock Closing Entries are not submitted/Completed after replenishment: {bad[:5]}")
    return {"cbal_rows": len(rows or []), "bad_rows": 0}


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def run_check_only(args: argparse.Namespace) -> int:
    site = connect_and_assert_site(args.site, args.allow_non_bench_site)
    frappe.db.sql("SET TRANSACTION READ ONLY")
    manifest, manifest_path = load_manifest(args.manifest)
    live_bins = fetch_live_bins()
    plan = compute_replenishment_plan(manifest, live_bins)
    payload = {
        "event": "stock_replenishment_check",
        "site": site,
        "manifest_path": manifest_path,
        "read_only": True,
        **plan,
    }
    print_json(payload)
    return 0 if plan["total_deficits"] == 0 else 4


def run_apply(args: argparse.Namespace) -> int:
    site = connect_and_assert_site(args.site, args.allow_non_bench_site)
    manifest, manifest_path = load_manifest(args.manifest)
    live_before = fetch_live_bins()
    plan = compute_replenishment_plan(manifest, live_before)
    if plan["total_deficits"] == 0:
        print_json(
            {
                "event": "stock_replenishment_apply",
                "site": site,
                "manifest_path": manifest_path,
                "applied": False,
                "total_deficits": 0,
                "hot_summary": plan["hot_summary"],
            }
        )
        return 0

    before_snapshots = hot_bin_snapshots(manifest, live_before)
    valuation_rates = resolve_valuation_rates(plan)
    try:
        vouchers = post_replenishment_stock_entries(manifest, plan, valuation_rates, batch_size=DEFAULT_BATCH_SIZE)
    except Exception:
        frappe.db.rollback()
        raise
    frappe.db.commit()

    live_after = fetch_live_bins()
    shortfalls = verify_plan_applied(plan, live_after)
    closing_verification = verify_completed_stock_closings()
    after_snapshots = hot_bin_snapshots(manifest, live_after)
    summary = {
        "event": "stock_replenishment_apply",
        "site": site,
        "manifest_path": manifest_path,
        "applied": True,
        "total_deficits": plan["total_deficits"],
        "item_count": sum(len(rows) for rows in plan["by_warehouse"].values()),
        "voucher_count": len(vouchers),
        "vouchers": vouchers,
        "hot_summary": plan["hot_summary"],
        "hot_bins_before": before_snapshots,
        "hot_bins_after": after_snapshots,
        "verification": {
            "shortfall_count": len(shortfalls),
            "shortfalls": shortfalls[:20],
            "stock_closing_entries": closing_verification,
        },
    }
    print_json(summary)
    if shortfalls:
        raise ReplenishmentError(f"{len(shortfalls)} replenished bins remained below target after commit")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replenish disposable Khitmee benchmark stock from its seed manifest")
    parser.add_argument("--site", default=BENCH_SITE)
    parser.add_argument("--allow-non-bench-site", action="store_true")
    parser.add_argument("--manifest")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check-only", action="store_true")
    modes.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.check_only:
            return run_check_only(args)
        return run_apply(args)
    except ReplenishmentSafetyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if os.environ.get("SEEDER_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
