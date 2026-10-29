"""Synthetic asset-pack and APK builders shared by the tests.

Everything here is fake but structurally valid: DLLs are MZ/BSJB blobs,
the shim is a minimal ELF32-ARM header, the APK is a small zip with the
entries build.py's inspection code expects. No game data, no real tools.
"""

import hashlib
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import assets  # noqa: E402

CORE_FILES = assets.CORE_FILES
VERSION = "5.4.23.5"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def fake_dll(tag):
    body = tag.encode("utf-8") if isinstance(tag, str) else tag
    blob = b"MZ" + body + b"\x00" * 60 + b"BSJB" + b"\x00" * 40 + body * 3
    return blob


def fake_shim(elf_class=1, machine=40):
    head = bytearray(128)
    head[0:4] = b"\x7fELF"
    head[4] = elf_class
    head[5] = 1  # little-endian
    head[16:18] = (3).to_bytes(2, "little")  # e_type = ET_DYN
    head[18:20] = machine.to_bytes(2, "little")
    return bytes(head)


def fake_dex():
    return b"dex\n035\x00" + b"\x00" * 200


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)


def build_core_dir(core_dir):
    """A structurally valid out/core stand-in with manifest + SHA256SUMS."""
    entries = []
    for name in CORE_FILES:
        if name.endswith(".dll"):
            data = fake_dll(name)
        else:
            data = b"<dllmap dll=\"libc.so.6\" target=\"libc.so\" os=\"linux,android\"/>\n"
        write(os.path.join(core_dir, name), data)
        entries.append(
            {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    with open(os.path.join(core_dir, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
        for item in sorted(entries, key=lambda e: e["name"]):
            f.write("%s  %s\n" % (item["sha256"], item["name"]))
    manifest = {
        "schema": 1,
        "kind": "bepinex-core",
        "source": {"version": VERSION, "revision": COMMIT},
        "files": sorted(entries, key=lambda e: e["name"]),
    }
    write(
        os.path.join(core_dir, "manifest.json"),
        json.dumps(manifest, indent=2) + "\n",
    )
    return core_dir


def build_pack(root, packaging_source=None, version=VERSION, commit=COMMIT):
    """Build a complete synthetic asset pack at `root`.

    packaging_source: copy real repository tool files into packaging/
    (needed when build.py is run from inside the pack); otherwise tiny
    placeholder content is written.
    """
    build_core_dir(os.path.join(root, "core"))
    write(os.path.join(root, assets.BOOTSTRAP_REL), fake_dll("Be5Bootstrap"))
    write(
        os.path.join(root, assets.CONFIG_REL),
        "[Preloader]\nApplyRuntimePatches = false\nHarmonyBackend = cecil\n",
    )
    write(os.path.join(root, assets.SHIM_REL), fake_shim())
    for name in assets.PACKAGING_FILES:
        if packaging_source:
            with open(os.path.join(packaging_source, *name.split("/")), "rb") as f:
                data = f.read()
        else:
            data = b"# synthetic pack tool placeholder: " + name.encode() + b"\n"
        write(os.path.join(root, "packaging", *name.split("/")), data)
    write(os.path.join(root, assets.PACK_README_REL), "# synthetic pack\n")
    write(
        os.path.join(root, assets.BUILD_INFO_REL),
        json.dumps(
            {
                "schema": 1,
                "kind": assets.PACK_KIND,
                "abi": assets.ABI,
                "version": version,
                "built_at": "2026-01-01T00:00:00+00:00",
                "source": {"commit": commit, "ref": "vX"},
            },
            indent=2,
        )
        + "\n",
    )
    rebuild_sums(root)
    return root


def rebuild_sums(root):
    """Recompute the root SHA256SUMS from the files on disk."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel == assets.ROOT_SUMS_REL:
                continue
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            entries.append((rel, digest))
    with open(os.path.join(root, assets.ROOT_SUMS_REL), "w", encoding="utf-8", newline="\n") as f:
        for name, digest in sorted(entries):
            f.write("%s  %s\n" % (digest, name))


def rewrite_sums(root, lines):
    """Replace the root SHA256SUMS with raw lines (failure-case tests)."""
    with open(os.path.join(root, assets.ROOT_SUMS_REL), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def sums_lines(root):
    with open(os.path.join(root, assets.ROOT_SUMS_REL), "r", encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def build_apk(path):
    """A minimal synthetic Unity arm32 APK."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("classes.dex", fake_dex())
        z.writestr("lib/armeabi-v7a/libmain.so", fake_shim())
        z.writestr("lib/armeabi-v7a/libunity.so", fake_shim())
        z.writestr(
            "assets/bin/Data/Managed/Assembly-CSharp.dll",
            fake_dll("Assembly-CSharp"),
        )
        z.writestr("AndroidManifest.xml", b"\x00\x01 binary manifest placeholder")
        z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
        z.writestr("META-INF/CERT.SF", b"signature file")
        z.writestr("META-INF/CERT.RSA", b"\x30\x82 fake signature block")
        z.writestr("META-INF/CERT.DSA", b"fake DSA signature")
        z.writestr("META-INF/CERT.EC", b"fake EC signature")
        z.writestr("META-INF/NOTICE.txt", b"notice data\n")
        z.writestr("META-INF/services/com.example.Plugin", b"com.example.Impl\n")
    return path
