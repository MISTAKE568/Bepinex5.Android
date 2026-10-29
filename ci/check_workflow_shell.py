#!/usr/bin/env python3
"""Check bash syntax without executing workflow commands or adding a YAML dependency.

This deliberately supports only the repository's step-level `run: command` and
`run: |`/`|-` forms, with the default Linux bash shell. It is not a YAML or
GitHub Actions schema validator. Unsupported run/shell forms fail closed.
Expressions are replaced with a neutral word for syntax checking only.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys


def run_blocks(text):
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        shell = re.match(r"^\s*(?:-\s+)?shell:\s*(.*?)\s*$", line)
        if shell and shell[1] != "bash":
            raise ValueError("line %d: unsupported shell" % (i + 1))
        match = re.match(r"^( *)(?:- +)?run:\s*(.*)$", line)
        if not match:
            i += 1
            continue
        start = i + 1
        indent = line.index("run:")
        value = match[2].strip()
        i += 1
        body = []
        while i < len(lines):
            following = lines[i]
            if following.strip() and len(following) - len(following.lstrip(" ")) <= indent:
                break
            body.append(following)
            i += 1
        if value in ("|", "|-"):
            nonempty = [s for s in body if s.strip()]
            if not nonempty:
                raise ValueError("line %d: empty run block" % start)
            width = len(nonempty[0]) - len(nonempty[0].lstrip(" "))
            if any(len(s) - len(s.lstrip(" ")) < width for s in nonempty):
                raise ValueError("line %d: inconsistent block indentation" % start)
            script = "\n".join(s[width:] if s.strip() else "" for s in body)
        else:
            if not value or value[0] in "|>\"'[{&*!" or any(s.strip() for s in body):
                raise ValueError("line %d: unsupported run scalar" % start)
            script = value
        script = re.sub(r"\$\{\{.*?\}\}", "EXPRESSION", script, flags=re.S)
        blocks.append((start, script + "\n"))
    if not blocks:
        raise ValueError("no supported run blocks found")
    return blocks


def check(path):
    blocks = run_blocks(Path(path).read_text(encoding="utf-8"))
    for line, script in blocks:
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        if result.returncode:
            raise ValueError("%s:%d: %s" % (path, line, result.stderr.strip()))
    print("[workflow-shell] %s: %d run blocks passed bash -n" % (path, len(blocks)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", nargs="?", default=".github/workflows/build-assets.yml")
    args = parser.parse_args(argv)
    try:
        check(args.workflow)
    except (OSError, ValueError) as exc:
        print("[workflow-shell] ERROR: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
