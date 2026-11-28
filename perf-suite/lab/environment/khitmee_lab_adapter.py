#!/usr/bin/env python3
"""Protected Khitmee lab arm adapter (stdlib only).

The supervisor invokes this executable with no arguments.  It reads one JSON arm
request on stdin and writes exactly one JSON object on stdout.  A zero exit means
every gate and measurement is complete; a nonzero exit is a refusal and never
contains substitute or synthetic measurements.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

ADAPTER_SCHEMA = "khitmee.lab-arm-evidence/v1"
HARNESS_REQUEST_SCHEMA = "khitmee.lab-harness-request/v1"
HARNESS_EVIDENCE_SCHEMA = "khitmee.lab-harness-evidence/v1"
PREFLIGHT_SCHEMA = "khitmee.lab-preflight/v1"
FIXTURE_SCHEMA = "khitmee.lab-fixture/v1"

SIGNALS = ("cpu_core_seconds", "peak_memory_bytes", "sql_seconds", "io_bytes", "lock_wait_ms")
LATENCIES = ("search_p95_ms", "sale_p95_ms", "transfer_p95_ms", "receipt_p95_ms",
             "purchase_invoice_p95_ms", "large_sale_p95_ms", "summary_p95_ms", "detail_p95_ms")
METRIC_FIELDS = SIGNALS + LATENCIES
GATES = ("isolated", "restored", "external_load_generator", "finance_parity",
         "mutation_controls", "workload_complete", "telemetry_complete")
PROBED_GATES = ("finance_parity", "mutation_controls", "workload_complete", "telemetry_complete")
COUNTER_FIELDS = ("oom_kills", "restarts", "dropped", "duplicate_postings", "lost_vouchers")

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
WORKLOAD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
NAMESPACE_RE = re.compile(r"khitmee-arm-[0-9a-f]{16}\Z")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
IMAGE_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,127}\Z")
IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

DEFAULT_SSH_CONFIG = "/var/lib/khitmee-experiment-guest/identity/ssh_config"
DEFAULT_SSH_HOST = "khitmee-lab"
DEFAULT_FIXTURE_DIR = "/var/lib/khitmee-experiment-guest/fixtures"
DEFAULT_GUEST_STATE_ROOT = "/var/lib/khitmee-lab-arms"
DEFAULT_IMAGE_PINS = {
    "mariadb:11.8": "sha256:e1d5495f445654134f226f55a41ce0da227116f6f27f283101f405c794d841fc",
    "redis:8.6-alpine": "sha256:91c71f9ea78fa3fe268ff832a985c48e592244f707993773f8b3e1b244ac1137",
    "frappe/erpnext:v16.60.0": "sha256:3847babae025dae001e638b7fc5503c944fd968216b3074e60edfcc240fb2db6",
}
MINIMAL_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|private[_-]?key|api[_-]?key|authorization|cookie)", re.I)
SENSITIVE_VALUE_MARKERS = (
    "BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "BEGIN PRIVATE KEY",
    "ADMIN_PASSWORD=", "DB_PASSWORD=", "MYSQL_ROOT_PASSWORD=",
)


class Refusal(Exception):
    """An expected safety, identity or evidence rejection."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Refusal(message)


def packed(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def sha256_json(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def sanitize_text(text: str, limit: int = 500) -> str:
    text = text.replace("\x00", " ")[:limit]
    for marker in SENSITIVE_VALUE_MARKERS:
        text = text.replace(marker, "[redacted-marker]")
    return text


def contains_sensitive(value: Any, path: str = "") -> str | None:
    """Return the first sensitive-looking key/value path in a JSON-compatible object."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if SENSITIVE_KEY_RE.search(key_text):
                return child_path
            found = contains_sensitive(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = contains_sensitive(child, f"{path}[{index}]")
            if found:
                return found
    elif isinstance(value, str):
        for marker in SENSITIVE_VALUE_MARKERS:
            if marker in value:
                return path or "<value>"
    return None


def valid_relative_artifact(name: str) -> PurePosixPath:
    require(isinstance(name, str) and name and not name.startswith("/"), "artifact path must be relative")
    require("\\" not in name, "artifact path must use POSIX separators")
    parts = PurePosixPath(name).parts
    require(parts and all(part not in ("", ".", "..") for part in parts), "unsafe artifact path")
    return PurePosixPath(*parts)


def artifact_file(artifacts_dir: Path, name: str) -> Path:
    relative = valid_relative_artifact(name)
    base = artifacts_dir.resolve(strict=True)
    path = base.joinpath(*relative.parts)
    require(not path.is_symlink(), "artifact may not be a symlink")
    resolved = path.resolve(strict=True)
    require(resolved.is_relative_to(base), "artifact escapes artifacts_dir")
    require(resolved.is_file(), "artifact is not a regular file")
    return resolved


def verify_artifact_reference(obj: dict, artifacts_dir: Path, kind: str) -> dict:
    require(isinstance(obj, dict), kind + " must be an object")
    artifact = obj.get("artifact")
    checksum = obj.get("sha256")
    require(isinstance(checksum, str) and SHA256_RE.fullmatch(checksum) is not None,
            kind + " has invalid sha256")
    path = artifact_file(artifacts_dir, artifact)
    require(file_hash(path) == checksum, kind + " artifact digest mismatch")
    return {"artifact": PurePosixPath(artifact).as_posix(), "sha256": checksum}


def validate_request(raw: Any) -> dict:
    require(isinstance(raw, dict), "request must be a JSON object")
    expected = {"experiment", "baseline_commit", "commit", "arm", "patch_sha256",
                "seed", "trial", "workload", "fixture_sha256", "artifacts_dir"}
    require(set(raw) == expected, "request fields do not match the pinned adapter contract")
    require(type(raw["experiment"]) is int and raw["experiment"] > 0, "invalid experiment id")
    for field in ("baseline_commit", "commit"):
        require(isinstance(raw[field], str) and COMMIT_RE.fullmatch(raw[field]) is not None,
                "invalid commit identity: " + field)
    for field in ("patch_sha256", "fixture_sha256"):
        require(isinstance(raw[field], str) and SHA256_RE.fullmatch(raw[field]) is not None,
                "invalid digest: " + field)
    require(raw["arm"] in ("baseline", "candidate"), "arm must be baseline or candidate")
    require(type(raw["seed"]) is int and raw["seed"] > 0, "seed must be a positive integer")
    require(type(raw["trial"]) is int and raw["trial"] >= 0, "trial must be a nonnegative integer")
    require(isinstance(raw["workload"], str) and WORKLOAD_RE.fullmatch(raw["workload"]) is not None,
            "invalid workload identifier")
    require(isinstance(raw["artifacts_dir"], str), "artifacts_dir must be a string")
    require(PurePosixPath(raw["artifacts_dir"]).is_absolute(), "artifacts_dir must be absolute")
    return dict(raw)


def arm_namespace(request: dict) -> str:
    identity = {key: request[key] for key in
                ("experiment", "baseline_commit", "commit", "arm", "patch_sha256",
                 "seed", "trial", "workload", "fixture_sha256")}
    return "khitmee-arm-" + digest(packed(identity))[:16]


def default_config() -> dict:
    return {
        "schema_version": 1,
        "target": {
            "kind": "ssh-qemu-guest",
            "ssh_config": DEFAULT_SSH_CONFIG,
            "ssh_host": DEFAULT_SSH_HOST,
            "expected_hostname": "khitmee-lab",
            "expected_virtualization": "kvm",
            "vcpus": 2,
            "max_memory_bytes": 2 * 1024**3,
            "require_zero_swap": True,
        },
        "fixture_dir": DEFAULT_FIXTURE_DIR,
        "guest_state_root": DEFAULT_GUEST_STATE_ROOT,
        "images": dict(DEFAULT_IMAGE_PINS),
        "fixtures": {},
        "harness": None,
    }


def merge_config(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def validate_config(config: dict) -> dict:
    require(config.get("schema_version") == 1, "unsupported environment config schema")
    target = config.get("target")
    require(isinstance(target, dict), "target config missing")
    kind = target.get("kind")
    if kind in ("shared-wsl", "shared-docker", "host-docker", "wsl"):
        raise Refusal("refusing shared WSL/Docker target; measurements require the dedicated QEMU guest")
    require(kind == "ssh-qemu-guest", "unsupported target kind")
    for field in ("ssh_config", "ssh_host", "expected_hostname", "expected_virtualization"):
        require(isinstance(target.get(field), str) and target[field], "target missing " + field)
    require(Path(target["ssh_config"]).is_absolute() or PurePosixPath(target["ssh_config"]).is_absolute(),
            "ssh_config must be absolute")
    require(HOST_RE.fullmatch(target["ssh_host"]) is not None, "invalid SSH host alias")
    require(type(target.get("vcpus")) is int and target["vcpus"] == 2, "target vcpus must be exactly 2")
    require(type(target.get("max_memory_bytes")) is int and
            0 < target["max_memory_bytes"] <= 2 * 1024**3, "target memory envelope must be <= 2 GiB")
    require(target.get("require_zero_swap") is True, "zero-swap target check is mandatory")
    require(isinstance(config.get("fixture_dir"), str) and
            (Path(config["fixture_dir"]).is_absolute() or PurePosixPath(config["fixture_dir"]).is_absolute()),
            "fixture_dir must be absolute")
    require(config.get("guest_state_root") == DEFAULT_GUEST_STATE_ROOT,
            "guest_state_root must remain the operator-pinned lab path")
    images = config.get("images")
    require(isinstance(images, dict) and images, "pinned image map missing")
    for tag, image_digest in images.items():
        require(IMAGE_TAG_RE.fullmatch(tag) is not None, "invalid image tag in pins")
        require(isinstance(image_digest, str) and IMAGE_DIGEST_RE.fullmatch(image_digest) is not None,
                "invalid image digest in pins")
    fixtures = config.get("fixtures", {})
    require(isinstance(fixtures, dict), "fixtures must be an object keyed by archive SHA256")
    for fixture_sha, metadata in fixtures.items():
        require(isinstance(fixture_sha, str) and SHA256_RE.fullmatch(fixture_sha) is not None,
                "invalid fixture key in config")
        require(isinstance(metadata, dict), "fixture metadata must be an object")
        workloads = metadata.get("workloads")
        require(isinstance(workloads, list) and workloads and
                all(isinstance(item, str) and WORKLOAD_RE.fullmatch(item) for item in workloads),
                "fixture metadata workloads missing")
        sensitive = contains_sensitive(metadata)
        require(sensitive is None, "fixture metadata may not contain sensitive material")
        source_sha = metadata.get("source_tar_sha256")
        require(isinstance(source_sha, str) and SHA256_RE.fullmatch(source_sha) is not None,
                "fixture metadata must record source_tar_sha256")
        recorded_fixture_sha = metadata.get("fixture_tar_gz_sha256")
        if recorded_fixture_sha is not None:
            require(recorded_fixture_sha == fixture_sha,
                    "fixture_tar_gz_sha256 metadata does not match its config key")
        git_commit = metadata.get("git_commit")
        if git_commit is not None:
            require(isinstance(git_commit, str) and COMMIT_RE.fullmatch(git_commit) is not None,
                    "invalid fixture git_commit")
        for digest_field in ("site_seed_manifest_sha256", "seed_manifest_sha256",
                             "restore_receipt_sha256", "verify_log_sha256"):
            value = metadata.get(digest_field)
            if value is not None:
                require(isinstance(value, str) and SHA256_RE.fullmatch(value) is not None,
                        "invalid fixture metadata digest: " + digest_field)
        image_ids = metadata.get("image_ids")
        if image_ids is not None:
            require(isinstance(image_ids, dict), "fixture image_ids must be an object")
            for tag, image_digest in image_ids.items():
                require(IMAGE_TAG_RE.fullmatch(tag) is not None and
                        isinstance(image_digest, str) and IMAGE_DIGEST_RE.fullmatch(image_digest) is not None,
                        "invalid fixture image pin")
    harness = config.get("harness")
    if harness is not None:
        require(isinstance(harness, dict), "harness must be an object or null")
        executable = harness.get("executable")
        checksum = harness.get("sha256")
        require(isinstance(executable, str) and
                (Path(executable).is_absolute() or PurePosixPath(executable).is_absolute()),
                "harness executable must be absolute")
        require(isinstance(checksum, str) and SHA256_RE.fullmatch(checksum) is not None,
                "harness executable sha256 missing or invalid")
        timeout = harness.get("timeout_seconds", 5 * 3600)
        require(type(timeout) is int and 60 <= timeout <= 19800,
                "harness timeout must be 60..19800 seconds")
        harness["timeout_seconds"] = timeout
    return config


def load_config(path: Path | None = None) -> dict:
    chosen = path
    if chosen is None and os.environ.get("KHITMEE_LAB_CONFIG"):
        chosen = Path(os.environ["KHITMEE_LAB_CONFIG"])
    default_path = Path(__file__).resolve().with_name("lab-environment.json")
    if chosen is None and default_path.exists():
        chosen = default_path
    config = default_config()
    if chosen is not None:
        require(not chosen.is_symlink(), "environment config may not be a symlink")
        override = json.loads(chosen.read_text(encoding="utf-8"))
        require(isinstance(override, dict), "environment config must be a JSON object")
        config = merge_config(config, override)
        config["_config_path"] = str(chosen)
    return validate_config(config)


TARGET_PROBE_SCRIPT = r'''
import json
import os
from pathlib import Path
import socket
import subprocess

mem = {}
for line in Path("/proc/meminfo").read_text().splitlines():
    key, rest = line.split(":", 1)
    if key in ("MemTotal", "SwapTotal"):
        mem[key] = int(rest.strip().split()[0]) * 1024

def output(*args):
    return subprocess.check_output(args, text=True).strip()

print(json.dumps({
    "hostname": socket.gethostname(),
    "virtualization": output("systemd-detect-virt"),
    "cpus": len(os.sched_getaffinity(0)),
    "memtotal_bytes": mem.get("MemTotal", 0),
    "swaptotal_bytes": mem.get("SwapTotal", 0),
    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    "machine_id_sha256": __import__("hashlib").sha256(Path("/etc/machine-id").read_bytes()).hexdigest(),
    "root_source": output("findmnt", "-no", "SOURCE", "/"),
}))
'''.strip()

IMAGE_PROBE_SCRIPT = r'''
import json
import subprocess

def output(*args):
    return subprocess.check_output(args, text=True).strip()

try:
    tag_format = "{{.Repository}}:{{.Tag}}"
    tags = [line for line in output("sudo", "docker", "image", "ls", "--format", tag_format).splitlines()
            if line and "<none>" not in line]
    images = []
    if tags:
        inspected = json.loads(output("sudo", "docker", "image", "inspect", *tags))
        for image in inspected:
            images.append({"id": image.get("Id"), "repo_tags": image.get("RepoTags") or [],
                           "repo_digests": image.get("RepoDigests") or []})
    def projects_for(ids):
        projects = set()
        if ids:
            for item in json.loads(output("sudo", "docker", "inspect", *ids)):
                labels = ((item.get("Config") or {}).get("Labels") or {})
                project = labels.get("com.docker.compose.project")
                if project:
                    projects.add(project)
        return sorted(projects)
    all_ids = [line for line in output("sudo", "docker", "ps", "-aq").splitlines() if line]
    active_ids = [line for line in output("sudo", "docker", "ps", "-q").splitlines() if line]
    print(json.dumps({"images": images, "compose_projects": projects_for(all_ids),
                      "active_compose_projects": projects_for(active_ids)}))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
    raise SystemExit(2)
'''.strip()


def local_host_is_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False
    return "microsoft" in release or "wsl" in release


def run_local(argv: list[str], *, input_bytes: bytes | None = None, timeout: int,
              cwd: Path | None = None, stderr_file=None) -> subprocess.CompletedProcess:
    env = dict(MINIMAL_ENV)
    return subprocess.run(argv, input=input_bytes, stdout=subprocess.PIPE, stderr=stderr_file or subprocess.PIPE,
                          timeout=timeout, cwd=cwd, env=env, check=False)


def ssh_command(config: dict, remote_argv: list[str], input_bytes: bytes, timeout: int = 30) -> bytes:
    target = config["target"]
    require(HOST_RE.fullmatch(target["ssh_host"]) is not None, "invalid SSH host alias")
    remote = shlex.join(remote_argv)
    completed = run_local(
        ["ssh", "-T", "-F", target["ssh_config"], target["ssh_host"], remote],
        input_bytes=input_bytes, timeout=timeout)
    if completed.returncode != 0:
        raise Refusal("SSH probe failed: " + sanitize_text(completed.stderr.decode(errors="replace"), 240))
    return completed.stdout


def parse_one_json(data: bytes, what: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(what + " was not one JSON object") from exc
    require(isinstance(value, dict), what + " must be a JSON object")
    return value


def evaluate_preflight(target_probe: dict, image_probe: dict, config: dict) -> dict:
    target = config["target"]
    errors = []
    def check(condition, message):
        if not condition:
            errors.append(message)

    check(target_probe.get("hostname") == target["expected_hostname"], "guest hostname mismatch")
    check(target_probe.get("virtualization") == target["expected_virtualization"], "guest virtualization mismatch")
    check(target_probe.get("cpus") == target["vcpus"], "guest vCPU count mismatch")
    memory = target_probe.get("memtotal_bytes")
    check(type(memory) is int and 0 < memory <= target["max_memory_bytes"], "guest memory envelope mismatch")
    check(target_probe.get("swaptotal_bytes") == 0, "guest swap is enabled")
    check(isinstance(target_probe.get("boot_id"), str) and len(target_probe["boot_id"]) >= 32,
          "guest boot identity missing")
    check(isinstance(target_probe.get("machine_id_sha256"), str) and
          SHA256_RE.fullmatch(target_probe["machine_id_sha256"]) is not None, "guest machine identity missing")

    images = {}
    if image_probe.get("error"):
        errors.append("guest image probe failed")
    for image in image_probe.get("images", []):
        for tag in image.get("repo_tags", []):
            images[tag] = {"id": image.get("id"), "repo_digests": image.get("repo_digests", [])}
    missing = []
    mismatched = []
    for tag, pinned in config["images"].items():
        entry = images.get(tag)
        if entry is None:
            missing.append(tag)
            continue
        digests = set(entry.get("repo_digests", []))
        ids = {entry.get("id")}
        digest_name = tag.split(":", 1)[0] + "@" + pinned
        if pinned not in digests and pinned not in ids and digest_name not in digests:
            mismatched.append(tag)
    if missing:
        errors.append("missing pinned guest images: " + ",".join(sorted(missing)))
    if mismatched:
        errors.append("guest image digest mismatch: " + ",".join(sorted(mismatched)))
    projects = set(image_probe.get("compose_projects", []))
    check("khitmee-erpnext" not in projects, "live khitmee-erpnext project is present in the guest")

    return {
        "schema_version": PREFLIGHT_SCHEMA,
        "target_ok": not errors,
        "errors": errors,
        "driver": {"linux": sys.platform.startswith("linux"), "is_wsl": local_host_is_wsl(),
                   "uses_external_vm": True},
        "target": {
            "hostname": target_probe.get("hostname"),
            "virtualization": target_probe.get("virtualization"),
            "vcpus": target_probe.get("cpus"),
            "memory_bytes": memory,
            "swap_bytes": target_probe.get("swaptotal_bytes"),
            "boot_id": target_probe.get("boot_id"),
            "machine_id_sha256": target_probe.get("machine_id_sha256"),
            "root_source": target_probe.get("root_source"),
        },
        "images": {tag: images.get(tag) for tag in sorted(config["images"])},
        "compose_projects": sorted(projects),
        "active_compose_projects": sorted(image_probe.get("active_compose_projects", [])),
        "ssh": {"config": config["target"]["ssh_config"], "host": config["target"]["ssh_host"]},
    }


def perform_preflight(config: dict) -> dict:
    require(sys.platform.startswith("linux"), "adapter must run on the Linux external driver")
    ssh_config = Path(config["target"]["ssh_config"])
    require(ssh_config.exists() and ssh_config.is_file() and not ssh_config.is_symlink(),
            "operator SSH config is missing")
    if os.name == "posix":
        require(stat.S_IMODE(ssh_config.stat().st_mode) & 0o077 == 0,
                "operator SSH config must be operator-only")
    identity = None
    for line in ssh_config.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "identityfile":
            identity = Path(parts[1])
            break
    require(identity is not None and identity.is_absolute() and identity.exists() and
            identity.is_file() and not identity.is_symlink(), "operator SSH identity missing")
    if os.name == "posix":
        require(stat.S_IMODE(identity.stat().st_mode) & 0o077 == 0,
                "operator SSH identity must be operator-only")
    target_data = parse_one_json(ssh_command(config, ["python3", "-"], TARGET_PROBE_SCRIPT.encode()), "target probe")
    image_data = parse_one_json(ssh_command(config, ["sudo", "python3", "-"], IMAGE_PROBE_SCRIPT.encode()), "image probe")
    return evaluate_preflight(target_data, image_data, config)


def mutable_path_reason(path: Path, what: str) -> str | None:
    if not path.exists() or not path.is_file() or path.is_symlink():
        return what + " missing or not a regular file"
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o022:
            return what + " is group/world writable"
        text = str(path)
        if text.startswith(("/mnt/", "/media/")):
            return what + " is on a mutable host mount"
    return None


def install_trust_report(config: dict) -> dict:
    paths = {"adapter": Path(__file__).resolve(),
             "guest_restore": Path(__file__).resolve().with_name("guest_restore.py")}
    if config.get("_config_path"):
        paths["config"] = Path(config["_config_path"])
    problems = {}
    for what, path in paths.items():
        reason = mutable_path_reason(path, what)
        if reason:
            problems[what] = reason
    return {"trusted_install": not problems, "problems": problems,
            "paths": {name: str(path) for name, path in paths.items()}}


def require_trusted_install(config: dict) -> None:
    report = install_trust_report(config)
    require(report["trusted_install"],
            "measurement requires a read-only installed harness outside mutable mounts: " +
            "; ".join(report["problems"].values()))


def validate_artifacts_dir(value: str) -> Path:
    path = Path(value)
    require(path.is_absolute(), "artifacts_dir must be absolute")
    require(path.exists() and path.is_dir(), "artifacts_dir must exist")
    require(not path.is_symlink(), "artifacts_dir may not be a symlink")
    resolved = path.resolve(strict=True)
    require(resolved != Path(resolved.anchor), "artifacts_dir may not be a filesystem root")
    return resolved


def verify_harness(config: dict) -> dict:
    harness = config.get("harness")
    require(harness is not None, "trusted seasonal/finance harness is not configured")
    executable = Path(harness["executable"])
    require(executable.exists() and executable.is_file() and not executable.is_symlink(),
            "trusted harness executable missing")
    require(os.access(executable, os.X_OK), "trusted harness is not executable")
    reason = mutable_path_reason(executable, "trusted harness")
    require(reason is None, reason or "trusted harness is mutable")
    actual = file_hash(executable)
    require(actual == harness["sha256"], "trusted harness executable digest mismatch")
    return {"executable": str(executable), "sha256": actual,
            "timeout_seconds": harness["timeout_seconds"]}


def safe_tar_name(name: str) -> list[str]:
    require(isinstance(name, str) and name and not name.startswith("/"), "unsafe absolute fixture member")
    require("\\" not in name, "fixture member contains a backslash")
    name = name.rstrip("/")
    parts = name.split("/")
    require(all(part not in ("", ".", "..") for part in parts), "unsafe fixture member path")
    return parts


def inspect_fixture_archive(path: Path) -> dict:
    """Validate the operator cold-volume tar without reading secret file contents."""
    volume_roots = {
        "perf-db-data/_data": "db",
        "perf-sites/_data": "sites",
        "perf-redis-queue-data/_data": "redis-queue",
    }
    structural_dirs = {"perf-db-data", "perf-sites", "perf-redis-queue-data"}
    allowed_symlink = ("perf-sites/_data/assets", "/home/frappe/frappe-bench/assets")
    env_seen = False
    root_seen = {logical: False for logical in volume_roots.values()}
    volume_counts = {logical: 0 for logical in volume_roots.values()}
    symlinks = {}
    entries = []
    seen = set()
    try:
        with tarfile.open(path, mode="r|gz") as archive:
            for member in archive:
                parts = safe_tar_name(member.name)
                normalized = "/".join(parts)
                require(normalized not in seen, "duplicate fixture member")
                seen.add(normalized)
                if member.issym():
                    require((normalized, member.linkname) == allowed_symlink,
                            "unsupported fixture symlink")
                    kind = "symlink"
                    symlinks[normalized] = member.linkname
                elif member.isfile():
                    kind = "file"
                elif member.isdir():
                    kind = "directory"
                else:
                    raise Refusal("fixture may contain only regular files, directories and the pinned asset symlink")

                if normalized == ".env":
                    require(member.isfile() and 0 < member.size <= 16384, "invalid guest .env member")
                    env_seen = True
                    continue
                if normalized in structural_dirs:
                    require(member.isdir(), "volume parent must be a directory")
                    continue

                matched = False
                for prefix, logical in volume_roots.items():
                    if normalized == prefix:
                        require(member.isdir(), "volume data root must be a directory")
                        root_seen[logical] = True
                        matched = True
                        break
                    if normalized.startswith(prefix + "/"):
                        matched = True
                        entries.append((normalized, logical, kind))
                        if member.isfile():
                            volume_counts[logical] += 1
                        break
                require(matched, "unexpected top-level fixture member: " + parts[0])
    except tarfile.TarError as exc:
        raise Refusal("fixture is not a readable tar.gz archive") from exc

    for symlink in symlinks:
        for normalized, _logical, _kind in entries:
            require(not normalized.startswith(symlink + "/"),
                    "fixture writes through the pinned asset symlink")
    require(env_seen, "fixture .env missing")
    require(all(root_seen.values()), "fixture volume data root missing")
    require(all(count > 0 for count in volume_counts.values()),
            "fixture must include db, sites and redis-queue volume contents")
    return {"archive_format": "cold-named-volume-tar/v1", "volume_file_counts": volume_counts,
            "symlinks": dict(symlinks)}


def validate_fixture(request: dict, config: dict) -> dict:
    checksum = request["fixture_sha256"]
    fixture_dir = Path(config["fixture_dir"])
    path = fixture_dir / f"{checksum}.tar.gz"
    require(path.exists() and path.is_file() and not path.is_symlink(), "pinned fixture archive missing")
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        require(mode & 0o077 == 0, "fixture archive must be operator-only")
    require(file_hash(path) == checksum, "fixture archive digest mismatch")
    metadata = config.get("fixtures", {}).get(checksum)
    require(isinstance(metadata, dict), "fixture archive is not operator-pinned in the environment config")
    recorded_size = metadata.get("fixture_tar_gz_bytes")
    if recorded_size is not None:
        require(type(recorded_size) is int and recorded_size == path.stat().st_size,
                "fixture archive byte size does not match operator metadata")
    workloads = metadata["workloads"]
    require(request["workload"] in workloads,
            f"fixture does not support workload {request['workload']!r}; refusing to use a smoke artifact for a larger target")
    image_ids = metadata.get("image_ids")
    if image_ids is not None:
        for tag, pinned in config["images"].items():
            require(image_ids.get(tag) == pinned, "fixture image pin mismatch: " + tag)
    inspected = inspect_fixture_archive(path)
    return {"path": str(path), "sha256": checksum, "manifest": metadata, **inspected}

def transfer_to_guest(config: dict, local_path: Path, guest_path: str, timeout: int = 3600) -> None:
    require(shutil.which("scp") is not None, "scp is required for guest transfer")
    target = config["target"]
    destination = f"{target['ssh_host']}:{guest_path}"
    completed = run_local(["scp", "-q", "-F", target["ssh_config"], str(local_path), destination], timeout=timeout)
    if completed.returncode != 0:
        raise Refusal("guest transfer failed: " + sanitize_text(completed.stderr.decode(errors="replace"), 240))


def guest_restore_worker() -> dict:
    path = Path(__file__).resolve().with_name("guest_restore.py")
    require(path.exists() and path.is_file() and not path.is_symlink(), "guest restore worker missing")
    return {"path": str(path), "sha256": file_hash(path)}


def remote_restore(config: dict, fixture: dict, namespace: str, mode: str) -> dict:
    guest_fixture = f"/tmp/{namespace}-fixture.tar.gz"
    guest_worker = f"/tmp/{namespace}-restore.py"
    worker = guest_restore_worker()
    try:
        transfer_to_guest(config, Path(fixture["path"]), guest_fixture)
        transfer_to_guest(config, Path(worker["path"]), guest_worker, timeout=120)
        remote_argv = ["sudo", "python3", guest_worker, namespace, guest_fixture,
                       config["guest_state_root"], fixture["sha256"], mode]
        output = ssh_command(config, remote_argv, b"", timeout=3600)
    except Exception:
        try:
            ssh_command(config, ["rm", "-f", "--", guest_fixture, guest_worker], b"", timeout=15)
        except Exception:
            pass
        raise
    state = parse_one_json(output, "guest restore result")
    require(state.get("schema_version") == "khitmee.lab-provision/v1", "invalid provision schema")
    require(state.get("namespace") == namespace, "provisioned namespace mismatch")
    require(state.get("fixture_sha256") == fixture["sha256"], "provisioned fixture mismatch")
    require(state.get("archive_format") == "cold-named-volume-tar/v1", "provisioned fixture format mismatch")
    require(state.get("restored_from_immutable_fixture") is True, "immutable restore was not verified")
    volumes = state.get("volumes")
    require(isinstance(volumes, dict) and set(volumes) == {"db", "sites", "redis-queue"},
            "restored volume set mismatch")
    for logical, name in volumes.items():
        require(isinstance(name, str) and name == f"{namespace}-{logical}", "restored volume name mismatch")
    state["restore_worker_sha256"] = worker["sha256"]
    return state

def write_json_artifact(path: Path, value: dict) -> dict:
    data = json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    path.write_bytes(data)
    return {"artifact": path.name, "sha256": digest(data)}


REPORT_SCREEN = "smoke-gross-profit-shop1-last-seed-day-by-item-v1"
REPORT_SCREEN_INVOCATIONS = 20
REPORT_SCREEN_ORACLE_SHA256 = "88bac0a4969de3fba8ae56b512237e13151460a450716a225f98194545aa3202"
REPORT_SCREEN_ORACLE_SOURCE_SHA256 = "fff7460f8e9609ab89a27f96c26af903c18ec6564b60e08702ad672d0bc11cce"
REPORT_SCREEN_EXACT_RULE = "Exact decimal equality of emitted ERP values, including any precision beyond report display."
REPORT_SCREEN_ORACLE_FIELDS = ("columns", "rows", "filters", "site", "seed_manifest_sha256", "currency", "precision")
FINANCIAL_MUTATIONS = ("precision_unit", "swapped_warehouses", "retry_duplicate_posting")
CASHIER_SCREEN = "cashier-sales-minute-v1"
CASHIER_LATENCIES = ("sale_p95_ms", "sale_10_p95_ms", "sale_50_p95_ms", "sale_200_p95_ms")
CASHIER_INVOCATIONS = 80
CASHIER_MUTATIONS = ("posted price changed by one unit", "stock deducted from different warehouse",
                     "unbalanced debit", "duplicate confirmation")
SEASONAL_WORKLOAD = "seasonal-v1"
SEASONAL_VOUCHER_KINDS = ("sale", "transfer", "purchase_receipt", "purchase_invoice")


def validate_numeric_metrics(metrics: Any, workload: str = "seasonal-v1") -> dict:
    require(isinstance(metrics, dict), "metrics must be an object")
    result = {}
    fields = METRIC_FIELDS
    if workload == REPORT_SCREEN:
        fields = SIGNALS + ("summary_p95_ms",)
    elif workload == CASHIER_SCREEN:
        fields = SIGNALS + CASHIER_LATENCIES
    for field in fields:
        value = metrics.get(field)
        require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
                "invalid metric: " + field)
        result[field] = value
    return result


def validate_counters(counters: Any) -> dict:
    require(isinstance(counters, dict), "telemetry counters missing")
    result = {}
    for field in COUNTER_FIELDS:
        value = counters.get(field)
        require(type(value) is int and value == 0, "failed counter gate: " + field)
        result[field] = value
    return result


def read_artifact_json(reference: dict, artifacts_dir: Path, kind: str) -> Any:
    path = artifact_file(artifacts_dir, reference["artifact"])
    require(path.stat().st_size <= 64 * 1024**2, kind + " artifact is too large to validate")
    try:
        return json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(kind + " artifact is not valid JSON") from exc


def validate_exact_mutation_artifact(document: Any, workload: str) -> None:
    require(isinstance(document, dict), workload + " mutation artifact must be an object")
    if workload == REPORT_SCREEN:
        require(document.get("corrupted_report_caught") is True,
                "report screen comparator mutation was not caught")
        verifier = document.get("verifier")
    else:
        verifier = document
    require(isinstance(verifier, dict) and verifier.get("ok") is True,
            workload + " mutation verifier did not pass")
    controls = verifier.get("controls")
    require(isinstance(controls, list), workload + " mutation controls artifact missing")
    by_name = {}
    for control in controls:
        require(isinstance(control, dict) and isinstance(control.get("name"), str),
                workload + " mutation control shape invalid")
        by_name[control["name"]] = control
    require(set(by_name) == set(FINANCIAL_MUTATIONS),
            workload + " mutation controls differ from the pinned contract")
    for name in FINANCIAL_MUTATIONS:
        require(by_name[name].get("caught") is True,
                workload + " mutation control was not caught: " + name)


def validate_report_screen_scope(finance_ref: dict, mutation_ref: dict, telemetry_ref: dict,
                                 artifacts_dir: Path) -> None:
    comparisons = read_artifact_json(finance_ref, artifacts_dir, "report finance evidence")
    require(isinstance(comparisons, list) and len(comparisons) == REPORT_SCREEN_INVOCATIONS,
            "report screen requires exactly 20 exact-oracle comparisons")
    for comparison in comparisons:
        require(isinstance(comparison, dict), "report comparison must be an object")
        require(comparison.get("matches") is True and comparison.get("differences") == [],
                "report screen differs from the exact stock oracle")
        require(comparison.get("rule") == REPORT_SCREEN_EXACT_RULE,
                "report screen comparison is not exact decimal equality")
        require(type(comparison.get("stock_rows")) is int and comparison["stock_rows"] > 0 and
                comparison.get("fast_rows") == comparison["stock_rows"],
                "report screen row-count evidence mismatch")
        numeric = comparison.get("numeric_cells_checked")
        require(isinstance(numeric, dict) and type(numeric.get("gross_profit")) is int and
                numeric["gross_profit"] > 0, "report screen did not compare gross-profit numeric cells")

    telemetry = read_artifact_json(telemetry_ref, artifacts_dir, "report telemetry evidence")
    raw_nested = telemetry.get("raw") if isinstance(telemetry, dict) else None
    require(isinstance(raw_nested, dict), "report telemetry does not reference raw oracle evidence")
    raw_ref = verify_artifact_reference(raw_nested, artifacts_dir, "report raw evidence")
    raw = read_artifact_json(raw_ref, artifacts_dir, "report raw evidence")
    require(isinstance(raw, dict) and isinstance(raw.get("stock"), dict),
            "report raw evidence missing stock oracle")
    stock = raw["stock"]
    require(all(field in stock for field in REPORT_SCREEN_ORACLE_FIELDS),
            "report stock oracle evidence incomplete")
    oracle = {field: stock[field] for field in REPORT_SCREEN_ORACLE_FIELDS}
    require(digest(packed(oracle)) == REPORT_SCREEN_ORACLE_SHA256,
            "report stock oracle digest differs from the pinned screen contract")
    require(stock.get("module_sha256") == REPORT_SCREEN_ORACLE_SOURCE_SHA256,
            "report stock oracle source digest differs from the pinned screen contract")
    require(isinstance(raw.get("fast"), list) and len(raw["fast"]) == REPORT_SCREEN_INVOCATIONS,
            "report raw evidence does not contain exactly 20 measured invocations")

    mutations = read_artifact_json(mutation_ref, artifacts_dir, "report mutation evidence")
    validate_exact_mutation_artifact(mutations, REPORT_SCREEN)


def validate_cashier_screen_scope(finance_ref: dict, mutation_ref: dict, telemetry_ref: dict,
                                  artifacts_dir: Path) -> None:
    finance = read_artifact_json(finance_ref, artifacts_dir, "cashier finance signature")
    require(isinstance(finance, dict), "cashier finance signature must be an object")
    sales = finance.get("sales")
    require(isinstance(sales, dict) and len(sales) == CASHIER_INVOCATIONS,
            "cashier finance signature does not cover exactly 80 committed sales")
    require(isinstance(finance.get("stock_by_warehouse_item"), list) and finance["stock_by_warehouse_item"],
            "cashier stock signature missing")
    require(isinstance(finance.get("gl_by_account_party"), list) and finance["gl_by_account_party"],
            "cashier GL signature missing")
    controls = read_artifact_json(mutation_ref, artifacts_dir, "cashier mutation evidence")
    require(isinstance(controls, list), "cashier mutation evidence must be a list")
    by_name = {control.get("name"): control for control in controls if isinstance(control, dict)}
    require(set(by_name) == set(CASHIER_MUTATIONS),
            "cashier mutation controls differ from the pinned contract")
    for name in CASHIER_MUTATIONS:
        require(by_name[name].get("expected_failure_detected") is True,
                "cashier mutation control was not caught: " + name)
    telemetry = read_artifact_json(telemetry_ref, artifacts_dir, "cashier telemetry evidence")
    window = telemetry.get("resource_window") if isinstance(telemetry, dict) else None
    require(isinstance(window, dict) and window.get("observer_status") == "complete" and
            type(window.get("site_digest_calls")) is int and window["site_digest_calls"] > 0,
            "cashier telemetry window incomplete")


def validate_seasonal_financial_scope(request: dict, finance_ref: dict, mutation_ref: dict,
                                      artifacts_dir: Path) -> None:
    finance = read_artifact_json(finance_ref, artifacts_dir, "seasonal finance verdict")
    require(isinstance(finance, dict) and finance.get("ok") is True,
            "seasonal committed finance verdict did not pass")
    require(finance.get("fixture_id") == request["fixture_sha256"],
            "seasonal committed finance fixture mismatch")
    require(finance.get("seed") == request["seed"], "seasonal committed finance seed mismatch")
    operations = finance.get("operations")
    require(isinstance(operations, list) and operations,
            "seasonal committed finance operations missing")
    kinds = set()
    proven_kinds = set()
    for operation in operations:
        require(isinstance(operation, dict), "seasonal finance operation must be an object")
        kind = operation.get("kind")
        require(kind in SEASONAL_VOUCHER_KINDS, "seasonal finance operation kind invalid")
        kinds.add(kind)
        require(isinstance(operation.get("client_voucher_id"), str) and operation["client_voucher_id"],
                "seasonal finance operation missing client voucher identity")
        require(operation.get("ok") is True and operation.get("differences") == [],
                "seasonal committed finance comparison failed")
        comparison = operation.get("comparison")
        require(isinstance(comparison, dict) and comparison.get("ok") is True,
                "seasonal operation lacks exact reference/candidate comparison")
        if operation.get("proves_committed_retry") is True:
            proven_kinds.add(kind)
    require(kinds == set(SEASONAL_VOUCHER_KINDS),
            "seasonal finance evidence does not cover every voucher kind")
    require(proven_kinds == set(SEASONAL_VOUCHER_KINDS),
            "seasonal committed retry proof missing for a voucher kind")
    mutations = read_artifact_json(mutation_ref, artifacts_dir, "seasonal mutation evidence")
    validate_exact_mutation_artifact(mutations, SEASONAL_WORKLOAD)


def validate_probes(evidence: dict, artifacts_dir: Path) -> list:
    probes = evidence.get("probes")
    require(isinstance(probes, list) and probes, "verified probe evidence missing")
    validated = []
    covered = set()
    for probe in probes:
        require(isinstance(probe, dict), "probe entry must be an object")
        gate = probe.get("gate")
        require(gate in GATES, "unknown probe gate")
        require(probe.get("read_only") is True, "probe is not declared read-only")
        require(probe.get("verified") is True, "probe is not verified")
        reference = verify_artifact_reference(probe, artifacts_dir, "probe evidence")
        validated.append({"gate": gate, "name": probe.get("name"), **reference})
        covered.add(gate)
    missing = [gate for gate in PROBED_GATES if gate not in covered]
    require(not missing, "missing verified probe evidence for: " + ",".join(missing))
    return validated


def validate_harness_evidence(evidence: dict, request: dict, artifacts_dir: Path) -> dict:
    require(evidence.get("schema_version") == HARNESS_EVIDENCE_SCHEMA,
            "unsupported trusted harness evidence schema")
    sensitive = contains_sensitive(evidence)
    require(sensitive is None, "harness evidence may contain sensitive material at " + str(sensitive))
    identity = evidence.get("identity")
    require(isinstance(identity, dict), "harness identity missing")
    for field in ("commit", "seed", "workload", "trial", "arm", "fixture_sha256"):
        require(identity.get(field) == request[field], "wrong harness identity: " + field)
    app = evidence.get("app")
    require(isinstance(app, dict), "app pin evidence missing")
    require(app.get("commit") == request["commit"], "app commit mismatch")
    require(app.get("patch_sha256") == request["patch_sha256"], "app patch digest mismatch")
    require(isinstance(app.get("source_sha256"), str) and SHA256_RE.fullmatch(app["source_sha256"]) is not None,
            "app source archive digest missing")

    gates = evidence.get("gates")
    require(isinstance(gates, dict), "harness gates missing")
    for gate in GATES:
        require(gates.get(gate) is True, "missing or failed evidence: " + gate)

    counters = validate_counters(evidence.get("counters"))
    metrics = validate_numeric_metrics(evidence.get("metrics"), request["workload"])

    seasonal = evidence.get("seasonal_result")
    require(isinstance(seasonal, dict), "seasonal result missing")
    seasonal_ref = verify_artifact_reference(seasonal, artifacts_dir, "seasonal result")
    require(seasonal.get("complete") is True, "seasonal workload incomplete")
    require(seasonal.get("workload") == request["workload"], "seasonal workload mismatch")
    for field in ("offered", "started", "completed"):
        require(type(seasonal.get(field)) is int and seasonal[field] > 0,
                "invalid seasonal count: " + field)
    require(seasonal["completed"] <= seasonal["started"] <= seasonal["offered"],
            "inconsistent seasonal counts")
    if request["workload"] == REPORT_SCREEN:
        require(all(seasonal[k] == REPORT_SCREEN_INVOCATIONS for k in ("offered", "started", "completed")),
                "report screen requires exactly 20 offered, started and completed invocations")
    if request["workload"] == CASHIER_SCREEN:
        require(all(seasonal[k] == CASHIER_INVOCATIONS for k in ("offered", "started", "completed")),
                "cashier minute requires exactly 80 offered, started and completed sales")
    require(type(seasonal.get("duration_seconds")) in (int, float) and
            math.isfinite(seasonal["duration_seconds"]) and seasonal["duration_seconds"] > 0,
            "invalid seasonal duration")

    finance = evidence.get("finance_evidence")
    require(isinstance(finance, dict), "finance evidence missing")
    finance_ref = verify_artifact_reference(finance, artifacts_dir, "finance evidence")
    require(finance.get("read_only") is True and finance.get("verified") is True,
            "finance evidence is not verified read-only probe evidence")
    checks = finance.get("checks")
    require(isinstance(checks, list) and checks, "finance checks missing")
    for check in checks:
        require(isinstance(check, dict) and check.get("passed") is True,
                "finance parity check failed or missing")

    telemetry = evidence.get("telemetry")
    require(isinstance(telemetry, dict), "telemetry evidence missing")
    telemetry_ref = verify_artifact_reference(telemetry, artifacts_dir, "telemetry evidence")
    require(telemetry.get("complete") is True, "telemetry incomplete")

    mutation = evidence.get("mutation_controls")
    require(isinstance(mutation, dict), "mutation control evidence missing")
    mutation_ref = verify_artifact_reference(mutation, artifacts_dir, "mutation control evidence")
    require(mutation.get("read_only") is True and mutation.get("verified") is True,
            "mutation controls are not verified read-only probe evidence")
    controls = mutation.get("controls")
    require(isinstance(controls, list) and controls, "mutation controls missing")
    for control in controls:
        require(isinstance(control, dict) and control.get("expected_failure_detected") is True,
                "mutation control did not fail as expected")

    probes = validate_probes(evidence, artifacts_dir)
    if request["workload"] == REPORT_SCREEN:
        validate_report_screen_scope(finance_ref, mutation_ref, telemetry_ref, artifacts_dir)
    elif request["workload"] == CASHIER_SCREEN:
        fifo_ref = verify_artifact_reference(finance.get("fifo_conservation"), artifacts_dir, "cashier FIFO conservation")
        fifo = read_artifact_json(fifo_ref, artifacts_dir, "cashier FIFO conservation")
        require(isinstance(fifo, dict) and fifo.get("passed") is True and fifo.get("currency") == "USD"
                and fifo.get("warehouse") == "Dist Hub 1 - KMD" and isinstance(fifo.get("bins"), dict)
                and set(fifo["bins"]) == {"KM-00101", "KM-00039"} and
                all(isinstance(b, dict) and b.get("passed") is True for b in fifo["bins"].values()),
                "cashier FIFO conservation did not pass for both profiled bins")
        validate_cashier_screen_scope(finance_ref, mutation_ref, telemetry_ref, artifacts_dir)
    elif request["workload"] == SEASONAL_WORKLOAD:
        validate_seasonal_financial_scope(request, finance_ref, mutation_ref, artifacts_dir)
    return {
        "gates": dict(gates), "counters": counters, "metrics": metrics,
        "app": {"commit": app["commit"], "patch_sha256": app["patch_sha256"],
                "source_sha256": app["source_sha256"]},
        "seasonal_result": {**seasonal, **seasonal_ref},
        "finance_evidence": {**finance, **finance_ref},
        "telemetry": {**telemetry, **telemetry_ref},
        "mutation_controls": {**mutation, **mutation_ref},
        "probes": probes,
    }


def validate_target_evidence(evidence: dict, preflight: dict) -> None:
    target = evidence.get("target")
    require(isinstance(target, dict), "target identity evidence missing")
    expected = preflight["target"]
    for field in ("hostname", "boot_id", "machine_id_sha256"):
        require(target.get(field) == expected[field], "target identity changed: " + field)
    require(target.get("virtualization") == expected["virtualization"], "target virtualization changed")
    require(target.get("vcpus") == expected["vcpus"], "target vCPU count changed")
    require(target.get("memory_bytes") == expected["memory_bytes"], "target memory identity changed")


def run_harness(config: dict, harness: dict, request: dict, artifacts_dir: Path,
                harness_input: dict) -> tuple[dict, bytes]:
    stderr_path = artifacts_dir / "harness.stderr"
    with stderr_path.open("wb") as stderr:
        completed = run_local([harness["executable"]], input_bytes=packed(harness_input),
                              timeout=harness["timeout_seconds"], cwd=artifacts_dir, stderr_file=stderr)
    stderr_bytes = stderr_path.read_bytes()
    if any(marker.encode() in stderr_bytes for marker in SENSITIVE_VALUE_MARKERS):
        stderr_path.write_bytes(b"[redacted: harness stderr contained a secret-bearing marker]\n")
    if completed.returncode != 0:
        raise Refusal("trusted harness failed; see harness.stderr")
    evidence = parse_one_json(completed.stdout, "trusted harness evidence")
    sensitive = contains_sensitive(evidence)
    require(sensitive is None, "trusted harness emitted sensitive material at " + str(sensitive))
    (artifacts_dir / "adapter-harness-stdout.json").write_bytes(completed.stdout)
    return evidence, completed.stdout


def build_final_evidence(request: dict, preflight: dict, provision: dict, validated: dict,
                         artifact_refs: dict) -> dict:
    target = preflight["target"]
    evidence = {
        "schema_version": ADAPTER_SCHEMA,
        "status": "complete",
        "experiment": request["experiment"],
        "baseline_commit": request["baseline_commit"],
        "commit": request["commit"],
        "patch_sha256": request["patch_sha256"],
        "seed": request["seed"],
        "trial": request["trial"],
        "arm": request["arm"],
        "workload": request["workload"],
        "fixture_sha256": request["fixture_sha256"],
        "identity": {key: request[key] for key in
                     ("experiment", "baseline_commit", "commit", "patch_sha256", "seed", "trial", "arm",
                      "workload", "fixture_sha256")},
        "isolated": True,
        "restored": provision["restored_from_immutable_fixture"] is True,
        "external_load_generator": validated["gates"]["external_load_generator"] is True,
        "finance_parity": validated["gates"]["finance_parity"] is True,
        "mutation_controls": validated["gates"]["mutation_controls"] is True,
        "workload_complete": validated["gates"]["workload_complete"] is True,
        "telemetry_complete": validated["gates"]["telemetry_complete"] is True,
        "vcpus": target["vcpus"],
        "host_memory_bytes": target["memory_bytes"],
        **validated["counters"],
        "metrics": validated["metrics"],
        "environment": {
            "hostname": target["hostname"],
            "virtualization": target["virtualization"],
            "boot_id": target["boot_id"],
            "machine_id_sha256": target["machine_id_sha256"],
            "root_source": target["root_source"],
            "driver_is_wsl": preflight["driver"]["is_wsl"],
            "ssh_config": preflight["ssh"]["config"],
            "ssh_host": preflight["ssh"]["host"],
        },
        "pins": {
            "fixture_sha256": request["fixture_sha256"],
            "app": validated["app"],
            "images": preflight["images"],
            "namespace": provision["namespace"],
        },
        "seasonal_result": validated["seasonal_result"],
        "finance_evidence": validated["finance_evidence"],
        "telemetry_evidence": validated["telemetry"],
        "mutation_control_evidence": validated["mutation_controls"],
        "verified_probes": validated["probes"],
        "adapter_artifacts": artifact_refs,
    }
    sensitive = contains_sensitive(evidence)
    require(sensitive is None, "adapter evidence would leak sensitive material at " + str(sensitive))
    return evidence


def provision_arm(request: dict, config: dict, mode: str) -> tuple[dict, dict, dict]:
    if mode not in ("provision", "reset"):
        raise Refusal("invalid provision mode")
    require_trusted_install(config)
    artifacts_dir = validate_artifacts_dir(request["artifacts_dir"])
    namespace = arm_namespace(request)
    preflight = perform_preflight(config)
    require(preflight["target_ok"] is True,
            "guest preflight failed: " + "; ".join(preflight["errors"]))
    active = set(preflight.get("active_compose_projects", []))
    unexpected = sorted(active - {namespace})
    require(not unexpected,
            "guest has active compose project(s) outside this arm: " + ",".join(unexpected))
    fixture = validate_fixture(request, config)
    provision = remote_restore(config, fixture, namespace, mode)
    return preflight, fixture, provision


def execute_arm(request: dict, config: dict) -> dict:
    harness = verify_harness(config)
    artifacts_dir = validate_artifacts_dir(request["artifacts_dir"])
    preflight, fixture, provision = provision_arm(request, config, "reset")
    preflight_ref = write_json_artifact(artifacts_dir / "environment-preflight.json", preflight)
    provision_ref = write_json_artifact(artifacts_dir / "provision-state.json", provision)
    harness_input = {
        "schema_version": HARNESS_REQUEST_SCHEMA,
        "request": request,
        "artifacts_dir": str(artifacts_dir),
        "fixture": {"sha256": fixture["sha256"], "manifest": fixture["manifest"]},
        "environment": preflight,
        "provision": provision,
        "adapter_artifacts": {"environment_preflight": preflight_ref, "provision_state": provision_ref},
    }
    harness_evidence, _raw = run_harness(config, harness, request, artifacts_dir, harness_input)
    validate_target_evidence(harness_evidence, preflight)
    validated = validate_harness_evidence(harness_evidence, request, artifacts_dir)
    harness_stdout_ref = verify_artifact_reference(
        {"artifact": "adapter-harness-stdout.json",
         "sha256": file_hash(artifacts_dir / "adapter-harness-stdout.json")}, artifacts_dir,
        "harness stdout")
    artifact_refs = {
        "environment_preflight": preflight_ref,
        "provision_state": provision_ref,
        "harness_stdout": harness_stdout_ref,
    }
    return build_final_evidence(request, preflight, provision, validated, artifact_refs)


def rejection_object(raw_request: Any, reason: str) -> dict:
    result = {
        "schema_version": ADAPTER_SCHEMA,
        "status": "rejected",
        "reason": sanitize_text(reason),
        "isolated": False,
        "restored": False,
        "external_load_generator": False,
        "finance_parity": False,
        "mutation_controls": False,
        "workload_complete": False,
        "telemetry_complete": False,
        "metrics": {},
    }
    if isinstance(raw_request, dict):
        for field in ("experiment", "baseline_commit", "commit", "patch_sha256", "seed", "trial",
                      "arm", "workload", "fixture_sha256"):
            if field in raw_request and isinstance(raw_request[field], (str, int)):
                result[field] = raw_request[field]
    return result


def adapter_identity() -> dict:
    path = Path(__file__).resolve()
    try:
        checksum = file_hash(path)
    except OSError:
        checksum = None
    return {"path": str(path), "sha256": checksum}


def bootstrap_report(config: dict) -> tuple[dict, int]:
    report = {
        "schema_version": "khitmee.lab-bootstrap/v1",
        "adapter": adapter_identity(),
        "external_driver": {
            "ssh_config": config["target"]["ssh_config"],
            "ssh_host": config["target"]["ssh_host"],
            "fixture_dir": config["fixture_dir"],
            "guest_state_root": config["guest_state_root"],
        },
        "install_note": "pin a read-only installed copy of this directory with experiments.py --harness; do not pin the mutable source checkout",
    }
    try:
        preflight = perform_preflight(config)
    except Refusal as exc:
        preflight = {"schema_version": PREFLIGHT_SCHEMA, "target_ok": False, "errors": [str(exc)]}
    report["preflight"] = preflight
    report["install_trust"] = install_trust_report(config)
    try:
        report["harness"] = verify_harness(config)
        report["ready_for_run"] = (preflight.get("target_ok") is True and
                                   report["install_trust"]["trusted_install"] is True)
    except Refusal as exc:
        report["harness"] = {"configured": False, "reason": str(exc)}
        report["ready_for_run"] = False
    return report, 0 if preflight.get("target_ok") is True else 2


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "preflight", "bootstrap", "provision", "reset"))
    parser.add_argument("--config", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_cli(sys.argv[1:] if argv is None else argv)
    raw_request = None
    try:
        config = load_config(args.config)
        if args.command == "preflight":
            result = perform_preflight(config)
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0 if result["target_ok"] else 2
        if args.command == "bootstrap":
            result, code = bootstrap_report(config)
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return code

        raw_request = json.load(sys.stdin)
        request = validate_request(raw_request)
        if args.command in ("provision", "reset"):
            preflight, _fixture, provision = provision_arm(request, config, args.command)
            output = {"schema_version": "khitmee.lab-provision-result/v1", "status": "complete",
                      "namespace": provision["namespace"], "preflight": preflight, "provision": provision}
            print(json.dumps(output, sort_keys=True, allow_nan=False))
            return 0
        result = execute_arm(request, config)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except Refusal as exc:
        result = rejection_object(raw_request, str(exc))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        print("rejected: " + sanitize_text(str(exc), 300), file=sys.stderr)
        return 2
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result = rejection_object(raw_request, "request was not one JSON object")
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        print("rejected: request was not one JSON object", file=sys.stderr)
        return 2
    except Exception as exc:
        result = rejection_object(raw_request, type(exc).__name__ + ": " + str(exc))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        print("rejected: " + sanitize_text(type(exc).__name__ + ": " + str(exc), 300), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
