#!/usr/bin/env python3
"""Restore the pinned cold-volume seasonal fixture into the isolated khitmee-perf volumes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile

PROJECT = "khitmee-perf"
def acceptance_fixture_pin():
    pin_path = Path(__file__).resolve().with_name("acceptance-fixture.json")
    data = json.loads(pin_path.read_text(encoding="utf-8"))
    return str(data["sha256"])


PINNED_FIXTURE_SHA256 = acceptance_fixture_pin()
ARCHIVE_FORMAT = "cold-named-volume-tar/v1"
VOLUMES = {
    "perf-db-data/_data": ("db", "perf-db-data"),
    "perf-sites/_data": ("sites", "perf-sites"),
    "perf-redis-queue-data/_data": ("redis-queue", "perf-redis-queue-data"),
}
ALLOWED_SYMLINK = ("perf-sites/_data/assets", "/home/frappe/frappe-bench/assets")


def fail(message):
    raise RuntimeError(message)


def docker(*args):
    result = subprocess.run(["docker", *args], text=True, capture_output=True)
    if result.returncode:
        fail("docker " + args[0] + " failed: " + (result.stderr or result.stdout)[:240])
    return result.stdout.strip()


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def companion_manifest_path(archive_path):
    name = archive_path.name
    if name.endswith(".tar.gz"):
        return archive_path.with_name(name[:-len(".tar.gz")] + ".manifest.json")
    return archive_path.with_name(name + ".manifest.json")


def flattened_json(value, prefix=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from flattened_json(child, prefix + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from flattened_json(child, prefix + (str(index),))
    else:
        yield prefix, value


def manifest_field_values(manifest, direct_keys, generic_keys, archive_parents):
    values = []
    for path, value in flattened_json(manifest):
        if not path or not isinstance(value, str):
            continue
        key = path[-1].lower()
        parent = path[-2].lower() if len(path) > 1 else ""
        if key in direct_keys or (key in generic_keys and (not parent or parent in archive_parents)):
            values.append((".".join(path), value))
    return values


def validate_fixture_manifest(manifest_path, expected_digest):
    supplied = Path(manifest_path)
    if not supplied.is_file() or supplied.is_symlink():
        fail(f"fixture manifest missing or not a regular file: {supplied}")
    path = supplied.resolve(strict=True)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"fixture manifest is not valid JSON: {path}: {exc}")
    if not isinstance(manifest, dict):
        fail("fixture manifest must be a JSON object")

    archive_parents = {"archive", "fixture", "fixture_archive", "fixture_tar_gz", "tar_gz"}
    digests = manifest_field_values(
        manifest,
        {"archive_sha256", "fixture_sha256", "fixture_tar_gz_sha256", "tar_gz_sha256"},
        {"sha256", "digest"},
        archive_parents,
    )
    all_strings = [value for _path, value in flattened_json(manifest) if isinstance(value, str)]
    if not digests:
        if expected_digest not in all_strings:
            fail("fixture manifest does not bind an archive digest")
    else:
        mismatched = [f"{key}={value}" for key, value in digests if value.lower() != expected_digest]
        if mismatched:
            fail("fixture manifest archive digest mismatch: " + ", ".join(mismatched))

    formats = manifest_field_values(
        manifest,
        {"archive_format", "fixture_format"},
        {"format", "kind", "type"},
        archive_parents,
    )
    if not formats:
        if ARCHIVE_FORMAT not in all_strings:
            fail("fixture manifest does not declare an archive format")
    elif not any(value == ARCHIVE_FORMAT for _key, value in formats):
        detail = ", ".join(f"{key}={value}" for key, value in formats)
        fail(f"fixture manifest format is not {ARCHIVE_FORMAT}: {detail}")
    return {
        "path": str(path),
        "sha256": sha256(path),
        "archive_format": ARCHIVE_FORMAT,
        "digest_fields": sorted(key for key, _value in digests),
    }


def safe_parts(name):
    if not name or name.startswith("/") or "\\" in name:
        fail("unsafe fixture member")
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail("unsafe fixture member path")
    return parts


def member_kind(member, normalized):
    if member.issym():
        if (normalized, member.linkname) != ALLOWED_SYMLINK:
            fail("unsupported fixture symlink")
        return "symlink"
    if member.isfile():
        return "file"
    if member.isdir():
        return "directory"
    fail("unsupported fixture member type")


def volume_mount(volume):
    labels = json.loads(docker("volume", "inspect", "--format", "{{json .Labels}}", volume) or "{}")
    if labels.get("com.docker.compose.project") != PROJECT:
        fail(f"refusing to restore {volume}: not owned by {PROJECT}")
    mount = Path(docker("volume", "inspect", "--format", "{{ .Mountpoint }}", volume))
    expected = Path("/var/lib/docker/volumes") / volume / "_data"
    if mount != expected:
        fail(f"unexpected Docker volume mountpoint for {volume}: {mount}")
    if not mount.is_dir() or mount.is_symlink():
        fail(f"Docker volume mountpoint is not a real directory: {mount}")
    return mount


def clear_directory(path):
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            fail(f"unsupported existing volume entry in {path}: {child}")


def restore(args):
    archive_path = Path(args.archive).resolve(strict=True)
    digest = sha256(archive_path)
    if digest != args.expected_sha256:
        fail(f"fixture digest mismatch: got {digest}, expected {args.expected_sha256}")
    manifest_arg = args.fixture_manifest or companion_manifest_path(archive_path)
    manifest = validate_fixture_manifest(manifest_arg, digest)
    running = docker("ps", "-q", "--filter", "label=com.docker.compose.project=" + PROJECT).splitlines()
    if any(running):
        fail(f"{PROJECT} containers are still running; stop them before cold-volume restore")

    mounts = {logical: volume_mount(volume) for _prefix, (logical, volume) in VOLUMES.items()}
    counts = {logical: 0 for logical in mounts}
    symlinks = {}
    env_bytes = None
    roots_seen = {logical: False for logical in mounts}
    parents_seen = set()
    seen = set()

    # The archive is operator-pinned and hash-verified before any mutation. Extraction
    # is a single streaming pass so restore time stays proportional to the fixture size.
    with tarfile.open(archive_path, "r|gz") as archive:
        # Opening the gzip stream succeeded; only now clear the disposable perf volumes.
        for mount in mounts.values():
            clear_directory(mount)
        for member in archive:
            normalized = "/".join(safe_parts(member.name))
            if normalized in seen:
                fail("duplicate fixture member")
            seen.add(normalized)
            kind = member_kind(member, normalized)
            if normalized == ".env":
                data = archive.extractfile(member).read()
                if not 0 < len(data) <= 16384:
                    fail("invalid fixture .env member")
                env_bytes = data
                continue
            if normalized in {"perf-db-data", "perf-sites", "perf-redis-queue-data"}:
                if not member.isdir():
                    fail("volume parent must be a directory")
                parents_seen.add(normalized)
                continue
            matched = None
            for prefix, (logical, _volume) in VOLUMES.items():
                if normalized == prefix:
                    if not member.isdir():
                        fail("volume data root must be a directory")
                    roots_seen[logical] = True
                    matched = (logical, "")
                    break
                if normalized.startswith(prefix + "/"):
                    matched = (logical, normalized[len(prefix):].lstrip("/"))
                    break
            if matched is None:
                fail("unexpected fixture member " + normalized)
            logical, relative = matched
            if not relative:
                continue
            target = mounts[logical].joinpath(*relative.split("/"))
            resolved_parent = target.parent.resolve(strict=False)
            if mounts[logical] not in resolved_parent.parents and resolved_parent != mounts[logical]:
                fail("fixture extraction escapes Docker volume")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode & 0o777)
                os.chown(target, member.uid, member.gid)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    fail("duplicate fixture symlink target")
                os.symlink(member.linkname, target)
                os.chown(target, member.uid, member.gid, follow_symlinks=False)
                symlinks[normalized] = member.linkname
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                os.chmod(target, member.mode & 0o777)
                os.chown(target, member.uid, member.gid)
                counts[logical] += 1

    if env_bytes is None or not all(roots_seen.values()) or not all(counts.values()):
        fail("incomplete fixture archive")
    for symlink in symlinks:
        if any(normalized.startswith(symlink + "/") for normalized in seen):
            fail("fixture writes through the pinned asset symlink")
    env_path = Path(args.docker_dir) / ".env"
    env_path.write_bytes(env_bytes)
    os.chmod(env_path, 0o600)
    return {
        "passed": True,
        "archive": str(archive_path),
        "fixture_sha256": digest,
        "archive_format": ARCHIVE_FORMAT,
        "fixture_manifest_path": manifest["path"],
        "fixture_manifest_sha256": manifest["sha256"],
        "fixture_manifest_digest_fields": manifest["digest_fields"],
        "volume_file_counts": counts,
        "symlinks": symlinks,
        "env_path": str(env_path),
        "restored_from_immutable_fixture": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--expected-sha256", default=PINNED_FIXTURE_SHA256)
    parser.add_argument("--fixture-manifest")
    parser.add_argument("--docker-dir", default=str(Path(__file__).resolve().parents[1] / "docker"))
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        result = restore(args)
    except Exception as exc:
        result = {"passed": False, "error": str(exc), "restored_from_immutable_fixture": False}
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
