#!/usr/bin/env python3
"""Require the host toolchain and reject skipped tests in CI."""

import os
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    required = (os.environ.get("CC", "cc"), "mcs", "mono", "bash")
    missing = [tool for tool in required if not shutil.which(tool)]
    if missing:
        print("[tests] missing required tools: " + ", ".join(missing), file=sys.stderr)
        return 1
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("[tests] skipped tests are not allowed in CI", file=sys.stderr)
    return 0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1


if __name__ == "__main__":
    sys.exit(main())
