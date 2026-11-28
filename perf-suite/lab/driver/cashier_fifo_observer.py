"""Read the two witnessed item/warehouse SLE chains inside the cashier audit transaction."""


def capture_fifo_trace(cursor):
    warehouse = "Dist Hub 1 - KMD"
    items = ("KM-00101", "KM-00039")
    cursor.execute(
        """SELECT item_code, valuation_method, stock_uom
             FROM tabItem WHERE item_code IN (%s, %s) ORDER BY item_code""", items)
    item_settings = cursor.fetchall()
    cursor.execute(
        """SELECT field, value FROM tabSingles WHERE doctype='Stock Settings'
             AND field IN ('valuation_method', 'allow_negative_stock') ORDER BY field""")
    stock_settings = cursor.fetchall()
    cursor.execute(
        """SELECT item_code, warehouse, actual_qty, valuation_rate, stock_value
             FROM tabBin WHERE warehouse=%s AND item_code IN (%s, %s) ORDER BY item_code""",
        (warehouse, *items))
    bins = cursor.fetchall()
    cursor.execute(
        """SELECT s.name, s.item_code, s.warehouse, s.company, s.posting_datetime, s.posting_date,
                  s.posting_time, s.creation, s.modified, s.actual_qty, s.qty_after_transaction,
                  s.valuation_rate, s.stock_value, s.stock_value_difference, s.stock_queue,
                  s.incoming_rate, s.outgoing_rate, s.voucher_type, s.voucher_no,
                  s.voucher_detail_no, s.is_cancelled,
                  SUBSTRING_INDEX(k.idempotency_key, ':sale:', -1) AS client_voucher_id
             FROM `tabStock Ledger Entry` s
             LEFT JOIN `tabKhitmee Idempotency Log` k
               ON s.voucher_type='Sales Invoice' AND k.reference_doctype=s.voucher_type
              AND k.reference_name=s.voucher_no AND k.idempotency_key LIKE '%%:sale:SCS-%%'
            WHERE s.warehouse=%s AND s.item_code IN (%s, %s)
            ORDER BY s.item_code, s.posting_datetime, s.creation, s.name""",
        (warehouse, *items))
    ledger = cursor.fetchall()
    if {row["item_code"] for row in bins} != set(items):
        raise ValueError("stock trace is missing a fixture Bin")
    if len({row["name"] for row in ledger}) != len(ledger):
        raise ValueError("ambiguous SLE to cashier voucher mapping")
    return {"warehouse": warehouse, "item_settings": item_settings, "stock_settings": stock_settings,
            "bins": bins, "ledger": ledger}
