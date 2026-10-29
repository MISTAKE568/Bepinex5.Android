"""Exercise the production asset copier against files and failing Android asset I/O."""

import ctypes
import errno
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

ASSET_HEADER = r'''
#ifndef TEST_ANDROID_ASSET_MANAGER_H
#define TEST_ANDROID_ASSET_MANAGER_H
#include <sys/types.h>
#include <stddef.h>
typedef struct AAssetManager AAssetManager;
typedef struct AAsset AAsset;
#define AASSET_MODE_STREAMING 2
AAsset *AAssetManager_open(AAssetManager *, const char *, int);
int AAsset_read(AAsset *, void *, size_t);
off64_t AAsset_getLength64(AAsset *);
off64_t AAsset_seek64(AAsset *, off64_t, int);
void AAsset_close(AAsset *);
#endif
'''

ASSET_STUB = r'''
#include <android/asset_manager.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

struct AAsset { FILE *file; off64_t length; size_t read; };
static int failure;
void test_set_failure(int value) { failure = value; }

AAsset *AAssetManager_open(AAssetManager *manager, const char *path, int mode) {
    (void)manager;
    (void)mode;
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    AAsset *asset = calloc(1, sizeof(*asset));
    if (!asset) { fclose(file); return NULL; }
    asset->file = file;
    fseek(file, 0, SEEK_END);
    asset->length = ftell(file);
    rewind(file);
    return asset;
}
int AAsset_read(AAsset *asset, void *buffer, size_t count) {
    if (failure == 1 && asset->read >= 8192) return -1;
    if (failure == 2 && asset->read >= 8192) return 0;
    if (failure == 8) return -1;
    int actual = (int)fread(buffer, 1, count, asset->file);
    asset->read += (size_t)actual;
    return actual;
}
off64_t AAsset_getLength64(AAsset *asset) { return asset->length; }
off64_t AAsset_seek64(AAsset *asset, off64_t offset, int whence) {
    asset->read = 0;
    return fseek(asset->file, (long)offset, whence) == 0 ? ftell(asset->file) : -1;
}
void AAsset_close(AAsset *asset) { fclose(asset->file); free(asset); }
size_t test_fwrite(const void *buffer, size_t size, size_t count, FILE *file) {
    if (failure == 3) { errno = ENOSPC; return 0; }
    return fwrite(buffer, size, count, file);
}
int test_fflush(FILE *file) {
    if (failure == 4) { errno = ENOSPC; return EOF; }
    return fflush(file);
}
int test_fsync(int fd) {
    if (failure == 5) { errno = EIO; return -1; }
    return fsync(fd);
}
int test_fclose(FILE *file) {
    int result = fclose(file);
    if (failure == 6) { errno = EIO; return EOF; }
    return result;
}
int test_rename(const char *source, const char *destination) {
    if (failure == 7) { errno = EACCES; return -1; }
    return rename(source, destination);
}
'''


class AssetCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which(os.environ.get("CC", "cc"))
        if not compiler:
            raise unittest.SkipTest("asset-copy tests require a C compiler")
        cls.workspace = tempfile.TemporaryDirectory(prefix="be5-copy-tests-")
        cls.addClassCleanup(cls.workspace.cleanup)
        root = Path(cls.workspace.name)
        (root / "android").mkdir()
        (root / "android/asset_manager.h").write_text(ASSET_HEADER)
        (root / "asset_stub.c").write_text(ASSET_STUB)
        options = [compiler, "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror",
                   "-fPIC", "-I", str(root), "-I", str(ROOT / "shim")]
        subprocess.run(options + ["-Dfwrite=test_fwrite", "-Dfflush=test_fflush",
                       "-Dfsync=test_fsync", "-Dfclose=test_fclose", "-Drename=test_rename",
                       "-c", str(ROOT / "shim/asset_copy.c"), "-o", str(root / "copy.o")],
                       check=True, capture_output=True, text=True)
        library = root / "asset_copy.so"
        subprocess.run(options + ["-shared", str(root / "copy.o"), str(root / "asset_stub.c"),
                                 "-o", str(library)], check=True, capture_output=True, text=True)
        cls.library = ctypes.CDLL(str(library), use_errno=True)
        cls.library.be5_copy_asset_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                   ctypes.c_char_p, ctypes.c_int]
        cls.library.be5_copy_asset_file.restype = ctypes.c_int
        cls.library.test_set_failure.argtypes = [ctypes.c_int]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="be5-copy-case-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "asset"
        self.destination = self.root / "app/BepInEx/core/runtime.dll"
        self.library.test_set_failure(0)

    def copy(self, preserve=False):
        ctypes.set_errno(0)
        return self.library.be5_copy_asset_file(None, os.fsencode(self.source),
                                                os.fsencode(self.destination), preserve)

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.rglob("*.tmp.*")), [])

    def test_create_update_and_repair_bundled_files(self):
        for data in (b"initial core", b"updated core", b"longer managed assembly" * 1000, b""):
            with self.subTest(length=len(data)):
                self.source.write_bytes(data)
                self.assertEqual(self.copy(), 1)
                self.assertEqual(self.destination.read_bytes(), data)
                self.assert_no_temporary_files()
        self.source.write_bytes(b"complete assembly")
        self.assertEqual(self.copy(), 1)
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())

    def test_unchanged_files_keep_their_inode_and_timestamp(self):
        self.source.write_bytes(b"unchanged" * 3000)
        self.assertEqual(self.copy(), 1)
        os.utime(self.destination, ns=(1_000_000_000, 2_000_000_000))
        before = self.destination.stat()
        self.assertEqual(self.copy(), 0)
        after = self.destination.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assert_no_temporary_files()

    def test_preserved_files_are_only_created_when_missing(self):
        for directory in ("config", "plugins", "patchers"):
            with self.subTest(directory=directory):
                self.destination = self.root / "app/BepInEx" / directory / "user-file"
                self.source.write_bytes(b"bundled default")
                self.assertEqual(self.copy(preserve=True), 1)
                self.destination.write_bytes(b"user changes")
                self.assertEqual(self.copy(preserve=True), 0)
                self.assertEqual(self.destination.read_bytes(), b"user changes")
                self.assert_no_temporary_files()

    def test_failed_reads_and_writes_keep_the_previous_file(self):
        for failure in range(1, 9):
            with self.subTest(failure=failure):
                self.source.write_bytes(b"new asset" * 3000)
                self.destination.parent.mkdir(parents=True, exist_ok=True)
                self.destination.write_bytes(b"previous installation")
                self.library.test_set_failure(failure)
                self.assertEqual(self.copy(), -1)
                self.assertNotEqual(ctypes.get_errno(), 0)
                self.assertEqual(self.destination.read_bytes(), b"previous installation")
                self.assert_no_temporary_files()

    def test_failed_first_install_leaves_no_partial_file(self):
        self.source.write_bytes(b"assembly data" * 3000)
        for failure in range(1, 9):
            with self.subTest(failure=failure):
                self.library.test_set_failure(failure)
                self.assertEqual(self.copy(), -1)
                self.assertFalse(self.destination.exists())
                self.assert_no_temporary_files()

    def test_comparison_read_error_does_not_replace_existing_file(self):
        self.source.write_bytes(b"new contents")
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"old contents")
        self.library.test_set_failure(8)
        self.assertEqual(self.copy(), -1)
        self.assertEqual(self.destination.read_bytes(), b"old contents")
        self.assert_no_temporary_files()

    def test_missing_asset_keeps_the_previous_file(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"installed")
        self.assertEqual(self.copy(), -1)
        self.assertEqual(ctypes.get_errno(), errno.ENOENT)
        self.assertEqual(self.destination.read_bytes(), b"installed")
        self.assert_no_temporary_files()

    def test_invalid_destination_fails_without_overwriting(self):
        self.source.write_bytes(b"asset")
        self.destination.parent.mkdir(parents=True)
        self.destination.mkdir()
        self.assertEqual(self.copy(), -1)
        self.assertTrue(self.destination.is_dir())
        self.destination.rmdir()
        self.destination.parent.rmdir()
        self.destination.parent.write_bytes(b"not a directory")
        self.assertEqual(self.copy(), -1)
        self.assertEqual(self.destination.parent.read_bytes(), b"not a directory")
        self.assert_no_temporary_files()
