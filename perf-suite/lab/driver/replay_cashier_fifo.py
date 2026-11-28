"""Compare two pinned strict cashier traces using the native persisted SLE ordering fields."""
import argparse
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path


D = Decimal
ITEMS = ("KM-00101", "KM-00039")


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def queue(row):
    values = json.loads(row["stock_queue"], parse_float=D, parse_int=D)
    return [[q, r] for q,r in values if q]


def consume(layers, units):
    """Consume the positive FIFO layers from the front, without rounding the rates."""
    remaining = []
    for quantity, rate in layers:
        need(quantity > 0 and units >= 0, "unexpected negative stock or incoming movement")
        used = min(units, quantity)
        units -= used
        if quantity > used:
            remaining.append([quantity-used, rate])
    need(not units, "issue exceeds available positive FIFO layers")
    return remaining


def layer_value(layers):
    return sum((quantity*rate for quantity, rate in layers), D(0))


def ledger(snapshot, sku):
    rows = [row for row in snapshot["ledger"] if row["item_code"] == sku and not row["is_cancelled"]]
    order = [(row["posting_datetime"], row["creation"], row["name"]) for row in rows]
    need(order == sorted(order), "SLE result is out of DB sort order")
    return rows


def matches_opening(prior, row):
    units = -D(row["actual_qty"])
    return (D(prior["qty_after_transaction"]) - units == D(row["qty_after_transaction"])
            and D(row["stock_value"]) - D(prior["stock_value"]) == D(row["stock_value_difference"])
            and consume(queue(prior), units) == queue(row))


def examine(receipt, sku):
    before, after = (receipt[boundary]["db"]["fifo_trace"] for boundary in ("before", "after"))
    pre, post = ledger(before, sku), ledger(after, sku)
    need(pre and pre == post[:len(pre)], "the opening SLE chain changed or new rows were backdated")
    additions = post[len(pre):]
    sales = {s["id"]:s for s in receipt["after"]["db"]["sales"]}
    need(all(sales[r["client_voucher_id"]]["voucher"] == r["voucher_no"] for r in additions),
         "unmatched SLE or extra transaction in the traced bin")
    initial = pre[-1]
    ordered = []
    observed = [initial]
    canonical = queue(initial)
    need(sum(q for q,r in canonical) == D(initial["qty_after_transaction"]), "opening queue/qty mismatch")
    quantity = D(initial["qty_after_transaction"])
    value = D(initial["stock_value"])
    for row in additions:
        delta = D(row["actual_qty"])
        need(delta < 0, "unexpected non-sale movement")
        canonical = consume(canonical, -delta)
        prior = observed[-1]
        # A matching prior persisted state can explain a row. It does not prove which transaction read it.
        candidates = [p["name"] for p in observed if matches_opening(p, row)]
        cost_openings = [p["name"] for p in observed if consume(queue(p), -delta) == queue(row)
                         and D(row["stock_value"]) - D(p["stock_value"]) == D(row["stock_value_difference"])]
        ordered.append({"entry":row, "previous_in_posting_order":prior["name"],
                        "posting_and_creation_tie":(prior["posting_datetime"],prior["creation"]) == (row["posting_datetime"],row["creation"]),
                        "matching_observed_openings":candidates, "matches_immediate_predecessor":prior["name"] in candidates,
                        "matching_valuation_openings_ignoring_qty_after_transaction":cost_openings,
                        "queue_quantity_minus_qty_after_transaction":sum(q for q,r in queue(row))-D(row["qty_after_transaction"]),
                        "signed_qty_gap_vs_previous":D(row["qty_after_transaction"]) - D(prior["qty_after_transaction"]) - delta,
                        "signed_value_gap_vs_previous":D(row["stock_value"]) - D(prior["stock_value"]) - D(row["stock_value_difference"]),
                        "fifo_queue_matches_all_issues_to_date":canonical == queue(row)})
        observed.append(row)
        quantity += delta
        value += D(row["stock_value_difference"])
    closing = next(b for b in after["bins"] if b["item_code"] == sku)
    opening = next(b for b in before["bins"] if b["item_code"] == sku)
    opening_snapshot = json.dumps(pre, sort_keys=True, separators=(",",":")).encode()
    return {"opening_bin":opening, "opening_predecessor":initial, "opening_ledger_rows":len(pre),
            "opening_ledger_sha256":hashlib.sha256(opening_snapshot).hexdigest(),
            "ordered_new_entries":ordered, "closing_bin":closing, "sum_issued_qty":quantity-D(initial["qty_after_transaction"]),
            "sum_posted_value_changes":value-D(initial["stock_value"]),
            "expected_closing_qty_from_all_issues":quantity, "expected_closing_fifo_value_unrounded":layer_value(canonical),
            "expected_closing_fifo_queue":canonical,
            "closing_persisted_fifo_queue_qty":sum(q for q,r in queue(post[-1])),
            "closing_bin_qty_gap_vs_all_issues":D(closing["actual_qty"])-quantity,
            "closing_bin_value_minus_opening_and_deltas":D(closing["stock_value"])-value}


def conservation_verdict(receipt):
    """Check the positive FIFO bins in this USD fixture independently of its paired arm."""
    before = receipt["before"]["db"]["fifo_trace"]
    after = receipt["after"]["db"]["fifo_trace"]
    need(before["warehouse"] == after["warehouse"] == "Dist Hub 1 - KMD", "wrong traced warehouse")
    need({(r["item_code"], r["valuation_method"]) for r in before["item_settings"]} ==
         {(sku, "FIFO") for sku in ITEMS} and before["item_settings"] == after["item_settings"],
         "wrong traced valuation method")
    need({s["invoice"]["currency"] for s in receipt["after"]["db"]["sales"]} == {"USD"},
         "expected the protected USD cashier fixture")
    bins = {}
    for sku in ITEMS:
        b = examine(receipt, sku)
        opening = b["opening_predecessor"]
        closing = b["closing_bin"]
        qty_gaps = {"opening_bin":D(b["opening_bin"]["actual_qty"])-D(opening["qty_after_transaction"]),
                    "closing_issued_qty":b["closing_bin_qty_gap_vs_all_issues"],
                    "closing_queue_minus_bin":b["closing_persisted_fifo_queue_qty"]-D(closing["actual_qty"])}
        value_gaps = {"opening_bin":D(b["opening_bin"]["stock_value"])-D(opening["stock_value"]),
                      "closing_bin_minus_opening_and_postings":D(closing["stock_value"])-D(b["opening_bin"]["stock_value"])-b["sum_posted_value_changes"],
                      "closing_bin_minus_expected_fifo":D(closing["stock_value"])-b["expected_closing_fifo_value_unrounded"]}
        bad_rows = [r["entry"]["name"] for r in b["ordered_new_entries"] if
                    r["queue_quantity_minus_qty_after_transaction"] or r["signed_qty_gap_vs_previous"]
                    or abs(r["signed_value_gap_vs_previous"]) >= D("0.005")
                    or not r["fifo_queue_matches_all_issues_to_date"]]
        bins[sku] = {"passed":not bad_rows and not any(qty_gaps.values()) and
                      all(abs(v) < D("0.005") for v in value_gaps.values()),
                     "entry_failures":bad_rows, "quantity_gaps":{k:str(v) for k,v in qty_gaps.items()},
                     "value_gaps_usd":{k:str(v) for k,v in value_gaps.items()}}
    return {"passed":all(b["passed"] for b in bins.values()), "warehouse":before["warehouse"],
            "quantity_precision":"exact", "currency":"USD", "absolute_value_error_must_be_below":"0.005", "bins":bins}


def replay(folders):
    results = []
    for folder in folders:
        archive = folder / "cashier-evidence.json.gz"
        data = archive.read_bytes()
        receipt = json.loads(gzip.decompress(data))
        before = receipt["before"]["db"]["fifo_trace"]
        run_key = (folder.parent/"receipt.sha256").read_text().strip()
        saved = (folder.parents[2]/"objects"/run_key).read_bytes()
        need(hashlib.sha256(saved).hexdigest() == run_key, "receipt digest mismatch")
        arm, = json.loads(saved)
        need(arm["raw"][archive.name] == hashlib.sha256(data).hexdigest(), "cashier trace digest mismatch")
        results.append({"receipt_sha256":run_key,"raw_sha256":arm["raw"][archive.name], "archive":str(folder),
                        "accepted":arm["returncode"] == 0, "fixture_sha256":receipt["setup"]["state"]["fixture_sha256"],
                        "namespace":receipt["setup"]["state"]["namespace"], "seed":receipt["seed"],
                        "run":receipt["run"], "application":arm["request"]["commit"],
                        "arrivals":receipt["result"]["groups"], "k6":receipt["result"]["k6"],
                        "item_settings":before["item_settings"],
                        "stock_settings":before["stock_settings"], "script_sha256":receipt["script_sha256"],
                        "future_fifo_gate":conservation_verdict(receipt),
                        "items":{k:examine(receipt,k) for k in ITEMS}})
    a,b = results
    need(a["namespace"] != b["namespace"], "duplicate restore namespace")
    for field in ("fixture_sha256", "seed", "run", "application", "script_sha256"):
        need(a[field] == b[field], "different replay identity: "+field)
    for item in ITEMS:
        for field in ("opening_predecessor", "opening_bin", "opening_ledger_sha256"):
            need(a["items"][item][field] == b["items"][item][field], "opening bin states differ")
    return {"observer":json.loads((folders[0].parents[4]/"releases"/folders[0].parents[2].name/"fifo-pin.json").read_bytes()),
            "order":"posting_datetime, creation, name for stable display; physical commit order and transaction read views were not observed",
            "currency":"USD", "runs":results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders",nargs=2,type=Path)
    print(json.dumps(replay(parser.parse_args().folders), indent=2, default=str))
