#!/usr/bin/env python3
"""Sustained native interaction latency and memory-stability benchmark.

Slint MCP click_element waits about 50ms even for a click on non-interactive text.
The invoice gate therefore reports both raw click latency and latency above that
per-run control-click floor; the combined p95 uses the floor-adjusted value so it
measures application work rather than the MCP injection wait.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from benchmark_native import benchmark_environment, mcp_ready, now_iso, process_memory_bytes


class McpClient:
    def __init__(self, port: int) -> None:
        self.endpoint = f"http://127.0.0.1:{port}/mcp"
        self.next_id = 1

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": method,
            "params": params or {},
        }).encode()
        self.next_id += 1
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"content-type": "application/json", "accept": "application/json, text/event-stream"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            text = response.read().decode()
        if text.startswith(("event:", "data:")):
            text = "\n".join(
                line[5:].strip() for line in text.splitlines() if line.startswith("data:")
            )
        parsed = json.loads(text)
        if parsed.get("error"):
            raise RuntimeError(f"MCP {method}: {parsed['error']}")
        return parsed["result"]

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise RuntimeError(f"MCP tool {name}: {result}")
        if "structuredContent" in result:
            return result["structuredContent"]
        text = next(
            (item.get("text") for item in result.get("content", []) if item.get("type") == "text"),
            None,
        )
        try:
            return json.loads(text) if text else result
        except json.JSONDecodeError:
            return result


def process_cpu_seconds(pid: int) -> float | None:
    if sys.platform == "win32":
        class FileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

        def filetime_seconds(value: FileTime) -> float:
            return ((value.dwHighDateTime << 32) + value.dwLowDateTime) / 10_000_000

        process_handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
        if not process_handle:
            return None
        creation, exit_time, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
        try:
            if not ctypes.windll.kernel32.GetProcessTimes(
                process_handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return filetime_seconds(kernel) + filetime_seconds(user)
        finally:
            ctypes.windll.kernel32.CloseHandle(process_handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(fields[13]) + int(fields[14])) / ticks
    except (OSError, IndexError, ValueError):
        return None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))
    return ordered[index]


def type_ids(element: dict[str, Any]) -> list[str]:
    values = []
    for entry in element.get("typeNamesAndIds") or []:
        if isinstance(entry, str):
            values.append(entry)
        elif isinstance(entry, dict):
            values.extend(str(value) for value in (entry.get("id"), entry.get("typeName")) if value)
    return values


def connect(port: int) -> tuple[McpClient, dict[str, Any], list[dict[str, Any]]]:
    client = McpClient(port)
    client.rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "khitmee-sustained-benchmark", "version": "1"},
    })
    try:
        client.rpc("notifications/initialized", {})
    except Exception:
        pass
    windows_result = client.tool("list_windows")
    windows = windows_result.get("windowHandles") or windows_result.get("windows") or []
    if not windows:
        raise RuntimeError(f"no Slint windows in MCP response: {windows_result}")
    window_handle = windows[0]
    properties = client.tool("get_window_properties", {"windowHandle": window_handle})
    root = properties.get("rootElementHandle") or properties.get("root_element_handle")
    if not root:
        raise RuntimeError(f"no root element in MCP response: {properties}")
    tree = client.tool("get_element_tree", {"elementHandle": root, "maxElements": 1200})
    elements = tree.get("elements") or []
    return client, {"windowHandle": window_handle, "root": root}, elements


def find_element(elements: list[dict[str, Any]], element_id: str, label: str) -> dict[str, Any]:
    for element in elements:
        ids = type_ids(element)
        if any(candidate == element_id or candidate.endswith(f"::{element_id}") for candidate in ids):
            if element.get("handle"):
                return element
    for element in elements:
        if element.get("accessibleLabel") == label and element.get("handle"):
            return element
    raise RuntimeError(f"target element {element_id}/{label!r} not found")


def invoice_counts(client: McpClient, handle: str) -> tuple[int, int] | None:
    properties = client.tool("get_element_properties", {"elementHandle": handle})
    label = properties.get("accessibleLabel") or ""
    try:
        loaded, total = label.split(" of ", 1)
        return int(loaded), int(total)
    except (TypeError, ValueError):
        return None


def visible_invoice_list_items(client: McpClient, root_handle: str) -> list[str]:
    tree = client.tool("get_element_tree", {"elementHandle": root_handle, "maxElements": 1200})
    return [
        element.get("accessibleLabel") or ""
        for element in tree.get("elements") or []
        if element.get("accessibleRole") == "ListItem"
    ]


def measure_workload(
    executable: Path,
    workload: str,
    rows: int,
    invoice_rows: int,
    port: int,
    iterations: int,
) -> dict[str, Any]:
    env = benchmark_environment(rows, invoice_rows, port)
    if workload in {"invoice", "scroll"}:
        env["KHITMEE_INITIAL_SCREEN"] = "invoice-list"
    started = time.perf_counter()
    process = subprocess.Popen([str(executable)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = started + 15
    startup_ms = None
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            break
        if mcp_ready(port):
            startup_ms = (time.perf_counter() - started) * 1000
            break
        time.sleep(0.025)

    result: dict[str, Any] = {
        "workload": workload,
        "rows": rows,
        "invoiceRows": invoice_rows,
        "iterations": iterations,
        "startupMs": startup_ms,
    }
    if startup_ms is None:
        process.kill()
        result["error"] = "application did not expose the Slint MCP server within 15 seconds"
        return result

    try:
        client, handles, elements = connect(port)
        if workload == "search":
            target = find_element(elements, "item-search", "Barcode / item search F2")["handle"]
            terms = ["retail", "wholesale", "ITEM-099", "8850000000000", "zzzz", "ဆန်", "Grocery", ""]
        elif workload == "invoice":
            target = find_element(elements, "invoice-filter", "ID")["handle"]
            load_more = find_element(elements, "", "Load More")["handle"]
            count_target = find_element(elements, "invoice-count", "")["handle"]
            list_target = next(
                (element for element in elements if element.get("accessibleRole") == "List" and element.get("handle")),
                {"handle": handles["root"]},
            )["handle"]
            terms = ["ACC", "paid", "customer", "မြတ", ""]
        else:
            target = next(
                (element for element in elements if element.get("accessibleRole") == "List" and element.get("handle")),
                {"handle": handles["root"]},
            )["handle"]
            load_more = find_element(elements, "", "Load More")["handle"]
            count_target = find_element(elements, "invoice-count", "")["handle"]
            terms = []

        if workload == "scroll":
            warmup_clicks = 0
            for _ in range(10):
                before_counts = invoice_counts(client, count_target)
                if before_counts is None:
                    raise RuntimeError("scroll warm-up could not read the invoice count text")
                before_count, total_count = before_counts
                expected_append = min(20, max(0, total_count - before_count))
                if expected_append == 0:
                    break
                client.tool("click_element", {"elementHandle": load_more})
                warmup_clicks += 1
                after_counts = invoice_counts(client, count_target)
                after_count = after_counts[0] if after_counts else None
                if after_count != before_count + expected_append:
                    raise RuntimeError(
                        f"scroll warm-up Load More did not append {expected_append} rows: {before_count} -> {after_count}"
                    )
            result["scrollWarmupClicks"] = warmup_clicks
            final_counts = invoice_counts(client, count_target)
            result["scrollWarmupLoadedRows"] = final_counts[0] if final_counts else None
        click_measurement_floor_ms = None
        if workload == "invoice":
            control_text = next(
                (element for element in elements if element.get("accessibleRole") == "Text" and element.get("handle")),
                None,
            )
            if control_text:
                control_latencies = []
                for _ in range(3):
                    click_started = time.perf_counter()
                    client.tool("click_element", {"elementHandle": control_text["handle"]})
                    control_latencies.append((time.perf_counter() - click_started) * 1000)
                click_measurement_floor_ms = statistics.median(control_latencies)

        start_memory = process_memory_bytes(process.pid, getattr(process, "_handle", None))
        start_cpu = process_cpu_seconds(process.pid)
        interaction_wall_started = time.perf_counter()
        latencies = []
        click_latencies = []
        verified_load_more_clicks = 0
        filtered_load_more_attempts = 0
        filtered_load_more_verified = 0
        memory_checkpoints = []
        for iteration in range(iterations):
            action_started = time.perf_counter()
            if workload == "scroll":
                client.tool("scroll_element", {
                    "elementHandle": target,
                    "deltaY": -500 if iteration % 2 == 0 else 500,
                })
            else:
                client.tool("set_element_value", {"elementHandle": target, "value": terms[iteration % len(terms)]})
            latencies.append((time.perf_counter() - action_started) * 1000)
            if workload == "invoice" and iteration % 10 == 9:
                use_filtered_page = (iteration // 10) % 2 == 0
                verification_term = "fixture-sinv-a" if use_filtered_page else ""
                edit_started = time.perf_counter()
                client.tool("set_element_value", {"elementHandle": target, "value": verification_term})
                latencies.append((time.perf_counter() - edit_started) * 1000)
                before_counts = invoice_counts(client, count_target)
                if before_counts is None:
                    raise RuntimeError("could not read invoice counts before Load More")
                before_count, total_count = before_counts
                expected_append = min(20, max(0, total_count - before_count))
                if expected_append == 0:
                    if use_filtered_page:
                        result["filteredLoadMoreSkipped"] = True
                    continue
                if use_filtered_page:
                    filtered_load_more_attempts += 1
                click_started = time.perf_counter()
                client.tool("click_element", {"elementHandle": load_more})
                click_latencies.append((time.perf_counter() - click_started) * 1000)
                after_counts = invoice_counts(client, count_target)
                after_count = after_counts[0] if after_counts else None
                appended_rows = after_count == before_count + expected_append
                filtered_content_matches = True
                if use_filtered_page and filtered_load_more_attempts == 1:
                    client.tool("scroll_element", {"elementHandle": list_target, "deltaY": 2000})
                    visible_rows = visible_invoice_list_items(client, handles["root"])
                    filtered_content_matches = bool(visible_rows) and all(
                        verification_term in row.lower()
                        for row in visible_rows
                    )
                    client.tool("scroll_element", {"elementHandle": list_target, "deltaY": -2000})
                    result["filteredLoadMoreContentChecked"] = filtered_content_matches
                if appended_rows and filtered_content_matches:
                    verified_load_more_clicks += 1
                    if use_filtered_page:
                        filtered_load_more_verified += 1
            if (iteration + 1) % 100 == 0:
                memory_checkpoints.append({
                    "iteration": iteration + 1,
                    "memoryBytes": process_memory_bytes(process.pid, getattr(process, "_handle", None)),
                })
        interaction_wall_seconds = time.perf_counter() - interaction_wall_started
        time.sleep(0.2)
        end_memory = process_memory_bytes(process.pid, getattr(process, "_handle", None))
        end_cpu = process_cpu_seconds(process.pid)
        cpu_seconds = (
            end_cpu - start_cpu if start_cpu is not None and end_cpu is not None else None
        )
        adjusted_click_latencies = [
            max(0.0, latency - click_measurement_floor_ms)
            for latency in click_latencies
        ] if click_measurement_floor_ms is not None else click_latencies
        all_latencies = latencies + adjusted_click_latencies
        result.update({
            "startMemoryBytes": start_memory,
            "endMemoryBytes": end_memory,
            "memoryGrowthBytes": (end_memory - start_memory) if start_memory is not None and end_memory is not None else None,
            "memoryCheckpoints": memory_checkpoints,
            "interactionWallSeconds": interaction_wall_seconds,
            "interactionCpuSeconds": cpu_seconds,
            "cpuMsPerInteraction": (cpu_seconds * 1000 / (len(latencies) + len(click_latencies))) if cpu_seconds is not None else None,
            "latencyMs": {
                "p50": statistics.median(all_latencies) if all_latencies else None,
                "p95": percentile(all_latencies, 0.95),
                "mean": statistics.mean(all_latencies) if all_latencies else None,
                "max": max(all_latencies) if all_latencies else None,
                "editP95": percentile(latencies, 0.95),
                "rawClickP95": percentile(click_latencies, 0.95) if click_latencies else None,
                "clickAboveControlP95": percentile(adjusted_click_latencies, 0.95) if adjusted_click_latencies else None,
                "clickMeasurementFloorMs": click_measurement_floor_ms,
            },
            "samples": len(all_latencies),
        })
        if workload == "invoice":
            result.update({
                "loadMoreAttempts": len(click_latencies),
                "loadMoreVerified": verified_load_more_clicks,
                "filteredLoadMoreAttempts": filtered_load_more_attempts,
                "filteredLoadMoreVerified": filtered_load_more_verified,
                "finalInvoiceCountText": client.tool("get_element_properties", {"elementHandle": count_target}).get("accessibleLabel"),
            })
    except Exception as error:
        result["error"] = str(error)
    finally:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        result["stdoutTail"] = stdout.decode(errors="replace")[-500:]
        result["stderrTail"] = stderr.decode(errors="replace")[-500:]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--invoice-rows", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--workloads", default="search,invoice,scroll")
    parser.add_argument("--max-p95-ms", type=float, default=30.0)
    parser.add_argument("--max-memory-growth-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--max-end-memory-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--max-cpu-ms-per-interaction", type=float, default=10.0)
    args = parser.parse_args()

    executable = Path(args.executable)
    workloads = [workload.strip() for workload in args.workloads.split(",") if workload.strip()]
    unknown = sorted(set(workloads) - {"search", "invoice", "scroll"})
    if unknown:
        parser.error(f"unknown workloads: {', '.join(unknown)}")

    runs = []
    for index, workload in enumerate(workloads):
        runs.append(measure_workload(executable, workload, args.rows, args.invoice_rows, 49820 + index * 3, args.iterations))

    failures = []
    for run in runs:
        label = run["workload"]
        if run.get("error"):
            failures.append(f"{label}: {run['error']}")
            continue
        latency = run.get("latencyMs", {})
        if latency.get("p95") is None or latency["p95"] > args.max_p95_ms:
            failures.append(f"{label}: p95 interaction latency missing or above {args.max_p95_ms}ms")
        growth = run.get("memoryGrowthBytes")
        if growth is None or growth > args.max_memory_growth_bytes:
            failures.append(f"{label}: memory growth missing or above {args.max_memory_growth_bytes} bytes")
        end_memory = run.get("endMemoryBytes")
        if end_memory is None or end_memory > args.max_end_memory_bytes:
            failures.append(f"{label}: end memory missing or above {args.max_end_memory_bytes} bytes")
        cpu_ms = run.get("cpuMsPerInteraction")
        if cpu_ms is None or cpu_ms > args.max_cpu_ms_per_interaction:
            failures.append(f"{label}: CPU per interaction missing or above {args.max_cpu_ms_per_interaction}ms")
        if label == "invoice" and run.get("loadMoreVerified") != run.get("loadMoreAttempts"):
            failures.append(
                f"{label}: only {run.get('loadMoreVerified')} of {run.get('loadMoreAttempts')} Load More clicks appended rows"
            )
        if label == "invoice" and run.get("filteredLoadMoreVerified") != run.get("filteredLoadMoreAttempts"):
            failures.append(
                f"{label}: only {run.get('filteredLoadMoreVerified')} of {run.get('filteredLoadMoreAttempts')} filtered Load More clicks appended rows"
            )
        if label == "invoice" and run.get("invoiceRows", 0) >= 100 and not run.get("filteredLoadMoreAttempts"):
            failures.append(f"{label}: filtered Load More paging was not exercised")
        if label == "invoice" and run.get("filteredLoadMoreAttempts") and not run.get("filteredLoadMoreContentChecked"):
            failures.append(f"{label}: filtered Load More visible rows did not all match the active filter")

    report = {
        "schemaVersion": 1,
        "generatedAt": now_iso(),
        "passed": not failures,
        "failures": failures,
        "gates": {
            "maxP95Ms": args.max_p95_ms,
            "maxMemoryGrowthBytes": args.max_memory_growth_bytes,
            "maxEndMemoryBytes": args.max_end_memory_bytes,
            "maxCpuMsPerInteraction": args.max_cpu_ms_per_interaction,
        },
        "runs": runs,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failures": failures, "output": str(output)}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
