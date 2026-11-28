#!/usr/bin/env python3
"""Independent seasonal-run database verifier.

This verifier intentionally does not call a Frappe/ERPNext application endpoint.  It
reads the client identities emitted by k6 and then inspects MariaDB directly through
the isolated perf compose project.  All SQL is read-only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

EVENT_PREFIX = "KHITMEE_SEASONAL_EVENT "
EXPECTED_DOCTYPE = {
    "sale": "Sales Invoice",
    "transfer": "Stock Entry",
    "purchase_receipt": "Purchase Receipt",
    "purchase_invoice": "Purchase Invoice",
}
CHILD_TABLE = {
    "Sales Invoice": "tabSales Invoice Item",
    "Stock Entry": "tabStock Entry Detail",
    "Purchase Receipt": "tabPurchase Receipt Item",
    "Purchase Invoice": "tabPurchase Invoice Item",
}
STOCK_LEDGER_TYPES = {"Sales Invoice", "Stock Entry", "Purchase Receipt"}
GL_TYPES = {"Sales Invoice", "Purchase Invoice"}


def sql_string(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def sql_like_contains(value):
    escaped = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
    return f"'%{escaped}%' ESCAPE '\\\\'"


def sql_in(values):
    return ",".join(sql_string(value) for value in values)


def chunks(values, size=200):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index : index + size]


def parse_events(path):
    started = {}
    completed = {}
    rejected = {}
    unknown = {}
    config = {}
    events = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read k6 log {path}: {exc}") from exc
    for line in lines:
        raw = None
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        if isinstance(record, dict) and isinstance(record.get("msg"), str) and record["msg"].startswith(EVENT_PREFIX):
            raw = record["msg"][len(EVENT_PREFIX) :].strip()
        if raw is None:
            marker = line.find(EVENT_PREFIX)
            if marker < 0:
                continue
            raw = line[marker + len(EVENT_PREFIX) :].strip()
        candidates = [raw]
        # k6 text output usually wraps console.log text in a logfmt msg="..."
        # field, with JSON quotes escaped as \". Decode that one quoting layer.
        encoded = raw
        for suffix in ('" source=console', '"'):
            if encoded.endswith(suffix):
                encoded = encoded[: -len(suffix)]
                break
        candidates.append(encoded.replace('\\"', '"'))
        event = None
        for candidate in candidates:
            try:
                event = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if event is None:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        kind = event.get("event")
        client_id = str(event.get("client_voucher_id") or "")
        if kind == "config" and isinstance(event.get("config"), dict):
            config = event["config"]
        elif kind == "voucher_start" and client_id:
            started[client_id] = event
        elif kind == "voucher_complete" and client_id:
            completed[client_id] = event
        elif kind == "voucher_reject" and client_id:
            rejected[client_id] = event
        elif kind == "voucher_unknown" and client_id:
            unknown[client_id] = event
    return {
        "events": events,
        "started": started,
        "completed": completed,
        "rejected": rejected,
        "unknown": unknown,
        "config": config,
    }


class DockerComposeDb:
    """Small read-only MariaDB client using the perf project's db container."""

    def __init__(self, docker_dir, project="khitmee-perf", site_name=""):
        self.docker_dir = Path(docker_dir)
        self.project = project
        self.site_name = site_name
        self.env_file = self.docker_dir / ".env"
        self._database_name = ""

    def compose_base(self):
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(self.docker_dir),
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.docker_dir / "compose.base.yaml"),
        ]

    def run_compose(self, args, timeout=30):
        command = self.compose_base() + args
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"docker compose command failed to execute: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            safe_detail = detail[-1] if detail else f"exit {result.returncode}"
            raise RuntimeError(f"docker compose command failed: {safe_detail[:240]}")
        return result.stdout

    def database_name(self):
        if self._database_name:
            return self._database_name
        if self.site_name:
            script = 'cat "$1"'
            path = f"/home/frappe/frappe-bench/sites/{self.site_name}/site_config.json"
        else:
            script = 'for f in /home/frappe/frappe-bench/sites/*/site_config.json; do cat "$f"; exit 0; done; exit 1'
            path = ""
        args = ["exec", "-T", "backend", "sh", "-lc", script, "sh"]
        if path:
            args.append(path)
        try:
            config = json.loads(self.run_compose(args, timeout=20))
        except json.JSONDecodeError as exc:
            raise RuntimeError("backend site_config.json was not valid JSON") from exc
        name = str(config.get("db_name") or "")
        if not name:
            raise RuntimeError("backend site_config.json does not contain db_name")
        self._database_name = name
        return name

    def query(self, sql):
        if not re.match(r"\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
            raise ValueError("verifier only permits one SELECT/WITH query")
        database = self.database_name()
        shell = 'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" --database="$1" --batch --raw --skip-column-names -e "$2"'
        output = self.run_compose(
            ["exec", "-T", "db", "sh", "-lc", shell, "sh", database, sql],
            timeout=60,
        )
        rows = []
        for line in output.splitlines():
            if line:
                rows.append(line.split("\t"))
        return rows


def row_value(row, index, default=""):
    return row[index] if len(row) > index else default


def parse_idempotency_rows(rows):
    result = defaultdict(list)
    for row in rows:
        key = row_value(row, 0)
        # Production keys are user:voucher_kind:client_voucher_id.  The exact
        # user and kind are not part of the client identity, so recover the
        # seasonal client ID by its K6S prefix rather than by a fixed colon
        # position.
        match = re.search(r"K6S-[A-Za-z0-9_.:-]+", key)
        client_id = match.group(0) if match else ""
        if not client_id:
            continue
        result[client_id].append(
            {
                "doctype": row_value(row, 1),
                "name": row_value(row, 2),
            }
        )
    return result


def parse_purchase_rows(rows, bill_no_index=None):
    result = defaultdict(list)
    pattern = re.compile(r"client_voucher_id=([^\s]+)")
    for row in rows:
        name = row_value(row, 0)
        docstatus = row_value(row, 1)
        text = " ".join(str(cell or "") for cell in row[2:])
        match = pattern.search(text)
        client_id = match.group(1) if match else ""
        if not client_id and bill_no_index is not None:
            client_id = row_value(row, bill_no_index)
        if client_id:
            result[client_id].append({"doctype": None, "name": name, "docstatus": docstatus})
    return result


def aggregate_rows(rows):
    return {row_value(row, 0): row for row in rows if row_value(row, 0)}


class Reconciler:
    def __init__(self, events, db_rows):
        self.events = events
        self.db_rows = db_rows
        self.defects = []
        self.warnings = []
        self.by_type = defaultdict(lambda: {"started": 0, "found": 0, "missing": 0, "duplicates": 0, "confirmed": 0, "unknown": 0})

    def defect(self, kind, client_id, detail):
        self.defects.append({"kind": kind, "client_voucher_id": client_id, "detail": detail})

    def document_refs(self, client_id, voucher_type):
        if voucher_type in {"sale", "transfer"}:
            rows = self.db_rows["idempotency"].get(client_id, [])
            return [{"doctype": row["doctype"], "name": row["name"], "docstatus": "1"} for row in rows if row.get("name")]
        key = "purchase_receipts" if voucher_type == "purchase_receipt" else "purchase_invoices"
        rows = self.db_rows[key].get(client_id, [])
        expected = EXPECTED_DOCTYPE[voucher_type]
        return [{"doctype": expected, "name": row["name"], "docstatus": row.get("docstatus", "")} for row in rows]

    def child_count(self, doctype, name):
        return int(float(row_value(self.db_rows["child_counts"].get((doctype, name), []), 1, "0") or 0))

    def ledger_row(self, ledger, doctype, name):
        return self.db_rows[ledger].get((doctype, name))

    def check_document_row_count(self, client_id, start, doctype, name):
        expected_lines = int(start.get("line_count") or 0)
        observed = self.child_count(doctype, name)
        if observed != expected_lines:
            self.defect(
                "row_count_mismatch",
                client_id,
                f"{doctype} {name} has {observed} child rows; expected {expected_lines}",
            )

    def check_ledgers(self, client_id, doctype, name, expected_lines):
        if doctype in STOCK_LEDGER_TYPES:
            row = self.ledger_row("stock_ledger", doctype, name)
            count = int(float(row_value(row or [], 1, "0") or 0))
            if count < expected_lines:
                self.defect(
                    "stock_ledger_missing",
                    client_id,
                    f"{doctype} {name} has {count} Stock Ledger rows; expected at least {expected_lines}",
                )
            if doctype == "Stock Entry":
                balance = float(row_value(row or [], 2, "0") or 0)
                if abs(balance) > 0.0001:
                    self.defect(
                        "stock_transfer_unbalanced",
                        client_id,
                        f"Stock Entry {name} net actual_qty is {balance}; expected zero",
                    )
        if doctype in GL_TYPES:
            row = self.ledger_row("gl_entries", doctype, name)
            count = int(float(row_value(row or [], 1, "0") or 0))
            balance = float(row_value(row or [], 2, "0") or 0)
            if count == 0:
                self.defect("gl_missing", client_id, f"{doctype} {name} has no GL Entry rows")
            elif abs(balance) > 0.01:
                self.defect("gl_unbalanced", client_id, f"{doctype} {name} net debit-credit is {balance}")

    def check_purchase_invoice_link(self, client_id, name):
        row = self.db_rows["purchase_invoice_links"].get(name)
        missing_receipt = int(float(row_value(row or [], 1, "0") or 0))
        missing_detail = int(float(row_value(row or [], 2, "0") or 0))
        if missing_receipt or missing_detail:
            self.defect(
                "purchase_invoice_link_missing",
                client_id,
                f"Purchase Invoice {name} has {missing_receipt} rows without purchase_receipt and {missing_detail} without pr_detail",
            )

    def reconcile(self):
        started = self.events["started"]
        completed = self.events["completed"]
        for client_id, start in sorted(started.items()):
            voucher_type = str(start.get("type") or "")
            expected_doctype = EXPECTED_DOCTYPE.get(voucher_type)
            type_stats = self.by_type[voucher_type or "unknown"]
            type_stats["started"] += 1
            if not expected_doctype:
                self.defect("unknown_voucher_type", client_id, f"type {voucher_type!r} is not part of the seasonal contract")
                continue
            refs = self.document_refs(client_id, voucher_type)
            unique = {}
            for ref in refs:
                unique[(ref["doctype"], ref["name"])] = ref
            refs = list(unique.values())
            unknown_event = client_id in self.events.get("unknown", {})
            if unknown_event:
                type_stats["unknown"] += 1
            if not refs:
                type_stats["missing"] += 1
                if client_id in completed:
                    self.defect("missing_document", client_id, f"client confirmed {expected_doctype}, but no independent database document was found")
                continue
            type_stats["found"] += 1
            if len(refs) > 1:
                type_stats["duplicates"] += 1
                self.defect(
                    "duplicate_document",
                    client_id,
                    f"{len(refs)} database documents share the client identity: {sorted(ref['name'] for ref in refs)}",
                )
            ref = refs[0]
            if ref["doctype"] != expected_doctype:
                self.defect("doctype_mismatch", client_id, f"expected {expected_doctype}, found {ref['doctype']}")
                continue
            if str(ref.get("docstatus", "1")) not in {"1", "1.0"}:
                self.defect("docstatus_not_submitted", client_id, f"{ref['doctype']} {ref['name']} docstatus={ref.get('docstatus')}")
            completion = completed.get(client_id)
            if completion:
                type_stats["confirmed"] += 1
                if completion.get("voucher_no") != ref["name"]:
                    self.defect(
                        "ack_mismatch",
                        client_id,
                        f"client confirmation {completion.get('voucher_no')} does not match database document {ref['name']}",
                    )
            else:
                self.defect("unconfirmed_document", client_id, f"{ref['doctype']} {ref['name']} exists but no client confirmation was observed")
            expected_lines = int(start.get("line_count") or 0)
            self.check_document_row_count(client_id, start, expected_doctype, ref["name"])
            self.check_ledgers(client_id, expected_doctype, ref["name"], expected_lines)
            if expected_doctype == "Purchase Invoice":
                self.check_purchase_invoice_link(client_id, ref["name"])
        return {
            "passed": not self.defects,
            "defects": self.defects,
            "warnings": self.warnings,
            "by_type": dict(sorted(self.by_type.items())),
        }


def rows_for_verification(querier, run_prefix):
    like = sql_like_contains(run_prefix)
    rows = {}
    rows["idempotency_raw"] = querier.query(
        "SELECT idempotency_key,reference_doctype,reference_name "
        "FROM `tabKhitmee Idempotency Log` "
        f"WHERE idempotency_key LIKE {like}"
    )
    rows["purchase_receipts_raw"] = querier.query(
        "SELECT name,docstatus,REPLACE(REPLACE(COALESCE(remarks,''),'\\t',' '),'\\n',' ') "
        "FROM `tabPurchase Receipt` "
        f"WHERE remarks LIKE {like}"
    )
    rows["purchase_invoices_raw"] = querier.query(
        "SELECT name,docstatus,bill_no,REPLACE(REPLACE(COALESCE(remarks,''),'\\t',' '),'\\n',' ') "
        "FROM `tabPurchase Invoice` "
        f"WHERE bill_no LIKE {like} OR remarks LIKE {like}"
    )
    return rows


def normalize_base_rows(events, raw_rows):
    return {
        "idempotency": parse_idempotency_rows(raw_rows["idempotency_raw"]),
        "purchase_receipts": parse_purchase_rows(raw_rows["purchase_receipts_raw"]),
        "purchase_invoices": parse_purchase_rows(raw_rows["purchase_invoices_raw"], bill_no_index=2),
    }


def collect_document_names(events, base_rows):
    names = defaultdict(set)
    reconciler_probe = Reconciler(events, dict(base_rows, child_counts={}, stock_ledger={}, gl_entries={}, purchase_invoice_links={}))
    for client_id, start in events["started"].items():
        voucher_type = str(start.get("type") or "")
        if voucher_type not in EXPECTED_DOCTYPE:
            continue
        for ref in reconciler_probe.document_refs(client_id, voucher_type):
            if ref.get("name"):
                names[ref["doctype"]].add(ref["name"])
    return names


def ledger_queries(querier, names):
    result = {
        "child_counts": {},
        "stock_ledger": {},
        "gl_entries": {},
        "purchase_invoice_links": {},
    }
    for doctype, docnames in sorted(names.items()):
        table = CHILD_TABLE[doctype]
        for batch in chunks(sorted(docnames)):
            in_list = sql_in(batch)
            for row in querier.query(
                f"SELECT parent,COUNT(*) FROM `{table}` WHERE parent IN ({in_list}) GROUP BY parent"
            ):
                result["child_counts"][(doctype, row_value(row, 0))] = row
            if doctype in STOCK_LEDGER_TYPES:
                for row in querier.query(
                    "SELECT voucher_no,COUNT(*),COALESCE(SUM(actual_qty),0) FROM `tabStock Ledger Entry` "
                    f"WHERE voucher_type={sql_string(doctype)} AND voucher_no IN ({in_list}) GROUP BY voucher_no"
                ):
                    result["stock_ledger"][(doctype, row_value(row, 0))] = row
            if doctype in GL_TYPES:
                for row in querier.query(
                    "SELECT voucher_no,COUNT(*),COALESCE(SUM(debit-credit),0) FROM `tabGL Entry` "
                    f"WHERE voucher_type={sql_string(doctype)} AND voucher_no IN ({in_list}) GROUP BY voucher_no"
                ):
                    result["gl_entries"][(doctype, row_value(row, 0))] = row
            if doctype == "Purchase Invoice":
                for row in querier.query(
                    "SELECT parent,"
                    "SUM(COALESCE(purchase_receipt,'')=''),"
                    "SUM(COALESCE(pr_detail,'')='') "
                    "FROM `tabPurchase Invoice Item` "
                    f"WHERE parent IN ({in_list}) GROUP BY parent"
                ):
                    result["purchase_invoice_links"][row_value(row, 0)] = row
    return result


def verify(events, querier):
    config = events["config"]
    run_prefix = str(config.get("run_prefix") or "")
    if not run_prefix:
        raise ValueError("k6 log does not contain the seasonal config run_prefix")
    if not events["started"]:
        raise ValueError("k6 log contains no voucher_start events; refusing to verify an empty run")
    raw_rows = rows_for_verification(querier, run_prefix)
    base_rows = normalize_base_rows(events, raw_rows)
    names = collect_document_names(events, base_rows)
    ledger_rows = ledger_queries(querier, names)
    db_rows = dict(base_rows)
    db_rows.update(ledger_rows)
    outcome = Reconciler(events, db_rows).reconcile()
    outcome.update(
        {
            "run_prefix": run_prefix,
            "run_id": config.get("run_id"),
            "started": len(events["started"]),
            "completed": len(events["completed"]),
            "rejected": len(events["rejected"]),
            "unknown": len(events.get("unknown", {})),
            "retried_completed": sum(1 for event in events["completed"].values() if event.get("retried")),
            "reconciled_completed": sum(1 for event in events["completed"].values() if event.get("reconciled")),
            "lost_ack_observed": sum(1 for event in events["completed"].values() if event.get("lost_ack"))
            + sum(1 for event in events["rejected"].values() if event.get("lost_ack"))
            + sum(1 for event in events.get("unknown", {}).values() if event.get("lost_ack")),
            "verifier": "independent-direct-mariadb-readonly",
        }
    )
    return outcome


def write_json(path, payload):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k6-log", required=True)
    parser.add_argument("--output")
    parser.add_argument("--docker-dir", default=str(Path(__file__).resolve().parents[1] / "docker"))
    parser.add_argument("--compose-project", default="khitmee-perf")
    parser.add_argument("--site-name", default="")
    args = parser.parse_args(argv)

    try:
        events = parse_events(args.k6_log)
        querier = DockerComposeDb(args.docker_dir, project=args.compose_project, site_name=args.site_name)
        outcome = verify(events, querier)
    except Exception as exc:
        outcome = {
            "passed": False,
            "defects": [{"kind": "verifier_error", "detail": str(exc)}],
            "verifier": "independent-direct-mariadb-readonly",
        }
    write_json(args.output, outcome)
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
