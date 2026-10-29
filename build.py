#!/usr/bin/env python3
"""Build an installable BepInEx 5 Android APK from a stock Unity Mono APK.

This is the single entry point for the whole pipeline:

    hooks -> dex patch + verify -> shim build -> repack -> sign -> report

Everything that can be discovered is auto-detected.  Anything that cannot be
discovered can be supplied explicitly with a command-line flag, an environment
variable, or a JSON config file. `--dry-run` writes the resolved plan to the
work report without modifying the APK or running packaging stages.

Examples
--------
    # Maintainer/CI only: build the BepInEx core asset:
    python build_core.py
    # User packaging consumes a downloaded CI asset pack:
    python build.py --apk game.apk --assets <asset-pack>

    # Explicit toolchain (useful on machines with several SDKs):
    python build.py --apk game.apk \
        --sdk "C:/Android/Sdk" --ndk "C:/Android/Sdk/ndk/27.0.12077973" \
        --baksmali tools/baksmali.jar --smali tools/smali.jar

    # Reuse an already patched dex and an already built shim:
    python build.py --apk game.apk --dex classes.patched.dex --shim libbe5hook.so

    # Inspect the resolved plan only:
    python build.py --apk game.apk --dry-run

    # Build without signing (output is not installable):
    python build.py --apk game.apk --no-sign

The generated `<work>/build.json` records every detected path, input hash,
step result and warning.  See PACKAGING.md for the full description.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone

import assets as asset_pack

ROOT = os.path.dirname(os.path.abspath(__file__))

PATCH_DEX = os.path.join(ROOT, "injector", "dex", "patch_dex.py")
VERIFY_DEX = os.path.join(ROOT, "injector", "dex", "verify_dex.py")
PATCH_HOOKS = os.path.join(ROOT, "injector", "hooks", "patch_hooks.py")
REPACK = os.path.join(ROOT, "injector", "apk", "repack.py")

DEFAULT_CORE = os.path.join(ROOT, "out", "core")
DEFAULT_PAYLOAD = os.path.join(ROOT, "payload")
DEFAULT_BOOT = os.path.join(ROOT, "payload", "bootstrap", "Be5Bootstrap.dll")
DEFAULT_KEYSTORE = os.path.expanduser(os.path.join("~", ".android", "debug.keystore"))
HOOKS_NAME = "HOOKS-Assembly-CSharp.dll"

# The packaging allow-list is exactly the set of files build_core.py stages.
# Import it so the core build and the APK packer cannot drift apart.
try:
    from build_core import CORE_FILES as CORE_REQUIRED
except ImportError:  # pragma: no cover - build_core.py is part of the source tree
    CORE_REQUIRED = (
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

ABI_ELF = {
    "armeabi-v7a": (1, 40),   # ELFCLASS32, EM_ARM
}

SUPPORTED_ABIS = tuple(ABI_ELF)

HINTS = {
    "apk": 'pass --apk <base.apk> (or place exactly one .apk under --input-dir)',
    "core": "pass --core <dir> in advanced override mode, or use --assets <dir>",
    "java": "pass --java <java> or --jdk <jdk-home>, or set JAVA_HOME",
    "baksmali": "pass --baksmali <baksmali.jar>, set BAKSMALI_JAR, or put it in tools/",
    "smali": "pass --smali <smali.jar>, set SMALI_JAR, or put it in tools/",
    "aapt": "pass --build-tools <dir>; only needed to read package/version info",
    "zipalign": "pass --build-tools <dir>, or use --no-sign",
    "apksigner": "pass --build-tools <dir>, or use --no-sign",
    "keystore": "pass --keystore <file>, set BEPINEX_KEYSTORE, or use --no-sign",
    "hookgen": "pass --hookgen <HookGen_net35.exe>, set HOOKGEN, or let optional hooks be skipped",
    "mono": "pass --mono <mono>, set MONO, or let optional hooks be skipped",
    "adb": "pass --adb <adb>; only needed for --install",
}

TOOL_NAMES = {
    "aapt": ("aapt.exe", "aapt"),
    "zipalign": ("zipalign.exe", "zipalign"),
    "apksigner": ("apksigner.bat", "apksigner"),
    "cmake": ("cmake.exe", "cmake"),
    "ninja": ("ninja.exe", "ninja"),
    "java": ("java.exe", "java"),
    "mono": ("mono.exe", "mono"),
    "adb": ("adb.exe", "adb"),
}


class BuildError(RuntimeError):
    """A user-facing build failure with an actionable hint."""


def log(msg):
    print("[build] " + msg, flush=True)


def warn(msg):
    print("[build] WARNING: " + msg, file=sys.stderr, flush=True)


def expand(path):
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def is_file(path):
    return bool(path) and os.path.isfile(expand(path))


def is_dir(path):
    return bool(path) and os.path.isdir(expand(path))


def first_file(*paths):
    for p in paths:
        if is_file(p):
            return expand(p)
    return None


def first_dir(*paths):
    for p in paths:
        if is_dir(p):
            return expand(p)
    return None


def find_named(directory, names):
    """Find one of `names` directly inside `directory` (or with .exe/.bat)."""
    if not is_dir(directory):
        return None
    for name in names:
        for candidate in (name, name + ".exe", name + ".bat"):
            p = os.path.join(expand(directory), candidate)
            if os.path.isfile(p):
                return p
    return None


def which(names):
    for name in names:
        p = shutil.which(name)
        if p:
            return expand(p)
    return None


def version_key(name):
    return tuple(int(x) for x in re.findall(r"\d+", name))


def latest_dir(parent, require=None):
    """Newest version-looking subdirectory, optionally requiring a child file."""
    if not is_dir(parent):
        return None
    dirs = [
        os.path.join(expand(parent), d)
        for d in os.listdir(parent)
        if os.path.isdir(os.path.join(parent, d))
    ]
    dirs.sort(key=lambda p: version_key(os.path.basename(p)), reverse=True)
    for d in dirs:
        if require is None or is_file(os.path.join(d, require)):
            return d
    return None


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_sha256sums(path):
    sums = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise BuildError(
                    "%s:%d: expected '<sha256>  <name>'" % (path, line_no)
                )
            digest, name = parts[0].lower(), parts[1].lstrip("*")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise BuildError("%s:%d: invalid SHA-256" % (path, line_no))
            if name in sums:
                raise BuildError("%s:%d: duplicate entry %s" % (path, line_no, name))
            sums[name] = digest
    return sums


def inspect_core(directory):
    """Validate a build_core.py output directory and return its metadata."""
    directory = expand(directory)
    if not is_dir(directory):
        raise BuildError(
            "core directory not found: %s\n  %s"
            % (directory, HINTS["core"])
        )
    sums_path = os.path.join(directory, "SHA256SUMS")
    if not is_file(sums_path):
        raise BuildError(
            "core/SHA256SUMS not found: %s\n  %s" % (sums_path, HINTS["core"])
        )
    sums = read_sha256sums(sums_path)
    missing = [name for name in CORE_REQUIRED if name not in sums]
    if missing:
        raise BuildError(
            "core/SHA256SUMS is missing: %s\n  %s"
            % (", ".join(missing), HINTS["core"])
        )

    manifest = None
    manifest_path = os.path.join(directory, "manifest.json")
    if is_file(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as exc:
            raise BuildError(
                "cannot read core manifest %s: %s" % (manifest_path, exc)
            ) from exc
        if manifest.get("kind") != "bepinex-core":
            raise BuildError("unexpected core manifest kind: " + manifest_path)
        names = [
            item.get("name")
            for item in (manifest.get("files") or [])
            if isinstance(item, dict)
        ]
        if not all(names):
            raise BuildError("invalid files list in " + manifest_path)
    else:
        names = sorted(sums)
    if len(set(names)) != len(names):
        raise BuildError("duplicate file names in core manifest")
    missing = [name for name in CORE_REQUIRED if name not in names]
    if missing:
        raise BuildError("core manifest is missing: " + ", ".join(missing))
    extra = sorted(set(sums) - set(names))
    if manifest is not None and extra:
        raise BuildError(
            "core/SHA256SUMS has files not in manifest.json: " + ", ".join(extra)
        )

    files = []
    for name in names:
        if os.path.basename(name) != name or name in (".", ".."):
            raise BuildError("unsafe core file name: " + name)
        path = os.path.join(directory, name)
        if not is_file(path):
            raise BuildError("core file missing: " + path)
        actual = sha256_file(path)
        if actual != sums.get(name):
            raise BuildError(
                "core file hash mismatch: %s\n  expected %s\n  actual   %s"
                % (name, sums.get(name), actual)
            )
        files.append({"name": name, "size": os.path.getsize(path), "sha256": actual})

    version = None
    if manifest:
        version = (manifest.get("source") or {}).get("version")
    return {
        "path": directory,
        "version": version,
        "files": files,
        "sha256sums": sha256_file(sums_path),
        "manifest": manifest_path if manifest is not None else None,
    }


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def run(cmd, cwd=None, env=None):
    printable = []
    for arg in cmd:
        text = str(arg)
        if text.startswith("pass:"):
            text = "pass:***"
        printable.append(text)
    log("$ " + " ".join(printable))
    try:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)
    except FileNotFoundError as exc:
        raise BuildError("command not found: %s" % cmd[0]) from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(
            "command failed (%d): %s" % (exc.returncode, " ".join(printable))
        ) from exc


# ---------------------------------------------------------------------------
# APK discovery and inspection
# ---------------------------------------------------------------------------


def find_apk(args):
    if args.apk:
        apk = expand(args.apk)
        if not is_file(apk):
            raise BuildError("APK not found: " + apk)
        return apk, "--apk"

    root = expand(args.input_dir or os.getcwd())
    if not is_dir(root):
        raise BuildError("input directory not found: " + root)

    skip = {
        ".git",
        ".venv",
        "__pycache__",
        "out",
        "node_modules",
        "upstream",
        "refs",
    }
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn.lower().endswith(".apk"):
                found.append(os.path.join(dirpath, fn))
    if len(found) == 1:
        return expand(found[0]), "auto (only .apk under %s)" % root
    if not found:
        raise BuildError(
            "no base APK found under %s; %s" % (root, HINTS["apk"])
        )
    listing = "\n  ".join(found[:20])
    raise BuildError(
        "several APKs found under %s; choose one with --apk:\n  %s" % (root, listing)
    )


def aapt_badging(aapt, apk):
    if not aapt:
        return {}
    try:
        proc = subprocess.run(
            [aapt, "dump", "badging", apk],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:
        warn("aapt failed: %s" % exc)
        return {}
    if proc.returncode != 0:
        warn("aapt dump badging failed (exit %d)" % proc.returncode)
        return {}
    text = proc.stdout
    out = {}
    m = re.search(r"package: name='([^']+)'", text)
    if m:
        out["package"] = m.group(1)
    m = re.search(r"versionName='([^']*)'", text)
    if m:
        out["version"] = m.group(1)
    m = re.search(r"versionCode='([^']*)'", text)
    if m:
        out["version_code"] = m.group(1)
    m = re.search(r"application-label:'([^']*)'", text)
    if m:
        out["label"] = m.group(1)
    m = re.search(r"sdkVersion:'([^']+)'", text)
    if m:
        out["min_sdk"] = m.group(1)
    m = re.search(r"targetSdkVersion:'([^']+)'", text)
    if m:
        out["target_sdk"] = m.group(1)
    m = re.search(r"native-code:([^\n]*)", text)
    if m:
        out["aapt_abis"] = re.findall(r"'([^']+)'", m.group(1))
    m = re.search(r"launchable-activity: name='([^']+)'", text)
    if m:
        out["activity"] = m.group(1)
    return out


def inspect_apk(apk, aapt, abi_arg, package_arg):
    try:
        with zipfile.ZipFile(apk) as z:
            names = z.namelist()
    except zipfile.BadZipFile as exc:
        raise BuildError("not a valid APK/zip: %s (%s)" % (apk, exc)) from exc

    dex_entries = sorted(
        [n for n in names if re.match(r"^classes\d*\.dex$", n)],
        key=lambda n: (len(n), n),
    )
    if "classes.dex" in dex_entries:
        dex_entry = "classes.dex"
    elif dex_entries:
        dex_entry = dex_entries[0]
        warn("no classes.dex; using %s" % dex_entry)
    else:
        raise BuildError(
            "APK has no classes.dex; is this a base APK (not a split/config APK)?"
        )
    if len(dex_entries) > 1:
        warn(
            "APK has several dex files (%s); only %s is patched"
            % (", ".join(dex_entries), dex_entry)
        )

    abis = sorted(
        {
            n.split("/")[1]
            for n in names
            if n.startswith("lib/") and len(n.split("/")) >= 3
        }
    )
    if not abis:
        raise BuildError("APK has no lib/<abi>/ entries; not a native Unity APK")

    if abi_arg:
        abi = abi_arg
        if abi not in abis:
            raise BuildError(
                "requested ABI %s is not in the APK (available: %s)"
                % (abi, ", ".join(abis))
            )
    elif "armeabi-v7a" in abis:
        abi = "armeabi-v7a"
    else:
        raise BuildError(
            "APK has no armeabi-v7a lib/ entries; this build is arm32 only "
            "(available: %s)" % ", ".join(abis)
        )

    managed = sorted(
        n for n in names if n.endswith("/Managed/Assembly-CSharp.dll")
    )
    hooks = sorted(n for n in names if n.endswith("/HOOKS/" + HOOKS_NAME))
    badging = aapt_badging(aapt, apk)

    info = {
        "path": apk,
        "size": os.path.getsize(apk),
        "sha256": sha256_file(apk),
        "abis": abis,
        "abi": abi,
        "abi_source": "--abi" if abi_arg else "auto (APK lib/)",
        "dex_entry": dex_entry,
        "dex_entries": dex_entries,
        "libmain": any(n == "lib/%s/libmain.so" % abi for n in names),
        "managed_prefix": managed[0][: -len("Assembly-CSharp.dll")] if managed else None,
        "assembly_csharp": managed[0] if managed else None,
        "hooks_member": hooks[0] if hooks else None,
        "aapt": badging,
    }
    info["package"] = package_arg or badging.get("package")
    if package_arg:
        info["package_source"] = "--package"
    elif badging.get("package"):
        info["package_source"] = "aapt"
    else:
        info["package_source"] = "unavailable"
    info["version"] = badging.get("version") or ""
    info["label"] = badging.get("label") or ""
    info["min_sdk"] = int(badging.get("min_sdk") or 24)
    if not info["managed_prefix"]:
        warn(
            "APK has no */Managed/Assembly-CSharp.dll; HookGen fallback will be unavailable"
        )
    if not info["libmain"]:
        warn("APK has no lib/%s/libmain.so (unusual Unity layout)" % abi)
    return info


# ---------------------------------------------------------------------------
# Toolchain discovery
# ---------------------------------------------------------------------------


def tools_root(args):
    """Caller-selected tool directory (--tools or BE5_TOOLS, else ./tools)."""
    value = getattr(args, "tools", None) or os.environ.get("BE5_TOOLS")
    if value:
        return expand(value)
    local = os.path.join(ROOT, "tools")
    return local if is_dir(local) else None


def resolve_sdk(args):
    candidates = []
    if args.sdk:
        candidates.append((args.sdk, "--sdk"))
    for env in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if os.environ.get(env):
            candidates.append((os.environ[env], "env:" + env))
    candidates.extend(
        [
            (os.path.expanduser("~/Android/Sdk"), "~/Android/Sdk"),
            (os.path.expanduser("~/Android/sdk"), "~/Android/sdk"),
            (os.path.expanduser("~/android-sdk"), "~/android-sdk"),
            (os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk"), "%LOCALAPPDATA%/Android/Sdk"),
            ("/usr/local/lib/android/sdk", "/usr/local/lib/android/sdk"),
        ]
    )
    root = tools_root(args)
    if root:
        candidates.extend(
            [
                (os.path.join(root, "sdk"), "--tools/sdk"),
                (os.path.join(root, "android-sdk"), "--tools/android-sdk"),
                (root, "--tools"),
            ]
        )
    if args.sdk:
        for sub in ("SDK", "sdk"):
            candidates.append((os.path.join(args.sdk, sub), "--sdk/" + sub))

    for value, source in candidates:
        p = expand(value)
        if not p:
            continue
        if is_dir(os.path.join(p, "build-tools")):
            return p, source
        if is_dir(os.path.join(p, "SDK")):
            return os.path.join(p, "SDK"), source
    return None, "not found"


def resolve_build_tools(args, sdk):
    value = args.build_tools or os.environ.get("ANDROID_BUILD_TOOLS")
    source = "--build-tools" if args.build_tools else "env:ANDROID_BUILD_TOOLS"
    if value:
        p = expand(value)
        if is_file(os.path.join(p, TOOL_NAMES["aapt"][0])) or is_file(
            os.path.join(p, "apksigner.bat")
        ) or is_file(os.path.join(p, "apksigner")):
            return p, source
        latest = latest_dir(p, "aapt.exe") or latest_dir(p, "aapt")
        if latest:
            return latest, source + " (newest build-tools)"
        return None, source + " (invalid)"

    if sdk:
        latest = latest_dir(os.path.join(sdk, "build-tools"), "aapt.exe") or latest_dir(
            os.path.join(sdk, "build-tools"), "aapt"
        )
        if latest:
            label = "sdk/build-tools (newest)"
            root = tools_root(args)
            if root and os.path.abspath(expand(sdk)) == os.path.abspath(root):
                label = "--tools/build-tools (newest)"
            return latest, label
    root = tools_root(args)
    if root:
        if find_named(root, TOOL_NAMES["aapt"]) or is_file(
            os.path.join(root, "apksigner")
        ):
            return root, "--tools"
        latest = latest_dir(os.path.join(root, "build-tools"), "aapt.exe") or latest_dir(
            os.path.join(root, "build-tools"), "aapt"
        )
        if latest:
            return latest, "--tools/build-tools (newest)"
    return None, "not found"


def derive_sdk_from_build_tools(build_tools):
    if not build_tools:
        return None
    p = expand(build_tools)
    # .../SDK/build-tools/<version> -> .../SDK
    parent = os.path.dirname(p)
    if os.path.basename(parent).lower() == "build-tools":
        return os.path.dirname(parent)
    return None


def derive_java_home(java):
    if not java:
        return None
    p = expand(java)
    if is_dir(p):
        return p if is_file(os.path.join(p, "bin", "java.exe")) or is_file(
            os.path.join(p, "bin", "java")
        ) else p
    parent = os.path.dirname(p)
    if os.path.basename(parent).lower() == "bin":
        return os.path.dirname(parent)
    return None


def resolve_java(args):
    if args.java:
        p = expand(args.java)
        if is_dir(p):
            p = find_named(os.path.join(p, "bin"), TOOL_NAMES["java"]) or find_named(
                p, TOOL_NAMES["java"]
            )
        if is_file(p):
            return p, "--java"
        return None, "--java (invalid)"
    if args.jdk:
        p = find_named(os.path.join(expand(args.jdk), "bin"), TOOL_NAMES["java"])
        return (p, "--jdk") if p else (None, "--jdk (no bin/java)")
    if os.environ.get("JAVA_HOME"):
        p = find_named(
            os.path.join(os.environ["JAVA_HOME"], "bin"), TOOL_NAMES["java"]
        )
        if p:
            return p, "env:JAVA_HOME"

    root = tools_root(args)
    if root:
        for bindir in (
            os.path.join(root, "jdk", "bin"),
            os.path.join(root, "java", "bin"),
            os.path.join(root, "bin"),
        ):
            p = find_named(bindir, TOOL_NAMES["java"])
            if p:
                return p, "--tools"
    p = which(TOOL_NAMES["java"])
    return (p, "PATH") if p else (None, "not found")


def resolve_ndk(args, sdk):
    if args.ndk:
        p = expand(args.ndk)
        if is_file(os.path.join(p, "build", "cmake", "android.toolchain.cmake")):
            return p, "--ndk"
        if is_file(p):
            # Maybe the toolchain file itself was passed.
            p = os.path.dirname(p)
            while p and not is_file(os.path.join(p, "build", "cmake", "android.toolchain.cmake")):
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
            if is_file(os.path.join(p, "build", "cmake", "android.toolchain.cmake")):
                return p, "--ndk"
        return None, "--ndk (no android.toolchain.cmake)"
    for env in ("ANDROID_NDK_ROOT", "ANDROID_NDK_HOME"):
        if os.environ.get(env):
            p = expand(os.environ[env])
            if is_file(os.path.join(p, "build", "cmake", "android.toolchain.cmake")):
                return p, "env:" + env
    if sdk:
        p = latest_dir(os.path.join(sdk, "ndk"), "android.toolchain.cmake")
        if not p:
            p = latest_dir(
                os.path.join(sdk, "ndk"),
                os.path.join("build", "cmake", "android.toolchain.cmake"),
            )
        if not p and is_dir(os.path.join(sdk, "ndk-bundle")):
            p = os.path.join(sdk, "ndk-bundle")
        if p and is_file(os.path.join(p, "build", "cmake", "android.toolchain.cmake")):
            return p, "sdk/ndk"
    root = tools_root(args)
    if root:
        for base in (os.path.join(root, "ndk"), root):
            if is_file(os.path.join(base, "build", "cmake", "android.toolchain.cmake")):
                return base, "--tools"
            p = latest_dir(
                base, os.path.join("build", "cmake", "android.toolchain.cmake")
            )
            if p:
                return p, "--tools (newest)"
    return None, "not found"


def resolve_cmake_ninja(args, sdk):
    cmake = None
    cmake_src = "not found"
    if args.cmake:
        cmake = expand(args.cmake)
        if is_dir(cmake):
            cmake = find_named(cmake, TOOL_NAMES["cmake"]) or find_named(
                os.path.join(cmake, "bin"), TOOL_NAMES["cmake"]
            )
        cmake_src = "--cmake"
    elif os.environ.get("CMAKE"):
        cmake = find_named(os.environ["CMAKE"], TOOL_NAMES["cmake"]) or expand(
            os.environ["CMAKE"]
        )
        cmake_src = "env:CMAKE"
    root = tools_root(args)
    if not is_file(cmake) and root:
        cmake = find_named(
            os.path.join(root, "cmake", "bin"), TOOL_NAMES["cmake"]
        ) or find_named(os.path.join(root, "bin"), TOOL_NAMES["cmake"])
        if cmake:
            cmake_src = "--tools"
    if not is_file(cmake) and sdk:
        bindir = latest_dir(os.path.join(sdk, "cmake"), "bin")
        if bindir:
            bindir = os.path.join(bindir, "bin")
        else:
            bindir = latest_dir(os.path.join(sdk, "cmake"), None)
            if bindir:
                bindir = os.path.join(bindir, "bin")
        if bindir:
            cmake = find_named(bindir, TOOL_NAMES["cmake"])
            cmake_src = "sdk/cmake"
    if not is_file(cmake):
        cmake = which(TOOL_NAMES["cmake"])
        cmake_src = "PATH" if cmake else cmake_src

    ninja = None
    ninja_src = "not found"
    if args.ninja:
        ninja = expand(args.ninja)
        if is_dir(ninja):
            ninja = find_named(ninja, TOOL_NAMES["ninja"])
        ninja_src = "--ninja"
    elif os.environ.get("NINJA"):
        ninja = find_named(os.environ["NINJA"], TOOL_NAMES["ninja"]) or expand(
            os.environ["NINJA"]
        )
        ninja_src = "env:NINJA"
    if not is_file(ninja) and root:
        ninja = (
            first_file(
                os.path.join(root, "ninja"), os.path.join(root, "bin", "ninja")
            )
            or find_named(os.path.join(root, "ninja"), TOOL_NAMES["ninja"])
            or find_named(os.path.join(root, "bin"), TOOL_NAMES["ninja"])
        )
        if ninja:
            ninja_src = "--tools"
    if not is_file(ninja) and cmake:
        ninja = find_named(os.path.dirname(cmake), TOOL_NAMES["ninja"])
        if ninja:
            ninja_src = "same dir as cmake"
    if not is_file(ninja):
        ninja = which(TOOL_NAMES["ninja"])
        ninja_src = "PATH" if ninja else ninja_src
    return cmake, cmake_src, ninja, ninja_src


def resolve_jar(args_value, env_names, filenames, search_dirs):
    value = args_value
    source = None
    if value:
        source = "command line"
    else:
        for env in env_names:
            if os.environ.get(env):
                value = os.environ[env]
                source = "env:" + env
                break
    if value:
        p = expand(value)
        if is_dir(p):
            found = find_named(p, filenames)
            if not found:
                jars = sorted(glob.glob(os.path.join(p, "*.jar")), key=version_key)
                for jar in jars:
                    base = os.path.basename(jar).lower()
                    if any(f.lower().replace(".jar", "") in base for f in filenames):
                        found = jar
                        break
            return (found, source) if found else (None, source + " (jar not found)")
        return (p, source) if is_file(p) else (None, source + " (invalid)")
    for directory in search_dirs:
        p = find_named(directory, filenames)
        if p:
            return p, "auto (" + directory + ")"
    return None, "not found"


def resolve_baksmali_smali(args):
    search = []
    root = tools_root(args)
    if root:
        search.append(root)
    search.extend([
        os.path.join(ROOT, "tools"),
        os.path.join(ROOT, "tools_tmp"),
        os.path.join(ROOT, ".tools"),
        os.path.join(ROOT, "third_party"),
        os.getcwd(),
    ])
    baksmali, bsrc = resolve_jar(
        args.baksmali,
        ("BAKSMALI_JAR", "BAKSMALI"),
        ("baksmali.jar",),
        search,
    )
    if not baksmali:
        # Also accept versioned jars (baksmali-2.5.2.jar).
        for d in search:
            if is_dir(d):
                jars = sorted(
                    glob.glob(os.path.join(d, "baksmali*.jar")), key=version_key
                )
                if jars:
                    baksmali, bsrc = jars[-1], "auto (" + d + ")"
                    break
    smali, ssrc = resolve_jar(
        args.smali,
        ("SMALI_JAR", "SMALI"),
        ("smali.jar",),
        search,
    )
    if not smali:
        for d in search:
            if is_dir(d):
                jars = sorted(glob.glob(os.path.join(d, "smali*.jar")), key=version_key)
                if jars:
                    smali, ssrc = jars[-1], "auto (" + d + ")"
                    break
    return baksmali, bsrc, smali, ssrc


def resolve_hookgen_mono(args):
    value = args.hookgen or os.environ.get("HOOKGEN")
    source = "--hookgen" if args.hookgen else "env:HOOKGEN"
    names = (
        "HookGen_net35.exe",
        "MonoMod.RuntimeDetour.HookGen.exe",
        "HookGen_net35",
        "MonoMod.RuntimeDetour.HookGen",
    )
    hookgen = None
    if value:
        p = expand(value)
        if is_dir(p):
            hookgen = find_named(p, names)
            if not hookgen:
                for dirpath, _, filenames in os.walk(p):
                    depth = dirpath[len(p):].count(os.sep)
                    if depth > 2:
                        continue
                    for fn in filenames:
                        if fn in names:
                            hookgen = os.path.join(dirpath, fn)
                            break
                    if hookgen:
                        break
        else:
            hookgen = p if is_file(p) else None
    else:
        dirs = []
        root = tools_root(args)
        if root:
            dirs.extend([os.path.join(root, "hookgen"), root])
        dirs.extend(
            [
                os.path.join(ROOT, "tools", "hookgen"),
                os.path.join(ROOT, "tools"),
                os.path.join(ROOT, "third_party", "hookgen"),
            ]
        )
        for directory in dirs:
            hookgen = find_named(directory, names)
            if hookgen:
                source = "auto (" + directory + ")"
                break
        if not hookgen:
            hookgen = which(names)
            if hookgen:
                source = "PATH"
    if hookgen and not is_file(hookgen):
        hookgen = None

    mono = args.mono or os.environ.get("MONO")
    msource = "--mono" if args.mono else "env:MONO"
    if mono:
        p = expand(mono)
        if is_dir(p):
            mono = find_named(p, TOOL_NAMES["mono"]) or find_named(
                os.path.join(p, "bin"), TOOL_NAMES["mono"]
            )
        else:
            mono = p if is_file(p) else None
    else:
        mono = which(TOOL_NAMES["mono"])
        msource = "PATH" if mono else "not found"
    if not mono:
        root = tools_root(args)
        if root:
            mono = find_named(
                os.path.join(root, "mono", "bin"), TOOL_NAMES["mono"]
            ) or find_named(os.path.join(root, "bin"), TOOL_NAMES["mono"])
            if mono:
                msource = "--tools"
    if not hookgen and source == "env:HOOKGEN":
        source = "not found"
    if not mono and msource == "not found":
        msource = "not found"
    return hookgen, source, mono, msource


def resolve_keystore(args, sign):
    keystore = args.keystore or os.environ.get("BEPINEX_KEYSTORE")
    ksource = "--keystore" if args.keystore else "env:BEPINEX_KEYSTORE"
    if not keystore and is_file(DEFAULT_KEYSTORE):
        keystore, ksource = DEFAULT_KEYSTORE, "~/.android/debug.keystore"
    if not keystore:
        root = tools_root(args)
        if root:
            p = first_file(
                os.path.join(root, "debug.keystore"),
                os.path.join(root, "keystore.jks"),
            )
            if p:
                keystore, ksource = p, "--tools"
    keystore = expand(keystore) if keystore else None
    debug = bool(
        keystore
        and os.path.basename(keystore).lower() == "debug.keystore"
    )
    alias = args.alias or os.environ.get("BEPINEX_KEY_ALIAS") or (
        "androiddebugkey" if debug else None
    )
    ks_pass = args.ks_pass or os.environ.get("BEPINEX_KS_PASS") or (
        "android" if debug else None
    )
    key_pass = args.key_pass or os.environ.get("BEPINEX_KEY_PASS") or ks_pass
    return {
        "path": keystore if is_file(keystore) else None,
        "source": ksource,
        "alias": alias,
        "ks_pass": ks_pass,
        "key_pass": key_pass,
        "debug": debug,
    }


def resolve_tools(args, sdk, build_tools):
    aapt = find_named(build_tools, TOOL_NAMES["aapt"]) if build_tools else None
    zipalign = find_named(build_tools, TOOL_NAMES["zipalign"]) if build_tools else None
    apksigner = find_named(build_tools, TOOL_NAMES["apksigner"]) if build_tools else None
    if not aapt:
        aapt = which(TOOL_NAMES["aapt"])
    if not zipalign:
        zipalign = which(TOOL_NAMES["zipalign"])
    if not apksigner:
        apksigner = which(TOOL_NAMES["apksigner"])

    java, java_src = resolve_java(args)
    ndk, ndk_src = resolve_ndk(args, sdk)
    cmake, cmake_src, ninja, ninja_src = resolve_cmake_ninja(args, sdk)
    baksmali, bsrc, smali, ssrc = resolve_baksmali_smali(args)
    hookgen, hgsrc, mono, msrc = resolve_hookgen_mono(args)
    keystore = resolve_keystore(args, args.sign is not False)

    adb = args.adb or which(TOOL_NAMES["adb"])
    if not adb and sdk:
        adb = find_named(os.path.join(sdk, "platform-tools"), TOOL_NAMES["adb"])

    return {
        "sdk": {"path": sdk, "source": "resolved"},
        "build_tools": {"path": build_tools, "source": "resolved"},
        "java": {"path": java, "source": java_src, "hint": HINTS["java"]},
        "aapt": {"path": aapt, "source": "build-tools/PATH", "hint": HINTS["aapt"]},
        "zipalign": {
            "path": zipalign,
            "source": "build-tools/PATH",
            "hint": HINTS["zipalign"],
        },
        "apksigner": {
            "path": apksigner,
            "source": "build-tools/PATH",
            "hint": HINTS["apksigner"],
        },
        "ndk": {"path": ndk, "source": ndk_src},
        "cmake": {"path": cmake, "source": cmake_src},
        "ninja": {"path": ninja, "source": ninja_src},
        "baksmali": {"path": baksmali, "source": bsrc, "hint": HINTS["baksmali"]},
        "smali": {"path": smali, "source": ssrc, "hint": HINTS["smali"]},
        "hookgen": {"path": hookgen, "source": hgsrc, "hint": HINTS["hookgen"]},
        "mono": {"path": mono, "source": msrc, "hint": HINTS["mono"]},
        "keystore": keystore,
        "adb": {"path": adb, "source": "PATH", "hint": HINTS["adb"]},
    }


# ---------------------------------------------------------------------------
# Plan / preflight
# ---------------------------------------------------------------------------


def safe_name(text, fallback="game"):
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._-")
    return text or fallback


def choose_outputs(args, info):
    label = args.name or info.get("label") or info.get("package") or ""
    base = safe_name(label or os.path.splitext(os.path.basename(info["path"]))[0])
    version = safe_name(info.get("version") or "")
    stem = base + ("-" + version if version else "")
    default_work = os.path.join(ROOT, "out", "build", stem + ".work")
    work = expand(args.work) if args.work else default_work
    default_final = os.path.join(ROOT, "out", "build", stem + "-bepinex.apk")
    final = expand(args.out) if args.out else default_final
    if is_dir(final) or (args.out and str(args.out).endswith(("/", os.sep))):
        final = os.path.join(final, stem + "-bepinex.apk")
    return work, final


def required_tools(args, info):
    required = []
    if not args.dex:
        required += ["java", "baksmali", "smali"]
    elif not args.skip_dex_verify:
        required += ["java", "baksmali"]
    if args.sign:
        required += ["java", "zipalign", "apksigner"]
        ks = info["_tools"]["keystore"]
        if not ks["path"]:
            required.append("keystore")
        if not ks.get("alias"):
            required.append("keystore_alias")
        if not ks.get("ks_pass"):
            required.append("keystore_password")
    return required


def collect_missing(args, info, tools):
    missing = []
    seen = set()
    for key in required_tools(args, info):
        if key in seen:
            continue
        seen.add(key)
        if key == "keystore_alias":
            missing.append(
                (
                    "keystore alias",
                    "pass --alias <alias> (or set BEPINEX_KEY_ALIAS)",
                )
            )
        elif key == "keystore_password":
            missing.append(
                (
                    "keystore password",
                    "pass --ks-pass <pass> (or set BEPINEX_KS_PASS), "
                    "or use --no-sign",
                )
            )
        else:
            item = tools.get(key, {})
            if not item.get("path"):
                missing.append((key, item.get("hint") or HINTS.get(key, "")))
    return missing


def step_plan(args, info, tools, work, final, core=None):
    return [
        {
            "name": "hooks",
            "run": not args.no_hooks,
            "detail": (
                "skip (--no-hooks)"
                if args.no_hooks
                else (
                    "use " + expand(args.hooks)
                    if args.hooks
                    else "extract from APK, else HookGen, else skip"
                )
            ),
        },
        {
            "name": "dex",
            "run": True,
            "detail": (
                "use " + expand(args.dex)
                if args.dex
                else "extract %s, patch loadNative, verify" % info["dex_entry"]
            ),
        },
        {
            "name": "shim",
            "run": True,
            "detail": (
                "use " + expand(args.shim)
                if args.shim
                else "build shim/ for %s with NDK+CMake+Ninja" % info["abi"]
            ),
        },
        {
            "name": "repack",
            "run": True,
            "detail": "base APK + dex + shim + core(%s) -> %s"
            % ((core or {}).get("path", DEFAULT_CORE), final),
        },
        {
            "name": "sign",
            "run": bool(args.sign),
            "detail": (
                "zipalign + apksigner (%s)" % (tools["keystore"].get("alias") or "?")
                if args.sign
                else "skip (--no-sign); output is not installable"
            ),
        },
    ]


def print_plan(args, info, tools, work, final, missing, core, pack=None):
    log("base APK: %s" % info["path"])
    log(
        "  package=%s version=%s label=%s"
        % (info.get("package") or "?", info.get("version") or "?", info.get("label") or "?")
    )
    log(
        "  ABI=%s (%s)  minSdk=%s  dex=%s"
        % (info["abi"], info["abi_source"], info.get("min_sdk"), info["dex_entry"])
    )
    log(
        "  Managed=%s  HOOKS=%s"
        % (info.get("managed_prefix") or "missing", info.get("hooks_member") or "missing")
    )
    if pack:
        log(
            "asset pack:    %s (version=%s, %d files)"
            % (pack["root"], pack.get("version") or "?", len(pack["files"]))
        )
    log(
        "core:          %s (%d files, version=%s)"
        % (core["path"], len(core["files"]), core.get("version") or "?")
    )
    log("work directory: " + work)
    log("output APK:    " + final)

    order = [
        "java",
        "build_tools",
        "aapt",
        "zipalign",
        "apksigner",
        "ndk",
        "cmake",
        "ninja",
        "baksmali",
        "smali",
        "hookgen",
        "mono",
        "keystore",
        "adb",
    ]
    log("tools:")
    for key in order:
        item = tools.get(key, {})
        if key == "keystore":
            value = item.get("path") or "MISSING"
            extra = " alias=" + (item.get("alias") or "?")
            if item.get("ks_pass"):
                extra += " password=set"
            log("  %-12s %s (%s)%s" % (key, value, item.get("source"), extra))
        else:
            log(
                "  %-12s %s (%s)"
                % (key, item.get("path") or "MISSING", item.get("source"))
            )
    log("steps:")
    for i, step in enumerate(step_plan(args, info, tools, work, final, core), 1):
        log("  %d. %-8s %s" % (i, step["name"], step["detail"]))
    if missing:
        log("missing required inputs:")
        for name, hint in missing:
            log("  - %s: %s" % (name, hint))
    else:
        log("preflight: all required inputs resolved")


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def extract_member(apk, member, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with zipfile.ZipFile(apk) as z, z.open(member) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)
    return dest


def verify_elf(path, abi):
    with open(path, "rb") as f:
        head = f.read(64)
    if len(head) < 20 or head[:4] != b"\x7fELF":
        raise BuildError("not an ELF shared library: " + path)
    elf_class = head[4]
    machine = int.from_bytes(head[18:20], "little")
    expected = ABI_ELF.get(abi)
    if not expected:
        raise BuildError("unsupported ABI for shim: " + abi)
    if (elf_class, machine) != expected:
        raise BuildError(
            "shim ABI mismatch: %s is ELF class=%d machine=%d, expected class=%d machine=%d for %s"
            % (path, elf_class, machine, expected[0], expected[1], abi)
        )
    return {"elf_class": elf_class, "machine": machine, "abi": abi}


def stage_dex(args, info, tools, work, env):
    os.makedirs(os.path.join(work, "dex"), exist_ok=True)
    if args.dex:
        dex = os.path.join(work, "dex", "classes.patched.dex")
        src = expand(args.dex)
        with open(src, "rb") as f:
            magic = f.read(8)
        if not magic.startswith(b"dex\n"):
            raise BuildError("--dex is not a raw .dex file: " + src)
        shutil.copy2(src, dex)
        log("using supplied dex: " + dex)
    else:
        pristine = os.path.join(work, "dex", info["dex_entry"])
        extract_member(info["path"], info["dex_entry"], pristine)
        log("extracted %s -> %s" % (info["dex_entry"], pristine))
        dex = os.path.join(work, "dex", "classes.patched.dex")
        cmd = [
            sys.executable,
            PATCH_DEX,
            "--input",
            pristine,
            "--output",
            dex,
            "--baksmali",
            tools["baksmali"]["path"],
            "--smali",
            tools["smali"]["path"],
            "--java",
            tools["java"]["path"],
        ]
        if args.keep_smali:
            cmd += ["--keep-smali", os.path.join(work, "dex", "smali")]
        run(cmd, env=env)

    if not args.skip_dex_verify:
        run(
            [
                sys.executable,
                VERIFY_DEX,
                "--dex",
                dex,
                "--baksmali",
                tools["baksmali"]["path"],
                "--java",
                tools["java"]["path"],
            ],
            env=env,
        )
    return {
        "path": dex,
        "size": os.path.getsize(dex),
        "sha256": sha256_file(dex),
        "verified": not args.skip_dex_verify,
        "source": expand(args.dex) if args.dex else "patched " + info["dex_entry"],
    }


def find_so(directory):
    hits = []
    for dirpath, _, filenames in os.walk(directory):
        if "libbe5hook.so" in filenames:
            hits.append(os.path.join(dirpath, "libbe5hook.so"))
    if not hits:
        return None
    hits.sort(key=lambda p: (len(p), p))
    return hits[0]


def stage_shim(args, info, tools, work, env):
    os.makedirs(os.path.join(work, "shim"), exist_ok=True)
    dest = os.path.join(work, "shim", "libbe5hook.so")
    if args.shim:
        shutil.copy2(expand(args.shim), dest)
        log("using supplied shim: " + dest)
    else:
        build_dir = os.path.join(work, "shim", "build-" + safe_name(info["abi"]))
        if args.clean and is_dir(build_dir):
            shutil.rmtree(build_dir)
        os.makedirs(build_dir, exist_ok=True)
        toolchain = os.path.join(
            tools["ndk"]["path"], "build", "cmake", "android.toolchain.cmake"
        )
        platform = str(args.android_platform or info.get("min_sdk") or 24)
        if platform.startswith("android-"):
            platform = platform[len("android-"):]
        cmd = [
            tools["cmake"]["path"],
            "-S",
            os.path.join(ROOT, "shim"),
            "-B",
            build_dir,
            "-G",
            "Ninja",
            "-DCMAKE_TOOLCHAIN_FILE=" + toolchain,
            "-DANDROID_NDK=" + tools["ndk"]["path"],
            "-DANDROID_ABI=" + info["abi"],
            "-DANDROID_PLATFORM=android-" + platform,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_MAKE_PROGRAM=" + tools["ninja"]["path"],
        ]
        if args.data_dir_name:
            cmd.append("-DGAME_DATA_DIR=" + args.data_dir_name)
        run(cmd, env=env)
        run(
            [
                tools["cmake"]["path"],
                "--build",
                build_dir,
                "--config",
                "Release",
            ],
            env=env,
        )
        built = find_so(build_dir)
        if not built:
            raise BuildError("cmake succeeded but libbe5hook.so was not produced")
        shutil.copy2(built, dest)
        log("built shim: " + dest)
    elf = verify_elf(dest, info["abi"])
    return {
        "path": dest,
        "size": os.path.getsize(dest),
        "sha256": sha256_file(dest),
        "source": expand(args.shim) if args.shim else "built from shim/",
        **elf,
    }


def stage_hooks(args, info, tools, work, env):
    hooks_dir = os.path.join(work, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    target = os.path.join(hooks_dir, HOOKS_NAME)

    if args.no_hooks:
        if is_file(target):
            os.remove(target)
        return {
            "status": "skipped",
            "reason": "--no-hooks",
            "path": None,
            "warnings": ["HOOKS disabled; mods using On.*/IL.* may fail"],
        }

    if args.hooks:
        src = expand(args.hooks)
        if not is_file(src):
            raise BuildError("HOOKS not found: " + src)
        with open(src, "rb") as f:
            data = f.read(64)
        if len(data) < 2 or data[:2] != b"MZ":
            raise BuildError("supplied HOOKS is not a .NET PE file: " + src)
        shutil.copy2(src, target)
        log("using supplied HOOKS: " + src)
        return {
            "status": "manual",
            "path": target,
            "sha256": sha256_file(target),
            "size": os.path.getsize(target),
            "warnings": [],
        }

    cmd = [
        sys.executable,
        PATCH_HOOKS,
        "--apk",
        info["path"],
        "--out",
        hooks_dir,
    ]
    if tools["hookgen"]["path"]:
        cmd += ["--hookgen", tools["hookgen"]["path"]]
    if tools["mono"]["path"]:
        cmd += ["--mono", tools["mono"]["path"]]
    # Always let patch_hooks skip cleanly; build.py decides whether a skip is
    # fatal so the error can include the actual reason.
    run(cmd, env=env)

    json_path = os.path.join(hooks_dir, "hookgen.json")
    result = {"status": "skipped", "reason": "hookgen.json missing", "path": None}
    if is_file(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    status = result.get("status", "skipped")
    warnings = []
    if status == "skipped":
        reason = result.get("reason", "unknown")
        warnings.append("HOOKS skipped: " + reason)
        if args.require_hooks:
            raise BuildError(
                "HOOKS required but unavailable: %s\n  %s"
                % (reason, "pass --hooks <file>, --hookgen <exe>, or drop --require-hooks")
            )
    elif status == "generated":
        warnings.append(
            "HOOKS was generated by HookGen, not extracted from the game; "
            "mods compiled against the official MMHOOK may not bind"
        )
    if status != "skipped" and is_file(target):
        result["path"] = target
        result["sha256"] = sha256_file(target)
        result["size"] = os.path.getsize(target)
    result["warnings"] = warnings
    return result


def stage_repack(args, info, tools, work, dex, shim, hooks, core, env):
    unsigned = os.path.join(work, "unsigned.apk")
    if is_file(unsigned):
        os.remove(unsigned)
    cmd = [
        sys.executable,
        REPACK,
        info["path"],
        unsigned,
        "--abi",
        info["abi"],
        "--dex",
        dex["path"],
        "--so",
        shim["path"],
        "--core",
        core["path"],
        "--payload",
        expand(args.payload),
        "--boot",
        expand(args.boot),
        "--no-payload-hooks",
    ]
    if args.plugins:
        cmd += ["--plugins-dir", expand(args.plugins)]
    else:
        cmd += ["--no-plugins"]
    if hooks.get("path") and is_file(hooks["path"]):
        cmd += ["--hooks", hooks["path"]]
    run(cmd, env=env)
    if not is_file(unsigned):
        raise BuildError("repack did not produce " + unsigned)
    expected = {
        "classes.dex": dex["path"],
        "lib/%s/libbe5hook.so" % info["abi"]: shim["path"],
        "assets/BepInEx/core/Be5Bootstrap.dll": expand(args.boot),
    }
    for item in core["files"]:
        expected["assets/BepInEx/core/" + item["name"]] = os.path.join(
            core["path"], item["name"]
        )
    config_dir = os.path.join(expand(args.payload), "config")
    for name in sorted(os.listdir(config_dir)):
        path = os.path.join(config_dir, name)
        if os.path.isfile(path):
            expected["assets/BepInEx/config/" + name] = path

    verified_entries = []
    try:
        with zipfile.ZipFile(unsigned) as output_zip:
            names = output_zip.namelist()
            if len(names) != len(set(names)):
                raise BuildError("repack output contains duplicate ZIP entries")
            missing = sorted(set(expected) - set(names))
            if missing:
                raise BuildError(
                    "repack output is missing required entries: " + ", ".join(missing)
                )
            for arcname, source in sorted(expected.items()):
                with open(source, "rb") as source_file:
                    source_data = source_file.read()
                output_data = output_zip.read(arcname)
                source_hash = hashlib.sha256(source_data).hexdigest()
                output_hash = hashlib.sha256(output_data).hexdigest()
                if output_hash != source_hash:
                    raise BuildError(
                        "repack output hash mismatch: %s\n  expected %s\n  actual   %s"
                        % (arcname, source_hash, output_hash)
                    )
                verified_entries.append(
                    {
                        "path": arcname,
                        "size": len(output_data),
                        "sha256": output_hash,
                    }
                )
    except zipfile.BadZipFile as exc:
        raise BuildError("repack output is not a valid APK/zip: %s" % unsigned) from exc
    return {
        "path": unsigned,
        "size": os.path.getsize(unsigned),
        "sha256": sha256_file(unsigned),
        "plugins": expand(args.plugins) if args.plugins else "none",
        "hooks": hooks.get("status"),
        "core": core["path"],
        "core_version": core.get("version"),
        "core_files": len(core["files"]),
        "output_entries": verified_entries,
    }


def java_env(tools):
    env = dict(os.environ)
    java = tools["java"]["path"]
    home = derive_java_home(java)
    if home:
        env["JAVA_HOME"] = home
        env["PATH"] = os.path.join(home, "bin") + os.pathsep + env.get("PATH", "")
    return env


def apksigner_cmd(tools, *args):
    cmd = [tools["apksigner"]["path"]] + list(args)
    # .bat files need cmd.exe when subprocess cannot execute them directly.
    if os.name == "nt" and cmd[0].lower().endswith(".bat"):
        cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c"] + cmd
    return cmd


def sign_stage(args, info, tools, work, final, unsigned, env):
    if not args.sign:
        os.makedirs(os.path.dirname(final), exist_ok=True)
        if os.path.abspath(unsigned) != os.path.abspath(final):
            shutil.copy2(unsigned, final)
        return {
            "signed": False,
            "path": final,
            "size": os.path.getsize(final),
            "sha256": sha256_file(final),
            "warnings": ["unsigned APK is not installable"],
        }

    aligned = os.path.join(work, "aligned.apk")
    if is_file(aligned):
        os.remove(aligned)
    run(
        [
            tools["zipalign"]["path"],
            "-f",
            "-p",
            "4",
            unsigned,
            aligned,
        ],
        env=env,
    )
    os.makedirs(os.path.dirname(final), exist_ok=True)
    # apksigner cannot write over its input; use a temp output when --out
    # points at the unsigned repack file.
    sign_output = final
    if os.path.abspath(final) == os.path.abspath(unsigned):
        sign_output = os.path.join(work, "signed.apk")
    if is_file(sign_output):
        os.remove(sign_output)
    sign_env = dict(env)
    sign_env["BEPINEX_KS_PASS"] = tools["keystore"]["ks_pass"]
    sign_env["BEPINEX_KEY_PASS"] = tools["keystore"]["key_pass"]
    cmd = apksigner_cmd(
        tools,
        "sign",
        "--ks",
        tools["keystore"]["path"],
        "--ks-key-alias",
        tools["keystore"]["alias"],
        "--ks-pass",
        "env:BEPINEX_KS_PASS",
        "--key-pass",
        "env:BEPINEX_KEY_PASS",
        "--min-sdk-version",
        str(args.min_sdk or info.get("min_sdk") or 24),
        "--out",
        sign_output,
        aligned,
    )
    run(cmd, env=sign_env)
    if os.path.abspath(sign_output) != os.path.abspath(final):
        os.replace(sign_output, final)
    run(apksigner_cmd(tools, "verify", final), env=sign_env)
    return {
        "signed": True,
        "path": final,
        "size": os.path.getsize(final),
        "sha256": sha256_file(final),
        "keystore": tools["keystore"]["path"],
        "alias": tools["keystore"]["alias"],
        "verified": True,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    ap = argparse.ArgumentParser(
        description="Build an installable BepInEx 5 Android APK from a base Unity Mono APK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--apk", help="base APK (auto-detected if omitted)")
    ap.add_argument("--input-dir", default=".", help="where to auto-find the base APK")
    ap.add_argument(
        "--assets",
        help="CI asset pack directory (standard input: core, Be5Bootstrap, config, shim). "
        "Validated against its SHA256SUMS before the APK is modified. "
        "Relative paths resolve against the current directory",
    )
    ap.add_argument("--out", help="output APK path (default: out/build/<name>-bepinex.apk)")
    ap.add_argument("--work", help="intermediate directory (default: out/build/<name>.work)")
    ap.add_argument("--name", help="output name; default from APK label/package")
    ap.add_argument(
        "--abi",
        choices=list(SUPPORTED_ABIS),
        help="ABI to patch; armeabi-v7a only (default: from APK)",
    )
    ap.add_argument("--package", help="package name if aapt is unavailable")
    ap.add_argument("--dex", help="already patched classes.dex (skip patching)")
    ap.add_argument(
        "--shim",
        help="advanced override: already built libbe5hook.so (skip NDK build)",
    )
    ap.add_argument("--hooks", help="HOOKS-Assembly-CSharp.dll to use")
    ap.add_argument("--no-hooks", action="store_true", help="do not add HOOKS")
    ap.add_argument(
        "--require-hooks",
        action="store_true",
        help="fail if HOOKS cannot be extracted/generated (default: skip)",
    )
    ap.add_argument("--plugins", help="directory of extra plugin .dll files")
    ap.add_argument(
        "--core",
        help="advanced override: built BepInEx core directory",
    )
    ap.add_argument(
        "--payload",
        help="advanced override: asset root containing config/",
    )
    ap.add_argument("--boot", help="advanced override: Be5Bootstrap.dll")

    ap.add_argument(
        "--tools",
        help="tool directory holding sdk/, build-tools/, jdk/, ndk/, cmake/, "
        "baksmali.jar, smali.jar and debug.keystore (env: BE5_TOOLS)",
    )
    ap.add_argument("--sdk", help="Android SDK root")
    ap.add_argument("--build-tools", help="Android build-tools directory")
    ap.add_argument("--jdk", help="JDK/JRE home")
    ap.add_argument("--java", help="java executable")
    ap.add_argument("--ndk", help="Android NDK directory")
    ap.add_argument("--cmake", help="cmake executable")
    ap.add_argument("--ninja", help="ninja executable")
    ap.add_argument("--baksmali", help="baksmali.jar")
    ap.add_argument("--smali", help="smali.jar")
    ap.add_argument("--hookgen", help="HookGen executable")
    ap.add_argument("--mono", help="mono executable")
    ap.add_argument("--adb", help="adb executable (only for --install)")

    ap.add_argument("--keystore", help="keystore file")
    ap.add_argument("--alias", help="key alias")
    ap.add_argument("--ks-pass", help="keystore password")
    ap.add_argument("--key-pass", help="key password (default: --ks-pass)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--sign", dest="sign", action="store_true", default=None)
    g.add_argument("--no-sign", dest="sign", action="store_false")

    ap.add_argument("--min-sdk", type=int, help="apksigner --min-sdk-version")
    ap.add_argument("--android-platform", help="shim ANDROID_PLATFORM, e.g. 24")
    ap.add_argument("--data-dir-name", help="optional GAME_DATA_DIR for the shim")
    ap.add_argument("--skip-dex-verify", action="store_true")
    ap.add_argument("--keep-smali", action="store_true")
    ap.add_argument("--clean", action="store_true", help="remove the work directory first")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="write the plan to build.json; do not modify the APK",
    )
    ap.add_argument(
        "--config",
        help="JSON defaults for CLI options, including assets; relative paths use the current directory",
    )
    ap.add_argument("--install", action="store_true", help="adb install -r after signing")
    ap.add_argument("--serial", help="adb serial (with --install)")
    return ap


def resolve_asset_pack(args):
    """Resolve --assets (or the pack this script ships in) and validate it.

    The pack is fully hash-verified here, before any APK is read or written.
    Pack inputs only provide defaults: explicit --core/--boot/--payload/--shim
    always win.
    """
    assets_dir = args.assets
    if not assets_dir and os.path.basename(ROOT) == "packaging":
        # build.py runs from <pack>/packaging/: use the enclosing pack.
        candidate = os.path.dirname(ROOT)
        if asset_pack.looks_like_pack(candidate):
            assets_dir = candidate
            log("asset pack detected next to build.py: " + candidate)
    if not assets_dir:
        overrides = {
            "--core": args.core,
            "--boot": args.boot,
            "--shim": args.shim,
            "--payload": args.payload,
        }
        missing = [flag for flag, value in overrides.items() if not value]
        if missing:
            raise BuildError(
                "--assets <asset-pack directory> is required for the standard "
                "build; download and extract the CI asset pack. For advanced "
                "overrides without --assets, provide all of: --core <dir>, "
                "--boot <Be5Bootstrap.dll>, --shim <libbe5hook.so>, "
                "--payload <asset-root>. Missing: %s" % ", ".join(missing)
            )
        return None
    try:
        pack = asset_pack.load_asset_pack(assets_dir)
    except asset_pack.AssetPackError as exc:
        raise BuildError(
            "invalid asset pack %s:\n  %s\n  re-download the asset pack, or pass "
            "--assets <directory> pointing at a verified copy"
            % (expand(assets_dir), exc)
        ) from exc
    log(
        "asset pack: %s (version=%s commit=%s, %d files verified)"
        % (
            pack["root"],
            pack.get("version") or "?",
            (pack.get("source") or {}).get("commit") or "?",
            len(pack["files"]),
        )
    )
    if not args.core:
        args.core = pack["paths"]["core"]
    if not args.boot:
        args.boot = pack["paths"]["bootstrap"]
    if not args.payload:
        args.payload = pack["root"]
    if not args.shim:
        args.shim = pack["paths"]["shim"]
    return pack


def load_config(path):
    if not path:
        return {}
    p = expand(path)
    if not is_file(p):
        raise BuildError("config not found: " + p)
    try:
        # Accept files saved by Windows PowerShell (UTF-8 with BOM).
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise BuildError("cannot read config %s: %s" % (p, exc)) from exc
    if not isinstance(data, dict):
        raise BuildError("config must be a JSON object: " + p)
    return data


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre_args, _ = pre.parse_known_args(argv)
    try:
        config = load_config(pre_args.config) if pre_args.config else {}
    except BuildError as exc:
        print("[build] ERROR: " + str(exc), file=sys.stderr)
        return 1

    known = {a.dest for a in parser._actions if a.dest != "help"}
    unknown = sorted(k for k in config if k not in known)
    if unknown:
        warn("ignoring unknown config keys: " + ", ".join(unknown))
    parser.set_defaults(**{k: v for k, v in config.items() if k in known})
    args = parser.parse_args(argv)
    if args.sign is None:
        args.sign = True

    started = datetime.now(timezone.utc).isoformat()
    work = None
    report = {
        "schema": 1,
        "started": started,
        "status": "running",
        "warnings": [],
        "steps": {},
    }
    try:
        pack = resolve_asset_pack(args)

        if args.clean and args.work:
            shutil.rmtree(expand(args.work), ignore_errors=True)

        for flag, value, want_dir in (
            ("--dex", args.dex, False),
            ("--shim", args.shim, False),
            ("--hooks", args.hooks, False),
            ("--core", args.core, True),
            ("--payload", args.payload, True),
            ("--plugins", args.plugins, True),
            ("--boot", args.boot, False),
        ):
            if value:
                exists = is_dir(value) if want_dir else is_file(value)
                if not exists:
                    raise BuildError("%s path not found: %s" % (flag, expand(value)))

        sdk, sdk_src = resolve_sdk(args)
        build_tools, bt_src = resolve_build_tools(args, sdk)
        if not sdk and build_tools:
            sdk = derive_sdk_from_build_tools(build_tools)
        tools = resolve_tools(args, sdk, build_tools)
        tools["build_tools"]["source"] = bt_src

        apk, apk_src = find_apk(args)
        info = inspect_apk(apk, tools["aapt"]["path"], args.abi, args.package)
        info["_tools"] = tools
        info["apk_source"] = apk_src

        work, final = choose_outputs(args, info)
        if os.path.normcase(os.path.realpath(apk)) == os.path.normcase(
            os.path.realpath(final)
        ):
            raise BuildError("input APK and output APK must be different paths")
        if args.clean and is_dir(work):
            shutil.rmtree(work)
        os.makedirs(work, exist_ok=True)

        core = inspect_core(args.core or DEFAULT_CORE)

        payload_dir = args.payload or DEFAULT_PAYLOAD
        if not is_dir(payload_dir):
            raise BuildError("payload directory not found: %s" % payload_dir)
        if not is_dir(os.path.join(expand(payload_dir), "config")):
            raise BuildError(
                "payload is missing config/: %s\n  pass --assets or --payload <asset-root>"
                % expand(payload_dir)
            )
        if not is_file(args.boot or DEFAULT_BOOT):
            raise BuildError(
                "bootstrap DLL not found: %s\n  pass --assets <asset-pack directory> "
                "(recommended; the pack contains the CI-built Be5Bootstrap.dll) "
                "or --boot <Be5Bootstrap.dll>" % (args.boot or DEFAULT_BOOT)
            )
        if args.plugins and not is_dir(args.plugins):
            raise BuildError("plugins directory not found: " + args.plugins)
        if args.no_hooks and args.hooks:
            raise BuildError("--no-hooks and --hooks are mutually exclusive")
        if args.no_hooks and args.require_hooks:
            raise BuildError("--no-hooks and --require-hooks are mutually exclusive")
        if args.install and not args.sign:
            raise BuildError("--install requires a signed APK; drop --no-sign")

        missing = collect_missing(args, info, tools)
        print_plan(args, info, tools, work, final, missing, core, pack)

        report.update(
            {
                "apk": {k: v for k, v in info.items() if not k.startswith("_")},
                "core": {
                    "path": core["path"],
                    "version": core.get("version"),
                    "files": [item["name"] for item in core["files"]],
                    "sha256sums": core["sha256sums"],
                    "manifest": core.get("manifest"),
                },
                "build": {
                    "abi": info["abi"],
                    "package": info.get("package"),
                    "min_sdk": args.min_sdk or info.get("min_sdk"),
                    "sign": bool(args.sign),
                    "work": work,
                    "output": final,
                },
                "tools": {
                    k: {
                        "path": v.get("path"),
                        "source": v.get("source"),
                        "alias": v.get("alias"),
                        "password_set": bool(v.get("ks_pass")),
                    }
                    for k, v in tools.items()
                    if isinstance(v, dict)
                },
                "config": {
                    k: ("***" if k in ("ks_pass", "key_pass") else v)
                    for k, v in config.items()
                },
            }
        )
        if pack:
            bootstrap_path = expand(args.boot or DEFAULT_BOOT)
            shim_path = expand(args.shim)
            config_path = os.path.join(
                expand(payload_dir), "config", "BepInEx.cfg"
            )
            report["assets"] = {
                "path": pack["root"],
                "version": pack.get("version"),
                "built_at": pack.get("built_at"),
                "source": pack.get("source"),
                "build": pack.get("build"),
                "root_sha256sums": pack["root_sha256sums"],
                "build_info_sha256": pack["build_info_sha256"],
                "files": len(pack["files"]),
                "bootstrap": {
                    "path": bootstrap_path,
                    "size": os.path.getsize(bootstrap_path),
                    "sha256": sha256_file(bootstrap_path),
                },
                "shim_sha256": sha256_file(shim_path),
                "config_sha256": sha256_file(config_path),
            }

        if missing:
            report["status"] = "missing-inputs"
            report["missing"] = [{"name": n, "hint": h} for n, h in missing]
            write_json(os.path.join(work, "build.json"), report)
            if args.dry_run:
                return 1
            raise BuildError(
                "missing required inputs:\n  "
                + "\n  ".join("%s: %s" % (n, h) for n, h in missing)
            )

        if args.dry_run:
            report["status"] = "planned"
            report["steps"] = {
                s["name"]: {"run": s["run"], "detail": s["detail"]}
                for s in step_plan(args, info, tools, work, final, core)
            }
            write_json(os.path.join(work, "build.json"), report)
            log("dry run: no files were changed")
            return 0

        env = java_env(tools)

        log("=== 1/5 hooks ===")
        hooks = stage_hooks(args, info, tools, work, env)
        report["steps"]["hooks"] = hooks
        report["warnings"].extend(hooks.get("warnings", []))

        log("=== 2/5 dex ===")
        dex = stage_dex(args, info, tools, work, env)
        report["steps"]["dex"] = dex

        log("=== 3/5 shim ===")
        shim = stage_shim(args, info, tools, work, env)
        report["steps"]["shim"] = shim

        log("=== 4/5 repack ===")
        unsigned = stage_repack(args, info, tools, work, dex, shim, hooks, core, env)
        report["steps"]["repack"] = unsigned

        log("=== 5/5 sign ===")
        output = sign_stage(args, info, tools, work, final, unsigned["path"], env)
        report["steps"]["sign"] = output
        report["warnings"].extend(output.get("warnings", []))

        report["status"] = "ok"
        report["finished"] = datetime.now(timezone.utc).isoformat()
        report["output"] = output
        write_json(os.path.join(work, "build.json"), report)

        log("done: " + output["path"])
        log("report: " + os.path.join(work, "build.json"))
        if not output.get("signed"):
            warn("output is unsigned and cannot be installed as-is")

        if args.install:
            if not tools["adb"]["path"]:
                raise BuildError("adb not found; " + HINTS["adb"])
            cmd = [tools["adb"]["path"]]
            if args.serial:
                cmd += ["-s", args.serial]
            cmd += ["install", "-r", output["path"]]
            run(cmd)
            log("installed")
        return 0

    except BuildError as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["finished"] = datetime.now(timezone.utc).isoformat()
        if work and is_dir(work):
            try:
                write_json(os.path.join(work, "build.json"), report)
            except OSError:
                pass
        print("[build] ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[build] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
