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
import time
from datetime import datetime, timezone

PROJECT = "khitmee-perf"


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


def collection_status(nginx_count, gunicorn_count):
    problems = []
    if nginx_count and not gunicorn_count:
        problems.append("nginx upstream attribution rows exist but zero Gunicorn application records parsed")
    return {"collection_passed": not problems, "collection_problems": problems}


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
    nginx_rows = parse_nginx_log(artifact / "nginx-attribution.log")
    gunicorn_rows = parse_gunicorn_log(artifact / "gunicorn.log")
    joined, missing = join_rows(nginx_rows, gunicorn_rows)
    with (artifact / "request-attribution.csv").open("w", newline="") as handle:
        fieldnames = ["request_id", "endpoint_class", "method", "uri", "status", "request_time_ms", "upstream_connect_ms", "upstream_header_ms", "upstream_response_ms", "gunicorn_service_ms", "estimated_dispatch_to_entry_ms", "join_status"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(joined)
    status = collection_status(len(nginx_rows), len(gunicorn_rows))
    collection_passed = status["collection_passed"]
    collection_problems = status["collection_problems"]
    summary = {
        "schema_version": "seasonal-request-attribution-v2",
        "collection_passed": collection_passed,
        "collection_problems": collection_problems,
        "queue_time_semantics": "estimated_dispatch_to_entry_ms = nginx upstream_response_time minus Gunicorn service duration; includes transport/proxy dispatch and is not a true nginx upstream_queue_time",
        "nginx_rows": len(nginx_rows),
        "gunicorn_rows": len(gunicorn_rows),
        "joined_rows": len(joined) - missing,
        "unjoined_nginx_rows": missing,
        "endpoint_classes": endpoint_summary(joined),
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
        child.set_defaults(func=func)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
