"""Offline C# logic/metadata checks; never invoke Android native functions."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/BepInEx/BepInEx.Preloader/RuntimeFixes/AndroidDetourFix.cs"


class AndroidDetourFixTests(unittest.TestCase):
    def test_query_and_permission_wiring(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertLess(source.index('GetProperty("SystemPageSize")'),
                        source.index("long r = sysconf(_SC_PAGESIZE);"))
        for statement in ("return ValidatePageSize(iv);", "return ValidatePageSize(lv);",
                          "return ValidatePageSize(r);",
                          "GetPageRange((long)start, len, PageSize, out startPage, out endPage);"):
            self.assertIn(statement, source)

    def test_csharp_logic_and_pinvoke_metadata(self):
        compiler, runtime = shutil.which("mcs"), shutil.which("mono")
        if not compiler or not runtime:
            self.skipTest("offline C# checks require existing mcs and mono; no restore attempted")
        with tempfile.TemporaryDirectory(prefix="be5-android-pages-") as tmp:
            executable = str(Path(tmp) / "AndroidPages.exe")
            compiled = subprocess.run(
                [compiler, "-out:" + executable, str(SOURCE),
                 str(Path(__file__).with_name("android_detour_fix_harness.cs"))],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            executed = subprocess.run([runtime, executable], capture_output=True,
                                      text=True, timeout=60)
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
            self.assertIn("Android page checks passed", executed.stdout)


if __name__ == "__main__":
    unittest.main()
