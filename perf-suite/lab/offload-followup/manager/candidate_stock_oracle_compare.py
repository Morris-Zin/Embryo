#!/usr/bin/env python3
"""Dev-only exact candidate-vs-stock oracle comparison inside a disposable bench clone.

This is not a service and never writes outside the mounted immutable-run output directory.
"""
import argparse
import gc
import gzip
import hashlib
import importlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, "/opt/repo/perf-suite/lab/offload-followup/manager")
WORKER_SCRIPT = Path(__file__).resolve()
sys.path.insert(0, str(WORKER_SCRIPT.parent))

try:
    import frappe
except ModuleNotFoundError:  # offline artifact validation outside a bench
    frappe = None

from manager_offload import canonical_json_streaming_sha256, canonical_json_text, canonical_json_update, frappe_json_handler
from stock_oracle_reference import ReferenceValidationError, validate_reference_manifest, validate_runtime_match
from verify_stock_oracle_independent import authenticate, canonical, normalize_frappe, sha256_text, type_shape

CANDIDATE_COMMIT = "d1be4a691bea7c0557b89d2a59e7fec9df89bae3"
PINNED_SNAPSHOT_ID = "0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd"
STOCK_REPORT = "Gross Profit"
CANDIDATE_REPORT = "Fast Gross Profit"
CREDENTIALS_BY_USER = {}
RUNTIME_SITE = ""
RUNTIME_CREDENTIALS = "/opt/manager-creds.json"
RUNTIME_SNAPSHOT_ID = ""


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonicalize(value):
    """Normalize small native Frappe values without serializing a full detail result."""
    handler = frappe_json_handler()
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=handler))


def normalize_report_small(result):
    message = normalize_frappe(frappe, result)
    return message.get("columns") or [], message.get("result") or []


def compare_exact_parts(candidate_columns, candidate_rows, stock_columns, stock_rows):
    mismatches = []
    if canonical(candidate_columns) != canonical(stock_columns):
        mismatches.append({"kind": "columns_mismatch"})
    if canonical(candidate_rows) != canonical(stock_rows):
        mismatches.append({
            "kind": "ordered_rows_value_mismatch",
            "candidate_row_count": len(candidate_rows),
            "stock_row_count": len(stock_rows),
        })
    if type_shape([candidate_columns, candidate_rows]) != type_shape([stock_columns, stock_rows]):
        mismatches.append({"kind": "ordered_rows_type_mismatch"})
    return mismatches


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")


def rewrite_json(path, value):
    """Atomically augment a checkpoint before the run is sealed as immutable evidence."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def json_hash(value):
    return hashlib.sha256(json.dumps(canonicalize(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_detail_rows_bytes(path, columns, rows, report_name, implementation, actor, filters, snapshot_id):
    """Write full detail rows as deterministic compressed NDJSON with restrictive permissions."""
    handler = frappe_json_handler()
    meta = {
        "schema_version": "oracle-detail-rows-v1",
        "report": report_name,
        "implementation": implementation,
        "actor": actor,
        "filters": canonicalize(filters),
        "snapshot_id": snapshot_id,
        "columns": canonicalize(columns),
        "row_count": len(rows),
    }
    def write_line(value):
        gz.write(json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=handler,
            allow_nan=False,
        ).encode("utf-8") + b"\n")

    with open(path, "xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz:
            write_line(meta)
            for index, row in enumerate(rows):
                write_line({"row_index": index, "row": canonicalize(row)})
    os.chmod(path, 0o600)
    st = os.stat(path)
    return {
        "rows_artifact": Path(path).name,
        "rows_artifact_format": "oracle-detail-rows-ndjson-gzip-v1",
        "rows_artifact_sha256": sha256_file(path),
        "rows_artifact_bytes": st.st_size,
        "rows_artifact_permissions": "0600",
        "snapshot_id": snapshot_id,
    }


def open_detail_rows_artifact(path, checkpoint, expected_filters, expected_snapshot_id, expected_actor, expected_implementation, require_snapshot=False):
    """Open detail row bytes only after recomputing file digest and validating pinned identity."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"detail rows artifact missing: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if os.name != "nt" and mode != 0o600:
        raise ValueError(f"detail rows artifact permissions must be exactly 0600, got {mode:04o}")
    if checkpoint.get("rows_artifact_permissions") != "0600":
        raise ValueError("checkpoint does not pin rows artifact permissions to 0600")
    if checkpoint.get("rows_artifact_format") != "oracle-detail-rows-ndjson-gzip-v1":
        raise ValueError("unsupported detail rows artifact format")
    actual_sha = sha256_file(path)
    if actual_sha != checkpoint.get("rows_artifact_sha256"):
        raise ValueError("detail rows artifact sha256 mismatch")
    actual_bytes = path.stat().st_size
    if actual_bytes != checkpoint.get("rows_artifact_bytes"):
        raise ValueError("detail rows artifact byte size mismatch")
    if require_snapshot and not expected_snapshot_id:
        raise ValueError("checkpoint-byte reuse requires a non-empty pinned snapshot id")

    stream = gzip.open(path, "rt", encoding="utf-8")
    try:
        first = stream.readline()
        if not first:
            raise ValueError("detail rows artifact is empty")
        meta = json.loads(first)
        if meta.get("schema_version") != "oracle-detail-rows-v1":
            raise ValueError("detail rows artifact schema mismatch")
        if meta.get("report") != checkpoint.get("report") or meta.get("implementation") != expected_implementation:
            raise ValueError("detail rows artifact report/implementation mismatch")
        if meta.get("actor") != expected_actor or checkpoint.get("actor") != expected_actor:
            raise ValueError("detail rows artifact actor mismatch")
        if canonicalize(meta.get("filters")) != canonicalize(expected_filters) or canonicalize(checkpoint.get("filters")) != canonicalize(expected_filters):
            raise ValueError("detail rows artifact filters/date range mismatch")
        if meta.get("snapshot_id") != expected_snapshot_id or checkpoint.get("snapshot_id") != expected_snapshot_id:
            raise ValueError("detail rows artifact snapshot mismatch")
        if canonicalize(meta.get("columns")) != canonicalize(checkpoint.get("columns")):
            raise ValueError("detail rows artifact columns mismatch")
    except Exception:
        stream.close()
        raise
    return stream, meta, {"sha256": actual_sha, "bytes": actual_bytes, "permissions": f"{mode:04o}"}


def load_detail_rows_bytes(path, checkpoint, expected_filters, expected_snapshot_id, expected_actor, expected_implementation, require_snapshot=False):
    """Materialize detail rows for legacy candidate-parity diagnostics after full identity validation."""
    stream, meta, _identity = open_detail_rows_artifact(
        path, checkpoint, expected_filters, expected_snapshot_id,
        expected_actor, expected_implementation, require_snapshot=require_snapshot,
    )
    rows = []
    try:
        for line in stream:
            item = json.loads(line)
            if item.get("row_index") != len(rows):
                raise ValueError("detail rows artifact row indexes are not contiguous")
            rows.append(item.get("row"))
    finally:
        stream.close()
    if meta.get("row_count") != len(rows) or checkpoint.get("row_count") != len(rows):
        raise ValueError("detail rows artifact row count mismatch")
    return meta.get("columns") or [], rows, meta


def exact_typed_equal(left, right):
    """Type-sensitive JSON equality; list order is significant, object key order is not."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(exact_typed_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(exact_typed_equal(a, b) for a, b in zip(left, right))
    return left == right


def typed_difference_location(left, right, path="$"):
    if type(left) is not type(right):
        return {"path": path, "left_type": type(left).__name__, "right_type": type(right).__name__}
    if isinstance(left, dict):
        if set(left) != set(right):
            return {"path": path, "left_keys": sorted(set(left) - set(right)), "right_keys": sorted(set(right) - set(left))}
        for key in sorted(left):
            found = typed_difference_location(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return {"path": path, "left_length": len(left), "right_length": len(right)}
        for index, (a, b) in enumerate(zip(left, right)):
            found = typed_difference_location(a, b, f"{path}[{index}]")
            if found:
                return found
        return None
    if left != right:
        return {"path": path, "left_type": type(left).__name__, "right_type": type(right).__name__}
    return None


def stream_compare_stock_detail_artifacts(stock_checkpoint, verify_checkpoint, detail_filters, user, out, page_length):
    """Bounded-memory exact ordered verification of two stock detail artifacts.

    Memory use is bounded by the first two persisted pages per implementation. The full
    row payloads are streamed once in lockstep; neither million-row list is materialized.
    """
    result = {
        "schema_version": "bounded-memory-stock-detail-stream-verification-v1",
        "comparison_mode": "exact-ordered-rows-types-and-first-two-pages",
        "passed": False,
        "mismatches": [],
        "mismatches_truncated": 0,
        "memory_bound": "streams both gzip artifacts; retains at most the first two pages for each implementation",
    }

    def add(kind, **data):
        if len(result["mismatches"]) < 100:
            result["mismatches"].append({"kind": kind, **data})
        else:
            result["mismatches_truncated"] += 1

    try:
        stock_path = detail_rows_path_for_checkpoint(out, stock_checkpoint)
        verify_path = detail_rows_path_for_checkpoint(out, verify_checkpoint)
        stock_stream, stock_meta, stock_identity = open_detail_rows_artifact(
            stock_path, stock_checkpoint, detail_filters, RUNTIME_SNAPSHOT_ID or stock_checkpoint.get("snapshot_id"),
            user, "stock", require_snapshot=False,
        )
        try:
            verify_stream, verify_meta, verify_identity = open_detail_rows_artifact(
                verify_path, verify_checkpoint, detail_filters, RUNTIME_SNAPSHOT_ID or verify_checkpoint.get("snapshot_id"),
                user, "stock-verify", require_snapshot=False,
            )
        except Exception:
            stock_stream.close()
            raise
    except Exception as exc:
        add("artifact_validation", error=f"{type(exc).__name__}: {exc}")
        return result
    if stock_checkpoint.get("snapshot_id") != verify_checkpoint.get("snapshot_id"):
        add("checkpoint_snapshot_mismatch", stock=stock_checkpoint.get("snapshot_id"), verify=verify_checkpoint.get("snapshot_id"))

    stock_hasher = hashlib.sha256()
    verify_hasher = hashlib.sha256()
    for hasher in (stock_hasher, verify_hasher):
        hasher.update(b"[")
    stock_head = []
    verify_head = []
    count = 0
    ended = False
    try:
        for left_line, right_line in zip_longest(stock_stream, verify_stream):
            if left_line is None or right_line is None:
                add("stream_length_mismatch", row_index=count)
                ended = True
                break
            try:
                left = json.loads(left_line)
                right = json.loads(right_line)
            except Exception as exc:
                add("row_json_parse", row_index=count, error=f"{type(exc).__name__}: {exc}")
                ended = True
                break
            if left.get("row_index") != count or right.get("row_index") != count:
                add("noncontiguous_row_index", row_index=count, stock_index=left.get("row_index"), verify_index=right.get("row_index"))
            left_row = left.get("row")
            right_row = right.get("row")
            if not exact_typed_equal(left_row, right_row):
                add("ordered_row_exact_type_or_value_mismatch", row_index=count, difference=typed_difference_location(left_row, right_row))
            if count:
                stock_hasher.update(b",")
                verify_hasher.update(b",")
            canonical_json_update(stock_hasher, left_row)
            canonical_json_update(verify_hasher, right_row)
            if count < page_length * 2:
                stock_head.append(left_row)
                verify_head.append(right_row)
            count += 1
    finally:
        stock_stream.close()
        verify_stream.close()

    for hasher in (stock_hasher, verify_hasher):
        hasher.update(b"]")
    stock_streamed_hash = stock_hasher.hexdigest()
    verify_streamed_hash = verify_hasher.hexdigest()
    result.update({
        "row_count_streamed": count,
        "stock_streamed_rows_sha256": stock_streamed_hash,
        "verify_streamed_rows_sha256": verify_streamed_hash,
        "stock_artifact_file": stock_identity,
        "verify_artifact_file": verify_identity,
        "artifact_file_hashes_differ_expected_due_implementation_metadata": stock_identity["sha256"] != verify_identity["sha256"],
    })
    if ended:
        result["mismatches_truncated"] += 0
        return result

    for label, checkpoint, meta, streamed_hash, head in (
        ("stock", stock_checkpoint, stock_meta, stock_streamed_hash, stock_head),
        ("stock-verify", verify_checkpoint, verify_meta, verify_streamed_hash, verify_head),
    ):
        if count != checkpoint.get("row_count") or count != meta.get("row_count"):
            add("row_count_mismatch", implementation=label, streamed=count, checkpoint=checkpoint.get("row_count"), metadata=meta.get("row_count"))
        if streamed_hash != checkpoint.get("rows_sha256"):
            add("checkpoint_ordered_rows_hash_mismatch", implementation=label, streamed=streamed_hash, checkpoint=checkpoint.get("rows_sha256"))
        pages = checkpoint.get("pages") or {}
        if set(pages) != {"page-0", "page-1"}:
            add("checkpoint_first_two_pages_missing", implementation=label)
        else:
            actual_pages = exact_page_slices(head, page_length)
            if not exact_typed_equal(actual_pages, pages):
                add("checkpoint_first_two_pages_mismatch", implementation=label, difference=typed_difference_location(actual_pages, pages))
    if stock_streamed_hash != verify_streamed_hash:
        add("streamed_ordered_rows_hash_mismatch")
    result["passed"] = not result["mismatches"] and not result["mismatches_truncated"]
    return result


def stream_compare_candidate_detail_artifacts(stock_checkpoint, candidate_checkpoint, detail_filters, user, out, page_length):
    """Bounded-memory exact ordered/type comparison of stock reference rows and fresh candidate rows."""
    result = {
        "schema_version": "bounded-memory-candidate-detail-stream-verification-v1",
        "comparison_mode": "exact-ordered-rows-types-and-first-two-pages",
        "passed": False,
        "mismatches": [],
        "mismatches_truncated": 0,
        "memory_bound": "streams both gzip artifacts; retains at most the first two pages for each implementation",
    }

    def add(kind, **data):
        if len(result["mismatches"]) < 100:
            result["mismatches"].append({"kind": kind, **data})
        else:
            result["mismatches_truncated"] += 1

    try:
        stock_path = detail_rows_path_for_checkpoint(out, stock_checkpoint)
        candidate_path = detail_rows_path_for_checkpoint(out, candidate_checkpoint)
        stock_stream, stock_meta, stock_identity = open_detail_rows_artifact(
            stock_path, stock_checkpoint, detail_filters, RUNTIME_SNAPSHOT_ID or stock_checkpoint.get("snapshot_id"),
            user, "stock", require_snapshot=False,
        )
        try:
            candidate_stream, candidate_meta, candidate_identity = open_detail_rows_artifact(
                candidate_path, candidate_checkpoint, detail_filters, RUNTIME_SNAPSHOT_ID or candidate_checkpoint.get("snapshot_id"),
                user, "candidate", require_snapshot=False,
            )
        except Exception:
            stock_stream.close()
            raise
    except Exception as exc:
        add("artifact_validation", error=f"{type(exc).__name__}: {exc}")
        return result

    stock_hasher = hashlib.sha256()
    candidate_hasher = hashlib.sha256()
    for hasher in (stock_hasher, candidate_hasher):
        hasher.update(b"[")
    stock_head = []
    candidate_head = []
    count = 0
    ended = False
    try:
        for left_line, right_line in zip_longest(stock_stream, candidate_stream):
            if left_line is None or right_line is None:
                add("stream_length_mismatch", row_index=count)
                ended = True
                break
            try:
                left = json.loads(left_line)
                right = json.loads(right_line)
            except Exception as exc:
                add("row_json_parse", row_index=count, error=f"{type(exc).__name__}: {exc}")
                ended = True
                break
            if left.get("row_index") != count or right.get("row_index") != count:
                add("noncontiguous_row_index", row_index=count, stock_index=left.get("row_index"), candidate_index=right.get("row_index"))
            left_row = left.get("row")
            right_row = right.get("row")
            if not exact_typed_equal(left_row, right_row):
                add("ordered_row_exact_type_or_value_mismatch", row_index=count, difference=typed_difference_location(left_row, right_row))
            if count:
                stock_hasher.update(b",")
                candidate_hasher.update(b",")
            canonical_json_update(stock_hasher, left_row)
            canonical_json_update(candidate_hasher, right_row)
            if count < page_length * 2:
                stock_head.append(left_row)
                candidate_head.append(right_row)
            count += 1
    finally:
        stock_stream.close()
        candidate_stream.close()

    for hasher in (stock_hasher, candidate_hasher):
        hasher.update(b"]")
    stock_streamed_hash = stock_hasher.hexdigest()
    candidate_streamed_hash = candidate_hasher.hexdigest()
    result.update({
        "row_count_streamed": count,
        "stock_streamed_rows_sha256": stock_streamed_hash,
        "candidate_streamed_rows_sha256": candidate_streamed_hash,
        "stock_artifact_file": stock_identity,
        "candidate_artifact_file": candidate_identity,
    })
    if ended:
        return result

    for label, checkpoint, meta, streamed_hash, head in (
        ("stock", stock_checkpoint, stock_meta, stock_streamed_hash, stock_head),
        ("candidate", candidate_checkpoint, candidate_meta, candidate_streamed_hash, candidate_head),
    ):
        if count != checkpoint.get("row_count") or count != meta.get("row_count"):
            add("row_count_mismatch", implementation=label, streamed=count, checkpoint=checkpoint.get("row_count"), metadata=meta.get("row_count"))
        if streamed_hash != checkpoint.get("rows_sha256"):
            add("checkpoint_ordered_rows_hash_mismatch", implementation=label, streamed=streamed_hash, checkpoint=checkpoint.get("rows_sha256"))
        pages = checkpoint.get("pages") or {}
        if set(pages) != {"page-0", "page-1"}:
            add("checkpoint_first_two_pages_missing", implementation=label)
        else:
            actual_pages = exact_page_slices(head, page_length)
            if not exact_typed_equal(actual_pages, pages):
                add("checkpoint_first_two_pages_mismatch", implementation=label, difference=typed_difference_location(actual_pages, pages))
    if stock_streamed_hash != candidate_streamed_hash:
        add("streamed_ordered_rows_hash_mismatch")
    result["passed"] = not result["mismatches"] and not result["mismatches_truncated"]
    return result


def detail_rows_path_for_checkpoint(out, checkpoint):
    reference_path = checkpoint.get("_reference_rows_path")
    if reference_path:
        return Path(reference_path)
    name = checkpoint.get("rows_artifact")
    if not name or Path(name).name != name:
        raise ValueError("checkpoint rows artifact name missing or unsafe")
    return Path(out) / name


def strip_internal_checkpoint_fields(checkpoint):
    return {key: value for key, value in checkpoint.items() if not key.startswith("_")}


def stage_reference_checkpoint(reference, case_id, implementation, out):
    """Copy a pinned checkpoint byte-for-byte and keep its large rows in the validated cache."""
    src = reference.checkpoint_path(case_id, implementation)
    dst = Path(out) / f"checkpoint-{case_id}-{implementation}.json"
    if dst.exists():
        raise RuntimeError(f"refusing to overwrite existing checkpoint while staging reference: {dst}")
    shutil.copy2(src, dst)
    checkpoint = json.loads(dst.read_text(encoding="utf-8"))
    if checkpoint.get("rows_artifact"):
        checkpoint["_reference_rows_path"] = str(reference.detail_rows_path(checkpoint))
    return checkpoint


def stock_reference_usage(reference, fresh_stock_executions):
    if reference is not None:
        return reference.usage_metadata()
    return {
        "mode": "fresh-double-stock-oracle",
        "fresh_stock_oracle_executions": fresh_stock_executions,
        "boundary": "stock and independent stock verification were executed in this comparator run",
    }


def write_reference_decision(out, passed, phase, decision, detail, reference=None):
    artifact = {
        "schema_version": "stock-oracle-reference-runtime-validation-v1",
        "created_utc": utc_now(),
        "passed": passed,
        "phase": phase,
        "decision": decision,
        "detail": detail,
    }
    if reference is not None:
        artifact["stock_reference"] = reference.usage_metadata()
    write_json(Path(out) / "stock-reference-validation.json", artifact)


def is_total_row(row):
    return any(row.get(field) == "Total" for field in ("sales_invoice", "item_code", "invoice_or_item", "warehouse", "monthly", "month"))


def row_indent(row):
    try:
        return int(row.get("indent") or 0)
    except (TypeError, ValueError):
        return 0


def invoice_header_key(row):
    return row.get("sales_invoice") or row.get("invoice_or_item")


def invoice_child_parent_key(row):
    return row.get("parent_invoice") or row.get("sales_invoice")


def parse_invoice_rows(rows):
    parsed = {}
    order = []
    duplicates = []
    errors = []
    current_key = None
    current_open = False
    for index, row in enumerate(rows):
        if is_total_row(row):
            continue
        indent = row_indent(row)
        if indent == 0:
            key = invoice_header_key(row)
            current_key = key
            current_open = bool(key)
            if key in parsed:
                duplicates.append(key)
            else:
                parsed[key] = {"header": row, "children": []}
                order.append(key)
            continue
        if indent == 1:
            parent = invoice_child_parent_key(row)
            target = parent if parent in parsed else current_key
            if not current_open or target not in parsed:
                errors.append({"kind": "invoice_tree", "row": index, "error": "child row has no contiguous open parent"})
                continue
            if parent != current_key:
                errors.append({"kind": "invoice_tree", "row": index, "error": "child row parent differs from open header", "expected_parent": current_key, "actual_parent": parent})
            parsed[target]["children"].append(row)
    return parsed, order, duplicates, errors


def compare_field_exact(stock_row, candidate_row, location, add):
    for field in sorted(set(stock_row) | set(candidate_row)):
        if stock_row.get(field) != candidate_row.get(field):
            add("field_value", location=location, field=field, stock=stock_row.get(field), candidate=candidate_row.get(field))


def compare_detail_rows_hybrid(stock_rows, candidate_rows, max_mismatches=100):
    """Compare invoice detail semantics while permitting order only inside same-date tie buckets."""
    mismatches = []
    truncated = 0

    def add(kind, **data):
        nonlocal truncated
        if len(mismatches) < max_mismatches:
            mismatches.append({"kind": kind, **data})
        else:
            truncated += 1

    stock_totals = [row for row in stock_rows if is_total_row(row)]
    candidate_totals = [row for row in candidate_rows if is_total_row(row)]
    if len(stock_totals) != len(candidate_totals):
        add("total_row_count", stock=len(stock_totals), candidate=len(candidate_totals))
    elif stock_totals:
        compare_field_exact(stock_totals[0], candidate_totals[0], "total", add)
    for side, rows in (("stock", stock_rows), ("candidate", candidate_rows)):
        indexes = [index for index, row in enumerate(rows) if is_total_row(row)]
        for index in indexes:
            if index != len(rows) - 1:
                add("total_row_position", side=side, index=index, expected=len(rows) - 1)

    stock_invoices, stock_order, stock_duplicates, stock_errors = parse_invoice_rows(stock_rows)
    candidate_invoices, candidate_order, candidate_duplicates, candidate_errors = parse_invoice_rows(candidate_rows)
    for error in stock_errors:
        add(error["kind"], side="stock", **{k: v for k, v in error.items() if k != "kind"})
    for error in candidate_errors:
        add(error["kind"], side="candidate", **{k: v for k, v in error.items() if k != "kind"})
    for key in stock_duplicates:
        add("duplicate_invoice_key", side="stock", invoice=key)
    for key in candidate_duplicates:
        add("duplicate_invoice_key", side="candidate", invoice=key)
    if set(stock_invoices) != set(candidate_invoices):
        add(
            "invoice_keys",
            missing_candidate=sorted((str(key) for key in set(stock_invoices) - set(candidate_invoices)))[:20],
            missing_stock=sorted((str(key) for key in set(candidate_invoices) - set(stock_invoices)))[:20],
        )

    for side, invoices, order in (("stock", stock_invoices, stock_order), ("candidate", candidate_invoices, candidate_order)):
        dates = [invoices[key]["header"].get("posting_date") for key in order]
        for index, (previous, current) in enumerate(zip(dates, dates[1:])):
            if previous is not None and current is not None and str(previous) < str(current):
                add("invoice_posting_date_order", side=side, index=index, previous=previous, current=current)

    for key in sorted(set(stock_invoices) & set(candidate_invoices), key=str):
        stock_invoice = stock_invoices[key]
        candidate_invoice = candidate_invoices[key]
        compare_field_exact(stock_invoice["header"], candidate_invoice["header"], f"invoice:{key}:header", add)
        stock_children = Counter(canonical_json_text(row) for row in stock_invoice["children"])
        candidate_children = Counter(canonical_json_text(row) for row in candidate_invoice["children"])
        if stock_children != candidate_children:
            add(
                "invoice_child_multiset",
                invoice=key,
                stock_child_count=sum(stock_children.values()),
                candidate_child_count=sum(candidate_children.values()),
                stock_unique_missing_in_candidate=sum((stock_children - candidate_children).values()),
                candidate_unique_missing_in_stock=sum((candidate_children - stock_children).values()),
            )
    return mismatches, truncated


def memory_snapshot():
    """Best-effort cgroup/v2 memory telemetry for immutable per-report checkpoints."""
    keys = ("memory.current", "memory.peak", "memory.max", "memory.swap.current", "memory.swap.max")
    snapshot = {}
    for key in keys:
        try:
            snapshot[key.replace(".", "_")] = Path("/sys/fs/cgroup", key).read_text().strip()
        except Exception:
            snapshot[key.replace(".", "_")] = None
    return snapshot


def reset_memory_peak():
    try:
        Path("/sys/fs/cgroup/memory.peak").write_text("0")
        return True
    except Exception:
        return False


def write_error_checkpoint(out, case_id, implementation, report, actor, filters, exc, elapsed, extra=None):
    artifact = {
        "schema_version": "oracle-report-error-v1",
        "case_id": case_id,
        "implementation": implementation,
        "report": report,
        "actor": actor,
        "filters": filters,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "elapsed_seconds": round(elapsed, 6),
        "memory": memory_snapshot(),
    }
    if extra:
        artifact.update(extra)
    write_json(out / f"checkpoint-{case_id}-{implementation}-error.json", artifact)
    return artifact


def execute_report(report_name, filters, user, normalize=True):
    """Execute one report through query_report.run as the pinned actor."""
    started = time.monotonic()
    credential = CREDENTIALS_BY_USER.get(user)
    if not credential:
        raise PermissionError(f"pinned actor credential missing for {user}")
    authenticate(frappe, user, credential["password"])
    filter_text = json.dumps(filters, separators=(",", ":"))
    frappe.local.form_dict = frappe._dict({
        "report_name": report_name,
        "filters": filter_text,
        "ignore_prepared_report": 1,
    })
    from frappe.desk.query_report import run

    result = run(report_name=report_name, filters=filter_text, ignore_prepared_report=1)
    elapsed = time.monotonic() - started
    if normalize:
        columns, rows = normalize_report_small(result)
    else:
        columns = result.get("columns") if isinstance(result, dict) else []
        rows = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), list) else []
    return result, columns, rows, elapsed


def exact_page_slices(rows, page_length, count=2):
    return {
        f"page-{page}": {
            "start": page * page_length,
            "page_length": page_length,
            "rows": canonicalize(rows[page * page_length:(page + 1) * page_length]),
        }
        for page in range(count)
    }


def detail_evidence(report_name, filters, user, page_length, rows_path=None, implementation=None, snapshot_id=""):
    result, columns, rows, elapsed = execute_report(report_name, filters, user, normalize=False)
    count = len(rows)
    rows_digest = canonical_json_streaming_sha256(rows, frappe_json_handler())
    try:
        pages = exact_page_slices(rows, page_length)
        pages_error = None
    except Exception as exc:
        pages = {}
        pages_error = f"{type(exc).__name__}: {exc}"
    evidence = {
        "columns": canonicalize(columns),
        "columns_sha256": json_hash(columns),
        "row_count": count,
        "rows_sha256": rows_digest,
        "elapsed_seconds": round(elapsed, 6),
        "pages": pages,
        "pages_error": pages_error,
    }
    if rows_path is not None:
        evidence.update(write_detail_rows_bytes(
            rows_path, columns, rows, report_name, implementation or "unknown", user, filters, snapshot_id,
        ))
    del result, columns, rows, pages
    gc.collect()
    return evidence


def worker_main():
    """Execute exactly one report in a short-lived process and checkpoint it.

    The parent orchestrator stays small. Memory allocated by the upstream report is
    released back to the kernel when this worker exits, instead of fragmenting the
    parent Python heap across stock and candidate executions.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--site", required=True)
    parser.add_argument("--report-name", required=True)
    parser.add_argument("--implementation", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--kind", choices=("summary", "detail"), required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--filters-json", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--page-length", type=int, default=50)
    parser.add_argument("--snapshot-id", default="")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    filters = json.loads(args.filters_json)
    global CREDENTIALS_BY_USER
    raw_credentials = json.loads(Path(args.credentials).read_text(encoding="utf-8"))
    CREDENTIALS_BY_USER = {entry["user"]: entry for entry in raw_credentials.values() if isinstance(entry, dict) and entry.get("user")}
    if frappe is None:
        raise RuntimeError("worker mode requires a bench environment with frappe installed")
    os.chdir("/home/frappe/frappe-bench/sites")
    frappe.init(site=args.site, sites_path="/home/frappe/frappe-bench/sites")
    frappe.connect()
    worker_started_utc = utc_now()
    memory_peak_reset = reset_memory_peak()
    memory_before = memory_snapshot()
    memory_before["memory_peak_reset"] = memory_peak_reset
    started = time.monotonic()
    try:
        if args.kind == "summary":
            result, columns, rows, elapsed = execute_report(args.report_name, filters, args.actor)
            artifact = {
                "schema_version": "oracle-report-checkpoint-v1",
                "case_id": args.case_id,
                "implementation": args.implementation,
                "report": args.report_name,
                "actor": args.actor,
                "filters": filters,
                "candidate_commit": CANDIDATE_COMMIT if args.implementation == "candidate" else None,
                "elapsed_seconds": round(elapsed, 6),
                "memory_before": memory_before,
                "memory_after": memory_snapshot(),
                "worker_started_utc": worker_started_utc,
                "worker_finished_utc": utc_now(),
                "row_count": len(rows),
                "columns": canonicalize(columns),
                "columns_sha256": json_hash(columns),
                "rows": canonicalize(rows),
                "rows_sha256": json_hash(rows),
            }
            del result, columns, rows
        else:
            rows_path = out / f"checkpoint-{args.case_id}-{args.implementation}.rows.ndjson.gz"
            artifact = detail_evidence(
                args.report_name,
                filters,
                args.actor,
                args.page_length,
                rows_path=rows_path,
                implementation=args.implementation,
                snapshot_id=args.snapshot_id,
            )
            artifact.update({
                "schema_version": "oracle-detail-checkpoint-v1",
                "case_id": args.case_id,
                "implementation": args.implementation,
                "report": args.report_name,
                "actor": args.actor,
                "filters": filters,
                "candidate_commit": CANDIDATE_COMMIT if args.implementation == "candidate" else None,
                "memory_before": memory_before,
                "memory_after": memory_snapshot(),
                "worker_started_utc": worker_started_utc,
                "worker_finished_utc": utc_now(),
            })
        write_json(out / f"checkpoint-{args.case_id}-{args.implementation}.json", artifact)
        return 0
    except Exception as exc:
        write_error_checkpoint(
            out, args.case_id, args.implementation, args.report_name, args.actor, filters,
            exc, time.monotonic() - started, extra={"memory_before": memory_before, "worker_started_utc": worker_started_utc, "worker_finished_utc": utc_now()},
        )
        return 1
    finally:
        if frappe is not None:
            frappe.destroy()


def run_report_worker(report_name, filters, user, page_length, implementation, out, case_id, kind):
    """Run one implementation/report in a fresh Python process and load its checkpoint."""
    cmd = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--worker",
        "--site", RUNTIME_SITE,
        "--report-name", report_name,
        "--implementation", implementation,
        "--case-id", case_id,
        "--kind", kind,
        "--actor", user,
        "--filters-json", json.dumps(filters, separators=(",", ":")),
        "--out", str(out),
        "--credentials", RUNTIME_CREDENTIALS,
        "--page-length", str(page_length),
        "--snapshot-id", RUNTIME_SNAPSHOT_ID,
    ]
    success_path = out / f"checkpoint-{case_id}-{implementation}.json"
    error_path = out / f"checkpoint-{case_id}-{implementation}-error.json"
    if any(os.path.lexists(path) for path in (success_path, error_path)):
        raise FileExistsError(f"checkpoint already exists for {case_id}/{implementation}")
    parent_started_utc = utc_now()
    started = time.monotonic()
    completed = subprocess.run(cmd, check=False)
    parent_finished_utc = utc_now()
    for path in ((error_path,) if completed.returncode else (error_path, success_path)):
        if path.exists():
            artifact = json.loads(path.read_text(encoding="utf-8"))
            artifact["worker_exitcode"] = completed.returncode
            artifact["worker_wall_seconds"] = round(time.monotonic() - started, 6)
            artifact["parent_started_utc"] = parent_started_utc
            artifact["parent_finished_utc"] = parent_finished_utc
            rewrite_json(path, artifact)
            return artifact
    synthetic = RuntimeError(f"worker exited without writing a checkpoint, exitcode={completed.returncode}")
    artifact = write_error_checkpoint(
        out, case_id, implementation, report_name, user, filters, synthetic, time.monotonic() - started,
        extra={"worker_exitcode": completed.returncode, "memory_before": memory_snapshot(), "parent_started_utc": parent_started_utc, "parent_finished_utc": parent_finished_utc},
    )
    return artifact


def detail_checkpoint(report_name, filters, user, page_length, implementation, out, case_id="detail"):
    return run_report_worker(report_name, filters, user, page_length, implementation, out, case_id, "detail")


def load_checkpoint_rows(checkpoint, detail_filters, user, out, implementation, mismatch_list, label):
    if checkpoint.get("schema_version") != "oracle-detail-checkpoint-v1":
        return None
    try:
        rows_path = detail_rows_path_for_checkpoint(out, checkpoint)
        _columns, rows, _meta = load_detail_rows_bytes(
            rows_path, checkpoint, detail_filters, RUNTIME_SNAPSHOT_ID,
            user, implementation, require_snapshot=False,
        )
        return rows
    except Exception as exc:
        mismatch_list.append({"kind": "rows_artifact_validation", "label": label, "error": f"{type(exc).__name__}: {exc}"})
        return None


def compare_detail(detail_filters, user, page_length, out, case_id="detail", detail_mode="candidate-parity", stock_reference=None):
    fresh_stock_executions = 0
    if stock_reference is not None:
        stock_detail = stage_reference_checkpoint(stock_reference, case_id, "stock", out)
        verify_detail = stage_reference_checkpoint(stock_reference, case_id, "stock-verify", out)
    else:
        stock_detail = detail_checkpoint(STOCK_REPORT, detail_filters, user, page_length, "stock", out, case_id)
        fresh_stock_executions += 1
        if stock_detail.get("schema_version") == "oracle-detail-checkpoint-v1":
            verify_detail = detail_checkpoint(STOCK_REPORT, detail_filters, user, page_length, "stock-verify", out, case_id)
            fresh_stock_executions += 1
        else:
            verify_detail = {"schema_version": "oracle-detail-skipped-v1", "skip_reason": "stock oracle incomplete"}
    if detail_mode == "native-stock":
        candidate_detail = {"schema_version": "oracle-detail-skipped-v1", "skip_reason": "native stock detail mode; Fast detail ordering diagnostic is separate evidence"}
    elif verify_detail.get("schema_version") == "oracle-detail-checkpoint-v1":
        candidate_detail = detail_checkpoint(CANDIDATE_REPORT, detail_filters, user, page_length, "candidate", out, case_id)
    else:
        candidate_detail = {"schema_version": "oracle-detail-skipped-v1", "skip_reason": "stock verification incomplete"}
    stock_complete = stock_detail.get("schema_version") == "oracle-detail-checkpoint-v1"
    verify_complete = verify_detail.get("schema_version") == "oracle-detail-checkpoint-v1"
    candidate_complete = candidate_detail.get("schema_version") == "oracle-detail-checkpoint-v1"
    reference_usage = stock_reference_usage(stock_reference, fresh_stock_executions)
    verification_mismatches = []
    verification_page_mismatches = []
    verification_stream = None
    if stock_complete and verify_complete:
        if stock_reference is not None:
            detail_evidence = dict(((stock_reference.manifest.get("independent_verification") or {}).get("detail") or {}))
            rows_hash = detail_evidence.get("stock_streamed_rows_sha256") or detail_evidence.get("rows_sha256")
            verification_stream = {
                "schema_version": "pinned-stock-reference-verification-reuse-v1",
                "comparison_mode": detail_evidence.get("comparison_mode") or "exact-ordered-rows-types-and-first-two-pages",
                "verification_execution": "not rerun on repeat; independently executed archived verification is pinned by the root manifest",
                "passed": detail_evidence.get("passed") is True,
                "validation_mode": detail_evidence.get("validation_mode"),
                "row_count_streamed": detail_evidence.get("row_count_streamed") or detail_evidence.get("row_count"),
                "stock_streamed_rows_sha256": rows_hash,
                "verify_streamed_rows_sha256": detail_evidence.get("verify_streamed_rows_sha256") or rows_hash,
                "stock_artifact_file": {"sha256": detail_evidence.get("stock_rows_artifact_sha256")},
                "verify_artifact_file": {"sha256": detail_evidence.get("verify_rows_artifact_sha256")},
                "mismatches": [],
                "mismatches_truncated": 0,
            }
        else:
            verification_stream = stream_compare_stock_detail_artifacts(
                stock_detail, verify_detail, detail_filters, user, out, page_length,
            )
        verification_mismatches.extend(verification_stream.get("mismatches") or [])
    hybrid_row_limit = 250000
    stock_rows = verify_rows = None
    hybrid_diagnostic = "exact stream comparison is authoritative; hybrid grouping diagnostic bounded to 250000 rows"
    if detail_mode == "candidate-parity" and stock_detail.get("row_count", 0) <= hybrid_row_limit:
        stock_rows = load_checkpoint_rows(stock_detail, detail_filters, user, out, "stock", verification_mismatches, "stock") if stock_complete else None
        verify_rows = load_checkpoint_rows(verify_detail, detail_filters, user, out, "stock-verify", verification_mismatches, "stock-verify") if verify_complete else None
    verification_hybrid_mismatches = []
    verification_hybrid_truncated = 0
    if stock_complete and verify_complete:
        if not stock_detail.get("rows_sha256") or stock_detail["rows_sha256"] != verify_detail.get("rows_sha256"):
            verification_mismatches.append({"kind": "ordered_rows_hash_mismatch"})
        if set(stock_detail["pages"]) != {"page-0", "page-1"} or set(verify_detail["pages"]) != {"page-0", "page-1"}:
            verification_mismatches.append({"kind": "incomplete_detail_pages"})
        if canonical(stock_detail["columns"]) != canonical(verify_detail["columns"]):
            verification_mismatches.append({"kind": "columns_mismatch"})
        if stock_detail["row_count"] != verify_detail["row_count"]:
            verification_mismatches.append({"kind": "row_count_mismatch"})
        if stock_rows is not None and verify_rows is not None:
            verification_hybrid_mismatches, verification_hybrid_truncated = compare_detail_rows_hybrid(stock_rows, verify_rows)
        for page_key in sorted(set(stock_detail["pages"]) | set(verify_detail["pages"])):
            if canonical(stock_detail["pages"].get(page_key)) != canonical(verify_detail["pages"].get(page_key)):
                verification_page_mismatches.append({"kind": "page_mismatch", "page_key": page_key})
    elif not verify_complete:
        verification_mismatches.append({"kind": "stock_verification_incomplete"})
    verification_passed = (
        stock_complete
        and verify_complete
        and not verification_mismatches
        and not verification_page_mismatches
        and not stock_detail.get("pages_error")
        and not verify_detail.get("pages_error")
    )
    write_json(out / "verification-detail.json", {
        "schema_version": "stock-oracle-detail-verification-v2",
        "comparison_mode": "exact-ordered-rows-and-pages",
        "passed": verification_passed,
        "actor": user,
        "stock_checkpoint": f"checkpoint-{case_id}-stock.json",
        "verification_checkpoint": f"checkpoint-{case_id}-stock-verify.json",
        "stock_complete": stock_complete,
        "verification_complete": verify_complete,
        "mismatches": verification_mismatches,
        "hybrid_mismatches": verification_hybrid_mismatches,
        "hybrid_mismatches_truncated": verification_hybrid_truncated,
        "hybrid_diagnostic": hybrid_diagnostic,
        "ordered_rows_hash_equal": stock_detail.get("rows_sha256") == verify_detail.get("rows_sha256"),
        "page_mismatches": verification_page_mismatches,
        "bounded_memory_exact_ordered": verification_stream,
        "stock_reference": reference_usage,
    })
    del verify_rows
    gc.collect()
    candidate_mismatches = []
    candidate_stream = None
    candidate_rows = None
    candidate_hybrid_mismatches = []
    candidate_hybrid_truncated = 0
    page_mismatches = []
    if stock_complete and candidate_complete:
        candidate_stream = stream_compare_candidate_detail_artifacts(
            stock_detail, candidate_detail, detail_filters, user, out, page_length,
        )
        candidate_mismatches.extend(candidate_stream.get("mismatches") or [])
        if not stock_detail.get("rows_sha256") or stock_detail["rows_sha256"] != candidate_detail.get("rows_sha256"):
            candidate_mismatches.append({"kind": "ordered_rows_hash_mismatch"})
        if set(stock_detail["pages"]) != {"page-0", "page-1"} or set(candidate_detail["pages"]) != {"page-0", "page-1"}:
            candidate_mismatches.append({"kind": "incomplete_detail_pages"})
        if canonical(stock_detail["columns"]) != canonical(candidate_detail["columns"]):
            candidate_mismatches.append({"kind": "columns_mismatch"})
        if stock_detail["row_count"] != candidate_detail["row_count"]:
            candidate_mismatches.append({"kind": "row_count_mismatch"})
        if stock_detail.get("row_count", 0) <= hybrid_row_limit:
            candidate_rows = load_checkpoint_rows(candidate_detail, detail_filters, user, out, "candidate", candidate_mismatches, "candidate")
            if stock_rows is not None and candidate_rows is not None:
                candidate_hybrid_mismatches, candidate_hybrid_truncated = compare_detail_rows_hybrid(stock_rows, candidate_rows)
        for page_key in sorted(set(stock_detail["pages"]) | set(candidate_detail["pages"])):
            if canonical(stock_detail["pages"].get(page_key)) != canonical(candidate_detail["pages"].get(page_key)):
                page_mismatches.append({"kind": "page_mismatch", "page_key": page_key})
    if detail_mode == "native-stock":
        detail_passed = bool(stock_complete and verification_passed and not stock_detail["pages_error"])
    else:
        detail_passed = (
            stock_complete
            and verification_passed
            and candidate_complete
            and not candidate_mismatches
            and not page_mismatches
            and not stock_detail["pages_error"]
            and not candidate_detail["pages_error"]
        )
    detail_artifact = {
        "schema_version": "native-stock-detail-verification-v1" if detail_mode == "native-stock" else "candidate-stock-detail-comparison-v3",
        "detail_mode": detail_mode,
        "comparison_mode": "exact-ordered-rows-types-and-pages-with-exact-json-types",
        "diagnostic_grouping_only": "posting_date order must not invert; same-date invoice/child permutation is tolerated only as hybrid diagnostic and never relaxes exact ordered parity",
        "passed": detail_passed,
        "stock_verification_passed": verification_passed,
        "verification_checkpoint": f"checkpoint-{case_id}-stock-verify.json",
        "actor": user,
        "candidate_report": CANDIDATE_REPORT,
        "stock_report": STOCK_REPORT,
        "candidate_commit": CANDIDATE_COMMIT,
        "stock_checkpoint": f"checkpoint-{case_id}-stock.json",
        "candidate_checkpoint": f"checkpoint-{case_id}-candidate.json",
        "stock_complete": stock_complete,
        "candidate_complete": candidate_complete,
        "candidate_execution": "fresh-worker-every-repeat" if candidate_complete else "not-run-in-native-stock-mode",
        "fresh_stock_oracle_executions": reference_usage.get("fresh_stock_oracle_executions"),
        "stock": {k: v for k, v in stock_detail.items() if k not in {"pages", "filters"} and not k.startswith("_")},
        "candidate": {k: v for k, v in candidate_detail.items() if k not in {"pages", "filters"} and not k.startswith("_")},
        "mismatches": candidate_mismatches,
        "hybrid_mismatches": candidate_hybrid_mismatches,
        "hybrid_mismatches_truncated": candidate_hybrid_truncated,
        "hybrid_diagnostic": hybrid_diagnostic,
        "page_mismatches": page_mismatches,
        "ordered_rows_hash_equal": stock_detail.get("rows_sha256") == candidate_detail.get("rows_sha256"),
        "bounded_memory_candidate_exact_ordered": candidate_stream,
        "exact_ordered_full_rows_proof": "required and enforced: exact columns, row count, canonical ordered rows SHA-256, ordered value equality, exact JSON types, and first two pages",
        "stock_reference": reference_usage,
    }
    write_json(out / "detail-comparison.json", detail_artifact)
    if stock_complete:
        write_json(out / "detail-stock-pages.json", {
            "schema_version": "stock-detail-pages-v1",
            "actor": user,
            "report": STOCK_REPORT,
            "columns": stock_detail["columns"],
            "pages": stock_detail["pages"],
        })
    if candidate_complete:
        write_json(out / "detail-candidate-pages.json", {
            "schema_version": "candidate-detail-pages-v1",
            "actor": user,
            "report": CANDIDATE_REPORT,
            "candidate_commit": CANDIDATE_COMMIT,
            "columns": candidate_detail["columns"],
            "pages": candidate_detail["pages"],
        })
    del candidate_rows, stock_rows
    gc.collect()
    return detail_artifact


def compare_summaries(report_filters, user, group_by, out, stock_reference=None):
    """Compare a fresh candidate summary with stock, cold or through pinned reference evidence.

    Cold mode executes stock twice as before. Reference mode stages byte-identical,
    independently verified stock checkpoints and still executes the candidate in a
    fresh worker for every repeat.
    """
    case_id = f"summary-{group_by.lower()}"
    filters = dict(report_filters)
    filters["group_by"] = group_by
    checkpoints = {}
    fresh_stock_executions = 0

    if stock_reference is not None:
        checkpoints["stock"] = stage_reference_checkpoint(stock_reference, case_id, "stock", out)
        checkpoints["stock-verify"] = stage_reference_checkpoint(stock_reference, case_id, "stock-verify", out)
    else:
        checkpoints["stock"] = run_report_worker(
            STOCK_REPORT, filters, user, 50, "stock", out, case_id, "summary"
        )
        fresh_stock_executions += 1
        gc.collect()
        if checkpoints["stock"].get("schema_version") == "oracle-report-checkpoint-v1":
            checkpoints["stock-verify"] = run_report_worker(
                STOCK_REPORT, filters, user, 50, "stock-verify", out, case_id, "summary"
            )
            fresh_stock_executions += 1
            gc.collect()
    if checkpoints.get("stock-verify", {}).get("schema_version") == "oracle-report-checkpoint-v1":
        checkpoints["candidate"] = run_report_worker(
            CANDIDATE_REPORT, filters, user, 50, "candidate", out, case_id, "summary"
        )
        gc.collect()

    stock_checkpoint = checkpoints.get("stock") or {}
    verify_checkpoint = checkpoints.get("stock-verify") or {}
    candidate_checkpoint = checkpoints.get("candidate") or {}
    stock_complete = stock_checkpoint.get("schema_version") == "oracle-report-checkpoint-v1"
    verify_complete = verify_checkpoint.get("schema_version") == "oracle-report-checkpoint-v1"
    candidate_complete = candidate_checkpoint.get("schema_version") == "oracle-report-checkpoint-v1"
    reference_usage = stock_reference_usage(stock_reference, fresh_stock_executions)
    verification_mismatches = []
    if stock_complete and verify_complete:
        verification_mismatches = compare_exact_parts(
            verify_checkpoint.get("columns") or [], verify_checkpoint.get("rows") or [],
            stock_checkpoint.get("columns") or [], stock_checkpoint.get("rows") or [],
        )
    elif not verify_complete:
        verification_mismatches.append({"kind": "stock_verification_incomplete"})
    verification_passed = stock_complete and verify_complete and not verification_mismatches
    verification_artifact = {
        "schema_version": "stock-oracle-summary-verification-v1",
        "passed": verification_passed,
        "group_by": group_by,
        "actor": user,
        "stock_checkpoint": f"checkpoint-{case_id}-stock.json",
        "verification_checkpoint": f"checkpoint-{case_id}-stock-verify.json",
        "stock_complete": stock_complete,
        "verification_complete": verify_complete,
        "mismatches": verification_mismatches,
        "stock_reference": reference_usage,
    }
    write_json(out / f"verification-summary-{group_by.lower()}.json", verification_artifact)
    mismatches = []
    if stock_complete and candidate_complete:
        mismatches = compare_exact_parts(
            candidate_checkpoint.get("columns") or [], candidate_checkpoint.get("rows") or [],
            stock_checkpoint.get("columns") or [], stock_checkpoint.get("rows") or [],
        )
    elif not stock_complete:
        mismatches.append({"kind": "stock_oracle_incomplete"})
    elif not candidate_complete:
        mismatches.append({"kind": "candidate_incomplete"})
    passed = stock_complete and verification_passed and candidate_complete and not mismatches
    artifact = {
        "schema_version": "candidate-stock-summary-comparison-v2",
        "passed": passed,
        "candidate_stock_equal": stock_complete and candidate_complete and not mismatches,
        "stock_verification_passed": verification_passed,
        "group_by": group_by,
        "actor": user,
        "candidate_report": CANDIDATE_REPORT,
        "stock_report": STOCK_REPORT,
        "candidate_commit": CANDIDATE_COMMIT,
        "stock_checkpoint": f"checkpoint-{case_id}-stock.json",
        "candidate_checkpoint": f"checkpoint-{case_id}-candidate.json",
        "stock_complete": stock_complete,
        "candidate_complete": candidate_complete,
        "candidate_execution": "fresh-worker-every-repeat",
        "fresh_stock_oracle_executions": reference_usage.get("fresh_stock_oracle_executions"),
        "candidate_elapsed_seconds": candidate_checkpoint.get("elapsed_seconds"),
        "stock_elapsed_seconds": stock_checkpoint.get("elapsed_seconds"),
        "stock_elapsed_is_repeat_measurement": stock_reference is None,
        "candidate_row_count": candidate_checkpoint.get("row_count"),
        "stock_row_count": stock_checkpoint.get("row_count"),
        "mismatches": mismatches,
        "stock_reference": reference_usage,
    }
    write_json(out / f"summary-{group_by.lower()}.json", artifact)
    del checkpoints, stock_checkpoint, candidate_checkpoint
    gc.collect()
    return artifact


def preflight_clone(company, from_date, to_date, actors, out):
    """Verify oracle wiring, candidate path, schema objects, actors, and baseline before timing."""
    from khitmee_ops.setup.indexes import verify_repost_triggers_installed

    module = sys.modules.get("khitmee_ops")
    module_file = getattr(module, "__file__", "") if module else ""
    tables = {}
    for table in ("tabDaily Profit by Shop", "tabKhitmee Profit Day State", "tabKhitmee Profit Replay Candidate", "tabKhitmee Profit Dirty Range"):
        rows = frappe.db.sql("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        tables[table] = bool(rows and int(rows[0][0] or 0))
    triggers_ok, missing_triggers = verify_repost_triggers_installed()
    baseline = frappe.db.sql(
        """
        SELECT COUNT(*), MIN(posting_date), MAX(posting_date)
        FROM `tabSales Invoice` WHERE company=%s AND docstatus=1
        """,
        (company,),
    )[0]
    sle = frappe.db.sql(
        "SELECT COUNT(*), MIN(posting_date), MAX(posting_date) FROM `tabStock Ledger Entry` WHERE company=%s",
        (company,),
    )[0]
    window_invoice_items = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name=sii.parent
        WHERE si.company=%s AND si.docstatus=1 AND si.posting_date BETWEEN %s AND %s
        """,
        (company, from_date, to_date),
    )[0][0]
    window_sle = frappe.db.sql(
        """
        SELECT COUNT(*) FROM `tabStock Ledger Entry`
        WHERE company=%s AND posting_date BETWEEN %s AND %s
        """,
        (company, from_date, to_date),
    )[0][0]
    actor_rows = {}
    for actor in actors:
        rows = frappe.db.sql("SELECT enabled FROM `tabUser` WHERE name=%s", (actor,), as_dict=True)
        roles = sorted(row[0] for row in frappe.db.sql("SELECT role FROM `tabHas Role` WHERE parent=%s ORDER BY role", (actor,)))
        actor_rows[actor] = {"exists": bool(rows), "enabled": bool(rows and int(rows[0].get("enabled") or 0)), "roles": roles}
        if actor in CREDENTIALS_BY_USER:
            authenticate(frappe, actor, CREDENTIALS_BY_USER[actor]["password"])
            actor_rows[actor]["password_verified"] = True
    try:
        import erpnext
        report_module = importlib.import_module("erpnext.accounts.report.gross_profit.gross_profit")
        report_source_file = inspect.getsourcefile(report_module) or ""
        report_source = {
            "module": "erpnext.accounts.report.gross_profit.gross_profit",
            "source_file": report_source_file,
            "source_sha256": sha256_file(report_source_file) if report_source_file and Path(report_source_file).is_file() else None,
            "erpnext_version": getattr(erpnext, "__version__", ""),
            "frappe_version": getattr(frappe, "__version__", ""),
        }
    except Exception as exc:
        report_source = {"module": "erpnext.accounts.report.gross_profit.gross_profit", "error": f"{type(exc).__name__}: {exc}"}
    def effective_precision(name, fallback):
        raw = frappe.db.get_default(name)
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            value = 0
        return value or fallback

    precision_settings = {
        "currency_precision_default": frappe.db.get_default("currency_precision"),
        "float_precision_default": frappe.db.get_default("float_precision"),
        "currency_precision_effective": effective_precision("currency_precision", 3),
        "float_precision_effective": effective_precision("float_precision", 2),
        "rounding_method": frappe.db.get_default("rounding_method") or "Banker's Rounding (legacy)",
        "currency": frappe.db.get_default("currency"),
        "comparison_contract": "exact JSON value/type equality; no numeric tolerance",
    }
    artifact = {
        "schema_version": "candidate-clone-preflight-v1",
        "site": frappe.local.site,
        "db_host": str(getattr(frappe.conf, "db_host", "")),
        "db_port": str(getattr(frappe.conf, "db_port", "")),
        "redis_cache": str(getattr(frappe.conf, "redis_cache", "")),
        "redis_queue": str(getattr(frappe.conf, "redis_queue", "")),
        "candidate_module_file": module_file,
        "candidate_module_path_ok": "/home/frappe/frappe-bench/apps/khitmee_ops" in module_file,
        "profit_summary_tables": tables,
        "repost_triggers_installed": triggers_ok,
        "missing_repost_triggers": missing_triggers,
        "actors": actor_rows,
        "report_source": report_source,
        "precision_settings": precision_settings,
        "financial_baseline": {
            "company": company,
            "submitted_sales_invoices": int(baseline[0] or 0),
            "sales_invoice_min_posting_date": str(baseline[1] or ""),
            "sales_invoice_max_posting_date": str(baseline[2] or ""),
            "stock_ledger_entries": int(sle[0] or 0),
            "stock_ledger_min_posting_date": str(sle[1] or ""),
            "stock_ledger_max_posting_date": str(sle[2] or ""),
            "requested_from_date": from_date,
            "requested_to_date": to_date,
            "requested_window_sales_invoice_items": int(window_invoice_items or 0),
            "requested_window_stock_ledger_entries": int(window_sle or 0),
        },
    }
    artifact["passed"] = (
        artifact["candidate_module_path_ok"]
        and all(tables.values())
        and triggers_ok
        and all(row.get("exists") and row.get("enabled") and row.get("password_verified") for row in actor_rows.values())
        and artifact["financial_baseline"]["submitted_sales_invoices"] > 0
        and artifact["financial_baseline"]["stock_ledger_entries"] > 0
        and bool(report_source.get("source_sha256"))
        and artifact.get("precision_settings", {}).get("comparison_contract") == "exact JSON value/type equality; no numeric tolerance"
    )
    write_json(out / "clone-preflight.json", artifact)
    if not artifact["passed"]:
        raise RuntimeError("disposable clone preflight failed; see clone-preflight.json")
    return artifact


ORIGINAL_EXPORT_ENQUEUE_METHOD = "khitmee_ops.profit_summary.export_service.enqueue_profit_csv_export"
ORIGINAL_EXPORT_DOWNLOAD_METHOD = "khitmee_ops.profit_summary.export_service.download_profit_csv_export"
ACTUAL_EXPORT_ENQUEUE_METHOD = "khitmee_ops.api.profit_export.enqueue_export"
ACTUAL_EXPORT_DOWNLOAD_METHOD = "khitmee_ops.api.profit_export.download_export"
EXPORT_DOCTYPE = "Khitmee Profit Export Log"
EXPORT_TABLE = "tabKhitmee Profit Export Log"


def describe_method_surface(dotted_path):
    """Describe whether a dotted Python method exists and is exposed through Frappe RPC."""
    surface = {"method": dotted_path, "exists": False, "whitelisted": False}
    try:
        module_name, function_name = dotted_path.rsplit(".", 1)
    except ValueError:
        surface["error"] = "invalid dotted method path"
        return surface
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        surface["import_error"] = f"{type(exc).__name__}: {exc}"
        return surface
    function = getattr(module, function_name, None)
    surface["module_file"] = getattr(module, "__file__", "")
    if function is None:
        return surface
    surface["exists"] = True
    surface["callable"] = callable(function)
    try:
        surface["source_file"] = inspect.getsourcefile(function)
        surface["source_line"] = inspect.getsourcelines(function)[1]
    except Exception:
        pass
    surface["whitelisted"] = function in (getattr(frappe, "whitelisted", None) or set())
    return surface


def sql_count(query, params=()):
    try:
        rows = frappe.db.sql(query, params)
        return int(rows[0][0] if rows else 0)
    except Exception:
        return None


def assess_export_capability(filters, user, out):
    """Independently assess the original workload's export surface without running export load.

    The year oracle intentionally has no web worker or long-queue worker. This assessment
    therefore separates implementation exposure/permissions from runtime execution
    dependencies and never treats alternate endpoint names as the original workload.
    """
    original_enqueue = describe_method_surface(ORIGINAL_EXPORT_ENQUEUE_METHOD)
    original_download = describe_method_surface(ORIGINAL_EXPORT_DOWNLOAD_METHOD)
    actual_enqueue = describe_method_surface(ACTUAL_EXPORT_ENQUEUE_METHOD)
    actual_download = describe_method_surface(ACTUAL_EXPORT_DOWNLOAD_METHOD)

    table_count = sql_count(
        "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (EXPORT_TABLE,),
    )
    doctype_count = sql_count("SELECT COUNT(*) FROM `tabDocType` WHERE name=%s", (EXPORT_DOCTYPE,))

    permission_probe = {"attempted": False, "passed": False}
    credential = CREDENTIALS_BY_USER.get(user)
    if not credential:
        permission_probe["error"] = "pinned export actor credential missing"
    else:
        try:
            authenticate(frappe, user, credential["password"])
            from khitmee_ops.profit_summary.eligibility import authorize_profit_request

            authorize_profit_request(filters, user, require_export=True)
            permission_probe.update({"attempted": True, "passed": True})
        except Exception as exc:
            permission_probe.update({"attempted": True, "error_type": type(exc).__name__, "error": str(exc)})

    implementation_dependencies = []
    if not original_enqueue.get("exists"):
        implementation_dependencies.append({
            "dependency": "original enqueue implementation missing",
            "method": ORIGINAL_EXPORT_ENQUEUE_METHOD,
            "detail": original_enqueue.get("import_error") or "function not present",
        })
    elif not original_enqueue.get("whitelisted"):
        implementation_dependencies.append({
            "dependency": "original enqueue method is not exposed through Frappe RPC",
            "method": ORIGINAL_EXPORT_ENQUEUE_METHOD,
            "detail": "function exists but is absent from frappe.whitelisted; use the whitelisted API wrapper or decorate this exact method",
        })
    if not original_download.get("exists"):
        implementation_dependencies.append({
            "dependency": "original download implementation missing",
            "method": ORIGINAL_EXPORT_DOWNLOAD_METHOD,
            "detail": original_download.get("import_error") or "function not present in khitmee_ops.profit_summary.export_service",
        })
    elif not original_download.get("whitelisted"):
        implementation_dependencies.append({
            "dependency": "original download method is not exposed through Frappe RPC",
            "method": ORIGINAL_EXPORT_DOWNLOAD_METHOD,
            "detail": "function exists but is absent from frappe.whitelisted",
        })
    if table_count == 0:
        implementation_dependencies.append({
            "dependency": "export log table missing",
            "method": EXPORT_TABLE,
            "detail": "the Khitmee Profit Export Log storage table is absent in the pinned clone",
        })
    if doctype_count == 0:
        implementation_dependencies.append({
            "dependency": "export log DocType metadata missing",
            "method": EXPORT_DOCTYPE,
            "detail": "the pinned site lacks the DocType metadata required by frappe.get_doc/insert",
        })

    original_surface_ready = bool(
        original_enqueue.get("exists")
        and original_enqueue.get("whitelisted")
        and original_download.get("exists")
        and original_download.get("whitelisted")
    )
    storage_ready = bool(table_count == 1 and doctype_count == 1)
    artifact = {
        "schema_version": "candidate-export-capability-assessment-v1",
        "created_utc": utc_now(),
        "candidate_commit": CANDIDATE_COMMIT,
        "actor": user,
        "filters": canonicalize(filters),
        "original_workload_methods": {
            "enqueue": original_enqueue,
            "download": original_download,
        },
        "whitelisted_alternate_methods_not_original_workload": {
            "enqueue": actual_enqueue,
            "download": actual_download,
        },
        "permission_probe": permission_probe,
        "storage": {
            "export_table": EXPORT_TABLE,
            "export_table_present": table_count == 1,
            "export_table_count": table_count,
            "export_doctype": EXPORT_DOCTYPE,
            "export_doctype_present": doctype_count == 1,
            "export_doctype_count": doctype_count,
        },
        "runtime_dependencies": {
            "frappe_web_rpc_service_required": True,
            "frappe_long_queue_worker_required": True,
            "available_in_year_oracle_comparator": False,
            "detail": "the disposable comparator starts MariaDB and Redis only; no original-workload HTTP service or long-queue worker is present",
        },
        "implementation_dependencies": implementation_dependencies,
        "qualified_for_original_workload": bool(original_surface_ready and storage_ready and permission_probe.get("passed")),
        "qualification_boundary": "export is assessed independently; alternate whitelisted endpoints are diagnostic and are not relabeled as the configured original workload",
    }
    write_json(Path(out) / "export-qualification.json", artifact)
    return artifact


def bootstrap_read_model(company, from_date, to_date, out, batch_days=30):
    from khitmee_ops.profit_summary import rebuilder, store
    from khitmee_ops.setup.indexes import setup_database_objects

    started = time.monotonic()
    setup_result = setup_database_objects(rebuild_item_search=False)
    frappe.db.commit()

    # The disposable clone starts without day states. One compact dirty range is the
    # candidate's own backfill mechanism; repeated processor invocations expand it in
    # bounded batches and publish each historical day through the normal CAS path.
    # If the requested span is already fully clean, do not dirty and rebuild it again.
    from datetime import date
    start_d = date.fromisoformat(str(from_date)[:10])
    end_d = date.fromisoformat(str(to_date)[:10])
    expected_days = (end_d - start_d).days + 1
    rows = frappe.db.sql(
        """
        SELECT COUNT(DISTINCT posting_date) FROM `tabKhitmee Profit Day State`
        WHERE company=%s AND posting_date BETWEEN %s AND %s AND status='clean'
        """,
        (company, from_date, to_date),
    )
    initial_clean_days = int(rows[0][0] if rows else 0)
    existing = store.get_unprocessed_dirty_ranges(company=company, limit=1)
    if initial_clean_days < expected_days and not existing:
        store.record_dirty_range(company, from_date, "external-bootstrap", CANDIDATE_COMMIT)
        frappe.db.commit()

    calls = 0
    processed = 0
    if initial_clean_days < expected_days or existing:
        while True:
            calls += 1
            processed += int(rebuilder.process_dirty_slices(company=company, max_slices=batch_days) or 0)
            rows = frappe.db.sql(
                """
                SELECT COUNT(*) FROM `tabKhitmee Profit Day State`
                WHERE company=%s AND posting_date BETWEEN %s AND %s AND status!='clean'
                """,
                (company, from_date, to_date),
            )
            unclean = int(rows[0][0] if rows else 0)
            rows = frappe.db.sql(
                """
                SELECT COUNT(DISTINCT posting_date) FROM `tabKhitmee Profit Day State`
                WHERE company=%s AND posting_date BETWEEN %s AND %s AND status='clean'
                """,
                (company, from_date, to_date),
            )
            clean_days = int(rows[0][0] if rows else 0)
            active = len(store.get_unprocessed_dirty_ranges(company=company, limit=1))
            if unclean == 0 and clean_days >= expected_days and active == 0:
                break
            if calls >= 40:
                raise RuntimeError(f"bootstrap did not finish after {calls} processor calls; unclean={unclean} clean_days={clean_days}/{expected_days} active_ranges={active}")

    rows = frappe.db.sql(
        """
        SELECT status, COUNT(*) FROM `tabKhitmee Profit Day State`
        WHERE company=%s AND posting_date BETWEEN %s AND %s GROUP BY status
        """,
        (company, from_date, to_date),
        as_dict=True,
    )
    artifact = {
        "schema_version": "candidate-read-model-bootstrap-v1",
        "candidate_commit": CANDIDATE_COMMIT,
        "company": company,
        "from_date": from_date,
        "to_date": to_date,
        "processor_calls": calls,
        "processor_slices_returned": processed,
        "expected_days": expected_days,
        "initial_clean_days": initial_clean_days,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "setup_result": setup_result,
        "day_state_counts": {row["status"]: int(row["COUNT(*)"]) for row in rows},
    }
    write_json(out / "read-model-bootstrap.json", artifact)
    return artifact


def offline_validate_detail_run(run_dir, output, page_length):
    """Validate retained detail artifacts without oracle access and write a separate recovery receipt."""
    run_dir = Path(run_dir)
    out = run_dir / "out"
    stock_checkpoint = json.loads((out / "checkpoint-detail-stock.json").read_text(encoding="utf-8"))
    verify_checkpoint = json.loads((out / "checkpoint-detail-stock-verify.json").read_text(encoding="utf-8"))
    if stock_checkpoint.get("snapshot_id") != PINNED_SNAPSHOT_ID or verify_checkpoint.get("snapshot_id") != PINNED_SNAPSHOT_ID:
        raise ValueError("offline recovery requires the corrected pinned 8cc3e00 snapshot")
    detail_filters = stock_checkpoint.get("filters")
    user = stock_checkpoint.get("actor")
    stream = stream_compare_stock_detail_artifacts(stock_checkpoint, verify_checkpoint, detail_filters, user, out, page_length)
    manifest = {}
    manifest_path = run_dir / "out-sha256sums.txt"
    if manifest_path.is_file():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, filename = line.split(None, 1)
                manifest[Path(filename.strip()).name] = digest
    manifest_mismatches = []
    for name, expected_digest in sorted(manifest.items()):
        path = out / name
        if not path.is_file() or sha256_file(path) != expected_digest:
            manifest_mismatches.append({"artifact": name})
    if manifest_mismatches:
        stream["passed"] = False
        stream.setdefault("mismatches", []).append({"kind": "copyback_manifest_digest_mismatch", "entries": manifest_mismatches})
    receipt = {
        "schema_version": "offline-native-stock-detail-recovery-receipt-v1",
        "created_utc": utc_now(),
        "passed": bool(stream.get("passed")),
        "heavy_report_queries_run": 0,
        "oracle_restarted": False,
        "original_run_rewritten": False,
        "run_dir": str(run_dir),
        "output": str(output),
        "scope": {
            "from_date": (detail_filters or {}).get("from_date"),
            "to_date": (detail_filters or {}).get("to_date"),
            "group_by": (detail_filters or {}).get("group_by"),
        },
        "actor": user,
        "snapshot_id": stock_checkpoint.get("snapshot_id"),
        "bounded_memory_exact_ordered": stream,
        "copyback_manifest_entries_validated": len(manifest),
        "fast_detail_rejection_preserved": True,
        "fast_detail_note": "Fast Invoice detail remains rejected from bounded evidence and is not serving evidence.",
        "full_year_note": "Full-year still requires its own execution after the cashier-controlled physical-window handoff.",
    }
    output = Path(output)
    if output.exists():
        raise ValueError(f"offline recovery receipt already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, receipt)
    os.chmod(output, 0o600)
    print(json.dumps({"passed": receipt["passed"], "result": str(output)}, sort_keys=True))
    return 0 if receipt["passed"] else 1


def offline_validate_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-detail-run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--page-length", type=int, default=50)
    args = parser.parse_args()
    return offline_validate_detail_run(args.validate_detail_run, args.output, args.page_length)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--detail-from-date", required=True)
    parser.add_argument("--detail-to-date", required=True)
    parser.add_argument("--warehouse-actor", required=True)
    parser.add_argument("--monthly-actor", required=True)
    parser.add_argument("--detail-actor", required=True)
    parser.add_argument("--export-actor", default="export-requester@seasonal.invalid")
    parser.add_argument("--out", required=True)
    parser.add_argument("--credentials", default="/opt/manager-creds.json")
    parser.add_argument("--page-length", type=int, default=100)
    parser.add_argument("--skip-bootstrap", action="store_true")
    parser.add_argument("--stop-on-mismatch", action="store_true")
    parser.add_argument("--snapshot-id", default="")
    parser.add_argument("--detail-mode", choices=("candidate-parity", "native-stock"), default="candidate-parity")
    parser.add_argument("--stock-reference-manifest", default="")
    parser.add_argument("--stock-reference-cache", default="")
    parser.add_argument("--stock-reference-mismatch", choices=("fail", "cold"), default="fail")
    args = parser.parse_args()
    if bool(args.stock_reference_manifest) != bool(args.stock_reference_cache):
        raise RuntimeError("--stock-reference-manifest and --stock-reference-cache must be supplied together")

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.stock_reference_manifest:
        args.stock_reference_manifest = Path(args.stock_reference_manifest).resolve()
        args.stock_reference_cache = Path(args.stock_reference_cache).resolve()
    global CREDENTIALS_BY_USER, RUNTIME_SITE, RUNTIME_CREDENTIALS, RUNTIME_SNAPSHOT_ID
    RUNTIME_SITE = args.site
    RUNTIME_CREDENTIALS = str(Path(args.credentials).resolve())
    RUNTIME_SNAPSHOT_ID = args.snapshot_id
    raw_credentials = json.loads(Path(RUNTIME_CREDENTIALS).read_text(encoding="utf-8"))
    CREDENTIALS_BY_USER = {entry["user"]: entry for entry in raw_credentials.values() if isinstance(entry, dict) and entry.get("user")}
    if frappe is None:
        raise RuntimeError("oracle comparison requires a bench environment with frappe installed")
    os.chdir("/home/frappe/frappe-bench/sites")
    frappe.init(site=args.site, sites_path="/home/frappe/frappe-bench/sites")
    frappe.connect()
    try:
        actors = [args.warehouse_actor, args.monthly_actor, args.detail_actor]
        # Schema setup is part of preparation, then preflight proves the clone before timing.
        from khitmee_ops.setup.indexes import setup_database_objects
        setup_database_objects(rebuild_item_search=False)
        frappe.db.commit()
        preflight = preflight_clone(args.company, args.from_date, args.to_date, actors, out)
        stock_reference = None
        stock_reference_state = "not-requested-fresh-double-stock-oracle"
        if args.stock_reference_manifest:
            try:
                stock_reference = validate_reference_manifest(args.stock_reference_manifest, args.stock_reference_cache)
                if stock_reference.manifest_sha256 not in {
                    "559395d13881f4ed1bed21a009673d1f72678af381e0febb4150d38a2d4ee144",
                    "f80bee2464ec7d2342b7be22db5d6ef579f0d7423e8a7cbaf5f0e410e93b664b",
                }:
                    raise ReferenceValidationError("manifest is not one of the verified frozen stock references")
                runtime_validation = validate_runtime_match(
                    stock_reference,
                    site=args.site,
                    company=args.company,
                    summary_from_date=args.from_date,
                    summary_to_date=args.to_date,
                    detail_from_date=args.detail_from_date,
                    detail_to_date=args.detail_to_date,
                    warehouse_actor=args.warehouse_actor,
                    monthly_actor=args.monthly_actor,
                    detail_actor=args.detail_actor,
                    snapshot_id=args.snapshot_id,
                    preflight=preflight,
                    page_length=args.page_length,
                )
                if not runtime_validation.get("passed"):
                    raise ReferenceValidationError("runtime provenance mismatch: " + json.dumps(runtime_validation.get("mismatches"), sort_keys=True))
                write_reference_decision(out, True, "runtime-provenance-and-artifact-hashes", "reuse-pinned-reference", runtime_validation, stock_reference)
                stock_reference_state = "reused-hash-pinned-reference"
            except Exception as exc:
                detail = {"error_type": type(exc).__name__, "error": str(exc)}
                if args.stock_reference_mismatch == "cold":
                    write_reference_decision(out, False, "runtime-provenance-and-artifact-hashes", "rejected-run-cold", detail, stock_reference)
                    stock_reference = None
                    stock_reference_state = "reference-rejected-run-cold"
                else:
                    write_reference_decision(out, False, "runtime-provenance-and-artifact-hashes", "rejected-fail-closed", detail, stock_reference)
                    raise RuntimeError("stock reference failed closed; see stock-reference-validation.json") from exc
        bootstrap = None
        if not args.skip_bootstrap:
            bootstrap = bootstrap_read_model(args.company, args.from_date, args.to_date, out)

        base_filters = {"company": args.company, "from_date": args.from_date, "to_date": args.to_date}
        warehouse = compare_summaries(base_filters, args.warehouse_actor, "Warehouse", out, stock_reference=stock_reference)
        monthly = None
        detail_artifact = None
        stop_reason = None
        skipped_units = []

        def failed_unit_stop(unit, artifact):
            if not args.stop_on_mismatch or artifact["passed"]:
                return None
            if (
                artifact.get("stock_complete")
                and artifact.get("stock_verification_passed")
                and artifact.get("candidate_complete")
                and not artifact.get("candidate_stock_equal")
            ):
                return f"{unit}_candidate_real_mismatch"
            return f"{unit}_incomplete_or_stock_unverified"

        stop_reason = failed_unit_stop("warehouse", warehouse)
        if stop_reason:
            skipped_units.extend(["monthly", "detail"])
        else:
            monthly = compare_summaries(base_filters, args.monthly_actor, "Monthly", out, stock_reference=stock_reference)
            stop_reason = failed_unit_stop("monthly", monthly)
            if stop_reason:
                skipped_units.append("detail")

        export_filters = dict(base_filters)
        export_filters.update({"from_date": args.detail_from_date, "to_date": args.detail_to_date, "group_by": "Invoice"})
        export_assessment = assess_export_capability(export_filters, args.export_actor, out)
        if not stop_reason:
            detail_filters = dict(export_filters)
            detail_artifact = compare_detail(detail_filters, args.detail_actor, args.page_length, out, detail_mode=args.detail_mode, stock_reference=stock_reference)

        detail_passed = bool(detail_artifact and detail_artifact["passed"])
        verdict = {
            "schema_version": "candidate-stock-oracle-verdict-v1",
            "passed": bool(warehouse["passed"] and monthly and monthly["passed"] and detail_passed),
            "warehouse_passed": warehouse["passed"],
            "monthly_passed": bool(monthly and monthly["passed"]),
            "detail_passed": detail_passed,
            "detail_mode": args.detail_mode,
            "export_qualification": {
                "artifact": "export-qualification.json",
                "qualified_for_original_workload": export_assessment.get("qualified_for_original_workload"),
                "implementation_dependency_count": len(export_assessment.get("implementation_dependencies") or []),
                "independent_of_report_verdict": True,
            },
            "stopped_early": bool(stop_reason),
            "stop_reason": stop_reason,
            "skipped_units": skipped_units,
            "candidate_commit": CANDIDATE_COMMIT,
            "stock_reference_state": stock_reference_state,
            "stock_reference": stock_reference.usage_metadata() if stock_reference is not None else {
                "mode": "fresh-double-stock-oracle",
                "boundary": "stock and independent stock verification were executed in this comparator run",
            },
            "repeat_boundary": "cached stock evidence, when present, is valid only for the exact pinned immutable fixture/filter/actor/report case; candidate work is fresh, while dynamic writes, voucher submission, integration availability, timing, and fresh-stock repeatability remain outside cache claims",
            "bootstrap": bootstrap,
            "scope": {
                "summary_from_date": args.from_date,
                "summary_to_date": args.to_date,
                "detail_from_date": args.detail_from_date,
                "detail_to_date": args.detail_to_date,
                "detail_pages": "first two ordered pages only are persisted; full ordered equality is proven by count+hash+exact JSON type comparison",
            },
        }
        write_json(out / "candidate-stock-verdict.json", verdict)
        print(json.dumps(verdict, sort_keys=True))
        return 0 if verdict["passed"] else 1
    finally:
        if frappe is not None:
            frappe.destroy()


if __name__ == "__main__":
    if "--validate-detail-run" in sys.argv:
        raise SystemExit(offline_validate_main())
    raise SystemExit(worker_main() if "--worker" in sys.argv else main())
