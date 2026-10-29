import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEX_DIR = os.path.join(REPO, "injector", "dex")
if DEX_DIR not in sys.path:
    sys.path.insert(0, DEX_DIR)

import patch_dex  # noqa: E402
import verify_dex  # noqa: E402


PLAYER = "com/unity3d/player/UnityPlayer.smali"
ACTIVITY = "com/unity3d/player/UnityPlayerActivity.smali"


def method(body):
    return (
        ".method public static loadNative(Ljava/lang/String;)Ljava/lang/String;\n"
        + body
        + "\n.end method\n"
    )


class DexInjectionTests(unittest.TestCase):
    def test_patch_rejects_loadnative_without_supported_native_load(self):
        with self.assertRaisesRegex(SystemExit, "System.load.*NativeLoader.load"):
            patch_dex.inject_into_loadnative(method("    return-object v0"))

    def test_patch_rejects_system_load_without_merge_label(self):
        body = "\n".join(
            [
                "    invoke-static {v0}, Ljava/lang/System;->load(Ljava/lang/String;)V",
                "    return-object v0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "no merge label"):
            patch_dex.inject_into_loadnative(method(body))

    def test_patch_rejects_native_loader_without_success_check(self):
        body = "\n".join(
            [
                "    invoke-static {v0}, Lcom/unity3d/player/NativeLoader;->load(Ljava/lang/String;)Z",
                "    return-object v0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "no if-eqz success check"):
            patch_dex.inject_into_loadnative(method(body))

    def test_verify_rejects_missing_injection(self):
        with tempfile.TemporaryDirectory() as root:
            player = os.path.join(root, PLAYER)
            activity = os.path.join(root, ACTIVITY)
            os.makedirs(os.path.dirname(player))
            with open(player, "w", encoding="utf-8") as f:
                f.write(method("    return-object v0"))
            with open(activity, "w", encoding="utf-8") as f:
                f.write(".class public Lcom/unity3d/player/UnityPlayerActivity;\n")
            with self.assertRaises(SystemExit):
                verify_dex.check_tree(root)

    def test_verify_rejects_injection_inside_try_range(self):
        body = "\n".join(
            [
                "    :try_start_0",
                "    invoke-static {v0}, Ljava/lang/System;->load(Ljava/lang/String;)V",
                "    const-string v1, \"be5hook\"",
                "    invoke-static {v1}, Ljava/lang/System;->loadLibrary(Ljava/lang/String;)V",
                "    :try_end_0",
                "    return-object v0",
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            player = os.path.join(root, PLAYER)
            os.makedirs(os.path.dirname(player))
            with open(player, "w", encoding="utf-8") as f:
                f.write(method(body))
            with self.assertRaises(SystemExit) as failure:
                verify_dex.check_tree(root)
            self.assertEqual(failure.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
