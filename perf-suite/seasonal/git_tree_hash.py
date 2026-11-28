#!/usr/bin/env python3
"""Compute Git-compatible tree object hashes for a checked-out directory.

This is the same object model used by `git rev-parse <commit>:<path>`: blob
and tree object IDs are SHA-1 digests of the Git object header plus canonical
tree entries. It lets the harness hash files as actually bind-mounted inside a
container without requiring a .git directory there.
"""

import hashlib
import json
import os
import pathlib
import stat
import sys


def object_id(kind, payload):
    return hashlib.sha1(kind.encode("ascii") + b" " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


def blob_hash(path):
    return object_id("blob", pathlib.Path(path).read_bytes())


def symlink_hash(path):
    return object_id("blob", os.readlink(path).encode("utf-8", "surrogateescape"))


def _sort_key(name, is_dir):
    # Git compares directory names as though they have a trailing slash.
    return (name + "/" if is_dir else name).encode("utf-8", "surrogateescape")


def git_tree_hash(root):
    root = pathlib.Path(root)
    if not root.is_dir():
        raise RuntimeError(f"tree path missing: {root}")
    entries = []
    children = sorted(root.iterdir(), key=lambda child: _sort_key(child.name, child.is_dir() and not child.is_symlink()))
    for child in children:
        if child.name == ".git":
            continue
        name = child.name.encode("utf-8", "surrogateescape")
        if child.is_symlink():
            mode = b"120000"
            oid = bytes.fromhex(symlink_hash(child))
        elif child.is_dir():
            mode = b"40000"
            oid = bytes.fromhex(git_tree_hash(child))
        elif child.is_file():
            mode = b"100755" if child.stat().st_mode & stat.S_IXUSR else b"100644"
            oid = bytes.fromhex(blob_hash(child))
        else:
            raise RuntimeError(f"unsupported file type in app tree: {child}")
        entries.append(mode + b" " + name + b"\0" + oid)
    return object_id("tree", b"".join(entries))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: git_tree_hash.py PATH", file=sys.stderr)
        return 2
    digest = git_tree_hash(argv[0])
    print("TREE_HASH_EVIDENCE " + json.dumps({"path": argv[0], "tree_hash": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
