def lock_voucher_stock(item_code):
    return frappe.db.sql(
        "SELECT name FROM `tabItem` WHERE name = %s FOR UPDATE", item_code
    )
