import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATE_HEADER = os.path.join(ROOT, "shim", "bootstrap_gate.h")


class BootstrapGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = shutil.which(os.environ.get("CC", "cc"))
        if not cls.cc:
            raise unittest.SkipTest("a C compiler is required for the pure gate test")

    def test_attempt_success_retry_and_chain_gating(self):
        harness = r'''#include <assert.h>
#include "bootstrap_gate.h"

int main(void) {
    be5_bootstrap_gate gate = {0};

    assert(be5_begin_preload(&gate) == 1);
    assert(gate.preload_attempted == 1 && gate.in_boot == 1);
    assert(be5_begin_preload(&gate) == 0);
    be5_finish_preload(&gate, 7);
    assert(gate.preload_succeeded == 0 && gate.in_boot == 0);
    assert(be5_begin_preload(&gate) == 0);
    assert(be5_begin_chain(&gate, 1) == 0);
    assert(gate.chain_attempted == 0);

    be5_bootstrap_gate success = {0};
    assert(be5_begin_preload(&success) == 1);
    be5_finish_preload(&success, 0);
    assert(success.preload_succeeded == 1);
    assert(be5_begin_chain(&success, 0) == 0);
    assert(be5_begin_chain(&success, 1) == 1);
    assert(success.chain_attempted == 1 && success.in_boot == 1);
    assert(be5_begin_chain(&success, 1) == 0);
    be5_finish_chain(&success, 9);
    assert(success.chain_succeeded == 0 && success.in_boot == 0);
    assert(be5_begin_chain(&success, 1) == 0);

    be5_bootstrap_gate reentrant = {0};
    reentrant.in_boot = 1;
    assert(be5_begin_preload(&reentrant) == 0);
    assert(be5_begin_chain(&reentrant, 1) == 0);
    reentrant.in_boot = 0;
    assert(be5_begin_preload(&reentrant) == 1);
    be5_finish_preload(&reentrant, 0);
    assert(be5_begin_chain(&reentrant, 1) == 1);
    be5_finish_chain(&reentrant, 0);
    assert(reentrant.chain_succeeded == 1 && reentrant.in_boot == 0);
    assert(be5_begin_chain(&reentrant, 1) == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="be5-gate-test-") as tmp:
            source = os.path.join(tmp, "gate_test.c")
            executable = os.path.join(tmp, "gate_test")
            with open(source, "w", encoding="utf-8") as f:
                f.write(harness)
            subprocess.run(
                [self.cc, "-std=c99", "-Wall", "-Wextra", "-Werror",
                 "-I", os.path.dirname(GATE_HEADER), source, "-o", executable],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([executable], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
