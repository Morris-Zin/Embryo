#!/usr/bin/env python3
"""Validate and materialize the exact khitmee_ops tree under test."""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile

APP_REL = pathlib.Path("apps") / "khitmee_ops"


def run_git(checkout, args):
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def validate_checkout(path):
    checkout = pathlib.Path(path).expanduser().resolve()
    if not checkout.is_dir():
        raise RuntimeError(f"app checkout does not exist: {checkout}")
    root = pathlib.Path(run_git(checkout, ["rev-parse", "--show-toplevel"])).resolve()
    if root != checkout:
        raise RuntimeError(f"--app-tree must be the git checkout root ({root}), got {checkout}")
    if not (checkout / APP_REL).is_dir():
        raise RuntimeError(f"{checkout} does not contain {APP_REL.as_posix()}")
    return checkout


def dirty_entries(checkout):
    out = run_git(checkout, ["status", "--porcelain=v1", "--untracked-files=all"])
    return [line for line in out.splitlines() if line.strip()]


def materialize(checkout, ref, output_dir):
    commit = run_git(checkout, ["rev-parse", f"{ref}^{{commit}}"] if ref else ["rev-parse", "HEAD"])
    tree_hash = run_git(checkout, ["rev-parse", f"{commit}:{APP_REL.as_posix()}"])
    dirty = dirty_entries(checkout)
    if dirty:
        raise RuntimeError(
            "candidate checkout is dirty; refusing to deploy. "
            "Commit or stash every tracked/untracked checkout change. First entries: " + "; ".join(dirty[:8])
        )

    destination = pathlib.Path(output_dir).resolve()
    if destination.exists():
        raise RuntimeError(f"materialize destination already exists; refusing to remove it: {destination}")
    destination.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="khitmee-app-tree-") as archive_dir:
        archive_name = pathlib.Path(archive_dir) / "tree.tar"
        archive = subprocess.run(
            ["git", "-C", str(checkout), "archive", "--format=tar", "-o", str(archive_name), commit, APP_REL.as_posix()],
            text=True,
            capture_output=True,
            check=False,
        )
        if archive.returncode != 0:
            raise RuntimeError(archive.stderr.strip() or archive.stdout.strip() or "git archive failed")
        with tarfile.open(archive_name, mode="r:") as tar:
            tar.extractall(destination, filter="data")
    app_path = destination / APP_REL
    if not app_path.is_dir():
        raise RuntimeError(f"git archive did not materialize {APP_REL.as_posix()}")
    return {
        "app_checkout": str(checkout),
        "app_ref": ref or "HEAD",
        "app_commit_sha": commit,
        "app_tree_hash": tree_hash,
        "app_tree_path": APP_REL.as_posix(),
        "checkout_dirty": bool(dirty),
        "materialized_app_path": str(app_path),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--materialize-dir", required=True)
    args = parser.parse_args()
    try:
        checkout = validate_checkout(args.checkout)
        metadata = materialize(checkout, args.ref.strip(), args.materialize_dir)
    except Exception as exc:
        print(f"NO-GO: {exc}", file=sys.stderr)
        return 2
    pathlib.Path(args.output).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
