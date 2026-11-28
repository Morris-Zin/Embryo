#!/usr/bin/env python3
"""Seasonal acceptance scorecard.

The scorecard keeps feature gates independent: cashier evidence can pass even when the
issue-#2 detail-pagination/export surfaces are absent, while full seasonal qualification
still fails closed until every applicable gate passes.
"""

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "seasonal"))
import verify_run  # noqa: E402

import evaluate_run as base  # noqa: E402

SEARCH_P95_MS = 300.0
SUBMIT_P95_MS = 1500.0
SUMMARY_P95_MS = 500.0
DETAIL_FIRST_ROWS_P95_MS = 2000.0
CORE_PHASES = ("baseline", "margin", "recovery")
VOUCHER_TYPES = ("sale", "transfer", "purchase_receipt", "purchase_invoice")


@dataclass
class Gate:
    name: str
    status: str
    observed: str
    threshold: str
    detail: str = ""


def percentile(values, quantile):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return values[index]


def fmt_ms(value):
    return "no data" if value is None else f"{value:.1f} ms"


def fmt_int(value):
    return "no data" if value is None else str(int(value))


def load_json(path, required=False):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise ValueError(f"cannot load JSON {path}: {exc}") from exc
        return None



def canonical_json_text(value):
    """canonical-json-v1 helper for trusted manager-output replay implementations.

    Object keys are sorted recursively, array order is preserved, and no whitespace is
    emitted. Numbers use at most 15 fractional digits with trailing zeroes trimmed;
    integer-valued floats are rendered as integers to match JavaScript's single Number
    type. Non-finite numbers are rejected rather than silently hashed.
    """
    if value is None or isinstance(value, bool):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot be canonically hashed")
        if value == 0:
            return "0"
        if value.is_integer() and abs(value) <= 9007199254740991:
            return str(int(value))
        text = format(value, ".15f").rstrip("0").rstrip(".")
        return text if text not in {"", "-0"} else "0"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json_text(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(str(key), ensure_ascii=False, separators=(",", ":")) + ":" + canonical_json_text(value[key])
            for key in sorted(value, key=lambda item: str(item))
        ) + "}"
    raise TypeError(f"unsupported canonical JSON value type: {type(value).__name__}")


def canonical_json_sha256(value):
    return hashlib.sha256(canonical_json_text(value).encode("utf-8")).hexdigest()

def metric_count(summary, name):
    return base.numeric_metric(summary, name, ["count", "value"])


def successful_events(events, kind):
    return [event for event in events["events"] if event.get("event") == kind]


def rejected_events(events, kinds):
    return [event for event in events["events"] if event.get("event") in kinds]


def latency_rows(events, kind, duration_field, phases=CORE_PHASES, group_field=None):
    rows = []
    for phase in phases:
        phase_events = [event for event in events["events"] if event.get("event") == kind and event.get("phase") == phase]
        groups = defaultdict(list)
        for event in phase_events:
            key = event.get(group_field) if group_field else "all"
            groups[key].append(event.get(duration_field))
        for key, durations in sorted(groups.items()):
            rows.append({"phase": phase, "group": key, "count": len(durations), "p95": percentile(durations, 0.95)})
    return rows


def latency_gate(name, rows, threshold, missing_is_fail=True):
    if not rows:
        return Gate(name, "FAIL" if missing_is_fail else "NO DATA", "no data", f"p95 < {threshold:.0f} ms")
    failures = []
    parts = []
    for row in rows:
        parts.append(f"{row['phase']}/{row['group']}: {fmt_ms(row['p95'])} (n={row['count']})")
        if row["p95"] is None or row["p95"] >= threshold or row["count"] < 1:
            failures.append(f"{row['phase']}/{row['group']}")
    status = "PASS" if not failures else "FAIL"
    return Gate(name, status, "; ".join(parts), f"p95 < {threshold:.0f} ms", "failed groups: " + ", ".join(failures) if failures else "")


def voucher_latency_gate(events):
    rows = []
    completes = [event for event in events["events"] if event.get("event") == "voucher_complete"]
    for phase in CORE_PHASES:
        for voucher_type in VOUCHER_TYPES:
            durations = [
                event.get("duration_ms")
                for event in completes
                if event.get("phase") == phase and event.get("type") == voucher_type
            ]
            rows.append({"phase": phase, "group": voucher_type, "count": len(durations), "p95": percentile(durations, 0.95)})
        large = [
            event.get("duration_ms")
            for event in completes
            if event.get("phase") == phase and event.get("type") == "sale" and int(event.get("line_count") or 0) == 200
        ]
        rows.append({"phase": phase, "group": "sale/200-lines", "count": len(large), "p95": percentile(large, 0.95)})
    return latency_gate("Cashier submit-to-confirmation p95", rows, SUBMIT_P95_MS)


def discovery_gate(events):
    discoveries = successful_events(events, "cashier_discovery")
    if discoveries and any(event.get("passed") for event in discoveries):
        return Gate("Real cashier-only discovery", "PASS", "login + counter search", "must pass before mutation")
    if discoveries:
        return Gate("Real cashier-only discovery", "FAIL", "probe failed", "must pass before mutation")
    return Gate("Real cashier-only discovery", "NO DATA", "no event", "must pass before mutation")


def shape_key(voucher_type, line_count):
    return (str(voucher_type), int(line_count))


REGULAR_TEMPLATE_SHAPES = {
    "sale_small": [("sale", 10)],
    "sale_medium": [("sale", 50)],
    "sale_large": [("sale", 200)],
    "transfer": [("transfer", 50)],
    "purchase_cycle": [("purchase_receipt", 50), ("purchase_invoice", 50)],
}


def integer_stream_bounds(segment, kind, base_per_minute):
    configured = (segment.get("arrival_bounds") or {}).get(kind) or {}
    duration = float(segment.get("duration_seconds") or 0)
    scale_num, scale_den = scale_ratio(segment)
    nominal = (duration / 60.0) * base_per_minute * scale_num / scale_den
    minimum = max(1, math.ceil(nominal - 1e-9))
    endpoint_aligned = abs(nominal - round(nominal)) < 1e-9
    maximum = minimum + (1 if endpoint_aligned else 0)
    failures = []
    for key, expected in (("minimum", minimum), ("maximum", maximum)):
        if configured.get(key) != expected:
            failures.append(f"{kind}.{key}={configured.get(key)}, expected {expected}")
    configured_nominal = configured.get("nominal")
    if not isinstance(configured_nominal, (int, float)) or abs(float(configured_nominal) - nominal) > 1e-9:
        failures.append(f"{kind}.nominal={configured.get('nominal')}, expected {nominal:g}")
    if configured.get("endpoint_aligned") is not endpoint_aligned:
        failures.append(f"{kind}.endpoint_aligned={configured.get('endpoint_aligned')}, expected {endpoint_aligned}")
    return {"minimum": minimum, "maximum": maximum, "nominal": nominal, "endpoint_aligned": endpoint_aligned, "failures": failures}


def counter_add_shape(counter, voucher_type, line_count, count=1):
    counter[shape_key(voucher_type, line_count)] += count


def scheduled_arrival_matrix(segment, regular_iterations, contention_iterations):
    template = segment.get("voucher_template") or []
    contention_template = segment.get("contention_template") or []
    if not template or not contention_template:
        raise ValueError("missing voucher_template or contention_template in seasonal config")
    scale_den = int(segment.get("scale_den") or 0)
    if scale_den <= 0:
        raise ValueError("missing positive scale_den in seasonal config")
    matrix = defaultdict(Counter)
    template_length = len(template)
    for iteration in range(regular_iterations):
        entry = template[iteration % template_length]
        entry_minute, mix_key = template_entry_parts(entry)
        minute = int(entry_minute) + (iteration // template_length) * scale_den
        shapes = REGULAR_TEMPLATE_SHAPES.get(mix_key)
        if not shapes:
            raise ValueError(f"unknown voucher template mix key {mix_key}")
        for voucher_type, line_count in shapes:
            counter_add_shape(matrix[minute], voucher_type, line_count)
    contention_length = len(contention_template)
    for iteration in range(contention_iterations):
        entry = contention_template[iteration % contention_length]
        minute = int(entry.get("minute")) + (iteration // contention_length) * scale_den
        counter_add_shape(matrix[minute], "sale", 10, 6)
        counter_add_shape(matrix[minute], "transfer", 50, 1)
    return matrix


def observed_arrival_matrix(events, segment_name):
    matrix = defaultdict(Counter)
    duplicate_ids = Counter()
    starts = 0
    for event in events["events"]:
        if event.get("event") != "voucher_start" or event.get("segment") != segment_name:
            continue
        starts += 1
        client_id = str(event.get("client_voucher_id") or "")
        if client_id:
            duplicate_ids[client_id] += 1
        try:
            minute = int(event.get("minute"))
            voucher_type = str(event.get("type") or "")
            line_count = int(event.get("line_count") or 0)
        except (TypeError, ValueError):
            minute = "invalid"
            voucher_type = "invalid"
            line_count = 0
        counter_add_shape(matrix[minute], voucher_type, line_count)
    duplicates = sorted(client_id for client_id, count in duplicate_ids.items() if count > 1)
    return matrix, starts, duplicates


def matrix_shape_totals(matrix):
    totals = Counter()
    for counter in matrix.values():
        totals.update(counter)
    return totals


def sorted_minutes(matrix):
    def key(value):
        return (0, value) if isinstance(value, int) else (1, 0)
    return sorted(matrix, key=key)


def matrix_differences(expected, observed, limit=8):
    differences = []
    for minute in sorted_minutes(set(expected) | set(observed)):
        expected_counter = expected.get(minute, Counter())
        observed_counter = observed.get(minute, Counter())
        for shape in sorted(set(expected_counter) | set(observed_counter)):
            expected_count = expected_counter.get(shape, 0)
            observed_count = observed_counter.get(shape, 0)
            if expected_count != observed_count:
                differences.append(
                    f"minute {minute} {shape[0]}x{shape[1]}: started {observed_count}, integer schedule {expected_count}"
                )
                if len(differences) >= limit:
                    return differences
    return differences




def template_entry_parts(entry):
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return entry[0], str(entry[1] or "")
    if isinstance(entry, dict):
        return entry.get("minute"), str(entry.get("mix_key") or "")
    return None, ""

def validate_template_structure(segment):
    template = segment.get("voucher_template") or []
    contention_template = segment.get("contention_template") or []
    failures = []
    scale_num, scale_den = scale_ratio(segment)
    if len(template) != 88 * scale_num:
        failures.append(f"voucher_template length {len(template)}, expected {88 * scale_num}")
    if len(contention_template) != scale_num:
        failures.append(f"contention_template length {len(contention_template)}, expected {scale_num}")
    expected_mix = {
        "sale_small": 54,
        "sale_medium": 16,
        "sale_large": 4,
        "transfer": 9,
        "purchase_cycle": 5,
    }
    by_minute = defaultdict(Counter)
    for position, entry in enumerate(template):
        minute, mix_key = template_entry_parts(entry)
        if mix_key not in expected_mix or not isinstance(minute, int) or minute < 0 or minute >= scale_den:
            failures.append(f"voucher_template[{position}] has invalid mix/minute")
            continue
        by_minute[minute][mix_key] += 1
    cumulative = Counter()
    cumulative_rows = 0
    for minute in range(scale_den):
        for mix_key in expected_mix:
            cumulative[mix_key] += by_minute[minute].get(mix_key, 0)
        cumulative_rows += sum(by_minute[minute].values())
        step = minute + 1
        expected_row_cumulative = (88 * scale_num * step) // scale_den
        if cumulative_rows != expected_row_cumulative:
            failures.append(
                f"voucher_template cumulative regular iterations through minute {minute}: "
                f"{cumulative_rows}, expected {expected_row_cumulative}"
            )
        for mix_key, per_minute in expected_mix.items():
            numerator = per_minute * scale_num * step
            lower = numerator // scale_den
            upper = -(-numerator // scale_den)
            observed = cumulative[mix_key]
            if observed < lower or observed > upper:
                failures.append(
                    f"voucher_template cumulative minute {minute} {mix_key}: {observed}, "
                    f"outside integer bounds {lower}..{upper}"
                )
    contention_counts = Counter(int(entry.get("minute")) for entry in contention_template if isinstance(entry.get("minute"), int))
    for minute in range(scale_den):
        expected = allocated_count(1, minute, scale_num, scale_den)
        if contention_counts.get(minute, 0) != expected:
            failures.append(f"contention_template minute {minute}: {contention_counts.get(minute, 0)}, expected {expected}")
    return failures

def match_integer_arrival_schedule(events, segment):
    name = str(segment.get("name") or "")
    regular = integer_stream_bounds(segment, "regular_iterations", 88)
    contention = integer_stream_bounds(segment, "contention_iterations", 1)
    config_failures = regular["failures"] + contention["failures"]
    observed, started_count, duplicates = observed_arrival_matrix(events, name)
    config_failures += validate_template_structure(segment)
    result = {
        "segment": name,
        "started": started_count,
        "duplicates": duplicates,
        "config_failures": config_failures,
        "matched": None,
        "differences": [],
        "total_range": None,
        "type_range": {},
    }
    if config_failures:
        result["differences"] = config_failures
        return result
    candidates = []
    try:
        for regular_count in range(regular["minimum"], regular["maximum"] + 1):
            for contention_count in range(contention["minimum"], contention["maximum"] + 1):
                matrix = scheduled_arrival_matrix(segment, regular_count, contention_count)
                totals = matrix_shape_totals(matrix)
                candidates.append((regular_count, contention_count, matrix, totals))
    except ValueError as exc:
        result["config_failures"] = [str(exc)]
        result["differences"] = [str(exc)]
        return result

    total_values = [sum(candidate[3].values()) for candidate in candidates]
    result["total_range"] = (min(total_values), max(total_values))
    for voucher_shape in sorted(set(shape for candidate in candidates for shape in candidate[3])):
        values = [candidate[3].get(voucher_shape, 0) for candidate in candidates]
        result["type_range"][f"{voucher_shape[0]}x{voucher_shape[1]}"] = (min(values), max(values))

    best = None
    for regular_count, contention_count, matrix, totals in candidates:
        differences = matrix_differences(matrix, observed)
        candidate = {
            "regular_iterations": regular_count,
            "contention_iterations": contention_count,
            "totals": totals,
            "differences": differences,
        }
        if not differences and not duplicates:
            result["matched"] = candidate
            return result
        if best is None or len(differences) < len(best["differences"]):
            best = candidate
    result["differences"] = (best or {}).get("differences") or []
    if duplicates:
        result["differences"] = [f"duplicate voucher_start client id {client_id}" for client_id in duplicates[:4]] + result["differences"]
    return result


def arrival_schedule_matches(events):
    segments = events["config"].get("phases") or []
    return [match_integer_arrival_schedule(events, segment) for segment in segments]


def offered_counts_gate(events, summary):
    config = events["config"]
    phases = config.get("phases") or []
    if not phases:
        return Gate("Offered/started/completed/rejected/dropped accounting", "NO DATA", "missing config", "exact bounded integer schedule required"), []
    matches = arrival_schedule_matches(events)
    rows = []
    mismatches = []
    total_started = 0
    total_minimum = 0
    total_maximum = 0
    for match in matches:
        total_started += match["started"]
        if match["total_range"]:
            total_minimum += match["total_range"][0]
            total_maximum += match["total_range"][1]
        type_parts = []
        for shape, bounds in sorted(match["type_range"].items()):
            type_parts.append(f"{shape} {bounds[0]}..{bounds[1]}")
        rows.append(
            {
                "segment": match["segment"],
                "expected": f"{match['total_range'][0]}..{match['total_range'][1]}" if match["total_range"] else "unknown",
                "expected_minimum": match["total_range"][0] if match["total_range"] else None,
                "expected_maximum": match["total_range"][1] if match["total_range"] else None,
                "started": match["started"],
                "types": ", ".join(type_parts),
            }
        )
        if match["config_failures"]:
            mismatches.append(f"{match['segment']}: invalid integer schedule config: {'; '.join(match['config_failures'][:4])}")
        elif not match["matched"]:
            mismatches.append(f"{match['segment']}: " + "; ".join((match["differences"] or ["no integer schedule candidate matched"])[:4]))
    dropped = metric_count(summary, "dropped_iterations")
    if dropped:
        mismatches.append(f"k6 reported {int(dropped)} dropped iterations; the generator must not drop offered load")
    unknown = events.get("unknown", {})
    unaccounted = len(events["started"]) - len(events["completed"]) - len(events["rejected"]) - len(unknown)
    if unaccounted:
        mismatches.append(f"{unaccounted} started vouchers have neither completion, rejection nor unknown events")
    observed = (
        f"started {total_started}; bounded integer schedule total {total_minimum}..{total_maximum}; "
        f"completed {len(events['completed'])}; rejected {len(events['rejected'])}; unknown {len(unknown)}; "
        f"dropped_iterations {fmt_int(dropped)}; unaccounted {unaccounted}"
    )
    return Gate(
        "Offered/started/completed/rejected/dropped accounting",
        "PASS" if not mismatches else "FAIL",
        observed,
        "started events exactly match one endpoint-bounded integer schedule; started = completed + rejected + unknown; dropped = 0",
        "; ".join(mismatches[:12]),
    ), rows

def contention_gate(events):
    starts = [event for event in events["events"] if event.get("event") == "voucher_start" and event.get("contention")]
    failures = []
    observed_parts = []
    for phase in CORE_PHASES:
        phase_events = [event for event in starts if event.get("phase") == phase]
        by_minute = defaultdict(list)
        for event in phase_events:
            by_minute[str(event.get("minute"))].append(event)
        if not by_minute:
            failures.append(f"{phase}: no contention burst")
            continue
        checked = 0
        for minute, minute_events in sorted(by_minute.items(), key=lambda item: int(item[0])):
            sales = [event for event in minute_events if event.get("type") == "sale"]
            transfers = [event for event in minute_events if event.get("type") == "transfer"]
            shops = {str(event.get("warehouse") or "") for event in sales}
            sale_hot = {str(event.get("shared_contention_item") or "") for event in sales if event.get("shared_contention_item")}
            transfer_hot = {str(event.get("shared_contention_item") or "") for event in transfers if event.get("shared_contention_item")}
            if len(shops) != 6 or len(sales) % 6:
                failures.append(f"{phase} minute {minute}: {len(sales)} contention sales across {len(shops)} shops")
            if not transfers:
                failures.append(f"{phase} minute {minute}: missing contention transfer")
            elif sale_hot != transfer_hot or not sale_hot:
                failures.append(f"{phase} minute {minute}: sale/transfer hot-item evidence mismatch")
            checked += 1
        observed_parts.append(f"{phase}: {checked} minute(s)")
    return Gate(
        "Six-shop hot-stock contention",
        "PASS" if not failures else "FAIL",
        "; ".join(observed_parts),
        "every minute has six simultaneous shop sales and one transfer sharing hot stock",
        "; ".join(failures[:8]),
    )


def retry_witness_start(event):
    if event.get("contention") or event.get("segment") != "baseline":
        return False
    try:
        minute = int(event.get("minute"))
        sequence = int(event.get("sequence"))
    except (TypeError, ValueError):
        return False
    return minute == 0 and sequence == 0 and event.get("mix_key") in {
        "sale_small", "transfer", "purchase_receipt", "purchase_invoice"
    }


def retry_identity_gate(events):
    completes = successful_events(events, "voucher_complete")
    mismatches = [
        event for event in completes
        if event.get("first_committed_voucher") and event.get("voucher_no") != event.get("first_committed_voucher")
    ]
    if mismatches:
        return Gate(
            "Lost-acknowledgement retry identity",
            "FAIL",
            f"{len(mismatches)} retry/voucher mismatch(es)",
            "sales/transfers resubmit idempotently in flight; purchases start read-only recovery in flight; both must return the original voucher",
        )
    expected_types = {"sale", "transfer", "purchase_receipt", "purchase_invoice"}
    witnesses = {}
    overlaps = {
        str(event.get("client_voucher_id") or ""): event
        for event in events.get("events", [])
        if event.get("event") == "voucher_retry_overlap"
    }
    for start in events["started"].values():
        if retry_witness_start(start):
            witnesses[str(start.get("type") or "")] = start
    missing = sorted(expected_types - set(witnesses))
    failures = []
    proofs = []
    for voucher_type in sorted(expected_types & set(witnesses)):
        start = witnesses[voucher_type]
        client_id = start.get("client_voucher_id")
        completion = events["completed"].get(client_id)
        if not completion:
            failures.append(f"{voucher_type}: witness has no completion")
            continue
        overlap = overlaps.get(str(client_id)) or {}
        base_proof = (
            completion.get("lost_ack") is True
            and completion.get("retried") is True
            and completion.get("retry_confirmed") is True
            and completion.get("retry_overlap") is True
            and overlap.get("first_inflight") is True
            and int(overlap.get("concurrent_attempts") or 0) >= 2
            and bool(completion.get("first_committed_voucher"))
            and completion.get("voucher_no") == completion.get("first_committed_voucher")
        )
        if voucher_type in {"purchase_receipt", "purchase_invoice"}:
            valid = (
                base_proof
                and completion.get("recovery_attempted") is True
                and completion.get("reconciled") is True
                and completion.get("resubmitted") is False
                and int(completion.get("attempts") or 0) == 1
                and int(completion.get("recovery_reads") or 0) >= 1
                and completion.get("retry_overlap_mode") == "submit_plus_readonly_recovery_batch"
            )
            mode = "read-only recovery started while submit was in flight"
        else:
            valid = (
                base_proof
                and completion.get("resubmitted") is True
                and int(completion.get("attempts") or 0) >= 2
                and completion.get("retry_overlap_mode") == "duplicate_idempotent_submit_batch"
            )
            mode = "idempotent resubmission started while the first submit was in flight"
        if valid:
            proofs.append((voucher_type, mode))
        else:
            failures.append(f"{voucher_type}: missing valid {mode} evidence")
    if missing or failures:
        detail = []
        if missing:
            detail.append("missing witness start(s): " + ", ".join(missing))
        detail.extend(failures)
        return Gate(
            "Lost-acknowledgement retry identity",
            "FAIL",
            f"{len(proofs)}/{len(expected_types)} verified retry confirmation(s)",
            "sales/transfers resubmit idempotently in flight; purchases start read-only recovery in flight; both must return the original voucher",
            "; ".join(detail[:10]),
        )
    return Gate(
        "Lost-acknowledgement retry identity",
        "PASS",
        f"{len(proofs)} verified retry confirmation(s) returned the original voucher",
        "sales/transfers resubmit idempotently in flight; purchases start read-only recovery in flight; both must return the original voucher",
    )


def correctness_gate(verification):
    if verification is None:
        return Gate("Independent database correctness", "NO DATA", "missing verifier JSON", "verifier must pass")
    if verification.get("passed") is True:
        observed = (
            f"started {verification.get('started')}; confirmed {verification.get('completed')}; unknown {verification.get('unknown', 0)}; "
            f"retried confirmations {verification.get('retried_completed')}; lost acks {verification.get('lost_ack_observed')}"
        )
        return Gate("Independent database correctness", "PASS", observed, "no lost/duplicate/ledger defects")
    defects = verification.get("defects") or []
    detail = "; ".join(f"{item.get('kind')}: {item.get('detail', '')}" for item in defects[:8])
    return Gate(
        "Independent database correctness",
        "FAIL",
        f"{len(defects)} defect(s)",
        "no lost/duplicate/ledger defects",
        detail or str(verification),
    )


def summary_gate(events, enabled):
    if not enabled:
        return Gate("Manager warm summary p95", "SKIP", "managers disabled", "p95 < 500 ms")
    rows = []
    summaries = successful_events(events, "manager_summary")
    for phase in CORE_PHASES:
        for group_by in ("Warehouse", "Monthly"):
            durations = [
                event.get("duration_ms")
                for event in summaries
                if event.get("phase") == phase and event.get("group_by") == group_by and event.get("warm")
            ]
            rows.append({"phase": phase, "group": group_by, "count": len(durations), "p95": percentile(durations, 0.95)})
    return latency_gate("Manager warm summary p95", rows, SUMMARY_P95_MS)


def detail_gate(events, enabled):
    if not enabled:
        return Gate("Issue #2 detail pagination", "SKIP", "managers disabled", "pages honored; first 50 p95 < 2000 ms")
    successes = successful_events(events, "manager_detail")
    rejects = successful_events(events, "manager_detail_reject")
    rows = []
    for phase in CORE_PHASES:
        durations = [
            event.get("first_50_duration_ms")
            for event in successes
            if event.get("phase") == phase and event.get("pagination_honored")
        ]
        rows.append({"phase": phase, "group": "first-50", "count": len(durations), "p95": percentile(durations, 0.95)})
    gate = latency_gate("Issue #2 detail pagination", rows, DETAIL_FIRST_ROWS_P95_MS)
    if gate.status != "PASS" and rejects:
        reasons = sorted({str(event.get("reason") or "detail failed") for event in rejects})
        gate.detail = (gate.detail + "; " if gate.detail else "") + "rejection evidence: " + "; ".join(reasons[:4])
        gate.status = "FAIL"
    return gate


def export_gate(events, enabled):
    if not enabled:
        return Gate("Issue #2 full-year export", "SKIP", "export disabled", "at least one verified CSV export")
    completes = successful_events(events, "export_complete")
    valid = [
        event for event in completes
        if int(event.get("row_count") or 0) > 0
        and int(event.get("body_bytes") or 0) > 0
        and isinstance(event.get("sha256"), str)
        and len(event.get("sha256")) == 64
    ]
    if valid:
        latest = valid[-1]
        return Gate(
            "Issue #2 full-year export",
            "PASS",
            f"{len(valid)} verified export(s); latest rows={latest.get('row_count')} bytes={latest.get('body_bytes')} sha256={str(latest.get('sha256') or '')[:16]}",
            "at least one verified non-empty CSV export with a body hash",
        )
    if completes:
        return Gate(
            "Issue #2 full-year export",
            "FAIL",
            f"{len(completes)} completion event(s) lacked positive row/byte evidence or a 64-hex body hash",
            "at least one verified non-empty CSV export with a body hash",
        )
    rejects = successful_events(events, "export_reject")
    if rejects:
        reasons = sorted({str(event.get("reason") or "export failed") for event in rejects})
        return Gate("Issue #2 full-year export", "FAIL", f"{len(rejects)} rejection(s)", "at least one verified CSV export", "; ".join(reasons[:4]))
    return Gate("Issue #2 full-year export", "NO DATA", "no export events", "at least one verified non-empty CSV export with a body hash")


def overload_failure_gate(events):
    rejects = [
        event
        for event in events["events"]
        if event.get("phase") == "overload" and event.get("event") in {"voucher_reject", "voucher_unknown", "search_reject"}
    ]
    slow = []
    for event in events["events"]:
        if event.get("phase") != "overload":
            continue
        if event.get("event") == "voucher_complete" and float(event.get("duration_ms") or 0) >= SUBMIT_P95_MS:
            slow.append(event)
        if event.get("event") == "search_complete" and float(event.get("duration_ms") or 0) >= SEARCH_P95_MS:
            slow.append(event)
    if rejects or slow:
        return Gate(
            "Overload failure induced",
            "PASS",
            f"{len(rejects)} rejection(s), {len(slow)} SLA breach sample(s)",
            "at least one cashier target must fail under overload",
        )
    return Gate(
        "Overload failure induced",
        "FAIL",
        "no cashier rejection or SLA breach observed",
        "at least one cashier target must fail under overload",
        "Increase overload multipliers/durations; surviving the maximum step does not prove recovery.",
    )


def recovery_gate(events):
    rejects = [
        event
        for event in events["events"]
        if event.get("phase") == "recovery" and event.get("event") in {"voucher_reject", "voucher_unknown", "search_reject"}
    ]
    rows = []
    completes = [event for event in events["events"] if event.get("event") == "voucher_complete" and event.get("phase") == "recovery"]
    searches = [event for event in events["events"] if event.get("event") == "search_complete" and event.get("phase") == "recovery"]
    rows.append({"phase": "recovery", "group": "vouchers", "count": len(completes), "p95": percentile([e.get("duration_ms") for e in completes], 0.95)})
    rows.append({"phase": "recovery", "group": "searches", "count": len(searches), "p95": percentile([e.get("duration_ms") for e in searches], 0.95)})
    bad_latency = any(row["p95"] is None or row["p95"] >= (SUBMIT_P95_MS if row["group"] == "vouchers" else SEARCH_P95_MS) for row in rows)
    if rejects or bad_latency:
        return Gate(
            "Recovery within 2 minutes",
            "FAIL",
            f"{len(rejects)} rejection(s); voucher p95 {fmt_ms(rows[0]['p95'])}; search p95 {fmt_ms(rows[1]['p95'])}",
            "no restart and core SLAs recovered within 2 minutes",
        )
    return Gate(
        "Recovery within 2 minutes",
        "PASS",
        f"voucher p95 {fmt_ms(rows[0]['p95'])} (n={rows[0]['count']}); search p95 {fmt_ms(rows[1]['p95'])} (n={rows[1]['count']})",
        "no restart and core SLAs recovered within 2 minutes",
    )


def memory_gates(memory):
    if not memory.row_count:
        detail = memory.error or "No container statistics rows were parsed."
        return [
            Gate("Container OOM/restart evidence", "NO DATA", "no stats", "0 OOM kills, 0 restarts", detail),
        ]
    missing_oom = sorted(entry.name for entry in memory.containers.values() if entry.oom_evidence_missing or entry.docker_evidence_missing)
    missing_restart = sorted(entry.name for entry in memory.containers.values() if entry.restart_evidence_missing)
    restarts = max((base.observed_restarts(entry) for entry in memory.containers.values()), default=0.0)
    docker_killed = sorted(entry.name for entry in memory.containers.values() if entry.docker_oom_killed)
    if missing_oom or missing_restart:
        return [
            Gate(
                "Container OOM/restart evidence",
                "FAIL",
                "missing telemetry",
                "0 OOM kills, 0 restarts",
                "missing OOM telemetry: " + ", ".join(missing_oom) + "; missing restart telemetry: " + ", ".join(missing_restart),
            )
        ]
    failed = memory.total_oom_kills > 0 or docker_killed or restarts > 0
    return [
        Gate(
            "Container OOM/restart evidence",
            "FAIL" if failed else "PASS",
            f"oom={memory.total_oom_kills:.0f}, docker_oom={len(docker_killed)}, restarts={restarts:.0f}",
            "0 OOM kills, 0 restarts",
        )
    ]



def normalize_events(events):
    """Accept either a raw event list or the indexed mapping returned by parse_events."""
    if isinstance(events, dict) and isinstance(events.get("events"), list):
        return events
    if not isinstance(events, list):
        raise TypeError("events must be a list of seasonal events or verify_run.parse_events() output")
    indexed = {"events": events, "started": {}, "completed": {}, "rejected": {}, "unknown": {}, "config": {}}
    for event in events:
        if not isinstance(event, dict):
            continue
        client_id = str(event.get("client_voucher_id") or "")
        kind = event.get("event")
        if kind == "config" and isinstance(event.get("config"), dict):
            indexed["config"] = event["config"]
        elif kind == "voucher_start" and client_id:
            indexed["started"][client_id] = event
        elif kind == "voucher_complete" and client_id:
            indexed["completed"][client_id] = event
        elif kind == "voucher_reject" and client_id:
            indexed["rejected"][client_id] = event
        elif kind == "voucher_unknown" and client_id:
            indexed["unknown"][client_id] = event
    return indexed


def allocated_count(per_minute, minute, scale_num, scale_den):
    local_minute = int(minute) % int(scale_den)
    end = ((local_minute + 1) * per_minute * scale_num) // scale_den
    start = (local_minute * per_minute * scale_num) // scale_den
    return end - start


def scale_ratio(segment):
    num = segment.get("scale_num")
    den = segment.get("scale_den")
    if isinstance(num, int) and isinstance(den, int) and num > 0 and den > 0:
        return num, den
    scale = float(segment.get("scale") or 1)
    den = 1000000
    num = round(scale * den)
    divisor = math.gcd(num, den)
    return num // divisor, den // divisor


def per_minute_mix_gate(events):
    segments = events["config"].get("phases") or []
    if not segments:
        return Gate(
            "Approved per-minute voucher mix",
            "NO DATA",
            "missing phase config",
            "exact endpoint-bounded integer schedule each logical minute",
        )
    matches = arrival_schedule_matches(events)
    failures = []
    observed_parts = []
    for match in matches:
        if match["config_failures"]:
            failures.append(f"{match['segment']}: {'; '.join(match['config_failures'][:4])}")
            continue
        if not match["matched"]:
            failures.append(f"{match['segment']}: " + "; ".join((match["differences"] or ["no integer schedule candidate matched"])[:4]))
            continue
        matched = match["matched"]
        observed_parts.append(
            f"{match['segment']}: regular={matched['regular_iterations']}, contention={matched['contention_iterations']}"
        )
    return Gate(
        "Approved per-minute voucher mix",
        "PASS" if not failures else "FAIL",
        "; ".join(observed_parts),
        "each logical minute exactly matches one endpoint-bounded integer schedule candidate",
        "; ".join(failures[:12]),
    )


def time_unit_minutes(value):
    text = str(value or "")
    if text.endswith("ms"):
        return float(text[:-2]) / 60000.0
    if text.endswith("s"):
        return float(text[:-1]) / 60.0
    if text.endswith("m"):
        return float(text[:-1])
    if text.endswith("h"):
        return float(text[:-1]) * 60.0
    return float(text) / 60.0


def rate_per_minute(rate):
    if not isinstance(rate, dict):
        return None
    try:
        return float(rate.get("rate")) / time_unit_minutes(rate.get("timeUnit"))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def approved_margin_rate_gate(events):
    config = events["config"]
    segments = config.get("phases") or []
    if not segments:
        return Gate("Approved margin/recovery offered rates", "NO DATA", "missing phase config", "baseline/recovery 1x; margin 1.25x; overload >1x")
    failures = []
    observed = []
    expected_scales = {"baseline": (1, 1), "margin": (5, 4), "recovery": (1, 1)}
    for segment in segments:
        name = str(segment.get("name") or "")
        phase = str(segment.get("phase") or name)
        num, den = scale_ratio(segment)
        scale = num / den
        if phase in expected_scales and (num, den) != expected_scales[phase]:
            failures.append(f"{name}: scale {num}/{den}, expected {expected_scales[phase][0]}/{expected_scales[phase][1]}")
        if phase == "overload" and scale <= 1:
            failures.append(f"{name}: overload scale {scale:g} is not above baseline")
        rates = {
            "regular": rate_per_minute(segment.get("regular_rate")),
            "contention": rate_per_minute(segment.get("contention_rate")),
            "search": rate_per_minute(segment.get("search_rate")),
        }
        expected = {"regular": 88.0 * scale, "contention": 1.0 * scale, "search": 80.0 * scale}
        for key, expected_rate in expected.items():
            if rates[key] is None or abs(rates[key] - expected_rate) > 1e-9:
                failures.append(f"{name}: {key} rate {rates[key]}/min, expected {expected_rate:g}/min")
        if int(segment.get("template_iterations_per_block") or 0) != 88 * num:
            failures.append(f"{name}: template block {segment.get('template_iterations_per_block')}, expected {88 * num}")
        observed.append(
            f"{name}: {scale:g}x regular={rates['regular'] if rates['regular'] is not None else 'missing'}/min "
            f"contention={rates['contention'] if rates['contention'] is not None else 'missing'}/min"
        )
    if str(config.get("profile") or "") == "acceptance":
        expected_durations = {"baseline": 3600, "margin": 900, "recovery": 120}
        for segment in segments:
            phase = str(segment.get("phase") or "")
            if phase in expected_durations and int(segment.get("duration_seconds") or 0) != expected_durations[phase]:
                failures.append(
                    f"{segment.get('name')}: duration {segment.get('duration_seconds')}s, expected {expected_durations[phase]}s"
                )
    return Gate(
        "Approved margin/recovery offered rates",
        "PASS" if not failures else "FAIL",
        "; ".join(observed),
        "exact 1x baseline/recovery, 1.25x margin, >1x overload offered rates",
        "; ".join(failures[:10]),
    )

def actor_separation_gate(events):
    actors = events["config"].get("actors") or {}
    managers_enabled = bool(events["config"].get("managers_enabled", True))
    required = ["cashier"] + (["summary_warehouse", "summary_monthly", "detail_reader", "export_requester"] if managers_enabled else [])
    failures = []
    for key in required:
        actor = actors.get(key)
        if not isinstance(actor, dict) or not actor.get("user") or not actor.get("authorization"):
            failures.append(f"missing explicit authorized actor {key}")
    users = [str(actors[key].get("user") or "") for key in required if isinstance(actors.get(key), dict)]
    normalized = [user.casefold() for user in users]
    if len(set(normalized)) != len(normalized):
        failures.append("actor users are not exact distinct users")
    if "administrator" in normalized:
        failures.append("Administrator is not an authorized seasonal role user")
    cashier_user = str((actors.get("cashier") or {}).get("user") or "")
    cashier_events = [
        event for event in events["events"]
        if event.get("event") in {"voucher_start", "voucher_complete", "voucher_reject", "voucher_unknown", "search_complete", "search_reject"}
    ]
    cashier_actor_users = {str(event.get("actor_user") or "") for event in cashier_events}
    if cashier_events and ("" in cashier_actor_users or cashier_actor_users != {cashier_user}):
        failures.append(f"cashier load events use actor users {sorted(cashier_actor_users)}, expected exactly {cashier_user}")
    for event in events["events"]:
        kind = event.get("event")
        if kind == "manager_summary":
            expected_keys = {"summary_warehouse" if event.get("group_by") == "Warehouse" else "summary_monthly"}
        else:
            expected_keys = {
                "manager_detail": {"detail_reader"},
                "export_complete": {"export_requester"},
            }.get(kind)
        if not expected_keys:
            continue
        actor_key = str(event.get("actor") or "")
        configured = str((actors.get(actor_key) or {}).get("user") or "")
        if actor_key not in expected_keys or not configured or str(event.get("actor_user") or "") != configured:
            failures.append(f"{event.get('event')} actor evidence {actor_key}/{event.get('actor_user')} does not match the configured authorized user")
    observed = "; ".join(f"{key}={actors.get(key, {}).get('user', 'missing')}" for key in required) or "no actors"
    return Gate(
        "Authorized exact distinct actor users",
        "PASS" if not failures else "FAIL",
        observed,
        "cashier, two summary readers, detail reader and export requester are distinct non-administrator users",
        "; ".join(failures),
    )


def report_window_gate(events):
    windows = events["config"].get("report_windows") or {}
    failures = []
    for scope in ("year_to_date", "full_year"):
        window = windows.get(scope)
        if not isinstance(window, dict) or not window.get("from_date") or not window.get("to_date") or window.get("scope") != scope:
            failures.append(f"missing explicit {scope} window")
    if not failures:
        full = windows["full_year"]
        try:
            from datetime import date
            start = date.fromisoformat(full["from_date"])
            end = date.fromisoformat(full["to_date"])
            if (end - start).days < 364:
                failures.append("full-year export window spans fewer than 365 inclusive dates")
        except (TypeError, ValueError):
            failures.append("full-year export window is not ISO YYYY-MM-DD")
    if not failures:
        expected_by_event = {
            "manager_summary": "year_to_date",
            "manager_detail": "year_to_date",
            "export_complete": "full_year",
        }
        for event in events["events"]:
            scope = expected_by_event.get(event.get("event"))
            if not scope:
                continue
            expected = windows[scope]
            actual = (event.get("window_scope"), event.get("window_from_date"), event.get("window_to_date"))
            wanted = (scope, expected["from_date"], expected["to_date"])
            if actual != wanted:
                failures.append(f"{event.get('event')} window evidence {actual} does not match explicit {wanted}")
    observed = "; ".join(
        f"{scope}={windows.get(scope, {}).get('from_date', '?')}..{windows.get(scope, {}).get('to_date', '?')}"
        for scope in ("year_to_date", "full_year")
    )
    return Gate(
        "Explicit YTD and full-year windows",
        "PASS" if not failures else "FAIL",
        observed,
        "manager events must declare year_to_date; export events must declare a >=365-date full_year window",
        "; ".join(failures),
    )


def workflow_completion_gate(events):
    failures = []
    for phase in CORE_PHASES:
        rejects = [
            event for event in events["events"]
            if event.get("phase") == phase and event.get("event") in {"voucher_reject", "search_reject"}
        ]
        unknown = [
            event for event in events["events"]
            if event.get("phase") == phase and event.get("event") == "voucher_unknown"
        ]
        if rejects or unknown:
            failures.append(f"{phase}: {len(rejects)} cashier rejection(s), {len(unknown)} unknown voucher(s)")
    purchase_pairs = 0
    missing_invoice = 0
    for client_id, start in events["started"].items():
        if start.get("type") != "purchase_receipt":
            continue
        purchase_pairs += 1
        expected_invoice = client_id.replace("-purchase_receipt-", "-purchase_invoice-")
        if expected_invoice not in events["started"]:
            missing_invoice += 1
    if missing_invoice:
        failures.append(f"{missing_invoice}/{purchase_pairs} purchase receipts have no linked invoice start")
    return Gate(
        "Baseline/margin/recovery workflow completion",
        "PASS" if not failures else "FAIL",
        f"{purchase_pairs} purchase receipt/invoice pair(s) started",
        "no core-phase rejection/unknown; every receipt workflow starts its linked invoice",
        "; ".join(failures[:8]),
    )


def collect_manager_outputs(events):
    """Return hash-only manager evidence in load order for external trusted replay comparison."""
    indexed = normalize_events(events)
    outputs = []
    for index, event in enumerate(indexed["events"]):
        kind = event.get("event")
        output = None
        if kind == "manager_summary" and event.get("result_sha256"):
            output = {
                "output_type": "ordered_financial_summary",
                "sha256": event.get("result_sha256"),
                "hash_input": "sha256(canonical_json([columns,result])); object keys sorted recursively, row/column order preserved, max 15 fractional digits",
            }
        elif kind == "manager_detail" and event.get("first_page_sha256"):
            output = {
                "output_type": "invoice_detail_first_page",
                "sha256": event.get("first_page_sha256"),
                "hash_input": "sha256(canonical_json([columns,result])); object keys sorted recursively, max 15 fractional digits, first ordered page only",
            }
        elif kind == "export_complete" and event.get("sha256"):
            output = {
                "output_type": "full_year_export_body",
                "sha256": event.get("sha256"),
                "hash_input": "sha256(raw CSV response body bytes decoded as text)",
            }
        if output:
            output.update(
                {
                    "event_index": index,
                    "observation_id": event.get("observation_id"),
                    "event": kind,
                    "phase": event.get("phase"),
                    "segment": event.get("segment"),
                    "group_by": event.get("group_by"),
                    "actor": event.get("actor"),
                    "actor_user": event.get("actor_user"),
                    "hash_algorithm": event.get("hash_algorithm") or "sha256",
                    "hash_canonicalization": event.get("hash_canonicalization"),
                    "window": {
                        "scope": event.get("window_scope"),
                        "from_date": event.get("window_from_date"),
                        "to_date": event.get("window_to_date"),
                    },
                }
            )
            outputs.append(output)
    return outputs


def comparable_manager_output_gate(events, enabled):
    if not enabled:
        return Gate("Comparable manager output hashes", "SKIP", "managers disabled", "summary/detail/export hashes required")
    outputs = collect_manager_outputs(events)
    required = {
        "ordered_financial_summary/Warehouse": any(o["output_type"] == "ordered_financial_summary" and o.get("group_by") == "Warehouse" for o in outputs),
        "ordered_financial_summary/Monthly": any(o["output_type"] == "ordered_financial_summary" and o.get("group_by") == "Monthly" for o in outputs),
        "invoice_detail_first_page": any(o["output_type"] == "invoice_detail_first_page" for o in outputs),
        "full_year_export_body": any(o["output_type"] == "full_year_export_body" for o in outputs),
    }
    missing = [name for name, present in required.items() if not present]
    bad_hash = [o["observation_id"] for o in outputs if not isinstance(o.get("sha256"), str) or len(o["sha256"]) != 64]
    status = "PASS" if not missing and not bad_hash and outputs else "FAIL"
    return Gate(
        "Comparable manager output hashes",
        status,
        f"{len(outputs)} hash-only observation(s)",
        "Warehouse/Monthly ordered summaries, first detail page and export body hashes present",
        ("missing: " + ", ".join(missing) if missing else "") + ("; invalid hash observations" if bad_hash else ""),
    )



def public_arrival_schedule(events):
    """Machine-readable integer-arrival interpretation for the protected driver."""
    rows = []
    for match in arrival_schedule_matches(events):
        matched = match.get("matched")
        rows.append(
            {
                "segment": match["segment"],
                "started": match["started"],
                "matched": bool(matched),
                "regular_iterations": matched.get("regular_iterations") if matched else None,
                "contention_iterations": matched.get("contention_iterations") if matched else None,
                "bounded_total_range": list(match["total_range"]) if match.get("total_range") else None,
                "bounded_type_ranges": {
                    shape: list(bounds) for shape, bounds in sorted((match.get("type_range") or {}).items())
                },
                "shape_totals": {
                    f"{shape[0]}x{shape[1]}": count
                    for shape, count in sorted((matched.get("totals") or {}).items())
                } if matched else {},
                "config_failures": match.get("config_failures") or [],
                "differences": (match.get("differences") or [])[:12],
            }
        )
    return rows

def evaluate_events(events, k6_summary, *, cashier_only=False):
    """Evaluate seasonal load evidence without requiring any production endpoint.

    ``events`` may be a raw list of KHITMEE_SEASONAL_EVENT payloads or the indexed
    mapping returned by ``verify_run.parse_events``. The result is JSON-serializable.
    It is a workload-evidence verdict, not a substitute for database/machine attestation.
    """
    indexed = normalize_events(events)
    summary = k6_summary or {}
    config = indexed["config"]
    managers_enabled = bool(config.get("managers_enabled", True)) and not cashier_only
    export_enabled = managers_enabled and bool(config.get("export_enabled", True))

    gates = [
        discovery_gate(indexed),
        actor_separation_gate(indexed),
        report_window_gate(indexed),
    ]
    counts_gate, count_rows = offered_counts_gate(indexed, summary)
    gates.extend(
        [
            counts_gate,
            per_minute_mix_gate(indexed),
            approved_margin_rate_gate(indexed),
            voucher_latency_gate(indexed),
            contention_gate(indexed),
        ]
    )
    searches = successful_events(indexed, "search_complete")
    search_rows = []
    for phase in CORE_PHASES:
        durations = [event.get("duration_ms") for event in searches if event.get("phase") == phase]
        search_rows.append({"phase": phase, "group": "all", "count": len(durations), "p95": percentile(durations, 0.95)})
    gates.extend(
        [
            latency_gate("Cashier search p95", search_rows, SEARCH_P95_MS),
            retry_identity_gate(indexed),
            workflow_completion_gate(indexed),
            overload_failure_gate(indexed),
            recovery_gate(indexed),
            summary_gate(indexed, managers_enabled),
            detail_gate(indexed, managers_enabled),
            export_gate(indexed, export_enabled),
            comparable_manager_output_gate(indexed, managers_enabled),
        ]
    )

    def status_of(names):
        relevant = [gate for gate in gates if gate.name in names]
        return "NO-GO" if any(gate.status in {"FAIL", "NO DATA"} for gate in relevant) else "GO"

    cashier_status = status_of(
        {
            "Real cashier-only discovery",
            "Authorized exact distinct actor users",
            "Offered/started/completed/rejected/dropped accounting",
            "Approved per-minute voucher mix",
            "Approved margin/recovery offered rates",
            "Cashier submit-to-confirmation p95",
            "Six-shop hot-stock contention",
            "Cashier search p95",
            "Lost-acknowledgement retry identity",
            "Baseline/margin/recovery workflow completion",
            "Overload failure induced",
            "Recovery within 2 minutes",
        }
    )
    summary_status = summary_gate(indexed, managers_enabled).status
    detail_status = detail_gate(indexed, managers_enabled).status
    export_status = export_gate(indexed, export_enabled).status
    feature_statuses = {
        "cashier qualification": cashier_status,
        "manager summary": "SKIP" if summary_status == "SKIP" else ("GO" if summary_status == "PASS" else "NO-GO"),
        "issue #2 detail pagination": "SKIP" if detail_status == "SKIP" else ("GO" if detail_status == "PASS" else "NO-GO"),
        "issue #2 full-year export": "SKIP" if export_status == "SKIP" else ("GO" if export_status == "PASS" else "NO-GO"),
    }
    pass_statuses = {"PASS"} if not cashier_only else {"PASS", "SKIP"}
    verdict = "GO" if all(gate.status in pass_statuses for gate in gates) else "NO-GO"
    return {
        "passed": verdict == "GO",
        "verdict": verdict,
        "scope": "cashier-only" if cashier_only else "standalone seasonal workload",
        "gates": [vars(gate) for gate in gates],
        "feature_statuses": feature_statuses,
        "offered_work": count_rows,
        "offered_totals": {
            "minimum": sum(row["expected_minimum"] or 0 for row in count_rows),
            "maximum": sum(row["expected_maximum"] or 0 for row in count_rows),
        },
        "arrival_schedule": public_arrival_schedule(indexed),
        "manager_outputs": collect_manager_outputs(indexed),
        "report_windows": config.get("report_windows") or {},
        "actors": config.get("actors") or {},
        "notes": [
            "This callable uses only k6 events and the k6 summary; it does not replace direct database, machine-size, or trusted-replay attestation.",
            "manager_outputs are hash-only and intentionally omit customer row values.",
        ],
    }

def render_markdown(run_label, gates, count_rows, feature_statuses, verification, memory=None):
    lines = [
        f"# Seasonal acceptance scorecard: {run_label}",
        "",
        "## Feature qualification",
        "",
        "| Feature | Status |",
        "|---|---|",
    ]
    for name, status in feature_statuses.items():
        lines.append(f"| {name} | {status} |")
    lines.extend(["", "## Gates", "", "| Gate | Status | Observed | Threshold | Detail |", "|---|---|---|---|---|"])
    for gate in gates:
        detail = str(gate.detail or "").replace("|", "\\|").replace("\n", " ")
        observed = str(gate.observed).replace("|", "\\|")
        lines.append(f"| {gate.name} | {gate.status} | {observed} | {gate.threshold} | {detail} |")
    if count_rows:
        lines.extend(["", "## Offered voucher mix by segment", "", "| Segment | Started / offered | Type breakdown |", "|---|---|---|"])
        for row in count_rows:
            lines.append(f"| {row['segment']} | {row['started']} / {row['expected']} | {row['types']} |")
    if memory is not None and memory.row_count:
        lines.extend(["", "## Container memory, OOM and restart evidence", "", "| Container | Peak cgroup MiB | Limit MiB | OOM kills | OOM events | Restarts | Docker OOM |", "|---|---:|---:|---:|---:|---:|---|"])
        for entry in sorted(memory.containers.values(), key=lambda item: item.name):
            restarts = base.observed_restarts(entry)
            lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                str(entry.name).replace("|", "\\|"),
                "no data" if entry.peak_cgroup_bytes is None else f"{entry.peak_cgroup_bytes / 1024**2:.1f}",
                "no data" if entry.memory_limit_bytes is None else f"{entry.memory_limit_bytes / 1024**2:.1f}",
                int(entry.peak_oom_kills),
                "no data" if entry.peak_cgroup_oom_events is None else int(entry.peak_cgroup_oom_events),
                int(restarts),
                "yes" if entry.docker_oom_killed else "no",
            ))
    if verification:
        lines.extend(["", "## Independent verifier", "", "```json", json.dumps({k: verification.get(k) for k in ("passed", "run_prefix", "started", "completed", "rejected", "unknown", "retried_completed", "reconciled_completed", "lost_ack_observed", "by_type")}, indent=2, sort_keys=True), "```"])
    lines.append("")
    return "\n".join(lines)


def evaluate(args):
    events = verify_run.parse_events(args.k6_log)
    summary = load_json(args.k6_summary, required=True)
    verification = load_json(args.verification) if args.verification else None
    memory = base.analyze_memory(args.stats_csv)
    config = events["config"]
    managers_enabled = bool(config.get("managers_enabled", True)) and not args.cashier_only
    export_enabled = managers_enabled and bool(config.get("export_enabled", True))

    gates = [discovery_gate(events), actor_separation_gate(events), report_window_gate(events)]
    counts_gate, count_rows = offered_counts_gate(events, summary)
    gates.append(counts_gate)
    gates.append(per_minute_mix_gate(events))
    gates.append(approved_margin_rate_gate(events))
    gates.append(voucher_latency_gate(events))
    gates.append(contention_gate(events))
    searches = successful_events(events, "search_complete")
    search_rows = []
    for phase in CORE_PHASES:
        durations = [event.get("duration_ms") for event in searches if event.get("phase") == phase]
        search_rows.append({"phase": phase, "group": "all", "count": len(durations), "p95": percentile(durations, 0.95)})
    gates.append(latency_gate("Cashier search p95", search_rows, SEARCH_P95_MS))
    gates.append(correctness_gate(verification))
    gates.append(retry_identity_gate(events))
    gates.append(workflow_completion_gate(events))
    gates.append(overload_failure_gate(events))
    gates.append(recovery_gate(events))
    gates.extend(memory_gates(memory))
    gates.append(summary_gate(events, managers_enabled))
    gates.append(detail_gate(events, managers_enabled))
    gates.append(export_gate(events, export_enabled))
    gates.append(comparable_manager_output_gate(events, managers_enabled))

    def status_of(names):
        relevant = [gate for gate in gates if gate.name in names]
        if any(gate.status in {"FAIL", "NO DATA"} for gate in relevant):
            return "NO-GO"
        return "GO"

    cashier_status = status_of(
        {
            "Real cashier-only discovery",
            "Offered/started/completed/rejected/dropped accounting",
            "Approved per-minute voucher mix",
            "Approved margin/recovery offered rates",
            "Cashier submit-to-confirmation p95",
            "Six-shop hot-stock contention",
            "Cashier search p95",
            "Independent database correctness",
            "Lost-acknowledgement retry identity",
            "Baseline/margin/recovery workflow completion",
            "Overload failure induced",
            "Recovery within 2 minutes",
            "Container OOM/restart evidence",
        }
    )
    summary_status = summary_gate(events, managers_enabled).status
    detail_status = detail_gate(events, managers_enabled).status
    export_status = export_gate(events, export_enabled).status
    feature_statuses = {
        "cashier qualification": cashier_status,
        "manager summary": "SKIP" if summary_status == "SKIP" else ("GO" if summary_status == "PASS" else "NO-GO"),
        "issue #2 detail pagination": "SKIP" if detail_status == "SKIP" else ("GO" if detail_status == "PASS" else "NO-GO"),
        "issue #2 full-year export": "SKIP" if export_status == "SKIP" else ("GO" if export_status == "PASS" else "NO-GO"),
    }
    cashier_only = bool(args.cashier_only)
    if cashier_only:
        feature_statuses["overall seasonal qualification"] = "NOT RUN (cashier-only mode; manager gates intentionally skipped)"
        verdict = cashier_status
        feature_statuses["cashier-only verdict"] = verdict
    else:
        verdict = "GO" if all(gate.status == "PASS" for gate in gates) else "NO-GO"
        feature_statuses["standalone workload verdict"] = verdict
        feature_statuses["2 GiB seasonal qualification"] = "Recorded by the protected supervisor after its guest and oracle checks"
    markdown = render_markdown(args.run_label, gates, count_rows, feature_statuses, verification, memory)
    return verdict, markdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k6-summary", required=True)
    parser.add_argument("--k6-log", required=True)
    parser.add_argument("--stats-csv", required=True)
    parser.add_argument("--verification")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-label", default="seasonal")
    parser.add_argument("--cashier-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        verdict, markdown = evaluate(args)
    except Exception as exc:
        verdict = "NO-GO"
        markdown = f"# Seasonal acceptance scorecard: {args.run_label}\n\nNO-GO: scorecard evaluation failed closed: {exc}\n"
    Path(args.output).write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
