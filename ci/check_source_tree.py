#!/usr/bin/env python3
"""Fail if the source tree tracks compiled build products.

This repository ships sources only; runtime binaries (BepInEx core DLLs,
Be5Bootstrap.dll, libbe5hook.so) and tool binaries (smali/baksmali jars) are
produced or provided elsewhere — CI assembles them into the asset pack.

Detection is content-based, not extension-based: a file is flagged when it
starts with a known compiled-artifact magic AND contains NUL bytes (so a
text file that happens to start with "MZ" is not misclassified).

Run from the repository root:  python ci/check_source_tree.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HEAD_BYTES = 8192

BINARY_MAGICS = (
    (b"MZ", "PE image (DLL/EXE)"),
    (b"\x7fELF", "ELF object (SO/O)"),
    (b"dex\n", "DEX bytecode"),
    (b"\xca\xfe\xba\xbe", "Java class / Mach-O fat binary"),
    (b"PK\x03\x04", "ZIP archive (JAR/APK)"),
)

# Explicit source-resource exceptions need a reason so build products stay excluded.
EXCEPTIONS = {}


def binary_reason(head):
    """Return a description if `head` (first bytes of a file) is a compiled
    artifact, else None."""
    if b"\x00" not in head:
        return None
    for magic, label in BINARY_MAGICS:
        if head.startswith(magic):
            return label
    return None


def check_path(path, head):
    """Check one file; returns an error string or None."""
    reason = binary_reason(head)
    if reason is None:
        return None
    if path in EXCEPTIONS:
        return None
    return (
        "%s: tracked file is a compiled artifact (%s); build products must "
        "not be committed — CI produces them into the asset pack" % (path, reason)
    )


def iter_tracked_files(cwd):
    """Yield repository-relative paths of all git-tracked files."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=cwd,
        check=True,
        capture_output=True,
    ).stdout
    for name in out.decode("utf-8", "surrogateescape").split("\0"):
        if name:
            yield name


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args(argv)

    problems = []
    try:
        for rel in iter_tracked_files(args.root):
            if rel in EXCEPTIONS:
                continue
            path = os.path.join(args.root, rel)
            try:
                with open(path, "rb") as f:
                    head = f.read(HEAD_BYTES)
            except OSError as exc:
                problems.append("%s: cannot read tracked file: %s" % (rel, exc))
                continue
            problem = check_path(rel, head)
            if problem:
                problems.append(problem)
    except subprocess.CalledProcessError as exc:
        print("[check-source-tree] ERROR: git ls-files failed: %s" % exc, file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("[check-source-tree] ERROR: git not available", file=sys.stderr)
        return 1

    if problems:
        print("[check-source-tree] ERROR: compiled binaries tracked in git:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    print("[check-source-tree] ok: no compiled artifacts tracked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
