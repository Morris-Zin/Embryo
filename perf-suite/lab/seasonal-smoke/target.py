"""Inspected over pinned SSH before and after the external cashier run."""
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys

namespace, run, mode, fixture = sys.argv[1:5]
if not re.fullmatch(r"khitmee-arm-[0-9a-f]{16}", namespace) or not re.fullmatch(r"[0-9a-f]{12}", run):
    raise ValueError("invalid disposable arm or run ID")
if socket.gethostname() != "khitmee-lab" or subprocess.check_output(["systemd-detect-virt"]).strip() != b"kvm":
    raise ValueError("unexpected guest")
docker = ["docker", "--host=unix:///var/run/docker.sock"]
active = subprocess.check_output(docker + ["ps", "--format", '{{.Label "com.docker.compose.project"}}']).decode().splitlines()
if set(active) != {namespace}:
    raise ValueError("target is not the only running project")
directory = Path("/var/lib/khitmee-lab-arms") / namespace
state = json.loads((directory / "state.json").read_bytes())
if (state["fixture_sha256"] != fixture
        or state["namespace"] != namespace or not state["restored_from_immutable_fixture"]):
    raise ValueError("fixture restore does not match expected snapshot")
services = {}
for service, vol in (("db", "db"), ("backend", "sites"), ("frontend", "sites")):
    info, = json.loads(subprocess.check_output(docker + ["inspect", namespace + "-" + service + "-1"]))
    if not any(m.get("Name") == namespace + "-" + vol for m in info["Mounts"]):
        raise ValueError("unexpected volume for " + service)
    services[service] = {"container_id": info["Id"], "image_id": info["Image"], "restarts": info["RestartCount"],
                         "oom": info["State"]["OOMKilled"]}
backend = services["backend"]["container_id"]
bench = "/home/frappe/frappe-bench"
exec_cmd = docker + ["exec", "-i", "-w", bench + "/sites", backend]
manifest = subprocess.check_output(exec_cmd + ["cat", "bench-loadtest.localhost/private/files/seed_manifest.json"])
env = dict(line.split("=", 1) for line in (directory / "benchmark.env").read_text().splitlines() if "=" in line)
if subprocess.check_output(docker + ["port", namespace + "-frontend-1", "8080/tcp"]).strip() != b"127.0.0.1:18086":
    raise ValueError("unexpected arm listener")
if mode == "setup":
    print(json.dumps({"manifest": json.loads(manifest), "password": env["ADMIN_PASSWORD"],
                      "manifest_sha256": hashlib.sha256(manifest).hexdigest(), "services": services,
                      "state": {"namespace": namespace, "fixture_sha256": state["fixture_sha256"]}}))
else:
    raw = subprocess.check_output(exec_cmd + [bench + "/env/bin/python", "-B", "-", run], input=AUDIT.encode())
    print(json.dumps({"db": json.loads(raw), "services": services}))
