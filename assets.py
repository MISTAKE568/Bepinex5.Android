#!/usr/bin/env python3
"""BepInEx 5 Android asset pack: layout contract, verification and assembly.

This module is the single authority for the asset-pack layout shared by

  * CI (``assets.py assemble``) — builds the pack from CI outputs,
  * the packager (``build.py --assets``) — validates the pack before
    touching the APK,
  * the tests (``tests/test_asset_pack.py``).

The layout (all paths relative to the pack root, forward slashes only):

    packaging/build.py                 user-side packager entry point
    packaging/assets.py                this contract module
    packaging/injector/apk/repack.py   APK repacker
    packaging/injector/dex/patch_dex.py
    packaging/injector/dex/verify_dex.py
    packaging/injector/hooks/patch_hooks.py
    core/<runtime assemblies>.dll      BepInEx core (see build_core.py)
    core/BepInEx.Preloader.dll.config
    core/manifest.json                 build_core.py core manifest
    core/SHA256SUMS                    per-core hash list
    bootstrap/Be5Bootstrap.dll         netstandard2.0 bootstrap
    config/BepInEx.cfg                 default BepInEx configuration
    shim/armeabi-v7a/libbe5hook.so     ELF32/ARM native shim
    README.md                          user-facing quick instructions
    PACKAGING.md, CREDITS.md           user guide and acknowledgements
    CODE_CHANGES.md                    file-level source notes
    LICENSE, licenses/                 project and dependency license texts
    build-info.json                    version / commit / toolchain info
    SHA256SUMS                         root manifest: EVERY file above

The root SHA256SUMS covers every file except itself. Every entry must
exist and hash-match; every required path above must be listed; files
physically present but absent from the manifest are rejected. Manifest
paths must be relative, forward-slash separated, free of ``.``/``..``
components, drive letters and backslashes; duplicate entries are
rejected.

Exit codes: 0 ok, 1 validation/assembly failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote

ROOT = os.path.dirname(os.path.abspath(__file__))

ABI = "armeabi-v7a"
SHIM_REL = "shim/%s/libbe5hook.so" % ABI
BOOTSTRAP_REL = "bootstrap/Be5Bootstrap.dll"
CONFIG_REL = "config/BepInEx.cfg"
CORE_DIR_REL = "core"
BUILD_INFO_REL = "build-info.json"
ROOT_SUMS_REL = "SHA256SUMS"
PACK_README_REL = "README.md"

# The runtime core set staged by build_core.py. Imported when running from
# the repository; the literal fallback keeps this module self-sufficient
# inside the distributed pack (where build_core.py is absent by design).
try:
    from build_core import CORE_FILES
except ImportError:  # pack-side: build_core.py is intentionally not shipped
    CORE_FILES = (
        "BepInEx.dll",
        "BepInEx.Preloader.dll",
        "BepInEx.Preloader.dll.config",
        "BepInEx.Harmony.dll",
        "0Harmony.dll",
        "0Harmony20.dll",
        "HarmonyXInterop.dll",
        "Mono.Cecil.dll",
        "MonoMod.RuntimeDetour.dll",
        "MonoMod.Utils.dll",
    )

# Packaging tool files copied verbatim from the repository into the pack.
PACKAGING_FILES = (
    "build.py",
    "assets.py",
    "injector/apk/repack.py",
    "injector/dex/patch_dex.py",
    "injector/dex/verify_dex.py",
    "injector/hooks/patch_hooks.py",
)

DOCUMENT_FILES = (
    "PACKAGING.md",
    "CODE_CHANGES.md",
    "CREDITS.md",
)

# Keep license texts verbatim; source-only documentation links are rewritten for the pack.
SUPPORT_FILES = {name: name for name in DOCUMENT_FILES}
SUPPORT_FILES.update({
    "LICENSE": "LICENSE",
    "licenses/BepInEx.txt": "src/BepInEx/LICENSE",
    "licenses/BepInEx.Harmony.txt": "src/BepInEx/submodules/BepInEx.Harmony/LICENSE",
    "licenses/HarmonyX.txt": "licenses/HarmonyX.txt",
    "licenses/MonoMod.txt": "licenses/MonoMod.txt",
    "licenses/MonoMod.Common.txt": "licenses/MonoMod.Common.txt",
    "licenses/Mono.Cecil.txt": "licenses/Mono.Cecil.txt",
    "licenses/SOURCES.txt": "licenses/SOURCES.txt",
})

PACK_KIND = "bep5-android-asset-pack"
PACK_SCHEMA = 1

ELF_MAGIC = b"\x7fELF"
ELFCLASS32 = 1
ELFDATA2LSB = 1
EM_ARM = 40


class AssetPackError(RuntimeError):
    """A user-facing asset-pack validation or assembly failure."""


def log(msg):
    print("[assets] " + msg, flush=True)


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def expand(path):
    if path is None:
        return None
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


# ---------------------------------------------------------------------------
# Safe manifest parsing
# ---------------------------------------------------------------------------

def safe_rel_path(name):
    """True if `name` is a safe relative pack path (see module docstring)."""
    if not name or name != name.strip():
        return False
    if "\\" in name:
        return False
    if name.startswith("/"):
        return False
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        return False
    parts = name.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return False
        if any(c in part for c in "\x00\r\n"):
            return False
    return True


def read_sums(path):
    """Parse a SHA256SUMS manifest into {relpath: digest}.

    Rejects malformed lines, invalid digests, unsafe paths and duplicates.
    """
    sums = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise AssetPackError(
                    "%s:%d: expected '<sha256>  <path>'" % (path, line_no)
                )
            digest, name = parts[0].lower(), parts[1].strip().lstrip("*")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise AssetPackError("%s:%d: invalid SHA-256" % (path, line_no))
            if not safe_rel_path(name):
                raise AssetPackError(
                    "%s:%d: unsafe manifest path: %r" % (path, line_no, name)
                )
            if name in sums:
                raise AssetPackError("%s:%d: duplicate manifest entry: %s" % (path, line_no, name))
            sums[name] = digest
    return sums


def write_sums(path, entries):
    """Write a SHA256SUMS manifest; entries is [(relpath, digest)]."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for name, digest in sorted(entries):
            f.write("%s  %s\n" % (digest, name))


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Payload checks
# ---------------------------------------------------------------------------

def check_assembly(path):
    """A .NET assembly must be a PE file carrying CLR metadata (BSJB)."""
    with open(path, "rb") as f:
        blob = f.read()
    if not blob.startswith(b"MZ"):
        raise AssetPackError("not a .NET PE assembly: " + path)
    if b"BSJB" not in blob:
        raise AssetPackError("missing .NET metadata (BSJB): " + path)


def check_elf32_arm(path):
    """The shim must be a little-endian ELF32 ARM shared object."""
    with open(path, "rb") as f:
        head = f.read(20)
    if len(head) < 20 or head[:4] != ELF_MAGIC:
        raise AssetPackError("not an ELF shared library: " + path)
    if head[4] != ELFCLASS32:
        raise AssetPackError("shim is not ELF32 (arm64 build?): " + path)
    if head[5] != ELFDATA2LSB:
        raise AssetPackError("shim is not little-endian: " + path)
    machine = int.from_bytes(head[18:20], "little")
    if machine != EM_ARM:
        raise AssetPackError(
            "shim ELF machine %d is not ARM (%d): %s" % (machine, EM_ARM, path)
        )


def read_json_file(path, what):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except OSError as exc:
        raise AssetPackError("cannot read %s %s: %s" % (what, path, exc)) from exc
    except ValueError as exc:
        raise AssetPackError("invalid JSON in %s %s: %s" % (what, path, exc)) from exc


# ---------------------------------------------------------------------------
# Pack verification
# ---------------------------------------------------------------------------

def required_paths():
    """Every path that must be listed in the root manifest.

    (The root SHA256SUMS itself is checked as a file, not as an entry —
    a manifest cannot contain its own hash.)
    """
    paths = [
        BUILD_INFO_REL,
        PACK_README_REL,
        BOOTSTRAP_REL,
        CONFIG_REL,
        SHIM_REL,
        "core/manifest.json",
        "core/SHA256SUMS",
    ]
    for name in PACKAGING_FILES:
        paths.append("packaging/" + name)
    for name in CORE_FILES:
        paths.append(CORE_DIR_REL + "/" + name)
    return tuple(paths)


def load_asset_pack(pack_root):
    """Fully validate an asset pack directory and return its description.

    Raises AssetPackError on any layout, integrity or safety problem.
    """
    root = expand(pack_root)
    if not root or not os.path.isdir(root):
        raise AssetPackError("asset pack directory not found: %s" % pack_root)

    root_real = os.path.realpath(root)
    sums_path = os.path.join(root, ROOT_SUMS_REL)
    if not os.path.isfile(sums_path):
        raise AssetPackError(
            "%s not found: %s\n  the asset pack is incomplete or corrupt; "
            "re-download and re-extract it" % (ROOT_SUMS_REL, sums_path)
        )
    sums = read_sums(sums_path)

    missing = [p for p in required_paths() if p not in sums]
    if missing:
        raise AssetPackError(
            "asset pack is missing required files:\n  " + "\n  ".join(missing)
        )

    # Every manifest entry must exist, stay inside the pack and hash-match.
    files = []
    for name, digest in sorted(sums.items()):
        path = os.path.join(root, *name.split("/"))
        if not os.path.isfile(path):
            raise AssetPackError("manifest file missing on disk: " + name)
        # Resolve symlinks: the target must still live inside the pack.
        resolved = os.path.realpath(path)
        if not resolved.startswith(root_real + os.sep):
            raise AssetPackError("manifest file escapes the pack: " + name)
        actual = sha256_file(path)
        if actual != digest:
            raise AssetPackError(
                "file hash mismatch: %s\n  expected %s\n  actual   %s"
                % (name, digest, actual)
            )
        files.append({"name": name, "size": os.path.getsize(path), "sha256": actual})

    # No unmanifested files (catches partial extraction, __MACOSX, .DS_Store).
    extra = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if rel == ROOT_SUMS_REL:
                continue
            if rel not in sums:
                extra.append(rel)
    if extra:
        raise AssetPackError(
            "files present in the pack but not listed in %s:\n  %s\n  "
            "remove them and retry (or re-extract the archive)"
            % (ROOT_SUMS_REL, "\n  ".join(sorted(extra)))
        )

    check_assembly(os.path.join(root, *BOOTSTRAP_REL.split("/")))
    check_elf32_arm(os.path.join(root, *SHIM_REL.split("/")))
    for name in CORE_FILES:
        if name.endswith(".dll"):
            check_assembly(os.path.join(root, CORE_DIR_REL, *name.split("/")))

    # build-info.json: identity and toolchain provenance.
    info = read_json_file(os.path.join(root, BUILD_INFO_REL), "pack metadata")
    if not isinstance(info, dict):
        raise AssetPackError(BUILD_INFO_REL + " must be a JSON object")
    if info.get("kind") != PACK_KIND:
        raise AssetPackError(
            "unexpected build-info kind %r (expected %r)" % (info.get("kind"), PACK_KIND)
        )
    if info.get("schema") != PACK_SCHEMA:
        raise AssetPackError(
            "unsupported build-info schema %r (expected %r)"
            % (info.get("schema"), PACK_SCHEMA)
        )
    if info.get("abi") != ABI:
        raise AssetPackError(
            "asset pack ABI %r does not match this packager (%s)" % (info.get("abi"), ABI)
        )

    # core/ inner manifest must agree with the outer manifest.
    core_manifest = read_json_file(
        os.path.join(root, CORE_DIR_REL, "manifest.json"), "core manifest"
    )
    if core_manifest.get("kind") != "bepinex-core":
        raise AssetPackError("unexpected core manifest kind in asset pack")
    core_names = [
        item.get("name")
        for item in (core_manifest.get("files") or [])
        if isinstance(item, dict)
    ]
    if not core_names or not all(core_names):
        raise AssetPackError("invalid files list in asset pack core manifest")
    for name in core_names:
        if CORE_DIR_REL + "/" + name not in sums:
            raise AssetPackError(
                "core manifest lists %s but the root manifest does not" % name
            )

    return {
        "root": root,
        "abi": ABI,
        "version": info.get("version"),
        "built_at": info.get("built_at"),
        "source": info.get("source") or {},
        "build": info.get("build") or {},
        "build_info": info,
        "build_info_sha256": sums.get(BUILD_INFO_REL),
        "root_sha256sums": sha256_file(sums_path),
        "files": files,
        "paths": {
            "root": root,
            "core": os.path.join(root, CORE_DIR_REL),
            "bootstrap": os.path.join(root, *BOOTSTRAP_REL.split("/")),
            "config": os.path.join(root, "config"),
            "shim": os.path.join(root, *SHIM_REL.split("/")),
            "packaging": os.path.join(root, "packaging"),
        },
    }


def looks_like_pack(directory):
    """Cheap structural probe used for auto-detection (no hashing)."""
    d = expand(directory)
    if not d or not os.path.isdir(d):
        return False
    for rel in (
        BUILD_INFO_REL,
        ROOT_SUMS_REL,
        BOOTSTRAP_REL,
        CONFIG_REL,
        SHIM_REL,
        "core/SHA256SUMS",
        "packaging/build.py",
    ):
        if not os.path.isfile(os.path.join(d, *rel.split("/"))):
            return False
    return True


# ---------------------------------------------------------------------------
# Pack assembly (CI side)
# ---------------------------------------------------------------------------

def git_revision(cwd):
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, stderr=subprocess.DEVNULL, text=True
        ).strip()
        return out or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def read_project_version(repo):
    data = read_json_file(os.path.join(repo, "project.json"), "project metadata")
    version = data.get("version")
    if not version:
        raise AssetPackError("project.json has no version")
    return version


def _copy_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


PACK_README_TEMPLATE = """\
# Bepinex5.Android — Asset Pack

**Breaking Fork**

BepInEx core, Bootstrap, the Android ARM32 JNI loader and APK packaging scripts.

## Package a game

Prepare Python 3.9+, a JDK, Android SDK build-tools, baksmali/smali JARs and a signing key.
From this extracted directory, run:

    python packaging/build.py --apk /path/to/game.apk --keystore /path/to/debug.keystore --out ./game-bepinex.apk

The script detects this asset pack automatically. Add tool paths as shown in the
[packaging guide](PACKAGING.md). Its source-build examples run from a repository checkout.

## Documentation

- [Packaging guide](PACKAGING.md)
- [Code changes](CODE_CHANGES.md)
- [Credits](CREDITS.md)
- [Project license](LICENSE) and [third-party license sources](licenses/SOURCES.txt)

Source repository: {repository}

Version: {version}
Commit: {commit}
"""


def pack_document(text, repo, repository, commit):
    """Keep bundled links local and point source-only links at the build revision."""
    destinations = {source: dest for dest, source in SUPPORT_FILES.items()}
    destinations.update({name: "packaging/" + name for name in PACKAGING_FILES})
    destinations["README.md"] = "README.md"

    def replace(match):
        label, target = match.groups()
        if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            return match.group(0)
        path, separator, fragment = target.partition("#")
        if path in destinations:
            return "[%s](%s%s%s)" % (label, destinations[path], separator, fragment)
        if repository:
            kind = "tree" if os.path.isdir(os.path.join(repo, path)) else "blob"
            revision = commit if commit and commit != "unknown" else "HEAD"
            url = "%s/%s/%s/%s" % (repository.rstrip("/"), kind,
                                     quote(revision, safe=""), quote(path, safe="/"))
            return "[%s](%s%s%s)" % (label, url, separator, fragment)
        return label

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace, text)


def assemble_asset_pack(
    out_dir,
    repo,
    core_dir,
    bootstrap,
    shim_so,
    version=None,
    commit=None,
    ref=None,
    repository=None,
    build_tools=None,
    config_dir=None,
):
    """Assemble a complete asset pack from CI build outputs.

    Returns the load_asset_pack() description of the finished pack.
    """
    out_dir = expand(out_dir)
    repo = expand(repo)
    core_dir = expand(core_dir)
    bootstrap = expand(bootstrap)
    shim_so = expand(shim_so)
    config_dir = expand(config_dir) if config_dir else os.path.join(repo, "payload", "config")

    if os.path.exists(out_dir):
        raise AssetPackError("output directory already exists: " + out_dir)

    for label, path, want_dir in (
        ("core directory", core_dir, True),
        ("bootstrap assembly", bootstrap, False),
        ("shim shared object", shim_so, False),
        ("config directory", config_dir, True),
    ):
        exists = os.path.isdir(path) if want_dir else os.path.isfile(path)
        if not exists:
            raise AssetPackError("%s not found: %s" % (label, path))
    for name in CORE_FILES:
        if not os.path.isfile(os.path.join(core_dir, name)):
            raise AssetPackError("core is missing %s: %s" % (name, core_dir))
    for name in ("manifest.json", "SHA256SUMS"):
        if not os.path.isfile(os.path.join(core_dir, name)):
            raise AssetPackError("core is missing %s: %s" % (name, core_dir))
    if not os.path.isfile(os.path.join(config_dir, os.path.basename(CONFIG_REL))):
        raise AssetPackError("config is missing BepInEx.cfg: " + config_dir)

    for source in SUPPORT_FILES.values():
        if not os.path.isfile(os.path.join(repo, *source.split("/"))):
            raise AssetPackError("documentation or license missing from repository: " + source)

    check_assembly(bootstrap)
    check_elf32_arm(shim_so)

    version = version or read_project_version(repo)
    commit = commit or git_revision(repo)

    tmp = out_dir + ".tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    try:
        os.makedirs(tmp)

        # core/
        for name in sorted(CORE_FILES) + ["manifest.json", "SHA256SUMS"]:
            _copy_file(os.path.join(core_dir, name), os.path.join(tmp, CORE_DIR_REL, name))

        # bootstrap/, config/, shim/
        _copy_file(bootstrap, os.path.join(tmp, *BOOTSTRAP_REL.split("/")))
        _copy_file(
            os.path.join(config_dir, os.path.basename(CONFIG_REL)),
            os.path.join(tmp, *CONFIG_REL.split("/")),
        )
        _copy_file(shim_so, os.path.join(tmp, *SHIM_REL.split("/")))

        # packaging/ — verbatim from the repository, exact file list.
        for name in PACKAGING_FILES:
            src = os.path.join(repo, *name.split("/"))
            if not os.path.isfile(src):
                raise AssetPackError("packaging source missing from repository: " + src)
            _copy_file(src, os.path.join(tmp, "packaging", *name.split("/")))

        for dest, source in SUPPORT_FILES.items():
            src = os.path.join(repo, *source.split("/"))
            dst = os.path.join(tmp, *dest.split("/"))
            if dest in DOCUMENT_FILES:
                with open(src, encoding="utf-8") as f:
                    text = pack_document(f.read(), repo, repository, commit)
                with open(dst, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
            else:
                _copy_file(src, dst)

        # README.md
        with open(os.path.join(tmp, PACK_README_REL), "w", encoding="utf-8", newline="\n") as f:
            f.write(
                PACK_README_TEMPLATE.format(
                    repository=repository or "(see the project home page)",
                    version=version,
                    commit=commit,
                )
            )

        # build-info.json
        build_info = {
            "schema": PACK_SCHEMA,
            "kind": PACK_KIND,
            "abi": ABI,
            "version": version,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "repository": repository,
                "commit": commit,
                "ref": ref,
            },
            "build": dict(build_tools or {}),
            "layout": {"schema": PACK_SCHEMA},
        }
        write_json(os.path.join(tmp, BUILD_INFO_REL), build_info)

        # Root manifest: every file except SHA256SUMS itself.
        entries = []
        for dirpath, dirnames, filenames in os.walk(tmp):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                abs_path = os.path.join(dirpath, fn)
                rel = os.path.relpath(abs_path, tmp).replace(os.sep, "/")
                entries.append((rel, sha256_file(abs_path)))
        write_sums(os.path.join(tmp, ROOT_SUMS_REL), entries)

        os.replace(tmp, out_dir)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    return load_asset_pack(out_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Asset pack contract: verify or assemble a pack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="validate an existing asset pack directory")
    v.add_argument("pack", help="asset pack root directory")

    a = sub.add_parser("assemble", help="assemble a pack from build outputs (CI)")
    a.add_argument("--out", required=True, help="absent output pack directory")
    a.add_argument("--repo", default=ROOT, help="repository root (packaging sources)")
    a.add_argument("--core", required=True, help="out/core directory from build_core.py")
    a.add_argument("--bootstrap", required=True, help="Be5Bootstrap.dll built by CI")
    a.add_argument("--shim", required=True, help="libbe5hook.so (armeabi-v7a)")
    a.add_argument("--config", help="config directory (default: <repo>/payload/config)")
    a.add_argument("--version", help="override version (default: project.json)")
    a.add_argument("--commit", help="override source commit (default: git HEAD)")
    a.add_argument("--ref", help="ref name recorded in build-info.json")
    a.add_argument("--repository", help="repository URL recorded in build-info.json")
    a.add_argument(
        "--build-tool",
        action="append",
        default=[],
        help="record build tool info as key=value (repeatable)",
    )

    args = ap.parse_args(argv)
    try:
        if args.cmd == "verify":
            pack = load_asset_pack(args.pack)
            log("asset pack OK: " + pack["root"])
            log("version: %s  commit: %s" % (pack.get("version") or "?", (pack.get("source") or {}).get("commit") or "?"))
            log("files:   %d (%s verified)" % (len(pack["files"]), ROOT_SUMS_REL))
            for item in pack["files"]:
                log("  %s  %s" % (item["sha256"][:12], item["name"]))
            return 0

        tools = {}
        for pair in args.build_tool:
            if "=" not in pair:
                raise AssetPackError("--build-tool expects key=value, got: %r" % pair)
            k, _, val = pair.partition("=")
            tools[k] = val
        pack = assemble_asset_pack(
            args.out,
            args.repo,
            args.core,
            args.bootstrap,
            args.shim,
            version=args.version,
            commit=args.commit,
            ref=args.ref,
            repository=args.repository,
            build_tools=tools,
            config_dir=args.config,
        )
        log("assembled asset pack: " + pack["root"])
        log("version: %s  commit: %s" % (pack.get("version") or "?", (pack.get("source") or {}).get("commit") or "?"))
        log("files:   %d" % len(pack["files"]))
        return 0
    except AssetPackError as exc:
        print("[assets] ERROR: " + str(exc), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("[assets] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
