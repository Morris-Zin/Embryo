"""Read the isolated arm's submitted sales and ledger rows in a read-only session."""
import json
from pathlib import Path
import sys

import pymysql

site = "bench-loadtest.localhost"
config = json.loads(Path(site, "site_config.json").read_bytes())
db = pymysql.connect(host="db", user=config.get("db_user", config["db_name"]), password=config["db_password"],
                     database=config["db_name"], cursorclass=pymysql.cursors.DictCursor)
with db.cursor() as cur:
    cur.execute("START TRANSACTION READ ONLY")
    totals = {}
    for table in ("Sales Invoice", "Sales Invoice Item", "GL Entry", "Stock Ledger Entry", "Khitmee Idempotency Log"):
        cur.execute(f"SELECT COUNT(*) n FROM `tab{table}`")
        totals[table] = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) n FROM `tabKhitmee Idempotency Log` WHERE idempotency_key LIKE '%:sale:SCS-%'")
    prior_sales = cur.fetchone()["n"]
    cur.execute("SELECT SUBSTRING_INDEX(idempotency_key, ':sale:', -1) id, reference_name voucher "
                "FROM `tabKhitmee Idempotency Log` WHERE idempotency_key LIKE %s",
                ("%:sale:SCS-" + sys.argv[1] + "-%",))
    records = cur.fetchall()
    for r in records:
        name = (r["voucher"],)
        cur.execute("SELECT docstatus, customer, company, currency, set_warehouse, grand_total, rounded_total, "
                    "paid_amount, outstanding_amount "
                    "FROM `tabSales Invoice` WHERE name=%s", name)
        r["invoice"] = cur.fetchone()
        cur.execute("SELECT item_code, stock_qty, warehouse, rate, base_net_amount "
                    "FROM `tabSales Invoice Item` WHERE parent=%s ORDER BY idx", name)
        r["items"] = cur.fetchall()
        cur.execute("SELECT item_code,warehouse,SUM(actual_qty) qty,SUM(stock_value_difference) value_change FROM `tabStock Ledger Entry` "
                    "WHERE voucher_type='Sales Invoice' AND voucher_no=%s AND is_cancelled=0 GROUP BY item_code,warehouse", name)
        r["stock"] = cur.fetchall()
        cur.execute("SELECT COUNT(*) entries, SUM(debit) debit,SUM(credit) credit FROM `tabGL Entry` "
                    "WHERE voucher_type='Sales Invoice' AND voucher_no=%s AND is_cancelled=0", name)
        r["gl"] = cur.fetchone()
        cur.execute("SELECT account,party_type,party,SUM(debit) debit,SUM(credit) credit FROM `tabGL Entry` "
                    "WHERE voucher_type='Sales Invoice' AND voucher_no=%s AND is_cancelled=0 "
                    "GROUP BY account,party_type,party", name)
        r["accounts"] = cur.fetchall()
    fifo_trace = capture_fifo_trace(cur)
print(json.dumps({"counts": totals, "smoke_idempotency_rows": prior_sales, "sales": records,
                  "fifo_trace": fifo_trace}, default=str))
