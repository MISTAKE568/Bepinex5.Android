import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

sys.path.insert(0, os.path.join(REPO, "ci"))
import check_source_tree  # noqa: E402


class CheckSourceTreeTests(unittest.TestCase):
    def test_flags_pe_dll(self):
        self.assertIsNotNone(check_source_tree.check_path("evil.dll", b"MZ\x00\x90\x00"))

    def test_flags_elf(self):
        self.assertIsNotNone(check_source_tree.check_path("evil.so", b"\x7fELF\x01\x01\x01\x00"))

    def test_flags_dex(self):
        self.assertIsNotNone(check_source_tree.check_path("evil.dex", b"dex\n035\x00"))

    def test_flags_zip_jar(self):
        self.assertIsNotNone(check_source_tree.check_path("evil.jar", b"PK\x03\x04\x14\x00"))

    def test_flags_java_class(self):
        self.assertIsNotNone(check_source_tree.check_path("evil.class", b"\xca\xfe\xba\xbe\x00\x00"))

    def test_text_starting_with_magic_prefix_is_not_flagged(self):
        # No NUL bytes: a text file may legitimately start with "MZ".
        self.assertIsNone(check_source_tree.check_path("notes.txt", b"MZ notes about mono"))
        self.assertIsNone(check_source_tree.check_path("notes.txt", b"PK notes"))

    def test_plain_text_not_flagged(self):
        self.assertIsNone(check_source_tree.check_path("README.md", b"# hello\n"))
        self.assertIsNone(
            check_source_tree.check_path(
                "BepInEx.Preloader.dll.config", b'<dllmap dll="libc.so.6"/>\n'
            )
        )

    def test_exception_list_blocks_flagging(self):
        head = b"MZ\x00\x90\x00"
        self.assertIsNotNone(check_source_tree.check_path("some/thing.dll", head))
        check_source_tree.EXCEPTIONS["some/thing.dll"] = "documented resource"
        try:
            self.assertIsNone(check_source_tree.check_path("some/thing.dll", head))
        finally:
            del check_source_tree.EXCEPTIONS["some/thing.dll"]


if __name__ == "__main__":
    unittest.main()
