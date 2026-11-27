#!/usr/bin/env python3
"""Aggregate desktop/browser catalog experiment artifacts into JSON and Markdown."""
import argparse
import json
import math
from pathlib import Path

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def rounded(value, digits=2):
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(float(value), digits)

def percentile(values, pct):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    index = math.ceil((pct / 100.0) * len(values)) - 1
    return values[max(0, min(index, len(values) - 1))]

def median(values):
    return percentile(values, 50)

def first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None

def aggregate_scenarios(rows):
    grouped = {}
    for row in rows:
        network = row.get("network") or {}
        payload = row.get("data") or {}
        scenario = row.get("scenario")
        bucket = grouped.setdefault(scenario, {
            "latencies": [],
            "build_ms": [],
            "api_requests": 0,
            "api_encoded_bytes": 0,
            "duration_ms": 0.0,
            "runs": 0,
            "mutation_records": 0,
        })
        latencies = payload.get("latenciesMs") or payload.get("scanLatenciesMs") or []
        bucket["latencies"].extend(float(value) for value in latencies if value is not None)
        if payload.get("buildMs") is not None:
            bucket["build_ms"].append(float(payload["buildMs"]))
        bucket["api_requests"] += network.get("apiRequestCount") or 0
        bucket["api_encoded_bytes"] += network.get("apiEncodedBytes") or 0
        bucket["duration_ms"] += row.get("durationMs") or 0.0
        bucket["runs"] += 1
        bucket["mutation_records"] += (row.get("probe") or {}).get("mutationRecords") or 0
    out = {}
    for scenario, bucket in sorted(grouped.items()):
        out[scenario] = {
            "runs": bucket["runs"],
            "median_ms": rounded(median(bucket["latencies"])),
            "p95_ms": rounded(percentile(bucket["latencies"], 95)),
            "build_median_ms": rounded(median(bucket["build_ms"])),
            "api_requests": bucket["api_requests"],
            "api_encoded_bytes": bucket["api_encoded_bytes"],
            "duration_ms": rounded(bucket["duration_ms"]),
            "mutation_records": bucket["mutation_records"] or None,
        }
    return out

def benchmark_metrics(path):
    data = load(path)
    rows = data.get("results") or []
    sync_rows = [row.get("catalogSync") for row in rows if row.get("catalogSync")]
    sync = sync_rows[0] if sync_rows else None
    return {
        "label": data.get("meta", {}).get("label"),
        "catalog_mode": data.get("meta", {}).get("catalogMode"),
        "api_requests": sum((row.get("network") or {}).get("apiRequestCount") or 0 for row in rows),
        "api_encoded_bytes": sum((row.get("network") or {}).get("apiEncodedBytes") or 0 for row in rows),
        "catalog_sync": sync,
        "scenarios": aggregate_scenarios(rows),
    }

def server_metrics(path):
    data = load(path)
    fleet = data.get("fleet") or {}
    containers = data.get("containers") or {}
    def container(fragment):
        for name, row in containers.items():
            if fragment in name:
                return row
        return {}
    def memory(row, avg_key, peak_key):
        return {
            avg_key: first_present(row.get("pss_mib_avg"), row.get("cgroup_current_mib_avg")),
            peak_key: first_present(row.get("pss_mib_peak"), row.get("cgroup_current_mib_peak")),
            "source": "pss" if row.get("pss_mib_avg") is not None else "cgroup",
        }
    backend_memory = memory(container("backend"), "backend_mem_avg_mib", "backend_mem_peak_mib")
    db_memory = memory(container("db"), "db_mem_avg_mib", "db_mem_peak_mib")
    return {
        "fleet_mem_avg_mib": first_present(fleet.get("pss_mib_avg"), fleet.get("cgroup_mib_avg")),
        "fleet_mem_peak_mib": first_present(fleet.get("pss_mib_peak"), fleet.get("cgroup_mib_peak")),
        "fleet_memory_source": "pss" if fleet.get("pss_mib_avg") is not None else "cgroup",
        "fleet_cpu_avg_percent": fleet.get("cpu_percent_avg"),
        **backend_memory,
        "backend_cpu_avg_percent": container("backend").get("cpu_percent_avg"),
        **db_memory,
        "db_cpu_avg_percent": container("db").get("cpu_percent_avg"),
        "sample_count": data.get("sample_count"),
    }

def markdown(summary):
    lines = [
        "| mode | scan p95 ms | search p95 ms | API requests | API KiB | sync KiB | backend mem peak MiB | backend CPU avg % | fleet mem peak MiB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, row in sorted(summary["modes"].items()):
        bench = row.get("browser") or {}
        server = row.get("server") or {}
        scan = (bench.get("scenarios") or {}).get("scan") or {}
        search = (bench.get("scenarios") or {}).get("search") or {}
        sync_network = ((bench.get("catalog_sync") or {}).get("network") or {})
        lines.append(
            "| {mode} | {scan} | {search} | {req} | {kib} | {synckib} | {bpss} | {bcpu} | {fpss} |".format(
                mode=mode,
                scan=scan.get("p95_ms"),
                search=search.get("p95_ms"),
                req=bench.get("api_requests"),
                kib=rounded((bench.get("api_encoded_bytes") or 0) / 1024.0),
                synckib=rounded((sync_network.get("encodedBytes") or 0) / 1024.0) if sync_network else None,
                bpss=server.get("backend_mem_peak_mib"),
                bcpu=server.get("backend_cpu_avg_percent"),
                fpss=server.get("fleet_mem_peak_mib"),
            )
        )
    lines.append("")
    lines.append("Memory falls back to cgroup current bytes when container process smaps are unreadable. Values are null when unavailable; missing evidence is never treated as zero.")
    return "\n".join(lines) + "\n"

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="desktop experiment artifact directory")
    args = parser.parse_args()
    root = Path(args.root)
    summary = {"schema_version": 1, "modes": {}, "evidence_type": "actual Linux VM measurement unless an artifact says simulated"}
    for bench in sorted(root.glob("*-benchmark.json")):
        mode = bench.name.replace("-benchmark.json", "")
        summary["modes"].setdefault(mode, {})["browser"] = benchmark_metrics(bench)
    for server in sorted(root.glob("*-server.json")):
        mode = server.name.replace("-server.json", "")
        summary["modes"].setdefault(mode, {})["server"] = server_metrics(server)
    (root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "summary.md").write_text(markdown(summary), encoding="utf-8")
    print(json.dumps({"summary": str(root / "summary.json"), "markdown": str(root / "summary.md"), "modes": sorted(summary["modes"])}, indent=2))

if __name__ == "__main__":
    main()
