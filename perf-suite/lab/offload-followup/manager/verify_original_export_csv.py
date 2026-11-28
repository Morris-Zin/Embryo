#!/usr/bin/env python3
"""Independently verify an original-workload profit CSV against native-stock detail bytes.

The verifier is streaming and exact: it recomputes the CSV bytes implied by the pinned
unmodified ERPNext detail checkpoint and compares the downloaded artifact byte-for-byte.
"""

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys

# Derived from the independently streamed full-year native-stock checkpoint.
# Its exact checkpoint bytes bind the fixture, filters, actor, columns and row order.
FULL_YEAR_CHECKPOINT_SHA = "829a22074fa63061d7ac651534bbeb5b3cc647eca2e083b301e58ba10b0e5ee5"
FULL_YEAR_CSV_SHA = "8cfe4b836ccf7bd733515ea0a2eb72ef0c1978d0c6d713059080199ea3f9224c"
FULL_YEAR_CSV_BYTES = 211904767


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def csv_value(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def serialize_csv_record(values) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(values)
    return buffer.getvalue().encode("utf-8")


def column_fieldnames(columns):
    return [str(column.get("fieldname") or column.get("label") or index) for index, column in enumerate(columns or [])]


def column_headers(columns):
    return [str(column.get("label") or column.get("fieldname") or "") for column in columns or []]


def row_values(row, fieldnames):
    if isinstance(row, dict):
        return [csv_value(row.get(fieldname)) for fieldname in fieldnames]
    if isinstance(row, (list, tuple)):
        return [csv_value(value) for value in row]
    return [csv_value(row)]


def validate_rows_artifact(checkpoint: dict, rows_path: Path) -> dict:
    require(rows_path.is_file(), f"native-stock rows artifact missing: {rows_path}")
    mode = stat.S_IMODE(rows_path.stat().st_mode)
    require(mode == 0o600, f"native-stock rows artifact permissions must be exactly 0600, got {mode:04o}")
    actual_sha = sha256_file(rows_path)
    require(actual_sha == checkpoint.get("rows_artifact_sha256"), "native-stock rows artifact sha256 mismatch")
    require(rows_path.stat().st_size == checkpoint.get("rows_artifact_bytes"), "native-stock rows artifact byte count mismatch")
    require(checkpoint.get("rows_artifact_format") == "oracle-detail-rows-ndjson-gzip-v1", "unsupported rows artifact format")
    require(checkpoint.get("report") == "Gross Profit", "native-stock checkpoint is not unmodified Gross Profit")
    require(checkpoint.get("implementation") == "stock", "native-stock checkpoint implementation mismatch")
    return {"sha256": actual_sha, "bytes": rows_path.stat().st_size, "permissions": f"{mode:04o}"}


def iter_rows_artifact(rows_path: Path, checkpoint: dict):
    stream = gzip.open(rows_path, "rt", encoding="utf-8")
    try:
        first = stream.readline()
        require(bool(first), "native-stock rows artifact is empty")
        meta = json.loads(first)
        require(meta.get("schema_version") == "oracle-detail-rows-v1", "native-stock rows artifact schema mismatch")
        require(meta.get("row_count") == checkpoint.get("row_count"), "native-stock rows artifact/checkpoint row count mismatch")
        require(meta.get("filters") == checkpoint.get("filters"), "native-stock rows artifact/checkpoint filters mismatch")
        require(meta.get("columns") == checkpoint.get("columns"), "native-stock rows artifact/checkpoint columns mismatch")
        index = 0
        for line in stream:
            item = json.loads(line)
            require(item.get("row_index") == index, "native-stock rows artifact row indexes are not contiguous")
            yield item.get("row")
            index += 1
        require(index == checkpoint.get("row_count"), "native-stock rows artifact streamed row count mismatch")
    finally:
        stream.close()


def expected_csv_digest(checkpoint: dict, rows_path: Path):
    columns = checkpoint.get("columns") or []
    fieldnames = column_fieldnames(columns)
    digest = hashlib.sha256()
    byte_count = 0

    def add(values):
        nonlocal byte_count
        data = serialize_csv_record(values)
        digest.update(data)
        byte_count += len(data)

    add(column_headers(columns))
    row_count = 0
    for row in iter_rows_artifact(rows_path, checkpoint):
        add(row_values(row, fieldnames))
        row_count += 1
    return digest.hexdigest(), byte_count, row_count, column_headers(columns), fieldnames


def diagnose_first_row_mismatch(csv_path: Path, checkpoint: dict, rows_path: Path, fieldnames):
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            actual_header = next(reader)
        except StopIteration:
            return {"error": "downloaded CSV is empty"}
        expected_header = column_headers(checkpoint.get("columns") or [])
        if actual_header != expected_header:
            return {"row_index": "header", "expected": expected_header, "actual": actual_header}
        for row_index, expected_row in enumerate(iter_rows_artifact(rows_path, checkpoint)):
            try:
                actual = next(reader)
            except StopIteration:
                return {"row_index": row_index, "error": "downloaded CSV ended early"}
            expected = ["" if value is None else value for value in row_values(expected_row, fieldnames)]
            actual_normalized = actual + [""] * max(0, len(expected) - len(actual))
            expected_normalized = [str(value) for value in expected] + [""] * max(0, len(actual) - len(expected))
            if actual_normalized != expected_normalized:
                column_index = next(
                    (index for index, pair in enumerate(zip(actual_normalized, expected_normalized)) if pair[0] != pair[1]),
                    None,
                )
                return {
                    "row_index": row_index,
                    "column_index": column_index,
                    "column": fieldnames[column_index] if column_index is not None and column_index < len(fieldnames) else None,
                    "actual": actual_normalized[column_index] if column_index is not None and column_index < len(actual_normalized) else None,
                    "expected": expected_normalized[column_index] if column_index is not None and column_index < len(expected_normalized) else None,
                }
        try:
            extra = next(reader)
            return {"row_index": checkpoint.get("row_count"), "error": "downloaded CSV has extra rows", "first_extra": extra}
        except StopIteration:
            pass
    return None


def validate_headers(headers_path: Path, filters: dict):
    if headers_path is None:
        return {"checked": False}
    raw = json.loads(headers_path.read_text(encoding="utf-8"))
    headers = {str(key).lower(): str(value) for key, value in raw.items()}
    filename = "fast-gross-profit-{}-to-{}.csv".format(filters.get("from_date"), filters.get("to_date"))
    checks = {
        "content_type_csv": "text/csv" in headers.get("content-type", "").lower(),
        "content_disposition_attachment": "attachment" in headers.get("content-disposition", "").lower(),
        "content_disposition_filename": filename in headers.get("content-disposition", ""),
        "cache_control_private_no_store": "private" in headers.get("cache-control", "").lower() and "no-store" in headers.get("cache-control", "").lower(),
        "nosniff": headers.get("x-content-type-options", "").lower() == "nosniff",
    }
    return {"checked": True, "expected_filename": filename, "checks": checks, "passed": all(checks.values())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--rows-artifact", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--headers-json")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--expected-bytes", type=int)
    args = parser.parse_args()

    csv_path = Path(args.csv)
    checkpoint_path = Path(args.checkpoint)
    rows_path = Path(args.rows_artifact)
    output_path = Path(args.output)
    require(csv_path.is_file(), f"downloaded CSV missing: {csv_path}")
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    filters = checkpoint.get("filters") or {}

    artifact_identity = validate_rows_artifact(checkpoint, rows_path)
    reuse_native_reference = (
        args.expected_sha256 == FULL_YEAR_CSV_SHA
        and args.expected_bytes == FULL_YEAR_CSV_BYTES
        and hashlib.sha256(checkpoint_bytes).hexdigest() == FULL_YEAR_CHECKPOINT_SHA
    )
    if reuse_native_reference:
        expected_sha, expected_bytes = FULL_YEAR_CSV_SHA, FULL_YEAR_CSV_BYTES
        expected_rows = checkpoint["row_count"]
        expected_header = column_headers(checkpoint["columns"])
        fieldnames = column_fieldnames(checkpoint["columns"])
    else:
        expected_sha, expected_bytes, expected_rows, expected_header, fieldnames = expected_csv_digest(checkpoint, rows_path)
    if args.expected_sha256:
        require(expected_sha == args.expected_sha256, "native-reference CSV sha256 pin mismatch")
    if args.expected_bytes is not None:
        require(expected_bytes == args.expected_bytes, "native-reference CSV byte-count pin mismatch")
    actual_sha = sha256_file(csv_path)
    actual_bytes = csv_path.stat().st_size
    exact_bytes = actual_sha == expected_sha and actual_bytes == expected_bytes

    first_mismatch = None
    if exact_bytes:
        # Byte identity covers the header and all logical records, including quoted newlines.
        header_exact = True
        actual_row_count = expected_rows
    else:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                header = []
            header_exact = header == expected_header
            actual_row_count = sum(1 for _row in reader)
    row_count_exact = actual_row_count == checkpoint.get("row_count") == expected_rows

    if not exact_bytes:
        first_mismatch = diagnose_first_row_mismatch(csv_path, checkpoint, rows_path, fieldnames)

    headers_result = validate_headers(Path(args.headers_json), filters) if args.headers_json else {"checked": False}
    failures = []
    if not exact_bytes:
        failures.append("downloaded CSV bytes differ from independent native-stock ordered rows")
    if not row_count_exact:
        failures.append("downloaded CSV row count differs from native-stock checkpoint")
    if not header_exact:
        failures.append("downloaded CSV header differs from native-stock report labels")
    if headers_result.get("checked") and not headers_result.get("passed"):
        failures.append("download response headers failed original workload/security checks")

    receipt = {
        "schema_version": "original-export-csv-verification-v1",
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "passed": not failures,
        "csv": str(csv_path),
        "checkpoint": str(checkpoint_path),
        "rows_artifact": str(rows_path),
        "native_stock_artifact_identity": artifact_identity,
        "native_reference_reused": reuse_native_reference,
        "filters": filters,
        "comparison_mode": "exact byte-for-byte CSV rows and ordering generated from unmodified ERPNext native-stock detail checkpoint",
        "expected_csv": {"sha256": expected_sha, "bytes": expected_bytes, "data_rows": expected_rows},
        "expected_csv_pin": {"sha256": args.expected_sha256, "bytes": args.expected_bytes},
        "actual_csv": {"sha256": actual_sha, "bytes": actual_bytes, "data_rows": actual_row_count},
        "header_exact": header_exact,
        "row_count_exact": row_count_exact,
        "exact_bytes": exact_bytes,
        "headers": headers_result,
        "first_mismatch": first_mismatch,
        "failures": failures,
        "memory_bound": "streams the compressed native-stock rows and downloaded CSV; does not materialize full detail rows",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(output_path, 0o600)
    print(json.dumps({"passed": receipt["passed"], "exact_bytes": exact_bytes, "rows": actual_row_count, "output": str(output_path)}, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"verify-original-export-csv ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
