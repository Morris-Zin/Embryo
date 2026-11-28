#!/usr/bin/env python3
"""Build, validate, and load hash-pinned stock-oracle reference evidence.

The manifest is the trust root and must remain outside the artifact cache it
pins.  Artifact bytes are never trusted because a caller says they are the
right fixture: every reused byte is hashed, and the coverage/provenance chain
must match the immutable year fixture exactly.
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from manager_offload import canonical_json_update

MANIFEST_SCHEMA_VERSION = "stock-oracle-reference-manifest-v1"
STOCK_REPORT = "Gross Profit"
PINNED_FIXTURE_SHA256 = "0e9188bff3dbbfca937ae88451e2738c8da4d8780308e657cf58eb9c8af8e3dd"
PINNED_SEED_MANIFEST_SHA256 = "1811b91867d80e5afe8d44f43d5816efa9f171940cc368a97a9c9fab393d9334"
PINNED_SITE = "bench-loadtest.localhost"
PINNED_COMPANY = "Khit Me Distribution"
PINNED_ERPNEXT_IMAGE = "frappe/erpnext:v16.60.0"
PINNED_ERPNEXT_IMAGE_ID_PREFIX = "sha256:3847babae025dae001e638b7fc5503c944fd968216b3074e60edfcc240fb2db6"
PINNED_REPORT_SOURCE = {
    "module": "erpnext.accounts.report.gross_profit.gross_profit",
    "source_file": "/home/frappe/frappe-bench/apps/erpnext/erpnext/accounts/report/gross_profit/gross_profit.py",
    "source_sha256": "fff7460f8e9609ab89a27f96c26af903c18ec6564b60e08702ad672d0bc11cce",
    "erpnext_version": "16.60.0",
    "frappe_version": "16.60.2",
}
PINNED_PRECISION_SETTINGS = {
    "currency_precision_effective": 3,
    "float_precision_effective": 2,
    "rounding_method": "Banker's Rounding (legacy)",
    "comparison_contract": "exact JSON value/type equality; no numeric tolerance",
    "identity_basis": "effective precision semantics pinned to the exact fixture database bytes and archived exact report output; raw default values are captured at runtime but are not semantically significant",
}
DETAIL_ROWS_FORMAT = "oracle-detail-rows-ndjson-gzip-v1"
SUMMARY_GROUPS = ("Warehouse", "Monthly")


class ReferenceValidationError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_data(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_sha256(value):
    return hashlib.sha256(canonical_data(value).encode("utf-8")).hexdigest()


def sorted_json_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def require(condition, message):
    if not condition:
        raise ReferenceValidationError(message)


def safe_artifact_relative(path):
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        raise ReferenceValidationError(f"unsafe artifact path: {path!r}")
    parts = Path(path).parts
    if any(part in ("", ".", "..") for part in parts):
        raise ReferenceValidationError(f"unsafe artifact path: {path!r}")
    return path


def type_shape(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return ["list", [type_shape(item) for item in value]]
    if isinstance(value, dict):
        return ["dict", {str(key): type_shape(value[key]) for key in sorted(value, key=str)}]
    return type(value).__name__


def exact_typed_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(exact_typed_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(exact_typed_equal(a, b) for a, b in zip(left, right))
    return left == right


def artifact_permissions(path):
    return f"{stat.S_IMODE(Path(path).stat().st_mode):04o}"


def artifact_entry(path, relative, kind, source=None, required=True):
    path = Path(path)
    relative = safe_artifact_relative(relative)
    if not path.is_file():
        if required:
            raise ReferenceValidationError(f"required artifact missing: {path}")
        return None
    if path.is_symlink():
        raise ReferenceValidationError(f"artifact must not be a symlink: {path}")
    entry = {
        "path": relative,
        "kind": kind,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "permissions": artifact_permissions(path),
    }
    if source is not None:
        entry["source_path"] = str(source)
    return entry


def copy_artifact(src, cache_root, relative, kind, entries, required=True):
    src = Path(src)
    cache_root = Path(cache_root)
    relative = safe_artifact_relative(relative)
    if not src.is_file():
        if required:
            raise ReferenceValidationError(f"required source artifact missing: {src}")
        return None
    if src.is_symlink():
        raise ReferenceValidationError(f"source artifact must not be a symlink: {src}")
    dst = cache_root / relative
    if dst.exists():
        raise ReferenceValidationError(f"refusing to overwrite cache artifact: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, stat.S_IMODE(src.stat().st_mode))
    entry = artifact_entry(dst, relative, kind, source=src, required=required)
    entries.append(entry)
    return entry


def validate_common_checkpoint(checkpoint, implementation, actor, filters, snapshot_id, require_snapshot=True):
    require(checkpoint.get("schema_version") in {"oracle-report-checkpoint-v1", "oracle-detail-checkpoint-v1"}, f"{implementation}: unsupported checkpoint schema")
    require(checkpoint.get("implementation") == implementation, f"{implementation}: checkpoint implementation mismatch")
    require(checkpoint.get("report") == STOCK_REPORT, f"{implementation}: checkpoint report mismatch")
    require(checkpoint.get("actor") == actor, f"{implementation}: checkpoint actor mismatch")
    require(canonical_data(checkpoint.get("filters")) == canonical_data(filters), f"{implementation}: checkpoint filters mismatch")
    checkpoint_snapshot = checkpoint.get("snapshot_id")
    if checkpoint_snapshot is None and not require_snapshot:
        return
    require(checkpoint_snapshot == snapshot_id, f"{implementation}: checkpoint snapshot mismatch")


def validate_summary_checkpoint_hashes(checkpoint, label):
    require(isinstance(checkpoint.get("columns"), list), f"{label}: columns are not a list")
    require(isinstance(checkpoint.get("rows"), list), f"{label}: rows are not a list")
    require(checkpoint.get("row_count") == len(checkpoint["rows"]), f"{label}: row count does not match embedded rows")
    require(checkpoint.get("columns_sha256") == sorted_json_sha256(checkpoint["columns"]), f"{label}: columns sha256 mismatch")
    require(checkpoint.get("rows_sha256") == sorted_json_sha256(checkpoint["rows"]), f"{label}: rows sha256 mismatch")


def validate_summary_pair(cache_root, company, scope, group, actors, snapshot_id):
    case_id = f"summary-{group.lower()}"
    stock = load_json(Path(cache_root) / "out" / f"checkpoint-{case_id}-stock.json")
    verify = load_json(Path(cache_root) / "out" / f"checkpoint-{case_id}-stock-verify.json")
    actor = actors[group.lower()]
    filters = {"company": company, "from_date": scope["from_date"], "to_date": scope["to_date"], "group_by": group}
    validate_common_checkpoint(stock, "stock", actor, filters, snapshot_id, require_snapshot=False)
    validate_common_checkpoint(verify, "stock-verify", actor, filters, snapshot_id, require_snapshot=False)
    if stock.get("snapshot_id") is not None or verify.get("snapshot_id") is not None:
        require(stock.get("snapshot_id") == verify.get("snapshot_id") == snapshot_id, f"{group}: inconsistent checkpoint snapshots")
    validate_summary_checkpoint_hashes(stock, f"{group}/stock")
    validate_summary_checkpoint_hashes(verify, f"{group}/stock-verify")
    require(canonical_data(stock["columns"]) == canonical_data(verify["columns"]), f"{group}: stock verification columns mismatch")
    require(canonical_data(stock["rows"]) == canonical_data(verify["rows"]), f"{group}: stock verification ordered rows mismatch")
    require(type_shape([stock["columns"], stock["rows"]]) == type_shape([verify["columns"], verify["rows"]]), f"{group}: stock verification type mismatch")
    verification_path = Path(cache_root) / "out" / f"verification-summary-{group.lower()}.json"
    verification = load_json(verification_path)
    require(verification.get("schema_version") == "stock-oracle-summary-verification-v1", f"{group}: unsupported verification schema")
    require(verification.get("passed") is True, f"{group}: archived independent verification did not pass")
    require(verification.get("stock_complete") is True and verification.get("verification_complete") is True, f"{group}: verification incomplete")
    require(verification.get("mismatches") == [], f"{group}: archived verification has mismatches")
    return {
        "group_by": group,
        "actor": actor,
        "filters": filters,
        "row_count": stock["row_count"],
        "columns_sha256": stock["columns_sha256"],
        "rows_sha256": stock["rows_sha256"],
        "verification_artifact": f"out/verification-summary-{group.lower()}.json",
        "type_exact": True,
        "ordered_exact": True,
    }


def validate_detail_file_identity(path, checkpoint):
    path = Path(path)
    require(path.is_file(), f"detail rows artifact missing: {path}")
    require(checkpoint.get("rows_artifact_format") == DETAIL_ROWS_FORMAT, "unsupported detail rows artifact format")
    require(checkpoint.get("rows_artifact_permissions") == "0600", "detail checkpoint does not pin 0600 permissions")
    if os.name != "nt":
        require(artifact_permissions(path) == "0600", f"detail rows artifact permissions are not 0600: {path}")
    require(sha256_file(path) == checkpoint.get("rows_artifact_sha256"), "detail rows artifact sha256 mismatch")
    require(path.stat().st_size == checkpoint.get("rows_artifact_bytes"), "detail rows artifact byte size mismatch")


def open_detail_rows(path, checkpoint, filters, snapshot_id, actor, implementation):
    validate_detail_file_identity(path, checkpoint)
    stream = gzip.open(path, "rt", encoding="utf-8")
    try:
        first = stream.readline()
        require(bool(first), "detail rows artifact is empty")
        meta = json.loads(first)
        require(meta.get("schema_version") == "oracle-detail-rows-v1", "detail rows metadata schema mismatch")
        require(meta.get("report") == STOCK_REPORT, "detail rows report mismatch")
        require(meta.get("implementation") == implementation, "detail rows implementation mismatch")
        require(meta.get("actor") == actor and checkpoint.get("actor") == actor, "detail rows actor mismatch")
        require(canonical_data(meta.get("filters")) == canonical_data(filters), "detail rows filters mismatch")
        require(canonical_data(checkpoint.get("filters")) == canonical_data(filters), "detail checkpoint filters mismatch")
        require(meta.get("snapshot_id") == snapshot_id and checkpoint.get("snapshot_id") == snapshot_id, "detail rows snapshot mismatch")
        require(canonical_data(meta.get("columns")) == canonical_data(checkpoint.get("columns")), "detail rows columns mismatch")
    except Exception:
        stream.close()
        raise
    return stream, meta


def exact_page_slices(rows, page_length, count=2):
    return {
        f"page-{page}": {
            "start": page * page_length,
            "page_length": page_length,
            "rows": rows[page * page_length:(page + 1) * page_length],
        }
        for page in range(count)
    }


def validate_detail_pair_deep(cache_root, company, scope, actor, snapshot_id, page_length):
    filters = {"company": company, "from_date": scope["from_date"], "to_date": scope["to_date"], "group_by": "Invoice"}
    checkpoints = {}
    streams = {}
    metas = {}
    errors = []
    try:
        for implementation in ("stock", "stock-verify"):
            checkpoint = load_json(Path(cache_root) / "out" / f"checkpoint-detail-{implementation}.json")
            validate_common_checkpoint(checkpoint, implementation, actor, filters, snapshot_id, require_snapshot=True)
            require(checkpoint.get("schema_version") == "oracle-detail-checkpoint-v1", f"{implementation}: unsupported detail checkpoint schema")
            require(isinstance(checkpoint.get("columns"), list), f"{implementation}: detail columns are not a list")
            require(checkpoint.get("columns_sha256") == sorted_json_sha256(checkpoint["columns"]), f"{implementation}: detail columns sha mismatch")
            artifact_name = checkpoint.get("rows_artifact")
            require(artifact_name and Path(artifact_name).name == artifact_name, f"{implementation}: unsafe detail rows artifact name")
            path = Path(cache_root) / "out" / artifact_name
            stream, meta = open_detail_rows(path, checkpoint, filters, snapshot_id, actor, implementation)
            checkpoints[implementation] = checkpoint
            streams[implementation] = stream
            metas[implementation] = meta

        left_hasher = hashlib.sha256()
        right_hasher = hashlib.sha256()
        for hasher in (left_hasher, right_hasher):
            hasher.update(b"[")
        heads = {"stock": [], "stock-verify": []}
        count = 0
        length_mismatch = False
        for left_line, right_line in zip_longest(streams["stock"], streams["stock-verify"]):
            if left_line is None or right_line is None:
                errors.append({"kind": "stream_length_mismatch", "row_index": count})
                length_mismatch = True
                break
            try:
                left = json.loads(left_line)
                right = json.loads(right_line)
            except Exception as exc:
                errors.append({"kind": "row_json_parse", "row_index": count, "error": str(exc)})
                break
            if left.get("row_index") != count or right.get("row_index") != count:
                errors.append({"kind": "noncontiguous_row_index", "row_index": count})
            left_row = left.get("row")
            right_row = right.get("row")
            if not exact_typed_equal(left_row, right_row):
                errors.append({"kind": "ordered_row_exact_type_or_value_mismatch", "row_index": count})
            if count:
                left_hasher.update(b",")
                right_hasher.update(b",")
            canonical_json_update(left_hasher, left_row)
            canonical_json_update(right_hasher, right_row)
            if count < page_length * 2:
                heads["stock"].append(left_row)
                heads["stock-verify"].append(right_row)
            count += 1
        for hasher in (left_hasher, right_hasher):
            hasher.update(b"]")
        if not length_mismatch:
            for implementation, hasher in (("stock", left_hasher), ("stock-verify", right_hasher)):
                checkpoint = checkpoints[implementation]
                meta = metas[implementation]
                if count != checkpoint.get("row_count") or count != meta.get("row_count"):
                    errors.append({"kind": "row_count_mismatch", "implementation": implementation, "streamed": count})
                if hasher.hexdigest() != checkpoint.get("rows_sha256"):
                    errors.append({"kind": "checkpoint_ordered_rows_hash_mismatch", "implementation": implementation})
                pages = checkpoint.get("pages") or {}
                if set(pages) != {"page-0", "page-1"}:
                    errors.append({"kind": "checkpoint_first_two_pages_missing", "implementation": implementation})
                elif not exact_typed_equal(exact_page_slices(heads[implementation], page_length), pages):
                    errors.append({"kind": "checkpoint_first_two_pages_mismatch", "implementation": implementation})
        require(not errors, "detail pair deep validation failed: " + canonical_data(errors[:5]))
        return {
            "validation_mode": "deep-stream-revalidation",
            "passed": True,
            "row_count_streamed": count,
            "stock_streamed_rows_sha256": left_hasher.hexdigest(),
            "verify_streamed_rows_sha256": right_hasher.hexdigest(),
            "ordered_exact": True,
            "type_exact": True,
            "first_two_pages_exact": True,
        }
    finally:
        for stream in streams.values():
            stream.close()


def validate_archived_detail_verification(cache_root, detail_stats, scope, snapshot_id):
    verification_path = Path(cache_root) / "out" / "verification-detail.json"
    if verification_path.is_file():
        verification = load_json(verification_path)
        stream = verification.get("bounded_memory_exact_ordered") or {}
        require(verification.get("schema_version") == "stock-oracle-detail-verification-v2", "unsupported detail verification schema")
        require(verification.get("passed") is True and stream.get("passed") is True, "archived detail verification did not pass")
        require(verification.get("ordered_rows_hash_equal") is True, "archived detail verification rows hash mismatch")
        require(stream.get("row_count_streamed") == detail_stats["row_count"], "archived detail row count mismatch")
        require(stream.get("stock_streamed_rows_sha256") == detail_stats["rows_sha256"], "archived stock streamed hash mismatch")
        require(stream.get("verify_streamed_rows_sha256") == detail_stats["rows_sha256"], "archived verify streamed hash mismatch")
        require((stream.get("stock_artifact_file") or {}).get("sha256") == detail_stats["stock_rows_artifact_sha256"], "archived stock artifact hash mismatch")
        require((stream.get("verify_artifact_file") or {}).get("sha256") == detail_stats["verify_rows_artifact_sha256"], "archived verify artifact hash mismatch")
        return "archived-in-run-independent-stream-verification"

    receipt_path = Path(cache_root) / "provenance" / "offline-detail-receipt.json"
    require(receipt_path.is_file(), "no archived detail verification artifact or offline receipt")
    receipt = load_json(receipt_path)
    stream = receipt.get("bounded_memory_exact_ordered") or {}
    receipt_scope = receipt.get("scope") or {}
    require(receipt.get("schema_version") == "offline-native-stock-detail-recovery-receipt-v1", "unsupported offline detail receipt schema")
    require(receipt.get("passed") is True and stream.get("passed") is True, "offline detail receipt did not pass")
    require(receipt.get("heavy_report_queries_run") == 0, "offline detail receipt is not zero-query evidence")
    require(receipt.get("original_run_rewritten") is False, "offline detail receipt rewrote original evidence")
    require(receipt.get("snapshot_id") == snapshot_id, "offline detail receipt snapshot mismatch")
    require(receipt_scope.get("from_date") == scope["from_date"] and receipt_scope.get("to_date") == scope["to_date"], "offline detail receipt scope mismatch")
    require(receipt_scope.get("group_by") == "Invoice", "offline detail receipt group mismatch")
    require(stream.get("row_count_streamed") == detail_stats["row_count"], "offline detail row count mismatch")
    require(stream.get("stock_streamed_rows_sha256") == detail_stats["rows_sha256"], "offline stock streamed hash mismatch")
    require(stream.get("verify_streamed_rows_sha256") == detail_stats["rows_sha256"], "offline verify streamed hash mismatch")
    require((stream.get("stock_artifact_file") or {}).get("sha256") == detail_stats["stock_rows_artifact_sha256"], "offline stock artifact hash mismatch")
    require((stream.get("verify_artifact_file") or {}).get("sha256") == detail_stats["verify_rows_artifact_sha256"], "offline verify artifact hash mismatch")
    return "archived-offline-zero-query-independent-stream-verification"


def validate_detail_reference(cache_root, company, scope, actor, snapshot_id, deep_row_limit):
    filters = {"company": company, "from_date": scope["from_date"], "to_date": scope["to_date"], "group_by": "Invoice"}
    stock = load_json(Path(cache_root) / "out" / "checkpoint-detail-stock.json")
    verify = load_json(Path(cache_root) / "out" / "checkpoint-detail-stock-verify.json")
    validate_common_checkpoint(stock, "stock", actor, filters, snapshot_id, require_snapshot=True)
    validate_common_checkpoint(verify, "stock-verify", actor, filters, snapshot_id, require_snapshot=True)
    require(stock.get("row_count") == verify.get("row_count"), "detail stock/verify row count mismatch")
    require(stock.get("rows_sha256") == verify.get("rows_sha256"), "detail stock/verify rows hash mismatch")
    require(canonical_data(stock.get("columns")) == canonical_data(verify.get("columns")), "detail stock/verify columns mismatch")
    for implementation, checkpoint in (("stock", stock), ("stock-verify", verify)):
        artifact_name = checkpoint.get("rows_artifact")
        require(artifact_name and Path(artifact_name).name == artifact_name, f"{implementation}: unsafe detail rows artifact name")
        validate_detail_file_identity(Path(cache_root) / "out" / artifact_name, checkpoint)
    stats = {
        "row_count": stock["row_count"],
        "rows_sha256": stock["rows_sha256"],
        "columns_sha256": stock["columns_sha256"],
        "stock_rows_artifact_sha256": stock["rows_artifact_sha256"],
        "verify_rows_artifact_sha256": verify["rows_artifact_sha256"],
    }
    page_length = ((stock.get("pages") or {}).get("page-0") or {}).get("page_length") or 50
    if int(stock.get("row_count") or 0) <= int(deep_row_limit):
        deep = validate_detail_pair_deep(cache_root, company, scope, actor, snapshot_id, page_length)
        stats.update(deep)
    else:
        stats["validation_mode"] = validate_archived_detail_verification(cache_root, stats, scope, snapshot_id)
        stats["passed"] = True
        stats["ordered_exact"] = True
        stats["type_exact"] = True
        stats["first_two_pages_exact"] = True
    return stats


def validate_copyback_manifest(cache_root):
    path = Path(cache_root) / "out-sha256sums.txt"
    require(path.is_file(), "scope out-sha256sums.txt missing")
    expected = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, guest_path = line.split(None, 1)
        name = Path(guest_path.strip()).name
        require(name and name not in expected, f"ambiguous copyback manifest artifact name: {name}")
        expected[name] = digest
    out = Path(cache_root) / "out"
    actual_names = {p.name for p in out.iterdir() if p.is_file()}
    require(set(expected) == actual_names, "copied out directory does not exactly match copyback manifest coverage")
    for name, digest in sorted(expected.items()):
        require(sha256_file(out / name) == digest, f"copyback manifest sha mismatch: {name}")
    return {"entries_validated": len(expected), "manifest_sha256": sha256_file(path)}


def validate_restore_identity(sequence, fixture_manifest, input_manifest, preflight, scope):
    errors = []
    def check(condition, kind, **data):
        if not condition:
            errors.append({"kind": kind, **data})

    fixture_sha = sequence.get("fixture_sha256")
    check(fixture_sha == PINNED_FIXTURE_SHA256, "fixture_sha_mismatch", actual=fixture_sha)
    check(sequence.get("seed_manifest_sha256") == PINNED_SEED_MANIFEST_SHA256, "seed_manifest_sha_mismatch")
    check(sequence.get("site") == PINNED_SITE, "site_mismatch")
    check(sequence.get("company") == PINNED_COMPANY, "company_mismatch")
    scopes = sequence.get("scopes") or []
    check(any(item.get("scope") == scope["scope"] and item.get("from_date") == scope["from_date"] and item.get("to_date") == scope["to_date"] for item in scopes), "sequence_scope_missing")

    check(fixture_manifest.get("fixture_tar_gz_sha256") == fixture_sha, "fixture_manifest_sha_mismatch")
    check(fixture_manifest.get("seed_manifest_sha256") == sequence.get("seed_manifest_sha256"), "fixture_seed_manifest_mismatch")
    check(fixture_manifest.get("site") == sequence.get("site"), "fixture_site_mismatch")
    image_id = (fixture_manifest.get("image_ids") or {}).get(PINNED_ERPNEXT_IMAGE, "")
    check(image_id.startswith(PINNED_ERPNEXT_IMAGE_ID_PREFIX), "fixture_erpnext_image_mismatch", actual=image_id)
    check((input_manifest.get("approved_erpnext_image_id_prefix") or "") == PINNED_ERPNEXT_IMAGE_ID_PREFIX, "input_image_prefix_mismatch")
    check(input_manifest.get("fixture_sha256") == fixture_sha, "input_fixture_mismatch")
    check(input_manifest.get("seed_manifest_sha256") == sequence.get("seed_manifest_sha256"), "input_seed_mismatch")

    baseline = preflight.get("financial_baseline") or {}
    counts = fixture_manifest.get("verified_counts") or {}
    window = fixture_manifest.get("report_window") or {}
    check(preflight.get("passed") is True, "preflight_not_passed")
    check(preflight.get("site") == sequence.get("site"), "preflight_site_mismatch")
    check(baseline.get("company") == sequence.get("company"), "baseline_company_mismatch")
    check(baseline.get("submitted_sales_invoices") == counts.get("tabSales Invoice"), "restore_sales_invoice_count_mismatch")
    check(baseline.get("stock_ledger_entries") == counts.get("tabStock Ledger Entry"), "restore_sle_count_mismatch")
    check(baseline.get("sales_invoice_min_posting_date") == window.get("from_date"), "restore_invoice_min_date_mismatch")
    check(baseline.get("sales_invoice_max_posting_date") == window.get("final_voucher_date"), "restore_invoice_max_date_mismatch")
    check(baseline.get("stock_ledger_min_posting_date") == window.get("from_date"), "restore_sle_min_date_mismatch")
    check(baseline.get("stock_ledger_max_posting_date") == window.get("final_voucher_date"), "restore_sle_max_date_mismatch")
    check(preflight.get("repost_triggers_installed") is True, "repost_triggers_missing")
    check(not preflight.get("missing_repost_triggers"), "repost_triggers_missing")

    fixture_actors = fixture_manifest.get("actors") or {}
    actor_keys = {"summary-warehouse@seasonal.invalid": "summary_warehouse", "summary-monthly@seasonal.invalid": "summary_monthly", "detail-reader@seasonal.invalid": "detail_reader"}
    for actor, fixture_key in actor_keys.items():
        observed = (preflight.get("actors") or {}).get(actor) or {}
        expected_roles = sorted((fixture_actors.get(fixture_key) or {}).get("roles") or [])
        check(observed.get("exists") is True and observed.get("enabled") is True and observed.get("password_verified") is True, "actor_authentication_or_enablement_mismatch", actor=actor)
        if "roles" in observed:
            check(sorted(observed.get("roles") or []) == expected_roles, "actor_roles_mismatch", actor=actor)
    require(not errors, "restore/provenance identity validation failed: " + canonical_data(errors))
    return {
        "fixture_sha256": fixture_sha,
        "seed_manifest_sha256": sequence.get("seed_manifest_sha256"),
        "snapshot_id": fixture_sha,
        "site": sequence.get("site"),
        "company": sequence.get("company"),
        "fixture_archive_bytes": input_manifest.get("fixture_archive_bytes") or fixture_manifest.get("fixture_tar_gz_bytes"),
        "restore_receipt_sha256": fixture_manifest.get("restore_receipt_sha256"),
        "financial_baseline": baseline,
        "erpnext_image": PINNED_ERPNEXT_IMAGE,
        "erpnext_image_id": image_id,
        "precision_contract": "exact JSON value and type equality with no numeric tolerance; decimal/currency/float behavior is fixed by the pinned fixture database bytes and ERPNext image",
    }


def build_reference_manifest(run_dir, scope_name, fixture_manifest_path, input_manifest_path, cache_dir, output_path, offline_detail_receipt=None, deep_row_limit=250000):
    run_dir = Path(run_dir)
    scope_dir = run_dir / scope_name
    out_dir = scope_dir / "out"
    cache_dir = Path(cache_dir)
    output_path = Path(output_path)
    require(run_dir.is_dir(), f"run directory missing: {run_dir}")
    require(out_dir.is_dir(), f"scope out directory missing: {out_dir}")
    require(not output_path.exists(), f"refusing to overwrite existing manifest: {output_path}")
    if cache_dir.exists() and any(cache_dir.iterdir()):
        raise ReferenceValidationError(f"refusing to use non-empty cache directory: {cache_dir}")
    cache_dir.mkdir(parents=True, exist_ok=True)

    fixture_manifest = load_json(fixture_manifest_path)
    input_manifest = load_json(input_manifest_path)
    sequence = load_json(run_dir / "sequence-request.json")
    scope_entries = [item for item in (sequence.get("scopes") or []) if item.get("scope") == scope_name]
    require(len(scope_entries) == 1, f"scope is missing or ambiguous in sequence request: {scope_name}")
    scope = {"scope": scope_name, "from_date": scope_entries[0]["from_date"], "to_date": scope_entries[0]["to_date"]}

    entries = []
    out_files = sorted(path for path in out_dir.iterdir() if path.is_file())
    require(out_files, "scope out directory has no files")
    for src in out_files:
        copy_artifact(src, cache_dir, f"out/{src.name}", "scope-output", entries)
    copy_artifact(scope_dir / "out-sha256sums.txt", cache_dir, "out-sha256sums.txt", "copyback-manifest", entries)
    for name in ("sequence-request.json", "sequence-status.json", "driver-status.json", "copyback-verification.json", "container-cleanup.json", "oracle-db-state.json"):
        src = run_dir / name
        copy_artifact(src, cache_dir, f"run/{name}", "run-provenance", entries, required=False)
    copy_artifact(input_manifest_path, cache_dir, "provenance/input-manifest.json", "input-manifest", entries)
    copy_artifact(fixture_manifest_path, cache_dir, "provenance/fixture-manifest.json", "fixture-manifest", entries)
    if offline_detail_receipt:
        copy_artifact(offline_detail_receipt, cache_dir, "provenance/offline-detail-receipt.json", "offline-detail-verification", entries)

    copyback = validate_copyback_manifest(cache_dir)
    copyback_verification_path = cache_dir / "run" / "copyback-verification.json"
    copyback_verification = None
    if copyback_verification_path.is_file():
        copyback_verification = load_json(copyback_verification_path)
        require(copyback_verification.get("schema_version") == "year-oracle-copyback-verification-v1", "unsupported copyback verification schema")
        require(copyback_verification.get("passed") is True, "copyback verification did not pass")
        require(copyback_verification.get("original_outputs_rewritten") is False, "copyback verification rewrote originals")
        require(not copyback_verification.get("failures"), "copyback verification has failures")

    preflight = load_json(cache_dir / "out" / "clone-preflight.json")
    restore_identity = validate_restore_identity(sequence, fixture_manifest, input_manifest, preflight, scope)
    actors = {
        "warehouse": "summary-warehouse@seasonal.invalid",
        "monthly": "summary-monthly@seasonal.invalid",
        "detail": "detail-reader@seasonal.invalid",
    }
    summaries = {}
    for group in SUMMARY_GROUPS:
        summaries[group.lower()] = validate_summary_pair(cache_dir, PINNED_COMPANY, scope, group, actors, PINNED_FIXTURE_SHA256)
    detail = validate_detail_reference(cache_dir, PINNED_COMPANY, scope, actors["detail"], PINNED_FIXTURE_SHA256, deep_row_limit)

    artifact_root_sha256 = canonical_sha256([
        {key: entry[key] for key in ("path", "kind", "sha256", "bytes", "permissions")}
        for entry in sorted(entries, key=lambda item: item["path"])
    ])
    coverage = {
        "scope": scope["scope"],
        "fixture_sha256": PINNED_FIXTURE_SHA256,
        "seed_manifest_sha256": PINNED_SEED_MANIFEST_SHA256,
        "snapshot_id": PINNED_FIXTURE_SHA256,
        "site": PINNED_SITE,
        "company": PINNED_COMPANY,
        "from_date": scope["from_date"],
        "to_date": scope["to_date"],
        "stock_report": STOCK_REPORT,
        "summary_groups": list(SUMMARY_GROUPS),
        "detail_group_by": "Invoice",
        "detail_page_length": ((load_json(cache_dir / "out" / "checkpoint-detail-stock.json").get("pages") or {}).get("page-0") or {}).get("page_length") or 50,
        "actors": actors,
        "filters": {
            "warehouse": summaries["warehouse"]["filters"],
            "monthly": summaries["monthly"]["filters"],
            "detail": {"company": PINNED_COMPANY, "from_date": scope["from_date"], "to_date": scope["to_date"], "group_by": "Invoice"},
        },
        "comparison_precision": {
            "numeric_tolerance": "none",
            "ordering": "full ordered rows are significant",
            "types": "exact JSON types are significant",
            "columns": "full column metadata is significant",
        },
    }
    independent_verification = {
        "summaries": summaries,
        "detail": detail,
        "copyback": copyback,
        "copyback_verification_passed": bool(copyback_verification and copyback_verification.get("passed")),
    }
    provenance = {
        "source_run_id": sequence.get("run_id"),
        "source_run_dir": str(run_dir),
        "source_scope_dir": str(scope_dir),
        "fixture_manifest_sha256": sha256_file(cache_dir / "provenance" / "fixture-manifest.json"),
        "input_manifest_sha256": sha256_file(cache_dir / "provenance" / "input-manifest.json"),
        "sequence_request_sha256": sha256_file(cache_dir / "run" / "sequence-request.json") if (cache_dir / "run" / "sequence-request.json").is_file() else None,
        "restore_identity": restore_identity,
        "actor_permission_evidence": {
            "fixture_roles": {key: (fixture_manifest.get("actors") or {}).get(key) for key in ("summary_warehouse", "summary_monthly", "detail_reader")},
            "preflight_actors": preflight.get("actors"),
            "successful_stock_execution_artifacts": [
                "out/checkpoint-summary-warehouse-stock.json",
                "out/checkpoint-summary-monthly-stock.json",
                "out/checkpoint-detail-stock.json",
            ],
        },
        "source_and_image_identity": {
            "erpnext_image": PINNED_ERPNEXT_IMAGE,
            "erpnext_image_id_prefix": PINNED_ERPNEXT_IMAGE_ID_PREFIX,
            "fixture_image_ids": fixture_manifest.get("image_ids"),
            "input_candidate_commit": input_manifest.get("candidate_commit"),
            "input_candidate_archive_sha256": input_manifest.get("candidate_archive_sha256"),
            "candidate_identity_note": "candidate identity is diagnostic only; stock oracle reference validity does not depend on the candidate source revision",
        },
        "preflight_report_source": preflight.get("report_source") or dict(PINNED_REPORT_SOURCE),
        "preflight_precision_settings": preflight.get("precision_settings") or dict(PINNED_PRECISION_SETTINGS),
    }
    core = {
        "coverage": coverage,
        "independent_verification": independent_verification,
        "provenance": provenance,
        "artifact_root_sha256": artifact_root_sha256,
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_utc": utc_now(),
        "reference_id": canonical_sha256(core),
        "coverage": coverage,
        "independent_verification": independent_verification,
        "provenance": provenance,
        "artifacts": sorted(entries, key=lambda item: item["path"]),
        "artifact_root_sha256": artifact_root_sha256,
        "validation": {
            "passed": True,
            "validated_at_build": True,
            "detail_validation_mode": detail.get("validation_mode"),
            "deep_row_limit": deep_row_limit,
            "usage_boundary": "exact pinned immutable fixture/report/filter/actor case only; not evidence for dynamic writes, fresh stock generation, timing, or voucher/integration behavior",
        },
    }
    write_json_new(output_path, manifest)
    return manifest


class StockReference:
    def __init__(self, manifest, manifest_path, cache_root, manifest_sha256):
        self.manifest = manifest
        self.manifest_path = Path(manifest_path)
        self.cache_root = Path(cache_root)
        self.manifest_sha256 = manifest_sha256

    def artifact_path(self, relative):
        relative = safe_artifact_relative(relative)
        return self.cache_root / relative

    def checkpoint_path(self, case_id, implementation):
        return self.artifact_path(f"out/checkpoint-{case_id}-{implementation}.json")

    def load_checkpoint(self, case_id, implementation):
        return load_json(self.checkpoint_path(case_id, implementation))

    def detail_rows_path(self, checkpoint):
        name = checkpoint.get("rows_artifact")
        require(name and Path(name).name == name, "unsafe detail rows artifact name in reference checkpoint")
        return self.artifact_path(f"out/{name}")

    def usage_metadata(self):
        coverage = self.manifest.get("coverage") or {}
        detail = ((self.manifest.get("independent_verification") or {}).get("detail") or {})
        return {
            "mode": "hash-pinned-independent-stock-reference-reuse",
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "reference_id": self.manifest.get("reference_id"),
            "cache_root": str(self.cache_root),
            "fresh_stock_oracle_executions": 0,
            "independent_reference_verification_mode": detail.get("validation_mode"),
            "covered_scope": {
                "scope": coverage.get("scope"),
                "from_date": coverage.get("from_date"),
                "to_date": coverage.get("to_date"),
                "fixture_sha256": coverage.get("fixture_sha256"),
            },
            "boundary": "stock reference bytes are reused only for the exact pinned immutable fixture/filter/actor/report case; this is not evidence for dynamic writes, fresh stock generation, timing, voucher submission, permissions beyond the archived proof, or integration runtime availability",
        }


def _resolved(path):
    return Path(path).resolve(strict=False)


def validate_reference_manifest(manifest_path, cache_root, validate_artifacts=True):
    manifest_path = Path(manifest_path)
    cache_root = Path(cache_root)
    require(manifest_path.is_file(), f"reference manifest missing: {manifest_path}")
    require(cache_root.is_dir(), f"reference cache root missing: {cache_root}")
    try:
        inside = _resolved(manifest_path).is_relative_to(_resolved(cache_root))
    except AttributeError:
        inside = str(_resolved(manifest_path)).startswith(str(_resolved(cache_root)) + os.sep)
    require(not inside, "reference manifest must be rooted outside the artifact cache")
    manifest = load_json(manifest_path)
    require(manifest.get("schema_version") == MANIFEST_SCHEMA_VERSION, "unsupported reference manifest schema")
    artifacts = manifest.get("artifacts")
    require(isinstance(artifacts, list) and artifacts, "reference manifest has no artifact entries")
    seen = set()
    computed_entries = []
    for entry in artifacts:
        relative = safe_artifact_relative(entry.get("path"))
        require(relative not in seen, f"duplicate artifact path in manifest: {relative}")
        seen.add(relative)
        for key in ("kind", "sha256", "bytes", "permissions"):
            require(key in entry, f"artifact entry missing {key}: {relative}")
        computed_entries.append({key: entry[key] for key in ("path", "kind", "sha256", "bytes", "permissions")})
    root_sha = canonical_sha256(sorted(computed_entries, key=lambda item: item["path"]))
    require(root_sha == manifest.get("artifact_root_sha256"), "artifact root hash mismatch")
    core = {
        "coverage": manifest.get("coverage"),
        "independent_verification": manifest.get("independent_verification"),
        "provenance": manifest.get("provenance"),
        "artifact_root_sha256": manifest.get("artifact_root_sha256"),
    }
    require(canonical_sha256(core) == manifest.get("reference_id"), "reference id does not match pinned manifest content")
    require((manifest.get("validation") or {}).get("passed") is True, "reference manifest validation did not pass")
    require((manifest.get("independent_verification") or {}).get("detail", {}).get("passed") is True, "reference detail verification did not pass")

    if validate_artifacts:
        for entry in artifacts:
            path = cache_root / entry["path"]
            require(path.is_file(), f"pinned artifact missing: {entry['path']}")
            require(path.stat().st_size == entry["bytes"], f"pinned artifact byte size mismatch: {entry['path']}")
            require(sha256_file(path) == entry["sha256"], f"pinned artifact sha256 mismatch: {entry['path']}")
            if os.name != "nt":
                require(artifact_permissions(path) == entry["permissions"], f"pinned artifact permissions mismatch: {entry['path']}")
    return StockReference(manifest, manifest_path, cache_root, sha256_file(manifest_path))


def validate_runtime_match(reference, *, site, company, summary_from_date, summary_to_date, detail_from_date, detail_to_date, warehouse_actor, monthly_actor, detail_actor, snapshot_id, preflight, page_length):
    coverage = reference.manifest.get("coverage") or {}
    actors = coverage.get("actors") or {}
    errors = []
    def check(condition, kind, **data):
        if not condition:
            errors.append({"kind": kind, **data})

    check(page_length == coverage.get("detail_page_length"), "detail_page_length_mismatch")
    check(site == coverage.get("site"), "site_mismatch")
    check(company == coverage.get("company"), "company_mismatch")
    check(snapshot_id == coverage.get("snapshot_id"), "snapshot_mismatch")
    check(summary_from_date == coverage.get("from_date") and summary_to_date == coverage.get("to_date"), "summary_window_mismatch")
    check(detail_from_date == coverage.get("from_date") and detail_to_date == coverage.get("to_date"), "detail_window_mismatch")
    check(warehouse_actor == actors.get("warehouse"), "warehouse_actor_mismatch")
    check(monthly_actor == actors.get("monthly"), "monthly_actor_mismatch")
    check(detail_actor == actors.get("detail"), "detail_actor_mismatch")
    check(preflight.get("passed") is True, "runtime_preflight_not_passed")

    expected_baseline = ((reference.manifest.get("provenance") or {}).get("restore_identity") or {}).get("financial_baseline") or {}
    actual_baseline = preflight.get("financial_baseline") or {}
    for key in (
        "company",
        "submitted_sales_invoices",
        "sales_invoice_min_posting_date",
        "sales_invoice_max_posting_date",
        "stock_ledger_entries",
        "stock_ledger_min_posting_date",
        "stock_ledger_max_posting_date",
    ):
        check(actual_baseline.get(key) == expected_baseline.get(key), "restore_identity_baseline_mismatch", field=key)

    expected_precision = ((reference.manifest.get("provenance") or {}).get("preflight_precision_settings") or None)
    if expected_precision:
        actual_precision = preflight.get("precision_settings") or {}
        if expected_precision.get("identity_basis"):
            for key in ("currency_precision_effective", "float_precision_effective", "rounding_method", "comparison_contract"):
                check(actual_precision.get(key) == expected_precision.get(key), "precision_settings_mismatch", field=key)
        else:
            check(actual_precision == expected_precision, "precision_settings_mismatch")
    expected_report_source = ((reference.manifest.get("provenance") or {}).get("preflight_report_source") or None)
    if expected_report_source:
        check(preflight.get("report_source") == expected_report_source, "report_source_mismatch")

    fixture_roles = (((reference.manifest.get("provenance") or {}).get("actor_permission_evidence") or {}).get("fixture_roles") or {})
    role_key_by_actor = {
        actors.get("warehouse"): "summary_warehouse",
        actors.get("monthly"): "summary_monthly",
        actors.get("detail"): "detail_reader",
    }
    for actor, fixture_key in role_key_by_actor.items():
        observed = (preflight.get("actors") or {}).get(actor) or {}
        check(observed.get("exists") is True and observed.get("enabled") is True and observed.get("password_verified") is True, "runtime_actor_authentication_mismatch", actor=actor)
        expected_roles = sorted((fixture_roles.get(fixture_key) or {}).get("roles") or [])
        if expected_roles:
            check(sorted(observed.get("roles") or []) == expected_roles, "runtime_actor_roles_mismatch", actor=actor)

    return {
        "passed": not errors,
        "mismatches": errors,
        "checked_actual": [
            "fixture/snapshot identity",
            "site/company/filter windows",
            "pinned actors",
            "actor enablement/password authentication",
            "actor roles when present in runtime preflight",
            "restore identity baseline from the live restored database",
            "report source and precision settings when pinned by the manifest",
        ],
        "precision_contract": (coverage.get("comparison_precision") or {}),
    }


def build_main(args):
    manifest = build_reference_manifest(
        args.run_dir,
        args.scope,
        args.fixture_manifest,
        args.input_manifest,
        args.cache_dir,
        args.output,
        offline_detail_receipt=args.offline_detail_receipt,
        deep_row_limit=args.deep_row_limit,
    )
    print(json.dumps({"passed": True, "reference_id": manifest["reference_id"], "output": str(args.output)}, sort_keys=True))
    return 0


def validate_main(args):
    reference = validate_reference_manifest(args.manifest, args.cache_root, validate_artifacts=not args.no_artifact_hash)
    print(json.dumps({"passed": True, "reference_id": reference.manifest.get("reference_id"), "manifest_sha256": reference.manifest_sha256}, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--run-dir", required=True)
    build.add_argument("--scope", required=True, choices=("year-to-date", "full-year"))
    build.add_argument("--fixture-manifest", required=True)
    build.add_argument("--input-manifest", required=True)
    build.add_argument("--cache-dir", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--offline-detail-receipt")
    build.add_argument("--deep-row-limit", type=int, default=250000)
    build.set_defaults(func=build_main)
    validate = sub.add_parser("validate")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--cache-root", required=True)
    validate.add_argument("--no-artifact-hash", action="store_true")
    validate.set_defaults(func=validate_main)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ReferenceValidationError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
