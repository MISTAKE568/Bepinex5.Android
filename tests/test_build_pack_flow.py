import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import assets  # noqa: E402
import build  # noqa: E402
import synth  # noqa: E402

BUILD = os.path.join(REPO, "build.py")


def run_build(args, cwd):
    return subprocess.run(
        [sys.executable, BUILD] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class BuildAssetsFlowTests(unittest.TestCase):
    """End-to-end build.py run against a synthetic pack and APK (no signing,
    supplied dex, no hooks): proves the --assets wiring feeds repack and that
    validation happens before the APK is touched."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="flow-test-")
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        self.pack = synth.build_pack(os.path.join(self.tmpdir, "pack"))
        self.apk = synth.build_apk(os.path.join(self.tmpdir, "game.apk"))
        self.dex = os.path.join(self.tmpdir, "supplied.dex")
        synth.write(self.dex, synth.fake_dex())
        with open(self.apk, "rb") as f:
            self.apk_hash_before = hashlib.sha256(f.read()).hexdigest()
        self.out = os.path.join(self.tmpdir, "out", "game.apk")
        self.work = os.path.join(self.tmpdir, "work")

    def base_args(self):
        return [
            "--apk", self.apk,
            "--assets", self.pack,
            "--package", "com.example.game",
            "--dex", self.dex,
            "--skip-dex-verify",
            "--no-hooks",
            "--no-sign",
            "--out", self.out,
            "--work", self.work,
        ]

    def test_full_pipeline_uses_pack_contents(self):
        proc = run_build(self.base_args(), cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(os.path.isfile(self.out), "output APK missing")

        with zipfile.ZipFile(self.out) as z:
            names = set(z.namelist())
            self.assertEqual(len(names), len(z.infolist()), "duplicate entries in APK")

            with open(os.path.join(self.pack, *assets.SHIM_REL.split("/")), "rb") as f:
                shim = f.read()
            self.assertEqual(z.read("lib/armeabi-v7a/libbe5hook.so"), shim)

            with open(os.path.join(self.pack, assets.BOOTSTRAP_REL), "rb") as f:
                boot = f.read()
            self.assertEqual(z.read("assets/BepInEx/core/Be5Bootstrap.dll"), boot)

            for name in synth.CORE_FILES:
                self.assertIn("assets/BepInEx/core/" + name, names, name)
            self.assertIn("assets/BepInEx/config/BepInEx.cfg", names)
            # APK signature files are stripped during repack.
            self.assertNotIn("META-INF/MANIFEST.MF", names)
            for signature in ("SF", "RSA", "DSA", "EC"):
                self.assertNotIn("META-INF/CERT." + signature, names)
            self.assertEqual(z.read("META-INF/NOTICE.txt"), b"notice data\n")
            self.assertEqual(
                z.read("META-INF/services/com.example.Plugin"),
                b"com.example.Impl\n",
            )

        # The base APK was not modified.
        with open(self.apk, "rb") as f:
            self.assertEqual(
                hashlib.sha256(f.read()).hexdigest(), self.apk_hash_before
            )

        # The report records the asset pack identity.
        with open(os.path.join(self.work, "build.json"), "r", encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["assets"]["version"], synth.VERSION)
        self.assertEqual(report["assets"]["source"]["commit"], synth.COMMIT)
        self.assertTrue(report["assets"]["root_sha256sums"])
        bootstrap_path = os.path.join(self.pack, assets.BOOTSTRAP_REL)
        with open(bootstrap_path, "rb") as f:
            bootstrap = f.read()
        self.assertEqual(report["assets"]["bootstrap"]["path"], bootstrap_path)
        self.assertEqual(report["assets"]["bootstrap"]["size"], len(bootstrap))
        self.assertEqual(
            report["assets"]["bootstrap"]["sha256"], hashlib.sha256(bootstrap).hexdigest()
        )
        with open(os.path.join(self.pack, assets.SHIM_REL), "rb") as f:
            self.assertEqual(
                report["assets"]["shim_sha256"], hashlib.sha256(f.read()).hexdigest()
            )
        with open(os.path.join(self.pack, assets.CONFIG_REL), "rb") as f:
            self.assertEqual(
                report["assets"]["config_sha256"], hashlib.sha256(f.read()).hexdigest()
            )
        self.assertEqual(report["steps"]["repack"]["core_files"], len(synth.CORE_FILES))
        verified = {
            item["path"]: item
            for item in report["steps"]["repack"]["output_entries"]
        }
        self.assertIn("classes.dex", verified)
        self.assertIn("lib/armeabi-v7a/libbe5hook.so", verified)
        self.assertIn("assets/BepInEx/core/Be5Bootstrap.dll", verified)
        self.assertEqual(
            verified["assets/BepInEx/core/Be5Bootstrap.dll"]["sha256"],
            hashlib.sha256(bootstrap).hexdigest(),
        )
        self.assertEqual(
            verified["classes.dex"]["sha256"],
            hashlib.sha256(synth.fake_dex()).hexdigest(),
        )

    def test_relative_paths_resolve_from_current_working_directory(self):
        args = [
            "--apk", "game.apk",
            "--assets", "pack",
            "--package", "com.example.game",
            "--dex", "supplied.dex",
            "--skip-dex-verify",
            "--no-hooks",
            "--no-sign",
            "--out", "out/relative.apk",
            "--work", "work-relative",
        ]
        proc = run_build(args, cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.tmpdir, "out", "relative.apk")))

    def test_missing_pack_file_fails_as_layout_error(self):
        os.remove(os.path.join(self.pack, assets.CONFIG_REL))
        proc = run_build(self.base_args(), cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("manifest file missing on disk", proc.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_tampered_pack_fails_before_writing_output(self):
        with open(os.path.join(self.pack, assets.BOOTSTRAP_REL), "ab") as f:
            f.write(b"tampered")
        proc = run_build(self.base_args(), cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("invalid asset pack", proc.stderr)
        self.assertIn("hash mismatch", proc.stderr)
        self.assertFalse(os.path.exists(self.out), "output must not exist on failure")

    def test_missing_pack_fails_with_actionable_error(self):
        empty = os.path.join(self.tmpdir, "empty-pack")
        os.makedirs(empty)
        args = self.base_args()
        args[args.index("--assets") + 1] = empty
        proc = run_build(args, cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SHA256SUMS not found", proc.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_assets_required_without_complete_explicit_overrides(self):
        args = self.base_args()
        del args[args.index("--assets"):args.index("--assets") + 2]
        proc = run_build(args, cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--assets <asset-pack directory> is required", proc.stderr)
        self.assertIn("--payload", proc.stderr)
        self.assertIn("download and extract the CI asset pack", proc.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_explicit_asset_overrides_work_without_assets_pack(self):
        args = [
            "--apk", self.apk,
            "--package", "com.example.game",
            "--dex", self.dex,
            "--skip-dex-verify",
            "--no-hooks",
            "--no-sign",
            "--core", os.path.join(self.pack, "core"),
            "--boot", os.path.join(self.pack, *assets.BOOTSTRAP_REL.split("/")),
            "--shim", os.path.join(self.pack, *assets.SHIM_REL.split("/")),
            "--payload", self.pack,
            "--out", self.out,
            "--work", self.work,
        ]
        proc = run_build(args, cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(os.path.isfile(self.out))

    def test_repack_requires_asset_paths(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "injector", "apk", "repack.py"),
             "input.apk", "output.apk", "--dex", self.dex, "--no-plugins"],
            cwd=self.tmpdir,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--so", proc.stderr)
        self.assertIn("--core", proc.stderr)
        self.assertIn("--payload", proc.stderr)
        self.assertIn("--boot", proc.stderr)

    def test_repack_rejects_duplicate_input_entries(self):
        duplicate_apk = os.path.join(self.tmpdir, "duplicate.apk")
        shutil.copyfile(self.apk, duplicate_apk)
        with zipfile.ZipFile(duplicate_apk, "a") as z:
            z.writestr("assets/duplicate.txt", b"first")
            z.writestr("assets/duplicate.txt", b"second")
        args = [
            sys.executable,
            os.path.join(REPO, "injector", "apk", "repack.py"),
            duplicate_apk,
            self.out,
            "--dex", self.dex,
            "--abi", "armeabi-v7a",
            "--so", os.path.join(self.pack, *assets.SHIM_REL.split("/")),
            "--core", os.path.join(self.pack, "core"),
            "--payload", self.pack,
            "--boot", os.path.join(self.pack, *assets.BOOTSTRAP_REL.split("/")),
            "--no-plugins",
        ]
        proc = subprocess.run(args, cwd=self.tmpdir, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("duplicate APK entries are not supported", proc.stderr)
        self.assertIn("assets/duplicate.txt", proc.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_build_rejects_input_as_output_without_touching_apk(self):
        proc = run_build(
            self.base_args()[:-4] + ["--out", self.apk, "--work", self.work],
            cwd=self.tmpdir,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("input APK and output APK must be different paths", proc.stderr)
        with open(self.apk, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.apk_hash_before)
        self.assertFalse(os.path.exists(self.work))

    def test_repack_rejects_input_as_output(self):
        args = [
            sys.executable,
            os.path.join(REPO, "injector", "apk", "repack.py"),
            self.apk,
            self.apk,
            "--dex", self.dex,
            "--so", os.path.join(self.pack, *assets.SHIM_REL.split("/")),
            "--core", os.path.join(self.pack, "core"),
            "--payload", self.pack,
            "--boot", os.path.join(self.pack, *assets.BOOTSTRAP_REL.split("/")),
            "--no-plugins",
        ]
        proc = subprocess.run(args, cwd=self.tmpdir, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("input APK and output APK must be different paths", proc.stderr)

    def test_repack_cleans_temporary_output_after_copy_failure(self):
        broken_apk = os.path.join(self.tmpdir, "broken.apk")
        with zipfile.ZipFile(broken_apk, "w") as z:
            z.writestr("classes.dex", synth.fake_dex())
            z.writestr("assets/broken.bin", b"bad")
            data_offset = (
                z.getinfo("assets/broken.bin").header_offset
                + 30
                + len("assets/broken.bin")
                + len(z.getinfo("assets/broken.bin").extra)
            )
        with open(broken_apk, "r+b") as f:
            f.seek(data_offset)
            f.write(b"x")
        output = os.path.join(self.tmpdir, "broken-out.apk")
        args = [
            sys.executable,
            os.path.join(REPO, "injector", "apk", "repack.py"),
            broken_apk,
            output,
            "--dex", self.dex,
            "--so", os.path.join(self.pack, *assets.SHIM_REL.split("/")),
            "--core", os.path.join(self.pack, "core"),
            "--payload", self.pack,
            "--boot", os.path.join(self.pack, *assets.BOOTSTRAP_REL.split("/")),
            "--no-plugins",
        ]
        proc = subprocess.run(args, cwd=self.tmpdir, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(output))
        self.assertFalse(os.path.exists(output + ".tmp"))

    def test_repack_output_validation_rejects_missing_required_entry(self):
        core = build.inspect_core(os.path.join(self.pack, "core"))
        shim_path = os.path.join(self.pack, *assets.SHIM_REL.split("/"))
        boot_path = os.path.join(self.pack, *assets.BOOTSTRAP_REL.split("/"))
        dex = os.path.join(self.tmpdir, "validated.dex")
        synth.write(dex, synth.fake_dex())
        work = os.path.join(self.tmpdir, "missing-entry-work")
        os.makedirs(work)

        def write_incomplete_output(cmd, env=None):
            output_path = cmd[3]
            with zipfile.ZipFile(output_path, "w") as z:
                z.writestr("classes.dex", synth.fake_dex())

        with mock.patch.object(build, "run", side_effect=write_incomplete_output):
            with self.assertRaisesRegex(build.BuildError, "missing required entries"):
                build.stage_repack(
                    argparse.Namespace(payload=self.pack, plugins=None, boot=boot_path),
                    {"path": self.apk, "abi": "armeabi-v7a"},
                    {}, work,
                    {"path": dex},
                    {"path": shim_path},
                    {"status": "skipped", "path": None},
                    core,
                    {},
                )

    def test_repack_output_validation_rejects_entry_hash_mismatch(self):
        core = build.inspect_core(os.path.join(self.pack, "core"))
        shim_path = os.path.join(self.pack, *assets.SHIM_REL.split("/"))
        dex = os.path.join(self.tmpdir, "validated.dex")
        synth.write(dex, synth.fake_dex())
        work = os.path.join(self.tmpdir, "hash-mismatch-work")
        os.makedirs(work)

        def write_mismatched_output(cmd, env=None):
            output_path = cmd[3]
            with zipfile.ZipFile(output_path, "w") as z:
                z.writestr("classes.dex", b"different dex bytes")
                z.writestr(
                    "lib/armeabi-v7a/libbe5hook.so",
                    Path(shim_path).read_bytes(),
                )
                z.writestr(
                    "assets/BepInEx/core/Be5Bootstrap.dll",
                    Path(os.path.join(self.pack, *assets.BOOTSTRAP_REL.split("/"))).read_bytes(),
                )
                for item in core["files"]:
                    with open(os.path.join(core["path"], item["name"]), "rb") as f:
                        z.writestr("assets/BepInEx/core/" + item["name"], f.read())
                with open(os.path.join(self.pack, assets.CONFIG_REL), "rb") as f:
                    z.writestr("assets/BepInEx/config/BepInEx.cfg", f.read())

        with mock.patch.object(build, "run", side_effect=write_mismatched_output):
            with self.assertRaisesRegex(build.BuildError, "repack output hash mismatch: classes.dex"):
                build.stage_repack(
                    argparse.Namespace(
                        payload=self.pack,
                        plugins=None,
                        boot=os.path.join(self.pack, *assets.BOOTSTRAP_REL.split("/")),
                    ),
                    {"path": self.apk, "abi": "armeabi-v7a"},
                    {}, work,
                    {"path": dex},
                    {"path": shim_path},
                    {"status": "skipped", "path": None},
                    core,
                    {},
                )

    def test_standard_preflight_does_not_require_native_build_tools(self):
        args = build.build_parser().parse_args(
            [
                "--assets", self.pack, "--dex", self.dex,
                "--skip-dex-verify", "--no-sign",
            ]
        )
        args.sign = False
        info = {"_tools": {"keystore": {"path": None}}}
        self.assertEqual(
            build.required_tools(args, info),
            [],
        )
        self.assertNotIn("ndk", build.HINTS)
        self.assertNotIn("cmake", build.HINTS)
        self.assertNotIn("ninja", build.HINTS)

    def test_assets_can_be_supplied_by_json_config(self):
        config_path = os.path.join(self.tmpdir, "build-config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"assets": self.pack}, f)
        args = self.base_args()
        del args[args.index("--assets"):args.index("--assets") + 2]
        args += ["--config", config_path]
        proc = run_build(args, cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(os.path.isfile(self.out))

    def test_dry_run_writes_report_without_modifying_apk(self):
        args = self.base_args() + ["--dry-run"]
        proc = run_build(args, cwd=self.tmpdir)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(os.path.isdir(self.work))
        report_path = os.path.join(self.work, "build.json")
        self.assertTrue(os.path.isfile(report_path))
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["status"], "planned")
        with open(self.apk, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.apk_hash_before)

    def test_pack_runs_from_packaging_dir_without_assets_flag(self):
        """The bundled packager auto-detects the pack it ships in."""
        # Rebuild the pack with the real repository tool files so build.py can
        # actually run from inside it.
        shutil.rmtree(self.pack)
        self.pack = synth.build_pack(self.pack, packaging_source=REPO)
        args = [
            "--apk", self.apk,
            "--package", "com.example.game",
            "--dex", self.dex,
            "--skip-dex-verify",
            "--no-hooks",
            "--no-sign",
            "--out", self.out,
            "--work", self.work,
        ]
        proc = subprocess.run(
            [sys.executable, os.path.join(self.pack, "packaging", "build.py")] + args,
            cwd=self.tmpdir,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("asset pack detected next to build.py", proc.stdout)
        self.assertTrue(os.path.isfile(self.out))


if __name__ == "__main__":
    unittest.main()
