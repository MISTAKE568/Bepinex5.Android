"""Exercise the real Bootstrap with isolated managed fixture assemblies."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapReturnTests(unittest.TestCase):
    def test_managed_return_contract(self):
        compiler, runtime = shutil.which("mcs"), shutil.which("mono")
        if not compiler or not runtime:
            self.skipTest("managed fixture test requires existing mcs/mono; no restore")
        preloader = '''using System;
namespace BepInEx.Preloader {
 public static class PreloaderRunner {
  public static void PreloaderPreMain() {
   if (Environment.GetEnvironmentVariable("BE5_TEST_MODE") == "preload-fail")
    throw new Exception("fixture preload failure");
  }
 }
}'''
        chainloader = '''using System;
using System.IO;
namespace BepInEx.Bootstrap {
 public static class Chainloader {
  public static void Initialize(string path, bool console, string[] search) {
   File.WriteAllText(Environment.GetEnvironmentVariable("BE5_TEST_MARKER"), "called");
   if (Environment.GetEnvironmentVariable("BE5_TEST_MODE") == "chain-fail")
    throw new Exception("fixture chain failure");
  }
  public static void Start() {}
 }
}'''
        runner = '''using System;
class Harness {
 static int Main(string[] args) {
  int result = args[0] == "Run" ? Be5Bootstrap.Boot.Run() :
   args[0] == "Preload" ? Be5Bootstrap.Boot.Preload() : Be5Bootstrap.Boot.Chain();
  Console.WriteLine("RESULT=" + result);
  return 0;
 }
}'''
        with tempfile.TemporaryDirectory(prefix="be5-managed-") as tmp:
            root = Path(tmp)
            for name, text in (("Preloader.cs", preloader), ("Chainloader.cs", chainloader),
                               ("Harness.cs", runner)):
                (root / name).write_text(text, encoding="utf-8")
            commands = [
                [compiler, "-target:library", "-out:" + str(root / "BepInEx.Preloader.dll"),
                 str(root / "Preloader.cs")],
                [compiler, "-target:library", "-out:" + str(root / "BepInEx.dll"),
                 str(root / "Chainloader.cs")],
                [compiler, "-out:" + str(root / "Harness.exe"),
                 str(ROOT / "payload/bootstrap/Boot.cs"), str(root / "Harness.cs")],
            ]
            for command in commands:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
            marker = root / "chain.called"
            for mode, entry, expected, called in (
                ("success", "Preload", 0, False),
                ("success", "Run", 0, True),
                ("preload-fail", "Run", 1, False),
                ("chain-fail", "Run", 1, True),
            ):
                with self.subTest(mode=mode, entry=entry):
                    if marker.exists():
                        marker.unlink()
                    env = dict(os.environ, BEPINEX_CORE_DIR=tmp, BEPINEX_DATA_DIR=tmp,
                               BE5_TEST_MODE=mode, BE5_TEST_MARKER=str(marker))
                    result = subprocess.run([runtime, str(root / "Harness.exe"), entry],
                                            env=env, check=True, capture_output=True,
                                            text=True, timeout=60)
                    self.assertEqual(result.stdout.strip(), "RESULT=" + str(expected))
                    self.assertEqual(list(root.glob("be5boot*")), [])
                    self.assertEqual(marker.exists(), called)
