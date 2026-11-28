#!/usr/bin/env python3
"""Validate owner-supplied host environment attestation for qualification.

In-guest measurements cannot prove the hypervisor assignment or the physical
placement of a second Docker daemon. Qualification therefore requires the
owner to attest those facts out of band; this module validates the declaration
and cross-checks it against evidence the harness can observe.
"""

import argparse
import json
import pathlib
import sys
from urllib.parse import urlparse

SCHEMA_VERSION = "seasonal-environment-attestation-v1"
REQUIRED_VCPUS = 2
REQUIRED_MEMORY_MIB = 2048
REQUIRED_SWAP_MIB = 0
MEMORY_RESERVATION_ALLOWANCE_RATIO = 0.10


def load_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def identity(value):
    return str(value or "").strip().lower()


def endpoint_host(endpoint):
    return (urlparse(str(endpoint or "")).hostname or "").strip().lower()


def shape_problems(data):
    problems = []
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append("environment attestation schema_version is missing or unsupported")
    for key in ("attested_at", "owner"):
        if not str(data.get(key) or "").strip():
            problems.append(f"environment attestation missing {key}")
    guest = data.get("guest") if isinstance(data.get("guest"), dict) else {}
    generator = data.get("load_generator") if isinstance(data.get("load_generator"), dict) else {}
    for key in ("host_identity", "vm_identity", "vcpus", "assigned_memory_mib", "swap_mib"):
        if key not in guest:
            problems.append(f"environment attestation missing guest.{key}")
    for key in ("host_identity", "address"):
        if key not in generator:
            problems.append(f"environment attestation missing load_generator.{key}")
    return problems, guest, generator


def validate(attestation_path, guest_evidence=None, generator_evidence=None):
    path = pathlib.Path(attestation_path) if attestation_path else None
    data = {}
    problems = []
    if not path or not str(path):
        problems.append("owner environment attestation file was not supplied")
    elif not path.is_file():
        problems.append("owner environment attestation file is missing")
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append("owner environment attestation is not valid JSON: " + str(exc))
    shape, guest, generator = shape_problems(data if isinstance(data, dict) else {})
    problems.extend(shape)

    if guest.get("vcpus") != REQUIRED_VCPUS:
        problems.append("owner attestation does not declare exactly 2 guest vCPUs")
    if guest.get("assigned_memory_mib") != REQUIRED_MEMORY_MIB:
        problems.append("owner attestation does not declare exactly 2048 MiB assigned guest memory")
    if guest.get("swap_mib") != REQUIRED_SWAP_MIB:
        problems.append("owner attestation does not declare guest swap disabled as 0 MiB")
    if not identity(guest.get("host_identity")) or not identity(guest.get("vm_identity")):
        problems.append("owner attestation lacks non-empty guest host/VM identity")

    guest_evidence = guest_evidence or {}
    observed_cpus = int(guest_evidence.get("online_cpu_count") or 0)
    if observed_cpus != REQUIRED_VCPUS:
        problems.append(f"observed guest online CPUs ({observed_cpus}) do not fit the owner-declared 2-vCPU assignment")
    observed_mem_mib = float(guest_evidence.get("guest_mem_total_mib") or 0)
    low = REQUIRED_MEMORY_MIB * (1 - MEMORY_RESERVATION_ALLOWANCE_RATIO)
    if not low <= observed_mem_mib <= REQUIRED_MEMORY_MIB:
        problems.append(
            f"observed guest MemTotal {observed_mem_mib:.1f} MiB does not fit the owner-declared 2048 MiB assignment"
        )
    if int(guest_evidence.get("guest_swap_total_bytes") or 0) != 0:
        problems.append("observed guest swap contradicts the owner-declared zero-swap assignment")

    generator_result = {"checked": True}
    generator_evidence = generator_evidence or {}
    observed_address = endpoint_host(generator_evidence.get("generator_endpoint"))
    declared_address = identity(generator.get("address"))
    declared_host = identity(generator.get("host_identity"))
    guest_host = identity(guest.get("host_identity"))
    guest_vm = identity(guest.get("vm_identity"))
    daemon_name = identity((generator_evidence.get("generator_daemon_identity") or {}).get("name"))
    distinct = bool(declared_host) and declared_host not in {guest_host, guest_vm}
    if not declared_host:
        problems.append("owner attestation lacks load-generator host identity")
    elif not distinct:
        problems.append("owner attestation does not establish a load-generator machine distinct from the guest host/VM")
    if not declared_address:
        problems.append("owner attestation lacks load-generator address")
    elif not observed_address or declared_address != observed_address:
        problems.append(
            f"observed generator endpoint host ({observed_address or 'missing'}) does not match owner-declared generator address"
        )
    if daemon_name and declared_host and daemon_name != declared_host:
        problems.append("generator Docker daemon name does not match owner-declared generator host identity")
    generator_result.update({
        "observed_address": observed_address,
        "declared_address": declared_address,
        "declared_host_identity": declared_host,
        "guest_host_identity": guest_host,
        "guest_vm_identity": guest_vm,
        "distinct_from_guest": distinct,
        "observed_daemon_name": daemon_name,
    })

    passed = not problems
    return {
        "schema_version": SCHEMA_VERSION + "-result",
        "attestation_path": str(path or ""),
        "passed": passed,
        "attestation": "qualifying" if passed else "diagnostic",
        "problems": problems,
        "guest_assignment": {
            "vcpus": guest.get("vcpus"),
            "assigned_memory_mib": guest.get("assigned_memory_mib"),
            "swap_mib": guest.get("swap_mib"),
            "host_identity": guest.get("host_identity"),
            "vm_identity": guest.get("vm_identity"),
        },
        "load_generator": generator_result,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--attestation", default="")
    parser.add_argument("--guest-evidence", required=True)
    parser.add_argument("--generator-evidence", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = validate(
        args.attestation,
        load_json(args.guest_evidence),
        load_json(args.generator_evidence) if args.generator_evidence else {},
    )
    pathlib.Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
