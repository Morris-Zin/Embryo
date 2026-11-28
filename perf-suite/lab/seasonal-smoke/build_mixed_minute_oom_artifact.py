#!/usr/bin/env python3
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

REPO = Path("/mnt/e/Khitmee-ERPNext")
sys.path.insert(0, str(REPO / "perf-suite/lab/driver"))
import process_kernel_telemetry as pkt

OUT = REPO / "perf-suite/lab/seasonal-smoke/mixed-minute-oom-attribution-2026-11-27.json"
SUMMARY_PATH = REPO / "perf-suite/lab/seasonal-smoke/mixed-minute-year-summary.json"
PORTABLE_PATH = REPO / "perf-suite/lab/seasonal-smoke/mixed-minute-year-2026-11-27.json.gz"
OPERATOR = Path("/srv/khitmee-lab/operator/year-minute-arms/e1-s42/measured-v2")
SSH_CONFIG = "/var/lib/khitmee-experiment-guest/identity/ssh_config"
RELEASE = Path("/srv/khitmee-lab/operator/releases/year-mixed-minute-v2")
PAGE_SIZE = 4096


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def remote_output(command):
    return subprocess.check_output(
        ["ssh", "-F", SSH_CONFIG, "khitmee-lab", command], stderr=subprocess.PIPE, timeout=60)


def load_kernel_records():
    cmd = ("sudo journalctl -k --since '2026-11-27 07:13:50' --until '2026-11-27 07:14:35' "
           "-o json --no-pager")
    raw = remote_output(cmd).decode(errors="replace")
    records = []
    for line in raw.splitlines():
        if line.startswith("{"):
            records.append(json.loads(line))
    return records


def task_with_bytes(row):
    out = dict(row)
    for field in ("total_vm", "rss", "rss_anon", "rss_file", "rss_shmem"):
        if row.get(field) is not None:
            out[field + "_bytes"] = row[field] * PAGE_SIZE
    return out


def classification(event):
    kind = event.get("event")
    if kind in ("voucher_start", "voucher_complete", "voucher_reject"):
        voucher_type = event.get("type")
        if voucher_type == "sale":
            return "voucher:sale:" + str(event.get("line_count")), "/api/method/khitmee_ops.api.vouchers.submit_sale"
        if voucher_type == "transfer":
            return "voucher:transfer:" + str(event.get("line_count")), "/api/method/khitmee_ops.api.vouchers.submit_transfer"
        return "voucher:" + str(voucher_type) + ":" + str(event.get("line_count")), "/api/method/frappe.desk.form.save.savedocs"
    if kind in ("search_complete", "search_reject"):
        return "search", "/api/method/khitmee_ops.api.search.search_items"
    if kind in ("manager_summary", "manager_summary_reject", "manager_detail", "manager_detail_reject"):
        return "manager_report:" + str(event.get("group_by") or "detail"), "/api/method/frappe.desk.query_report.run"
    return str(kind), "unmapped"


def route_window(events, victim_ts):
    terminal = {"voucher_complete", "voucher_reject", "search_complete", "search_reject",
                "manager_summary", "manager_summary_reject", "manager_detail", "manager_detail_reject"}
    active = []
    for event in events:
        if event.get("event") not in terminal or "duration_ms" not in event or "at" not in event:
            continue
        end = parse_time(event["at"])
        start = end - float(event["duration_ms"]) / 1000.0
        if start <= victim_ts <= end:
            label, endpoint = classification(event)
            active.append({"label": label, "endpoint": endpoint, "start": start, "end": end, "event": event})
    counts = {}
    for row in active:
        key = row["label"] + " " + row["endpoint"]
        counts[key] = counts.get(key, 0) + 1
    nearest = []
    for row in sorted(active, key=lambda item: item["end"] - victim_ts)[:10]:
        event = row["event"]
        nearest.append({
            "terminal_event": event.get("event"),
            "label": row["label"],
            "endpoint": row["endpoint"],
            "client_operation_started_at": iso(row["start"]),
            "client_operation_terminal_at": iso(row["end"]),
            "terminal_seconds_after_victim_selection": round(row["end"] - victim_ts, 3),
            "client_voucher_id": event.get("client_voucher_id"),
            "observation_id": event.get("observation_id"),
            "group_by": event.get("group_by"),
            "reason": event.get("reason"),
        })
    return counts, nearest


def relevant_kernel_line(message):
    return bool(
        "invoked oom-killer" in message or re.search(r"CPU: \d+ PID: \d+ Comm:", message)
        or message.startswith("memory: usage ") or message.startswith("Memory cgroup stats for ")
        or message.startswith("[  pid  ]") or re.match(r"\[\s*\d+\]", message)
        or message.startswith("oom-kill:") or message.startswith("Memory cgroup out of memory: Killed process")
    )


def main():
    summary = json.loads(SUMMARY_PATH.read_bytes())
    telemetry = json.loads((OPERATOR / "arm-telemetry.json").read_bytes())
    provenance = json.loads((OPERATOR / "mixed-provenance.json").read_bytes())
    events = json.load(gzip.open(OPERATOR / "mixed-events.json.gz", "rt", encoding="utf-8"))

    artifact_hashes = {}
    all_match = True
    for name, expected in sorted(summary["operator_artifacts"]["sha256"].items()):
        actual = sha(OPERATOR / name)
        artifact_hashes[name] = {"sha256": actual, "matches_receipt": actual == expected}
        all_match = all_match and actual == expected
    if not all_match:
        raise RuntimeError("protected artifact digest mismatch")

    portable_actual = sha(PORTABLE_PATH)
    portable_expected = summary["portable_evidence"]["sha256"]
    if portable_actual != portable_expected:
        raise RuntimeError("portable evidence digest mismatch")

    manifest_actual = sha(RELEASE / "manifest.json")
    manifest_expected = summary["protected_harness_manifest_sha256"]
    if manifest_actual != manifest_expected:
        raise RuntimeError("protected harness manifest digest mismatch")
    manifest = json.loads((RELEASE / "manifest.json").read_text())
    runtime_rel = "perf-suite/k6/lib/seasonal_runtime.js"
    runtime_path = RELEASE / "harness" / runtime_rel
    runtime_hash = sha(runtime_path)
    if runtime_hash != manifest[runtime_rel] or runtime_hash != provenance["script_hashes"]["k6/lib/seasonal_runtime.js"]:
        raise RuntimeError("seasonal runtime digest mismatch")

    kernel_records = load_kernel_records()
    oom_events = pkt.parse_oom_events(kernel_records)
    if len(oom_events) != 2:
        raise RuntimeError("expected exactly two OOM events")
    if [event["victim"]["pid"] for event in oom_events] != [11408, 11409]:
        raise RuntimeError("unexpected victim sequence")
    if [event["allocator"]["tid"] for event in oom_events] != [12646, 12648]:
        raise RuntimeError("unexpected allocator sequence")

    backend_identity = telemetry["targets"]["backend"]["identity"]
    backend_cgroup = telemetry["targets"]["backend"]["cgroup_path"]
    boot_id = telemetry["raw"]["start"]["target"]["boot_id"].replace("-", "")
    journal_boots = {record.get("_BOOT_ID", "").replace("-", "") for record in kernel_records if record.get("_BOOT_ID")}
    if journal_boots != {boot_id}:
        raise RuntimeError("kernel journal boot identity mismatch")

    guest_profile_text = remote_output(
        "sudo cat /srv/khitmee-reference/perf-suite/docker/profile-2gb.yaml; "
        "printf '\\n__SHA256__\\n'; sudo sha256sum /srv/khitmee-reference/perf-suite/docker/profile-2gb.yaml")
    guest_text = guest_profile_text.decode(errors="replace")
    guest_hash = re.search(r"__SHA256__\n([0-9a-f]{64}) ", guest_text).group(1)
    backend_start = guest_text.find("  backend:" + chr(10))
    queue_start = guest_text.find(chr(10) + "  queue:", backend_start)
    if backend_start < 0 or queue_start < 0:
        raise RuntimeError("backend compose block missing")
    backend_body = guest_text[backend_start:queue_start]
    command_args = re.findall(r"^      - (.*)$", backend_body, re.M)
    expected_args = ["/home/frappe/frappe-bench/env/bin/gunicorn", "--chdir=/home/frappe/frappe-bench/sites",
                     "--bind=0.0.0.0:8000", "--threads=2", "--workers=2", "--worker-class=gthread",
                     "--worker-tmp-dir=/dev/shm", "--timeout=120", "--keep-alive=65", "--preload",
                     "--max-requests=1500", "--max-requests-jitter=300", "frappe.app:application"]
    if command_args != expected_args:
        raise RuntimeError("protected backend command mismatch")

    exact_events = []
    route_events = []
    excerpts = []
    for event in oom_events:
        victim_ts = event["victim_selection"]["timestamp_us"] / 1_000_000
        counts, nearest = route_window(events, victim_ts)
        route_events.append({"victim_selection_at": event["victim_selection"]["timestamp"],
                             "active_client_operation_counts": counts,
                             "nearest_terminal_operations": nearest,
                             "caveat": "Client operation spans include retry/backoff time; they are temporal correlations, not proof that a server thread or victim PID was executing that route."})
        tasks = [task_with_bytes(row) for row in event["tasks"]]
        exact = {
            "invoked_at": event["invoked_at"],
            "allocator": {
                "kernel_reported_task_pid": event["allocator"]["tid"],
                "comm": event["allocator"]["comm"],
                "kernel_timestamp": event["allocator"]["timestamp"],
                "allocation_context": "GFP_KERNEL order=0 anonymous-page charge; mem_cgroup_out_of_memory call chain",
                "owning_tgid_process": None,
                "owning_process_resolution": "not recorded in prior evidence",
            },
            "memory_cgroup": event["memory_cgroup"],
            "memory_cgroup_path": event["memory_cgroup_path"],
            "constraint": event["victim_selection"]["constraint"],
            "cpuset": event["oom_kill"].get("cpuset"),
            "oom_memcg": event["victim_selection"]["oom_memcg"],
            "task_memcg": event["victim_selection"]["task_memcg"],
            "victim": {
                "pid": event["victim"]["pid"],
                "tgid": event["victim"]["pid"],
                "comm": event["victim"]["comm"],
                "uid": event["victim"]["uid"],
                "selected_at": event["victim_selection"]["timestamp"],
                "killed_report_at": event["victim"]["timestamp"],
                "total_vm_bytes": event["victim"]["total_vm_bytes"],
                "rss_bytes": event["victim"]["rss_bytes"],
                "rss_anon_bytes": event["victim"]["rss_anon_bytes"],
                "rss_file_bytes": event["victim"]["rss_file_bytes"],
                "rss_shmem_bytes": event["victim"]["rss_shmem_bytes"],
                "pgtables_bytes": event["victim"]["pgtables_bytes"],
                "oom_score_adj": event["victim"]["oom_score_adj"],
            },
            "tasks_at_selection": tasks,
        }
        exact_events.append(exact)
        for index in event["raw_record_indexes"]:
            record = kernel_records[index]
            message = record.get("MESSAGE", "")
            if relevant_kernel_line(message):
                excerpts.append({"realtime": pkt.iso_from_unix_us(record["__REALTIME_TIMESTAMP"]),
                                 "message": pkt.sanitize_text(message)})

    samples = []
    for sample in telemetry["raw"]["interval_samples"]:
        samples.append({"collected_at": sample["collected_at"],
                        "backend_cgroup_memory_bytes": sample["cgroup_memory_bytes"]["backend"],
                        "db_cgroup_memory_bytes": sample["cgroup_memory_bytes"]["db"],
                        "guest_memory_bytes": sample["resource"]["memory_bytes"]})

    sql_context = None
    for ranking in telemetry.get("rankings", []):
        if ranking.get("metric") == "sql_digest_time_delta_ms" and ranking.get("entries"):
            top = ranking["entries"][0]
            sql_context = {"digest": top["digest"], "time_ms_delta": top["value"],
                           "share_of_measured_sql_digest_time": top["share_of_measured_total"],
                           "digest_text": top["digest_text"],
                           "interpretation_limit": "SQL digest time is cumulative across concurrent executions and does not identify a process, PID, thread, or memory allocator."}

    endpoint_runtime_hash = runtime_hash
    artifact = {
        "schema": "khitmee.mixed-minute-oom-attribution/v1",
        "generated_at": pkt.utc_now(),
        "redaction": {
            "status": "redacted-derived-artifact",
            "excluded": ["actor credentials", "site/database credentials", "request payload bodies", "database rows",
                         "kernel register dumps and call traces that can contain memory fragments", "private keys"],
            "retained": ["kernel attribution lines", "container/process identity", "synthetic client operation identifiers",
                         "aggregate SQL/cgroup measurements"],
        },
        "verification": {
            "read_only_actions": ["read operator artifact bytes and hashes", "read guest kernel journal with journalctl",
                                   "read protected compose profile", "no workload, restore, reset, or container mutation"],
            "operator_folder": str(OPERATOR),
            "operator_artifact_sha256": artifact_hashes,
            "portable_evidence": {"path": "perf-suite/lab/seasonal-smoke/mixed-minute-year-2026-11-27.json.gz",
                                   "sha256": portable_actual, "matches_receipt": True},
            "summary_file": {"path": "perf-suite/lab/seasonal-smoke/mixed-minute-year-summary.json",
                              "sha256": sha(SUMMARY_PATH)},
            "protected_harness_manifest": {"path": str(RELEASE / "manifest.json"), "sha256": manifest_actual,
                                            "matches_receipt": True},
            "kernel_journal_boot_id_matches_arm_boot_id": True,
            "arm_boot_id": telemetry["raw"]["start"]["target"]["boot_id"],
            "guest_backend_compose_profile": {"path": "/srv/khitmee-reference/perf-suite/docker/profile-2gb.yaml",
                                               "sha256": guest_hash, "backend_command": command_args},
            "k6_endpoint_source": {"path": str(runtime_path), "sha256": endpoint_runtime_hash},
        },
        "arm": {
            "namespace": telemetry["raw"]["start"]["namespace"],
            "fixture_sha256": provenance["fixture_sha256"],
            "workload": "year-mixed-minute-diagnostic-v1",
            "run_id": provenance["run_id"],
            "seed": provenance["seed"],
            "backend": {"container_id": backend_identity["container_id"], "image_id": backend_identity["image_id"],
                         "container_init_pid": backend_identity["pid"], "started_at": backend_identity["started_at"],
                         "cgroup_path": backend_cgroup,
                         "memory_limit_bytes": telemetry["targets"]["backend"]["memory"]["limit_bytes"],
                         "command_from_protected_profile": command_args},
        },
        "oom_events": exact_events,
        "process_role_assessment": {
            "verified": [
                "PID 11180 was the backend container init PID in the arm telemetry boundary.",
                "The protected backend command was gunicorn with --workers=2, --threads=2 and --worker-class=gthread.",
                "Kernel OOM task tables list PID 11180, 11408 and 11409 with comm gunicorn in the backend container memcg; 11409 was the victim in the second event and 13595 was another gunicorn process present after the first kill.",
            ],
            "inferred_not_independently_logged": [
                "PIDs 11408 and 11409 were the two gunicorn worker processes rather than the container-init/master process. This follows from the protected gunicorn process model and container init identity, but the prior evidence does not retain their PPid or /proc status.",
                "The comm ThreadPoolExecu is consistent with a Python/gunicorn gthread pool worker thread. The prior evidence does not record which TGID owned kernel task PIDs 12646 or 12648.",
            ],
        },
        "memory_timeline": samples,
        "sql_context": sql_context,
        "route_correlation": {
            "status": "temporal-correlation-only; no route-to-PID/thread attribution is evidenced",
            "endpoint_map": {
                "sale": "/api/method/khitmee_ops.api.vouchers.submit_sale",
                "transfer": "/api/method/khitmee_ops.api.vouchers.submit_transfer",
                "purchase_receipt_and_purchase_invoice": "/api/method/frappe.desk.form.save.savedocs",
                "search": "/api/method/khitmee_ops.api.search.search_items",
                "manager_summary_and_detail": "/api/method/frappe.desk.query_report.run",
            },
            "windows": route_events,
        },
        "exact_conclusions": [
            "Both OOM events were memcg-constrained kills in the backend container cgroup docker-f54f7734f1726fbde591997dcac17ee3d12effe9f79240bc39b20662ac0cfcc6.scope; no other service cgroup recorded an OOM kill in the telemetry window.",
            "At 2026-11-27T07:13:56.505394Z the kernel selected and killed PID/TGID 11408 (comm gunicorn, UID 1000): total VM 417853440 bytes, RSS 325341184 bytes (anon 319180800 + file 6160384), pgtables 774144 bytes. The allocating task was kernel PID 12646, comm ThreadPoolExecu.",
            "At 2026-11-27T07:14:29.716928Z the kernel selected and killed PID/TGID 11409 (comm gunicorn, UID 1000): total VM 584622080 bytes, RSS 469823488 bytes (anon 464580608 + file 5242880), pgtables 1056768 bytes. The allocating task was kernel PID 12648, comm ThreadPoolExecu.",
            "The backend cgroup limit was 603979776 bytes. Reported memcg usage was 603996160 bytes with failcnt 626 in the first event and 603979776 bytes with failcnt 4257 in the second.",
            "The backend container did not restart: start/end inspection retained container f54f7734f1726fbde591997dcac17ee3d12effe9f79240bc39b20662ac0cfcc6, start time 2026-11-27T07:12:08.548999982Z, restart_count 0 and init PID 11180. PID 11180 remained present in both kernel OOM task tables while worker victims were replaced outside the container boundary.",
        ],
        "unverified_attribution": [
            "The exact owning process/TGID of allocator task 12646 is not evidenced. It is therefore unproved whether the first allocator belonged to victim 11408, surviving worker 11409, or another thread in the backend cgroup.",
            "The exact owning process/TGID of allocator task 12648 is not evidenced for the same reason, although victim 11409 was the largest process in the second task table.",
            "No HTTP route, request ID, user action, SQL statement, or client voucher ID is bound to either victim or allocator by the prior evidence. Temporal correlation shows heavy concurrency but cannot select a causal route.",
            "The large Fast Gross Profit SQL digest dominates measured SQL time, and manager report requests/searches/vouchers were active near the kills; this does not prove that report execution allocated the Python memory or caused either victim selection.",
        ],
        "kernel_excerpt": excerpts,
    }
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    with OUT.open("xb") as handle:
        handle.write(raw)
    print(json.dumps({"artifact": str(OUT), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                      "oom_events": len(exact_events)}))


if __name__ == "__main__":
    main()
