import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import assets  # noqa: E402
import synth  # noqa: E402


class SafeRelPathTests(unittest.TestCase):
    def test_accepts_normal_relative_paths(self):
        for ok in (
            "core/BepInEx.dll",
            "shim/armeabi-v7a/libbe5hook.so",
            "packaging/injector/apk/repack.py",
            "README.md",
        ):
            self.assertTrue(assets.safe_rel_path(ok), ok)

    def test_rejects_unsafe_paths(self):
        for bad in (
            "/etc/passwd",
            "C:\\Windows\\evil.dll",
            "C:/evil.dll",
            "c:evil",
            "..",
            "../evil",
            "core/../evil.dll",
            "core/./BepInEx.dll",
            "",
            " ",
            " core/BepInEx.dll",
            "core//BepInEx.dll",
            "core/BepInEx.dll\n",
            "core/BepIn\x00Ex.dll",
            "core\\BepInEx.dll",
        ):
            self.assertFalse(assets.safe_rel_path(bad), repr(bad))


class ReadSumsTests(unittest.TestCase):
    def test_rejects_duplicate_entries(self):
        path = os.path.join(self.tmp(), "SHA256SUMS")
        with open(path, "w") as f:
            f.write("a" * 64 + "  core/BepInEx.dll\n")
            f.write("b" * 64 + "  core/BepInEx.dll\n")
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.read_sums(path)
        self.assertIn("duplicate", str(ctx.exception))

    def test_rejects_dangerous_paths(self):
        for dangerous in ("/etc/passwd", "../escape", "C:\\x.dll"):
            path = os.path.join(self.tmp(), "SHA256SUMS")
            with open(path, "w") as f:
                f.write("a" * 64 + "  " + dangerous + "\n")
            with self.assertRaises(assets.AssetPackError) as ctx:
                assets.read_sums(path)
            self.assertIn("unsafe", str(ctx.exception))

    def test_rejects_bad_digest(self):
        path = os.path.join(self.tmp(), "SHA256SUMS")
        with open(path, "w") as f:
            f.write("zz" * 32 + "  core/BepInEx.dll\n")
        with self.assertRaises(assets.AssetPackError):
            assets.read_sums(path)

    def tmp(self):
        import tempfile

        d = tempfile.mkdtemp(prefix="asset-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class LoadAssetPackTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmpdir = tempfile.mkdtemp(prefix="pack-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir, ignore_errors=True))
        self.root = synth.build_pack(os.path.join(self.tmpdir, "pack"))

    def test_valid_pack_loads(self):
        pack = assets.load_asset_pack(self.root)
        self.assertEqual(pack["version"], synth.VERSION)
        self.assertEqual(pack["source"]["commit"], synth.COMMIT)
        self.assertEqual(pack["abi"], assets.ABI)
        self.assertEqual(
            pack["paths"]["bootstrap"],
            os.path.join(self.root, assets.BOOTSTRAP_REL),
        )
        expected_files = (
            len(synth.CORE_FILES)
            + 2  # core manifest.json + SHA256SUMS
            + 1  # bootstrap
            + 1  # config
            + 1  # shim
            + len(assets.PACKAGING_FILES)
            + 1  # README.md
            + 1  # build-info.json
        )
        self.assertEqual(len(pack["files"]), expected_files)
        for rel in assets.required_paths():
            self.assertTrue(
                os.path.isfile(os.path.join(self.root, *rel.split("/"))), rel
            )

    def test_missing_bootstrap_fails(self):
        os.remove(os.path.join(self.root, assets.BOOTSTRAP_REL))
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn(assets.BOOTSTRAP_REL, str(ctx.exception))

    def test_tampered_file_fails_hash_check(self):
        path = os.path.join(self.root, "core", "BepInEx.dll")
        with open(path, "ab") as f:
            f.write(b"tamper")
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("hash mismatch", str(ctx.exception))

    def test_duplicate_manifest_entry_fails(self):
        lines = synth.sums_lines(self.root)
        synth.rewrite_sums(self.root, lines + [lines[0]])
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("duplicate", str(ctx.exception))

    def test_absolute_and_traversal_entries_fail(self):
        lines = synth.sums_lines(self.root)
        synth.rewrite_sums(
            self.root,
            lines + ["a" * 64 + "  /etc/passwd", "b" * 64 + "  ../escape.dll"],
        )
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("unsafe", str(ctx.exception))

    def test_unmanifested_file_fails(self):
        synth.write(os.path.join(self.root, ".DS_Store"), b"junk")
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("not listed", str(ctx.exception))

    def test_missing_packaging_tool_fails(self):
        os.remove(os.path.join(self.root, "packaging", "build.py"))
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("packaging/build.py", str(ctx.exception))

    def test_missing_build_info_fails(self):
        os.remove(os.path.join(self.root, assets.BUILD_INFO_REL))
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        # Either "missing required files" (not listed) or "manifest file
        # missing on disk" (listed but absent) is an actionable failure.
        self.assertIn(assets.BUILD_INFO_REL, str(ctx.exception))

    def test_wrong_pack_kind_fails(self):
        path = os.path.join(self.root, assets.BUILD_INFO_REL)
        with open(path, "r", encoding="utf-8") as f:
            info = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(info.replace(assets.PACK_KIND, "something-else"))
        synth.rebuild_sums(self.root)
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("kind", str(ctx.exception))

    def test_arm64_shim_rejected(self):
        path = os.path.join(self.root, *assets.SHIM_REL.split("/"))
        with open(path, "wb") as f:
            f.write(synth.fake_shim(elf_class=2, machine=183))
        synth.rebuild_sums(self.root)
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("not ELF32", str(ctx.exception))

    def test_core_missing_required_file_fails(self):
        os.remove(os.path.join(self.root, "core", "0Harmony.dll"))
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("0Harmony.dll", str(ctx.exception))

    def test_core_manifest_drift_fails(self):
        # core/manifest.json lists a file that the root manifest does not.
        path = os.path.join(self.root, "core", "manifest.json")
        with open(path, "r", encoding="utf-8") as f:
            manifest = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(manifest.replace("BepInEx.dll", "BepInEx2.dll"))
        synth.rebuild_sums(self.root)
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("root manifest does not", str(ctx.exception))

    def test_non_assembly_bootstrap_rejected(self):
        path = os.path.join(self.root, assets.BOOTSTRAP_REL)
        with open(path, "wb") as f:
            f.write(b"not an assembly")
        synth.rebuild_sums(self.root)
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(self.root)
        self.assertIn("not a .NET PE assembly", str(ctx.exception))

    def test_missing_root_fails_with_actionable_error(self):
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.load_asset_pack(os.path.join(self.tmpdir, "no-such-pack"))
        self.assertIn("not found", str(ctx.exception))

    def test_looks_like_pack(self):
        self.assertTrue(assets.looks_like_pack(self.root))
        self.assertFalse(assets.looks_like_pack(os.path.join(self.tmpdir, "nope")))
        self.assertFalse(assets.looks_like_pack(REPO))


class AssembleTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile

        self.tmpdir = tempfile.mkdtemp(prefix="assemble-test-")
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        self.repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(os.path.join(self.repo, "payload", "config"))
        synth.build_core_dir(os.path.join(self.repo, "out", "core"))
        for name in assets.PACKAGING_FILES:
            src = os.path.join(REPO, *name.split("/"))
            os.makedirs(os.path.dirname(os.path.join(self.repo, name)), exist_ok=True)
            shutil.copy2(src, os.path.join(self.repo, name))
        for source in set(assets.SUPPORT_FILES.values()):
            dest = os.path.join(self.repo, *source.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(REPO, *source.split("/")), dest)
        os.makedirs(os.path.join(self.repo, "tests"), exist_ok=True)
        with open(os.path.join(self.repo, "project.json"), "w") as f:
            f.write('{"version": "9.9.9-test"}\n')
        with open(os.path.join(self.repo, "payload", "config", "BepInEx.cfg"), "w") as f:
            f.write("[Preloader]\n")
        synth.write(os.path.join(self.tmpdir, "boot.dll"), synth.fake_dll("Be5Bootstrap"))
        synth.write(os.path.join(self.tmpdir, "libbe5hook.so"), synth.fake_shim())

    def test_assemble_roundtrip(self):
        out = os.path.join(self.tmpdir, "pack")
        pack = assets.assemble_asset_pack(
            out,
            self.repo,
            os.path.join(self.repo, "out", "core"),
            os.path.join(self.tmpdir, "boot.dll"),
            os.path.join(self.tmpdir, "libbe5hook.so"),
            repository="https://example.invalid/repo",
        )
        self.assertEqual(pack["version"], "9.9.9-test")
        self.assertEqual(pack["source"]["repository"], "https://example.invalid/repo")
        # The finished pack validates through the normal loader.
        again = assets.load_asset_pack(out)
        self.assertEqual(again["version"], "9.9.9-test")
        # Packaging files are byte-identical to the repository sources.
        for name in assets.PACKAGING_FILES:
            with open(os.path.join(self.repo, name), "rb") as f:
                src = f.read()
            with open(os.path.join(out, "packaging", name), "rb") as f:
                dst = f.read()
            self.assertEqual(src, dst, name)

    def test_documentation_links_and_license_bytes_survive_assembly(self):
        import re
        from pathlib import Path

        out = os.path.join(self.tmpdir, "documented-pack")
        revision = "a" * 40
        assets.assemble_asset_pack(
            out, self.repo, os.path.join(self.repo, "out", "core"),
            os.path.join(self.tmpdir, "boot.dll"), os.path.join(self.tmpdir, "libbe5hook.so"),
            repository="https://example.invalid/repo", commit=revision,
        )
        root = Path(out)
        sums = assets.read_sums(str(root / assets.ROOT_SUMS_REL))
        for dest, source in assets.SUPPORT_FILES.items():
            self.assertIn(dest, sums)
            if dest not in assets.DOCUMENT_FILES:
                self.assertEqual((root / dest).read_bytes(), Path(self.repo, source).read_bytes())
        for name in (*assets.DOCUMENT_FILES, "README.md"):
            for target in re.findall(r"\]\(([^)]+)\)", (root / name).read_text()):
                if not re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                    self.assertTrue((root / target.split("#")[0]).exists(), (name, target))
        changes = (root / "CODE_CHANGES.md").read_text()
        self.assertIn("(packaging/build.py)", changes)
        self.assertIn("https://example.invalid/repo/blob/" + revision + "/src/", changes)
        self.assertIn("https://example.invalid/repo/tree/" + revision + "/tests/", changes)
        with (root / "PACKAGING.md").open("a") as f:
            f.write("modified")
        with self.assertRaisesRegex(assets.AssetPackError, "file hash mismatch: PACKAGING.md"):
            assets.load_asset_pack(out)

    def test_assembly_requires_docs_and_licenses_before_creating_output(self):
        from pathlib import Path

        for source in ("PACKAGING.md", "CREDITS.md", "LICENSE", "licenses/Mono.Cecil.txt"):
            with self.subTest(source=source):
                path = Path(self.repo, source)
                original = path.read_bytes()
                path.unlink()
                out = os.path.join(self.tmpdir, "incomplete-pack")
                try:
                    with self.assertRaisesRegex(assets.AssetPackError, "documentation or license missing"):
                        assets.assemble_asset_pack(
                            out, self.repo, os.path.join(self.repo, "out", "core"),
                            os.path.join(self.tmpdir, "boot.dll"), os.path.join(self.tmpdir, "libbe5hook.so"),
                        )
                    self.assertFalse(os.path.exists(out))
                    self.assertFalse(os.path.exists(out + ".tmp"))
                finally:
                    path.write_bytes(original)

    def test_local_pack_documents_do_not_link_to_absent_source(self):
        text = "[tool](build.py) [source](src/Example.cs) [license](LICENSE)"
        result = assets.pack_document(text, self.repo, None, "unknown")
        self.assertEqual(result, "[tool](packaging/build.py) source [license](LICENSE)")

    def test_assemble_refuses_existing_output(self):
        out = os.path.join(self.tmpdir, "pack")
        assets.assemble_asset_pack(
            out,
            self.repo,
            os.path.join(self.repo, "out", "core"),
            os.path.join(self.tmpdir, "boot.dll"),
            os.path.join(self.tmpdir, "libbe5hook.so"),
        )
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.assemble_asset_pack(
                out,
                self.repo,
                os.path.join(self.repo, "out", "core"),
                os.path.join(self.tmpdir, "boot.dll"),
                os.path.join(self.tmpdir, "libbe5hook.so"),
            )
        self.assertIn("already exists", str(ctx.exception))

    def test_assemble_rejects_arm64_shim(self):
        synth.write(os.path.join(self.tmpdir, "arm64.so"), synth.fake_shim(elf_class=2, machine=183))
        with self.assertRaises(assets.AssetPackError) as ctx:
            assets.assemble_asset_pack(
                os.path.join(self.tmpdir, "pack2"),
                self.repo,
                os.path.join(self.repo, "out", "core"),
                os.path.join(self.tmpdir, "boot.dll"),
                os.path.join(self.tmpdir, "arm64.so"),
            )
        self.assertIn("not ELF32", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
