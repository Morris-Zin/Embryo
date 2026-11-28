#!/usr/bin/env python3
"""Attest whether the k6 load generator executes outside the qualification guest.

The qualification default (a container on the guest's local Docker daemon, or a
host k6 process inside the guest) is useful for smoke diagnostics but is never
qualification evidence. A qualifying generator must use a demonstrably different
Docker daemon/host. Same-daemon endpoint aliases fail closed.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
from urllib.parse import urlparse

SCHEMA = "seasonal-load-generator-attestation-v1"
LOCAL_ENDPOINT_ALIASES = {"", "unix:///var/run/docker.sock", "unix:///var/run/docker.sock/"}


def docker_info(endpoint=""):
    env = dict(os.environ)
    if endpoint:
        env["DOCKER_HOST"] = endpoint
    else:
        env.pop("DOCKER_HOST", None)
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        text=True,
        capture_output=True,
        timeout=15,
        env=env,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "docker info failed")[:240])
    data = json.loads(result.stdout)
    return {
        "daemon_id": str(data.get("ID") or ""),
        "name": str(data.get("Name") or ""),
        "server_version": str(data.get("ServerVersion") or ""),
        "operating_system": str(data.get("OperatingSystem") or ""),
        "kernel_version": str(data.get("KernelVersion") or ""),
    }


def remote_endpoint(endpoint):
    if not endpoint or endpoint in LOCAL_ENDPOINT_ALIASES:
        return False, "missing K6_DOCKER_HOST remote daemon endpoint"
    parsed = urlparse(endpoint)
    if parsed.scheme in {"tcp", "http", "https", "ssh"}:
        host = (parsed.hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"}:
            return False, "K6_DOCKER_HOST points at the local host"
        return True, ""
    return False, "K6_DOCKER_HOST scheme does not identify a remote daemon"


def attest(runtime="docker", endpoint="", info_reader=None):
    info_reader = info_reader or docker_info
    problems = []
    result = {
        "schema_version": SCHEMA,
        "runtime": runtime,
        "generator_endpoint": endpoint,
        "passed": False,
        "off_guest": False,
        "attestation": "diagnostic",
    }
    try:
        result["guest_daemon_identity"] = info_reader("")
    except Exception as exc:
        result["guest_daemon_identity"] = {"error": str(exc)}
        problems.append("cannot identify the guest-local Docker daemon")

    if runtime == "host":
        problems.append("host k6 executes inside the qualification guest")
    elif runtime != "docker":
        problems.append("K6_RUNTIME must resolve explicitly to docker for off-guest daemon attestation")
    else:
        remote, problem = remote_endpoint(endpoint)
        if problem:
            problems.append(problem)
        if remote:
            try:
                generator = info_reader(endpoint)
                result["generator_daemon_identity"] = generator
                if not generator.get("daemon_id"):
                    problems.append("remote generator daemon identity is missing")
                elif generator.get("daemon_id") == (result.get("guest_daemon_identity") or {}).get("daemon_id"):
                    problems.append("remote endpoint resolves to the same Docker daemon as the guest")
            except Exception as exc:
                result["generator_daemon_identity"] = {"error": str(exc)}
                problems.append("cannot identify the remote k6 Docker daemon")
    passed = not problems
    result["problems"] = problems
    result["passed"] = passed
    result["off_guest"] = passed
    result["attestation"] = "qualifying" if passed else "diagnostic"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--docker-host", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = attest(args.runtime, args.docker_host)
    pathlib.Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
