"""Executed inside the perf backend by khitmee_dev.py."""
import json
import re
import sys
from pathlib import Path


def upstream(args):
    mode, name, *options = args
    root = Path("../apps").resolve()
    path = (root / name).resolve()
    if path.suffix != ".py" or not any(path.is_relative_to(root / app) for app in ("frappe", "erpnext")):
        raise ValueError("expected installed Python source")
    lines = path.read_text().splitlines()
    if mode == "read":
        first, last = map(int, options or [1, 80])
        if first < 1 or not first <= last < first + 200:
            raise ValueError("select 1 to 200 lines")
        matches = range(first - 1, min(last, len(lines)))
    elif mode == "grep":
        pattern, = options
        matches = (i for i, line in enumerate(lines) if re.search(pattern, line))
    else:
        raise ValueError("use read or grep")
    for count, i in enumerate(matches):
        if count == 200:
            print("[truncated]")
            break
        print(f"{i + 1} {lines[i][:500]}")


def main():
    cmd, *args = sys.argv[1:]
    if cmd == "upstream":
        upstream(args)
        return
    # Discover the installed site without transporting credentials to the host.
    config, = Path.cwd().glob("*/site_config.json")
    if cmd == "py":
        import frappe
        write = args[:1] == ["--write"]
        code, = args[int(write):]
        frappe.init(site=config.parent.name, sites_path=str(Path.cwd()))
        try:
            if frappe.conf.db_host != "db":
                raise RuntimeError("unexpected database host")
            frappe.connect()
            if not write:
                frappe.db.rollback()
                frappe.db.sql("SET SESSION TRANSACTION READ ONLY")
                frappe.db.sql("START TRANSACTION READ ONLY")
            exec(code, {"frappe": frappe})
            if write:
                frappe.db.commit()
        finally:
            frappe.destroy()
        return

    import pymysql
    conf = json.loads(config.read_text())
    query, = args
    query = query.strip().removesuffix(";")
    if not re.match(r"(select|with)\b", query, re.I) or ";" in query:
        raise ValueError("expected a single SELECT or WITH query")
    prefix = {"sql": "", "analyze": "ANALYZE FORMAT=JSON ", "explain": "EXPLAIN "}[cmd]
    with pymysql.connect(host="db", user=conf.get("db_user", conf["db_name"]),
                         password=conf["db_password"], database=conf["db_name"],
                         cursorclass=pymysql.cursors.SSCursor) as db:
        with db.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("START TRANSACTION READ ONLY")
            cur.execute(prefix + query)
            print("\t".join(col[0] for col in cur.description))
            for i, row in enumerate(cur):
                if i == 40:
                    print("[truncated after 40 rows]")
                    break
                print("\t".join(str(cell) for cell in row)[:6000 if cmd == "analyze" else 500])


try:
    main()
except Exception as error:
    sys.exit(f"perf session failed ({type(error).__name__})")
