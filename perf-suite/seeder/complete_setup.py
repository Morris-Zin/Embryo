import frappe

frappe.init(site="bench-loadtest.localhost", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
try:
    settings = frappe.get_doc("System Settings")
    settings.setup_complete = 1
    settings.language = settings.language or "en"
    settings.time_zone = settings.time_zone or "UTC"
    settings.save(ignore_permissions=True)
    frappe.db.sql("update `tabInstalled Application` set is_setup_complete=1 where app_name in ('frappe','erpnext')")
    frappe.db.commit()
    if not frappe.is_setup_complete():
        raise RuntimeError("setup completion did not take effect")
    print("isolated benchmark setup marked complete")
finally:
    frappe.destroy()
