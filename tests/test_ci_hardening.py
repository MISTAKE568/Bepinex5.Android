"""Offline CI checks: never invoke dotnet, NDK, or a release service."""

import contextlib
import io
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
import zipfile

import build_core
from ci import check_workflow_shell, run_tests


ROOT = Path(__file__).resolve().parents[1]


class CoreReferenceTests(unittest.TestCase):
    def test_generated_targets_are_exact_idempotent_and_net35_only(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            build_core.ensure_net35_targets(tmp)
            path = Path(tmp) / "Directory.Build.targets"
            self.assertEqual(path.read_text(), build_core.NET35_TARGETS)
            with mock.patch("builtins.open", wraps=open) as opened:
                build_core.ensure_net35_targets(tmp)
                self.assertEqual([call.args[1] for call in opened.call_args_list], ["rb"])
            group = ET.fromstring(path.read_text()).find("ItemGroup")
            self.assertEqual(group.attrib["Condition"], "'$(TargetFramework)' == 'net35'")
            self.assertEqual(group.find("PackageReference").attrib, {
                "Include": "Microsoft.NETFramework.ReferenceAssemblies.net35",
                "Version": "1.0.3", "PrivateAssets": "all",
            })

    def test_dry_run_does_not_generate(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            build_core.ensure_net35_targets(tmp, dry_run=True)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_conflicting_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Directory.Build.targets"
            path.write_text("custom targets")
            for dry_run in (False, True):
                with self.assertRaises(build_core.CoreBuildError):
                    build_core.ensure_net35_targets(tmp, dry_run=dry_run)
            self.assertEqual(path.read_text(), "custom targets")

    def test_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "original"
            target.write_text(build_core.NET35_TARGETS)
            (Path(tmp) / "Directory.Build.targets").symlink_to(target)
            with self.assertRaises(build_core.CoreBuildError):
                build_core.ensure_net35_targets(tmp)

    def test_main_dry_run_preserves_publish_directory(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / "core"
            publish = Path(str(out) + ".publish")
            publish.mkdir()
            marker = publish / "keep"
            marker.write_text("existing")
            with mock.patch.object(build_core, "find_dotnet", return_value="fake-dotnet"), \
                 mock.patch.object(build_core, "dotnet_version", return_value="8.test"), \
                 mock.patch.object(build_core, "ensure_net35_targets") as targets, \
                 mock.patch.object(build_core.subprocess, "run") as run:
                self.assertEqual(build_core.main(["--dry-run", "--out", str(out)]), 0)
                run.assert_not_called()
                if build_core.sys.platform.startswith("linux"):
                    targets.assert_called_once_with(build_core.DEFAULT_SRC, dry_run=True)
            self.assertEqual(marker.read_text(), "existing")


class RequiredTestToolsTests(unittest.TestCase):
    def test_missing_mono_fails_before_discovery(self):
        with mock.patch.object(run_tests.shutil, "which", side_effect=lambda name: None if name == "mono" else name), \
             mock.patch.object(run_tests.unittest.defaultTestLoader, "discover") as discover, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_tests.main(), 1)
            discover.assert_not_called()

    def test_skipped_or_empty_suite_fails_even_when_successful(self):
        for skipped, count, expected in (([("fixture", "missing runtime")], 1, 1), ([], 0, 1), ([], 1, 0)):
            with self.subTest(skipped=skipped, count=count):
                result = mock.Mock(skipped=skipped, testsRun=count)
                result.wasSuccessful.return_value = True
                with mock.patch.object(run_tests.shutil, "which", return_value="tool"), \
                     mock.patch.object(run_tests.unittest.defaultTestLoader, "discover"), \
                     mock.patch.object(run_tests.unittest, "TextTestRunner") as runner, \
                     contextlib.redirect_stderr(io.StringIO()):
                    runner.return_value.run.return_value = result
                    self.assertEqual(run_tests.main(), expected)


@unittest.skipUnless(shutil.which("bash"), "bash is required for syntax checks")
class WorkflowShellTests(unittest.TestCase):
    def test_current_workflow_and_release_script(self):
        with contextlib.redirect_stdout(io.StringIO()):
            check_workflow_shell.check(ROOT / ".github/workflows/build-assets.yml")
        subprocess.run(["bash", "-n", str(ROOT / "ci/prepare_release_zip.sh")], check=True)

    def test_literal_heredoc_inline_and_expression(self):
        blocks = check_workflow_shell.run_blocks(
            "steps:\n  - run: |\n      cat <<'EOF'\n      payload\n      EOF\n"
            "  - run: printf '%s' '${{ env.VALUE }}'\n")
        self.assertEqual(len(blocks), 2)
        self.assertIn("\npayload\nEOF\n", blocks[0][1])
        self.assertIn("EXPRESSION", blocks[1][1])

    def test_bad_bash_fails_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow.yml"
            marker = Path(tmp) / "must-not-exist"
            path.write_text("steps:\n  - run: |\n      touch %s\n      if then\n" % marker)
            with self.assertRaises(ValueError):
                check_workflow_shell.check(path)
            self.assertFalse(marker.exists())

    def test_unsupported_forms_fail_closed(self):
        for text in ("steps: []", "  run: >\n    echo ok\n", "  run: 'echo ok'",
                     "  run: |\n", "  shell: pwsh\n  run: echo ok",
                     "  run: echo\n    continued"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                check_workflow_shell.run_blocks(text)

    @unittest.skipUnless(shutil.which("unzip"), "unzip is required for artifact verification")
    def test_downloaded_zip_is_verified_before_release_commands(self):
        workflow = (ROOT / ".github/workflows/build-assets.yml").read_text()
        build, release = workflow.split("  release:\n", 1)
        self.assertNotIn("contents: write", build)
        self.assertIn("contents: write", release)
        self.assertIn("needs: assets-arm32", release)
        self.assertIn("github.event_name == 'push'", release)
        self.assertIn("refs/tags/v", release)
        self.assertNotIn("prepare_release_zip.sh", release)
        self.assertIn("path: dist/${{ steps.pack.outputs.name }}.zip", build)
        blocks = check_workflow_shell.run_blocks(release)
        self.assertEqual(len(blocks), 2)
        script = "set -euo pipefail\n" + "\n".join(body for _, body in blocks)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "dist").mkdir()
            name = "bep5-android-arm32-assets-v1-test"
            archive = base / "dist" / (name + ".zip")
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("README.md", "fixture")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            tools = base / "tools"
            tools.mkdir()
            gh = tools / "gh"
            gh.write_text('#!%s\nprintf "%%s\n" "$*" >> "$JOURNAL"\n[ "$2" != "view" ]\n' % shutil.which("bash"))
            gh.chmod(0o755)
            journal = base / "journal"
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                       PACK_NAME=name, PACK_SHA256=digest, GITHUB_REF_NAME="v1-test", JOURNAL=str(journal))
            result = subprocess.run(["bash"], input=script, cwd=base, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = journal.read_text().splitlines()
            self.assertEqual(len(commands), 3)
            self.assertIn("--verify-tag", commands[1])
            self.assertIn("--notes-file release-notes.txt", commands[1])
            self.assertEqual(commands[-1], "release upload v1-test " + name + ".zip --clobber")
            journal.unlink()
            archive.write_bytes(archive.read_bytes() + b"tampered")
            result = subprocess.run(["bash"], input=script, cwd=base, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(journal.exists())

    def test_release_checks_gate_upload_and_keep_single_attachment(self):
        workflow = (ROOT / ".github/workflows/build-assets.yml").read_text()
        self.assertNotIn("cat > src/BepInEx/Directory.Build.targets", workflow)
        self.assertIn('bash ci/prepare_release_zip.sh "$PACK_NAME"', workflow)
        self.assertIn('gh release upload "$GITHUB_REF_NAME" "${PACK_NAME}.zip" --clobber', workflow)
        self.assertEqual(workflow.count("gh release upload"), 1)
        for assertion in ("python assets.py verify", "ELF32", "Machine:[[:space:]]*ARM", "JNI_OnLoad"):
            self.assertIn(assertion, workflow)
        # Stub every external command so this tests ordering/failure without builds.
        script = ROOT / "ci/prepare_release_zip.sh"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            name = "bep5-android-arm32-assets-test"
            pack = base / "dist" / name
            pack.mkdir(parents=True)
            (pack / "SHA256SUMS").write_text("digest  file\n")
            tools = base / "tools"
            tools.mkdir()
            for tool in ("python", "sha256sum", "zip"):
                stub = tools / tool
                stub.write_text('#!%s\nprintf "%%s\\n" "${0##*/} $*" >> "$JOURNAL"\n' % shutil.which("bash") +
                                'case "${0##*/} $*" in "$FAIL"*) exit 1;; esac\n')
                stub.chmod(0o755)
            journal = base / "journal"
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                       JOURNAL=str(journal), FAIL="never-match")
            result = subprocess.run(["bash", str(script), name], cwd=base, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = journal.read_text().splitlines()
            self.assertEqual(lines, ["python assets.py verify dist/" + name,
                                    "sha256sum --check SHA256SUMS",
                                    "zip -r %s.zip %s" % (name, name),
                                    "zip -T %s.zip" % name, "sha256sum %s.zip" % name])
            self.assertIn("digest  file", result.stdout)
            for failure in ("python", "sha256sum --check", "zip -T"):
                journal.write_text("")
                result = subprocess.run(["bash", str(script), name], cwd=base,
                                        env=dict(env, FAIL=failure), capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("sha256sum %s.zip" % name, journal.read_text())
            (base / "dist" / (name + ".zip")).write_text("old")
            result = subprocess.run(["bash", str(script), name], cwd=base, env=env,
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((base / "dist" / (name + ".zip")).read_text(), "old")


if __name__ == "__main__":
    unittest.main()
