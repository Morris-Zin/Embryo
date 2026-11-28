#!/usr/bin/env python3
"""Collect and join k6 request IDs through nginx and Gunicorn.

The queue field reported here is explicitly an estimate: nginx upstream response
time minus Gunicorn service duration. It includes proxy dispatch/transport and any
worker admission wait; nginx cannot expose a true upstream queue time unless built
with the upstream-queue feature.
"""

import argparse
import csv
import json
import math
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import verify_run
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import verify_run

PROJECT = "khitmee-perf"
# At least 95% of nginx upstream rows must join to a Gunicorn application
# record. This allows a small amount of infrastructure/probe noise while
# rejecting disjoint request-id streams or systematically unparsed app logs.
MIN_JOIN_RATIO = 0.95
EXPECTED_ENDPOINT_CLASSES = ["cashier_submit", "search", "login/bootstrap", "manager_summary", "manager_detail", "manager_export"]
WINDOW_FILTER_TOLERANCE_SECONDS = 1.0


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run(cmd, timeout=20):
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
        return {"ok": result.returncode == 0, "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as exc:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": str(exc)}


def read_db_password(docker_dir):
    try:
        for line in (pathlib.Path(docker_dir) / ".env").read_text().splitlines():
            if line.startswith("DB_PASSWORD="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return ""


def redis_info(service):
    return run(["docker", "exec", f"{PROJECT}-{service}-1", "redis-cli", "INFO", "commandstats"])


def redis_latency(service):
    return run(["docker", "exec", f"{PROJECT}-{service}-1", "timeout", "2", "redis-cli", "--latency"], timeout=5)


def db_locks(docker_dir):
    password = read_db_password(docker_dir)
    sql = "SHOW GLOBAL STATUS WHERE Variable_name IN ('Innodb_row_lock_current_waits','Innodb_row_lock_time','Innodb_row_lock_time_avg','Innodb_row_lock_time_max','Innodb_row_lock_waits')"
    return run([
        "docker", "exec", "-e", f"MYSQL_PWD={password}", f"{PROJECT}-db-1",
        "mariadb", "-uroot", "-N", "-B", "-e", sql,
    ])


def snapshot(docker_dir):
    return {
        "at": utcnow(),
        "redis_cache_commandstats": redis_info("redis-cache"),
        "redis_queue_commandstats": redis_info("redis-queue"),
        "redis_cache_latency": redis_latency("redis-cache"),
        "redis_queue_latency": redis_latency("redis-queue"),
        "db_innodb_row_locks": db_locks(docker_dir),
    }


def commandstats(text):
    rows = {}
    for line in text.splitlines():
        if not line.startswith("cmdstat_"):
            continue
        name = line.split(":", 1)[0][len("cmdstat_"):]
        values = {}
        for part in line.split(":", 1)[1].split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                try:
                    values[key] = float(value)
                except ValueError:
                    pass
        rows[name] = values
    return rows


def row_locks(text):
    rows = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                rows[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return rows


def latency_snapshot(text):
    # redis-cli --latency emits lines such as: min: 0, max: 3, avg: 0.42 (96 samples)
    values = []
    for match in re.finditer(r"min:\s*([0-9.]+),\s*max:\s*([0-9.]+),\s*avg:\s*([0-9.]+)", text):
        values.append({"min_ms": float(match.group(1)), "max_ms": float(match.group(2)), "avg_ms": float(match.group(3))})
    return values


def pct(values, quantile):
    values = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not values:
        return None
    return values[max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))]


def finite_float(value):
    try:
        if value in ("-", "", None):
            return None
        # nginx uses comma-separated values for retries/multi-upstreams; use the last.
        return float(str(value).split(",")[-1].strip())
    except Exception:
        return None


def parse_nginx_log(path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        rows.append({
            "time": row.get("time") or "",
            "request_id": row.get("request_id") or "",
            "endpoint_class": row.get("endpoint_class") or "unclassified",
            "method": row.get("method") or "",
            "uri": row.get("uri") or "",
            "status": int(row.get("status") or 0),
            "request_time_ms": (finite_float(row.get("request_time")) or 0) * 1000,
            "upstream_connect_ms": (finite_float(row.get("upstream_connect_time")) or 0) * 1000,
            "upstream_header_ms": (finite_float(row.get("upstream_header_time")) or 0) * 1000,
            "upstream_response_ms": (finite_float(row.get("upstream_response_time")) or 0) * 1000,
        })
    return rows


def parse_gunicorn_log(path):
    rows = {}
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        # Docker log lines may have a timestamp prefix before the pipe-delimited access record.
        if "|" not in line:
            continue
        match = re.search(r"([^|\s]+)\|([^|\s]+)\|([^|\s]+)\|([^|\s]+)\|([^|]*)\|([^|\s]+)\|([^|\s]+)\|([^|\s]+)", line)
        if not match:
            continue
        request_id, endpoint_class, method, path_part, query, status, duration_usec, pid = match.groups()
        if request_id in ("-", ""):
            continue
        try:
            # Gunicorn's %(p)s expansion is bracketed as <42>. Accept bare
            # digits only for older hand-written fixtures; angle brackets are
            # the authoritative runtime form.
            pid_match = re.fullmatch(r"<(\d+)>|(\d+)", pid)
            if not pid_match:
                raise ValueError("unparseable gunicorn pid " + pid)
            rows[request_id] = {
                "request_id": request_id,
                "endpoint_class": endpoint_class,
                "method": method,
                "path": path_part,
                "query": query,
                "status": int(status),
                "service_ms": float(duration_usec) / 1000.0,
                "pid": int(pid_match.group(1) or pid_match.group(2)),
            }
        except ValueError:
            continue
    return rows


def join_rows(nginx_rows, gunicorn_rows):
    joined = []
    missing = 0
    for row in nginx_rows:
        out = dict(row)
        app = gunicorn_rows.get(row["request_id"])
        if app:
            out["gunicorn_service_ms"] = app["service_ms"]
            out["estimated_dispatch_to_entry_ms"] = max(0.0, row["upstream_response_ms"] - app["service_ms"])
            out["join_status"] = "joined"
        else:
            out["gunicorn_service_ms"] = None
            out["estimated_dispatch_to_entry_ms"] = None
            out["join_status"] = "nginx_only"
            missing += 1
        joined.append(out)
    return joined, missing


def endpoint_summary(joined):
    classes = ["cashier_submit", "search", "login/bootstrap", "manager_summary", "manager_detail", "manager_export", "unclassified"]
    result = {}
    for endpoint_class in classes:
        rows = [row for row in joined if (row.get("endpoint_class") or "unclassified") == endpoint_class]
        if not rows:
            continue
        result[endpoint_class] = {
            "count": len(rows),
            "joined_count": sum(1 for row in rows if row.get("join_status") == "joined"),
            "nginx_request_ms": {"p50": pct([r["request_time_ms"] for r in rows], .50), "p95": pct([r["request_time_ms"] for r in rows], .95)},
            "upstream_connect_ms": {"p50": pct([r["upstream_connect_ms"] for r in rows], .50), "p95": pct([r["upstream_connect_ms"] for r in rows], .95)},
            "upstream_header_ms": {"p50": pct([r["upstream_header_ms"] for r in rows], .50), "p95": pct([r["upstream_header_ms"] for r in rows], .95)},
            "upstream_response_ms": {"p50": pct([r["upstream_response_ms"] for r in rows], .50), "p95": pct([r["upstream_response_ms"] for r in rows], .95)},
            "gunicorn_service_ms": {"p50": pct([r["gunicorn_service_ms"] for r in rows], .50), "p95": pct([r["gunicorn_service_ms"] for r in rows], .95)},
            "estimated_dispatch_to_entry_ms": {"p50": pct([r["estimated_dispatch_to_entry_ms"] for r in rows], .50), "p95": pct([r["estimated_dispatch_to_entry_ms"] for r in rows], .95)},
        }
    return result


def delta_commandstats(before_text, after_text):
    before = commandstats(before_text)
    after = commandstats(after_text)
    rows = []
    for name, end in after.items():
        start = before.get(name, {})
        calls = end.get("calls", 0) - start.get("calls", 0)
        usec = end.get("usec", 0) - start.get("usec", 0)
        if calls > 0:
            rows.append({"command": name, "calls": int(calls), "total_ms": usec / 1000.0, "avg_ms": usec / max(calls, 1) / 1000.0})
    return sorted(rows, key=lambda row: row["total_ms"], reverse=True)


def delta_locks(before_text, after_text):
    before = row_locks(before_text)
    after = row_locks(after_text)
    waits = after.get("Innodb_row_lock_waits", 0) - before.get("Innodb_row_lock_waits", 0)
    time_ms = after.get("Innodb_row_lock_time", 0) - before.get("Innodb_row_lock_time", 0)
    return {
        "row_lock_waits_delta": int(waits),
        "row_lock_time_ms_delta": time_ms,
        "row_lock_time_avg_ms_end": after.get("Innodb_row_lock_time_avg"),
        "row_lock_time_max_ms_end": after.get("Innodb_row_lock_time_max"),
    }


def parse_iso_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def parse_tag_selector(key):
    if "{" not in key or not key.endswith("}"):
        return key, {}
    metric, selector = key.split("{", 1)
    tags = {}
    for part in selector[:-1].split(","):
        if ":" in part:
            name, value = part.split(":", 1)
            tags[name.strip()] = value.strip()
    return metric, tags


def metric_count(metric):
    if not isinstance(metric, dict):
        return 0
    containers = [metric]
    if isinstance(metric.get("values"), dict):
        containers.append(metric["values"])
    for container in containers:
        for key in ("count", "value"):
            if key not in container:
                continue
            try:
                return int(container.get(key) or 0)
            except (TypeError, ValueError):
                pass
    return 0


def k6_endpoint_counts(summary):
    """Return actual per-endpoint-class HTTP request counts from k6 summary metrics."""
    counts = {}
    metrics = (summary or {}).get("metrics") or {}
    for key, metric in metrics.items():
        name, tags = parse_tag_selector(key)
        if name != "http_reqs" or not tags:
            continue
        endpoint_class = tags.get("endpoint_class")
        if endpoint_class:
            counts[endpoint_class] = counts.get(endpoint_class, 0) + metric_count(metric)
    return counts


def configured_run_id(k6_log):
    try:
        config = verify_run.parse_events(k6_log).get("config") or {}
    except Exception:
        return ""
    return str(config.get("run_id") or "").strip()


def scope_rows_to_run(nginx_rows, gunicorn_rows, run_id):
    prefix = str(run_id or "").strip() + "-"
    if not str(run_id or "").strip():
        return [], {}, len(nginx_rows)
    scoped_nginx = [row for row in nginx_rows if str(row.get("request_id") or "").startswith(prefix)]
    scoped_gunicorn = {key: value for key, value in gunicorn_rows.items() if str(key).startswith(prefix)}
    return scoped_nginx, scoped_gunicorn, len(nginx_rows) - len(scoped_nginx)


def filter_rows_to_window(rows, measured_start, measured_end, tolerance_seconds=WINDOW_FILTER_TOLERANCE_SECONDS):
    if measured_start is None or measured_end is None:
        return rows, 0
    start = measured_start.timestamp() - tolerance_seconds
    end = measured_end.timestamp() + tolerance_seconds
    scoped = []
    excluded = 0
    for row in rows:
        stamp = parse_iso_time(row.get("time"))
        if stamp is not None and start <= stamp.timestamp() <= end:
            scoped.append(row)
        else:
            excluded += 1
    return scoped, excluded


def endpoint_coverage(joined, expected_counts, minimum_ratio=MIN_JOIN_RATIO, required_classes=()):
    rows = {}
    problems = []
    classes = sorted(set(required_classes) | set(expected_counts) | {row.get("endpoint_class") or "unclassified" for row in joined})
    for endpoint_class in classes:
        class_rows = [row for row in joined if (row.get("endpoint_class") or "unclassified") == endpoint_class]
        nginx_count = len(class_rows)
        joined_count = sum(1 for row in class_rows if row.get("join_status") == "joined")
        expected = int(expected_counts.get(endpoint_class) or 0)
        nginx_ratio = nginx_count / expected if expected else None
        joined_ratio = joined_count / expected if expected else None
        rows[endpoint_class] = {
            "k6_requests": expected,
            "nginx_rows": nginx_count,
            "joined_rows": joined_count,
            "nginx_coverage_ratio": nginx_ratio,
            "joined_coverage_ratio": joined_ratio,
            "coverage_passed": bool(expected) and nginx_ratio is not None and joined_ratio is not None and nginx_ratio >= minimum_ratio and joined_ratio >= minimum_ratio,
        }
        if expected <= 0:
            if endpoint_class in set(required_classes):
                problems.append(f"{endpoint_class}: missing or zero k6 request denominator")
        else:
            if nginx_ratio is None or nginx_ratio < minimum_ratio:
                problems.append(f"{endpoint_class}: nginx rows cover {nginx_count}/{expected} k6 requests, below {minimum_ratio:.2f}")
            if joined_ratio is None or joined_ratio < minimum_ratio:
                problems.append(f"{endpoint_class}: joined rows cover {joined_count}/{expected} k6 requests, below {minimum_ratio:.2f}")
    return {"classes": rows, "problems": problems}


def measured_window_from_log(path):
    start = None
    end = None
    try:
        parsed = verify_run.parse_events(path)
    except Exception:
        return None, None
    for event in parsed.get("events") or []:
        if event.get("event") == "measured_window_start":
            start = parse_iso_time(event.get("at"))
        elif event.get("event") == "measured_window_end":
            end = parse_iso_time(event.get("at"))
    return start, end


def nginx_window_coverage(nginx_rows, measured_start, measured_end, tolerance_seconds=30.0):
    times = [parse_iso_time(row.get("time")) for row in nginx_rows]
    times = [value for value in times if value is not None]
    result = {
        "measured_start": measured_start.isoformat().replace("+00:00", "Z") if measured_start else None,
        "measured_end": measured_end.isoformat().replace("+00:00", "Z") if measured_end else None,
        "first_nginx_at": min(times).isoformat().replace("+00:00", "Z") if times else None,
        "last_nginx_at": max(times).isoformat().replace("+00:00", "Z") if times else None,
        "tolerance_seconds": tolerance_seconds,
        "passed": False,
        "problems": [],
    }
    if measured_start is None or measured_end is None:
        result["problems"].append("k6 measured-window start/end events are missing")
    if not times:
        result["problems"].append("nginx attribution rows lack parseable first/last timestamps")
    if result["problems"]:
        return result
    first = min(times)
    last = max(times)
    if (measured_start - first).total_seconds() > tolerance_seconds:
        result["problems"].append("first nginx attribution timestamp predates the measured window start")
    if (first - measured_start).total_seconds() > tolerance_seconds:
        result["problems"].append("first nginx attribution timestamp is too late to cover the measured window start")
    if (measured_end - last).total_seconds() > tolerance_seconds:
        result["problems"].append("last nginx attribution timestamp is too early to cover the measured window end")
    result["passed"] = not result["problems"]
    return result


def db_lock_snapshot_status(snapshot, label):
    evidence = (snapshot or {}).get("db_innodb_row_locks") or {}
    stdout = str(evidence.get("stdout") or "")
    rows_present = bool(stdout.strip())
    ok = evidence.get("ok") is True and rows_present
    return {
        "label": label,
        "command_ok": evidence.get("ok") is True,
        "returncode": evidence.get("rc"),
        "rows_present": rows_present,
        "passed": ok,
        "problem": "" if ok else f"MariaDB InnoDB row-lock {label} snapshot failed or returned no rows",
    }


def collection_status(nginx_count, gunicorn_count, joined_count=None, extra_problems=None):
    problems = list(extra_problems or [])
    if joined_count is None:
        joined_count = min(nginx_count, gunicorn_count)
    join_ratio = (joined_count / nginx_count) if nginx_count else 0.0
    if not nginx_count:
        problems.append("nginx attribution log missing, unreadable, or contains no upstream rows")
    if nginx_count and not gunicorn_count:
        problems.append("nginx upstream attribution rows exist but zero Gunicorn application records parsed")
    if nginx_count and joined_count <= 0:
        problems.append("zero nginx upstream requests joined to Gunicorn application records")
    if nginx_count and join_ratio < MIN_JOIN_RATIO:
        problems.append(f"request-id join ratio {join_ratio:.3f} is below required minimum {MIN_JOIN_RATIO:.2f}")
    return {
        "collection_passed": not problems,
        "collection_problems": problems,
        "join_ratio": join_ratio,
        "minimum_join_ratio": MIN_JOIN_RATIO,
    }


def cmd_start(args):
    artifact = pathlib.Path(args.artifact_dir)
    artifact.mkdir(parents=True, exist_ok=True)
    data = snapshot(args.docker_dir)
    (artifact / "attribution-start.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return 0


def cmd_finish(args):
    artifact = pathlib.Path(args.artifact_dir)
    end = snapshot(args.docker_dir)
    (artifact / "attribution-end.json").write_text(json.dumps(end, indent=2, sort_keys=True) + "\n")
    frontend_log = run(["docker", "exec", f"{PROJECT}-frontend-1", "sh", "-c", "cat /var/log/nginx/seasonal-attribution.log 2>/dev/null || true"])
    backend_logs = run(["docker", "logs", f"{PROJECT}-backend-1"], timeout=30)
    cashier_logs = run(["docker", "logs", f"{PROJECT}-cashier-1"], timeout=30)
    (artifact / "nginx-attribution.log").write_text(frontend_log["stdout"], errors="replace")
    (artifact / "gunicorn.log").write_text(
        backend_logs["stdout"] + backend_logs["stderr"] + cashier_logs["stdout"] + cashier_logs["stderr"],
        errors="replace",
    )
    try:
        start = json.loads((artifact / "attribution-start.json").read_text())
    except Exception:
        start = {}
    raw_nginx_rows = parse_nginx_log(artifact / "nginx-attribution.log")
    raw_gunicorn_rows = parse_gunicorn_log(artifact / "gunicorn.log")
    measured_start, measured_end = measured_window_from_log(args.k6_log) if args.k6_log else (None, None)
    run_id = str(getattr(args, "run_id", "") or "").strip() or configured_run_id(args.k6_log)
    run_nginx_rows, run_gunicorn_rows, out_of_run_rows = scope_rows_to_run(raw_nginx_rows, raw_gunicorn_rows, run_id)
    nginx_rows, out_of_window_rows = filter_rows_to_window(run_nginx_rows, measured_start, measured_end)
    joined, missing = join_rows(nginx_rows, run_gunicorn_rows)
    with (artifact / "request-attribution.csv").open("w", newline="") as handle:
        preferred = ["time", "request_id", "endpoint_class", "method", "uri", "status", "request_time_ms", "upstream_connect_ms", "upstream_header_ms", "upstream_response_ms", "gunicorn_service_ms", "estimated_dispatch_to_entry_ms", "join_status"]
        present = sorted({key for row in joined for key in row.keys()})
        fieldnames = [key for key in preferred if key in present] + [key for key in present if key not in preferred]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(joined)
    joined_count = len(joined) - missing
    try:
        k6_summary = json.loads(pathlib.Path(args.k6_summary).read_text(encoding="utf-8")) if args.k6_summary else {}
    except Exception:
        k6_summary = {}
    expected_counts = k6_endpoint_counts(k6_summary)
    coverage = endpoint_coverage(joined, expected_counts, required_classes=EXPECTED_ENDPOINT_CLASSES)
    window = nginx_window_coverage(nginx_rows, measured_start, measured_end)
    coverage_problems = []
    if not expected_counts:
        coverage_problems.append("k6 summary is missing per-endpoint-class http_reqs counts")
    if not run_id:
        coverage_problems.append("run-scoped request-id prefix is missing")
    if run_id and not run_nginx_rows:
        coverage_problems.append("no nginx attribution rows match the run-scoped request-id prefix")
    coverage_problems.extend(coverage["problems"])
    coverage_problems.extend(window["problems"])
    db_lock_snapshots = {
        "start": db_lock_snapshot_status(start, "start"),
        "end": db_lock_snapshot_status(end, "end"),
    }
    coverage_problems.extend(row["problem"] for row in db_lock_snapshots.values() if row["problem"])
    status = collection_status(len(nginx_rows), len(run_gunicorn_rows), joined_count, coverage_problems)
    collection_passed = status["collection_passed"]
    collection_problems = status["collection_problems"]
    summary = {
        "schema_version": "seasonal-request-attribution-v2",
        "collection_passed": collection_passed,
        "collection_problems": collection_problems,
        "join_ratio": status["join_ratio"],
        "minimum_join_ratio": status["minimum_join_ratio"],
        "queue_time_semantics": "estimated_dispatch_to_entry_ms = nginx upstream_response_time minus Gunicorn service duration; includes transport/proxy dispatch and is not a true nginx upstream_queue_time",
        "run_id": run_id,
        "request_id_prefix": (run_id + "-") if run_id else "",
        "nginx_rows": len(nginx_rows),
        "gunicorn_rows": len(run_gunicorn_rows),
        "nginx_rows_raw": len(raw_nginx_rows),
        "gunicorn_rows_raw": len(raw_gunicorn_rows),
        "excluded_out_of_run_rows": out_of_run_rows,
        "excluded_out_of_window_rows": out_of_window_rows,
        "window_filter_tolerance_seconds": WINDOW_FILTER_TOLERANCE_SECONDS,
        "joined_rows": joined_count,
        "unjoined_nginx_rows": missing,
        "endpoint_classes": endpoint_summary(joined),
        "k6_endpoint_request_counts": expected_counts,
        "endpoint_coverage": coverage["classes"],
        "window_coverage": window,
        "db_lock_snapshots": db_lock_snapshots,
        "redis_cache_commandstats_delta": delta_commandstats((start.get("redis_cache_commandstats") or {}).get("stdout", ""), (end.get("redis_cache_commandstats") or {}).get("stdout", "")),
        "redis_queue_commandstats_delta": delta_commandstats((start.get("redis_queue_commandstats") or {}).get("stdout", ""), (end.get("redis_queue_commandstats") or {}).get("stdout", "")),
        "redis_cache_latency_end": latency_snapshot((end.get("redis_cache_latency") or {}).get("stdout", "") + (end.get("redis_cache_latency") or {}).get("stderr", "")),
        "redis_queue_latency_end": latency_snapshot((end.get("redis_queue_latency") or {}).get("stdout", "") + (end.get("redis_queue_latency") or {}).get("stderr", "")),
        "db_innodb_row_locks_delta": delta_locks((start.get("db_innodb_row_locks") or {}).get("stdout", ""), (end.get("db_innodb_row_locks") or {}).get("stdout", "")),
    }
    (artifact / "attribution-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0 if collection_passed else 1


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("start", cmd_start), ("finish", cmd_finish)):
        child = sub.add_parser(name)
        child.add_argument("--artifact-dir", required=True)
        child.add_argument("--docker-dir", required=True)
        child.add_argument("--k6-summary", default="")
        child.add_argument("--k6-log", default="")
        child.add_argument("--run-id", default="")
        child.set_defaults(func=func)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
