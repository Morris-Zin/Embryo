"""Replay the published stock/GL discrepancy from the two protected raw cashier receipts."""
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile


def verified(path, expected):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"archived digest differs for {path.name}")
    return data


def compare(actual, expected, name):
    if actual != expected:
        raise ValueError(f"{name} differs from the published witness")


def historical_signature(folder, release, manifest):
    # Use the verifier installed for these receipts. Later pins record more per-voucher fields.
    module = folder.parents[4] / "releases" / release / "harness/perf-suite/lab/seasonal-smoke"
    verified(module / "supervisor_arm.py", "bb7438166f055478c4e273eb987c35ce91e931d26787bc1b9877cc3896c0afcd")
    script = """
import gzip, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from supervisor_arm import committed
receipt = json.loads(gzip.decompress(Path(sys.argv[2]).read_bytes()))
signature = committed(receipt, json.load(sys.stdin))
print(json.dumps(signature, sort_keys=True, separators=(',', ':'), allow_nan=False))
"""
    return subprocess.check_output([sys.executable, "-I", "-B", "-c", script, str(module),
                                    str(folder / "cashier-evidence.json.gz")], input=manifest)


def replay(witness_path):
    witness = json.loads(witness_path.read_bytes())
    data = verified(witness_path.with_name(witness["request_file"]), witness["request_file_sha256"])
    requests = {r["client_voucher_id"]: r for r in map(json.loads, data.splitlines())}
    warehouse, sku = witness["warehouse"], witness["item_code"]
    release_folder = Path(witness["runs"]["cashier-screen-v3"]["archive"])
    fixture = release_folder.parents[4] / "fixtures" / (witness["fixture_sha256"] + ".tar.gz")
    with fixture.open("rb") as f:
        compare(hashlib.file_digest(f, "sha256").hexdigest(), witness["fixture_sha256"], "fixture archive")
    with tarfile.open(fixture) as archive:
        path = "perf-sites/_data/bench-loadtest.localhost/private/files/seed_manifest.json"
        manifest = archive.extractfile(path).read()
    compare(hashlib.sha256(manifest).hexdigest(), witness["fixture_manifest_sha256"], "seed manifest")
    results = {}
    signatures = {}
    signature_payloads = []
    for release, run in witness["runs"].items():
        folder = Path(run["archive"])
        raw = verified(folder / "cashier-evidence.json.gz", run["raw_sha256"])
        replayed = historical_signature(folder, release, manifest)
        original = verified(folder / "cashier-finance-signature.json", run["signature_sha256"])
        compare(replayed, original, "independently regenerated financial signature")
        signatures[release] = hashlib.sha256(replayed).hexdigest()
        signature_payloads.append(json.loads(replayed))
        receipt = json.loads(gzip.decompress(raw))
        compare(receipt["setup"]["state"]["fixture_sha256"], witness["fixture_sha256"], "fixture")
        sales = receipt["after"]["db"]["sales"]
        selected = []
        debits, credits = Decimal(0), Decimal(0)
        for sale in sales:
            compare(sale["invoice"]["currency"], witness["currency"], "invoice currency")
            for row in sale["stock"]:
                if (row["warehouse"], row["item_code"]) == (warehouse, sku):
                    selected.append({"client_voucher_id":sale["id"], "voucher":sale["voucher"], **row})
            for row in sale["accounts"]:
                if row["account"] == "Cost of Goods Sold - KMD":
                    debits += Decimal(row["debit"])
                if row["account"] == "Stock - Dist Hub 1 - KMD":
                    credits += Decimal(row["credit"])
        events = [e for e in receipt["events"] if e.get("id") in requests]
        confirmed = [e["id"] for e in events if e["type"] == "confirmed"]
        selected.sort(key=lambda r: confirmed.index(r["client_voucher_id"]))
        compare({r["client_voucher_id"] for r in selected}, set(requests), "participating sales")
        compare(selected, run["selected_stock_postings_in_confirmation_order"], "stock postings")
        for e in events:
            if e["type"] == "submit":
                payload = requests[e["id"]]
                compare(e["items"], [[r["item_code"], r["qty"]] for r in payload["items"]], "basket")
                compare(e["warehouse"], payload["warehouse"], "warehouse")
        stock = sum(Decimal(r["value_change"]) for r in selected)
        compare(stock, Decimal(run["total_stock_value_change"]), "stock value")
        results[release] = (stock, debits, credits)
    old_signature, new_signature = signature_payloads
    compare(old_signature["sales"], new_signature["sales"], "sales")
    by_bin = [{tuple(row[:2]):row[2:] for row in sig["stock_by_warehouse_item"]} for sig in signature_payloads]
    compare({key:r[0] for key,r in by_bin[0].items()}, {key:r[0] for key,r in by_bin[1].items()},
            "issued quantities")
    divergent = [key for key, row in by_bin[0].items() if row != by_bin[1][key]]
    compare(len(divergent), witness["differing_stock_value_groups"], "divergent warehouse/item count")
    old, new = (results[n] for n in ("cashier-screen-v2", "cashier-screen-v3"))
    delta = [b-a for a,b in zip(old,new)]
    print(json.dumps({"warehouse":warehouse, "item":sku, "currency":witness["currency"],
                      "regenerated_signature_sha256":signatures, "stock_value_groups_with_differences":len(divergent),
                      "contributing_requests":list(requests), "stock_value_each_run":{n:str(v[0]) for n,v in results.items()},
                      "selected_stock_value_change_difference":str(delta[0]),
                      "full_workload_cogs_debit_difference":str(delta[1]),
                      "full_workload_dist_hub_stock_credit_difference":str(delta[2])}, indent=2))


if __name__ == "__main__":
    replay(Path(__file__).with_name("cashier-valuation-witness.json"))
