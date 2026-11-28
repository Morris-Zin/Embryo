#!/usr/bin/env python3
"""Profile restored smoke-fixture Gross Profit and verify the raw rows outside the guest."""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


HERE = Path(__file__).resolve().parent
LAB = HERE.parent
ROOT = LAB.parents[1]
WORKLOAD = "smoke-gross-profit-shop1-last-seed-day-by-item-v1"


def bundled_module(name, path):
    """Load a checked-out standard-library-only helper without installing files on the guest."""
    return (f"import sys, types\n{name} = types.ModuleType({name!r})\n"
            f"sys.modules[{name!r}] = {name}\nexec({path.read_text(encoding='utf8')!r}, {name}.__dict__)\n")


def capture(fixture):
    probe = bundled_module("perf_profile", ROOT / "tools" / "perf_profile.py") + (HERE / "probe.py").read_text()
    source = (bundled_module("telemetry_profiler", LAB / "telemetry" / "telemetry_profiler.py")
              + (HERE / "guest.py").read_text() + f"\nmain({probe!r}, {fixture!r})\n")
    proc = subprocess.run(["bash", str(LAB / "guest" / "guest.sh"), "ssh", "sudo", "-n", "python3", "-B", "-"],
                          input=source.encode(), stdout=subprocess.PIPE, check=True, timeout=600)
    result = json.loads(proc.stdout)
    result["driver_payload_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    return result


def index_rows(arm):
    fields = [column.get("fieldname") or column["label"].lower().replace(" ", "_") for column in arm["columns"]]
    if (len(fields) != len(set(fields)) or not {"item_code", "qty", "buying_amount", "gross_profit"} <= set(fields)
            or not {"base_amount", "selling_amount"}.intersection(fields)):
        raise ValueError("unexpected Gross Profit columns")
    rows = {}
    for row in arm["rows"]:
        if isinstance(row, list):
            row = dict(zip(fields, row, strict=True))
        if not isinstance(row, dict) or not row.get("item_code") or row["item_code"] in rows:
            raise ValueError("missing or duplicate Item Code group")
        rows[row["item_code"]] = row
    body = [row for key, row in rows.items() if key != "Total"]
    if not body or not any(Decimal(str(row["qty"] or 0)) for row in body):
        raise ValueError("empty or zero-quantity smoke result")
    return rows


def compare(stock, fast):
    for field in ("filters", "seed_id", "seed_manifest_sha256", "site", "currency", "precision", "columns"):
        if stock[field] != fast[field]:
            raise ValueError("stock/fast metadata differ: " + field)
    for arm in (stock, fast):
        if arm["guest_window"]["counter_resets"] or arm["guest_window"]["identity_changes"]:
            raise ValueError("container or counter discontinuity")
    for target in ("backend", "db"):
        if stock["guest_window"]["targets"][target]["identity"] != fast["guest_window"]["targets"][target]["identity"]:
            raise ValueError("container identity changed between reports")
    numeric = {column["fieldname"] for column in stock["columns"]
               if column.get("fieldtype") in ("Float", "Currency", "Percent", "Int")}
    numeric.update(("qty", "base_rate", "buying_rate", "avg._selling_rate", "valuation_rate",
                    "base_amount", "selling_amount", "buying_amount", "gross_profit", "gross_profit_%",
                    "gross_profit_percent"))
    left, right = index_rows(stock), index_rows(fast)
    diffs = []
    checked = Counter()
    for item in sorted(left.keys() | right.keys()):
        if item not in left or item not in right:
            diffs.append({"item": item, "missing_from": "stock" if item not in left else "fast"})
            continue
        for field in sorted(left[item].keys() | right[item].keys()):
            if field not in left[item] or field not in right[item]:
                diffs.append({"item": item, "missing_field": field})
                continue
            a, b = left[item][field], right[item][field]
            if field in numeric and a is not None and b is not None:
                a, b = Decimal(str(a)), Decimal(str(b))
                if not a.is_finite() or not b.is_finite():
                    raise ValueError("nonfinite Gross Profit result")
                checked[field] += 1
            if a != b:
                diffs.append({"item": item, "field": field, "stock": str(a), "fast": str(b)})
    if not all(checked[key] for key in ("qty", "buying_amount", "gross_profit")):
        raise ValueError("no comparable financial values")
    return {"stock_rows": len(left), "fast_rows": len(right), "numeric_cells_checked": dict(checked),
            "declared_report_precision": stock["precision"],
            "rule": "Exact decimal equality of emitted ERP values, including any precision beyond report display.",
            "differences": diffs, "matches": not diffs}


def hypothesis(arm):
    measurement = arm["measurements"]
    top = measurement["top_sql_templates"]
    if not top or not measurement["sql_ms"]:
        return {"status": "unavailable", "reason": "no attributed SQL time"}
    entry = top[0]
    return {"metric": "fast_report_sql_ms", "top_template": entry,
            "share_of_sql_ms": entry["total_ms"] / measurement["sql_ms"],
            "test": "Inspect the highest-time SQL template's plan and call count on this fixture. "
                    "Test whether reducing its cost lowers paired report SQL and wall time with identical rows."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-sha256", required=True, help="supervisor-pinned fixture archive digest")
    parser.add_argument("--output", type=Path, required=True, help="new local JSON evidence file")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output file already exists")
    if not re.fullmatch("[0-9a-f]{64}", args.fixture_sha256):
        parser.error("expected lowercase SHA-256")
    receipt = capture(args.fixture_sha256)
    receipt.update(workload=WORKLOAD, scope="one serial SQL-instrumented module invocation per implementation",
                   seasonal_capacity_evidence=False)
    try:
        receipt["oracle"] = compare(receipt["arms"]["stock"], receipt["arms"]["fast"])
        receipt["next_hypothesis"] = hypothesis(receipt["arms"]["fast"])
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        receipt["oracle"] = {"matches": False, "error": str(exc)}
    with args.output.open("x", encoding="utf8") as handle:
        json.dump(receipt, handle, allow_nan=False, separators=(",", ":"))
        handle.write("\n")
    oracle = receipt["oracle"]
    print(json.dumps({"workload": WORKLOAD, "output": str(args.output),
                      "exact_match": oracle["matches"], "stock_rows": oracle.get("stock_rows")}))
    return 0 if oracle["matches"] else 1


if __name__ == "__main__":
    sys.exit(main())
