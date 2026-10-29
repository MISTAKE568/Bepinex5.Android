#!/usr/bin/env python3
"""APK repacker: base APK + BepInEx assets + libbe5hook + patched dex.

Preserves original zip compression for untouched entries (minimal diff).
"""
import argparse, glob, hashlib, json, os, shutil, subprocess, sys, time, zipfile


def _env_path(name):
    value = os.environ.get(name)
    return os.path.expanduser(value) if value else None


def _tools_dir():
    return _env_path("BE5_TOOLS")


def _has_tool(directory, *names):
    return any(os.path.isfile(os.path.join(directory, n)) for n in names)


def _find_build_tools():
    explicit = _env_path("ANDROID_BUILD_TOOLS")
    if explicit:
        return explicit
    root = _tools_dir()
    if root:
        for directory in (os.path.join(root, "build-tools"), root):
            if not os.path.isdir(directory):
                continue
            if _has_tool(directory, "zipalign", "zipalign.exe"):
                return directory
            versions = [
                v for v in sorted(glob.glob(os.path.join(directory, "*")))
                if os.path.isdir(v)
            ]
            if versions:
                return versions[-1]
    return None


def _find_jdk():
    explicit = _env_path("JAVA_HOME")
    if explicit:
        return explicit
    root = _tools_dir()
    for candidate in (
        os.path.join(root, "jdk") if root else None,
        os.path.join(root, "java") if root else None,
        root,
        os.path.expanduser("~/jdk"),
    ):
        if candidate and os.path.isfile(os.path.join(candidate, "bin", "java")):
            return candidate
    return None


def _find_keystore():
    root = _tools_dir()
    if root:
        for name in ("debug.keystore", "keystore.jks"):
            p = os.path.join(root, name)
            if os.path.isfile(p):
                return p
    return os.path.join(os.path.expanduser("~"), ".android", "debug.keystore")


SDK_BT = _find_build_tools()
JDK = _find_jdk()
KEYSTORE = _find_keystore()
KEY_ALIAS = "androiddebugkey"
KEY_PASS = "android"
MIN_SDK = "24"

HERE = os.path.dirname(os.path.abspath(__file__))

DEX = _env_path("BE5_INJECTED_DEX")
DEX_OVERRIDE = None
HOOKS_OVERRIDE = None
SO = None
CORE = None
PAYLOAD = None
BOOT = None
PATCHER = _env_path("BE5_PATCHER_DLL")

ABI = "armeabi-v7a"
PLUGINS_DIR = os.path.join(HERE, "plugins")
INCLUDE_PLUGINS = True
EXCLUDE_PAYLOAD_HOOKS = False


def add_file(zout, arcname, data, method=zipfile.ZIP_DEFLATED):
    zi = zipfile.ZipInfo(arcname, time.localtime()[:6])
    zi.compress_type = method
    zi.external_attr = 0o644 << 16
    zout.writestr(zi, data)


def read_sums(path):
    sums = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise SystemExit("%s:%d: expected '<sha256>  <name>'" % (path, line_no))
            digest, name = parts[0].lower(), parts[1].lstrip("*")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise SystemExit("%s:%d: invalid SHA-256" % (path, line_no))
            if name in sums:
                raise SystemExit("%s:%d: duplicate entry %s" % (path, line_no, name))
            sums[name] = digest
    return sums


def core_entries(core_dir):
    """Load the built core, using manifest.json/SHA256SUMS as the allow-list."""
    sums_path = os.path.join(core_dir, "SHA256SUMS")
    if not os.path.isfile(sums_path):
        raise SystemExit(
            "core/SHA256SUMS not found: %s\n  run: python build_core.py" % sums_path
        )
    sums = read_sums(sums_path)
    if not sums:
        raise SystemExit("core/SHA256SUMS is empty: " + sums_path)

    names = None
    manifest_path = os.path.join(core_dir, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8-sig") as f:
            manifest = json.load(f)
        if manifest.get("kind") != "bepinex-core":
            raise SystemExit("unexpected core manifest kind: " + manifest_path)
        names = [
            item.get("name")
            for item in (manifest.get("files") or [])
            if isinstance(item, dict)
        ]
        if not names or not all(names):
            raise SystemExit("invalid files list in " + manifest_path)
        extra = sorted(set(sums) - set(names))
        if extra:
            raise SystemExit(
                "core/SHA256SUMS has files not in manifest.json: " + ", ".join(extra)
            )
    else:
        names = sorted(sums)

    if len(set(names)) != len(names):
        raise SystemExit("duplicate file names in core manifest")

    entries = {}
    for name in names:
        if os.path.basename(name) != name or name in (".", ".."):
            raise SystemExit("unsafe core file name: " + name)
        if EXCLUDE_PAYLOAD_HOOKS and name == "HOOKS-Assembly-CSharp.dll":
            continue
        path = os.path.join(core_dir, name)
        if not os.path.isfile(path):
            raise SystemExit("core file missing: " + path)
        with open(path, "rb") as f:
            data = f.read()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sums.get(name):
            raise SystemExit(
                "core file hash mismatch: %s\n  expected %s\n  actual   %s"
                % (name, sums.get(name), actual)
            )
        entries["assets/BepInEx/core/" + name] = data
    return entries


def is_signature_entry(name):
    upper = name.upper()
    if upper == "META-INF/MANIFEST.MF":
        return True
    if not upper.startswith("META-INF/"):
        return False
    leaf = upper[len("META-INF/"):]
    return "/" not in leaf and leaf.endswith((".SF", ".RSA", ".DSA", ".EC"))


def same_path(left, right):
    return os.path.normcase(os.path.realpath(os.path.abspath(left))) == os.path.normcase(
        os.path.realpath(os.path.abspath(right))
    )


def repack(in_apk, out_apk, with_patcher=False):
    if same_path(in_apk, out_apk):
        raise SystemExit("input APK and output APK must be different paths")
    if HOOKS_OVERRIDE and not os.path.isfile(HOOKS_OVERRIDE):
        raise SystemExit("HOOKS not found: " + HOOKS_OVERRIDE)
    with zipfile.ZipFile(in_apk) as zin:
        entry_names = zin.namelist()
        seen = set()
        duplicates = set()
        for name in entry_names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        if duplicates:
            raise SystemExit(
                "duplicate APK entries are not supported: "
                + ", ".join(sorted(duplicates))
            )
        names = set(entry_names)
        if "classes.dex" not in names:
            raise SystemExit("no classes.dex")

        dex_path = DEX_OVERRIDE or DEX
        if not dex_path:
            raise SystemExit(
                "injected dex not found: pass --dex or set BE5_INJECTED_DEX"
            )
        dex = open(dex_path, "rb").read()
        so = open(SO, "rb").read()
        boot = open(BOOT, "rb").read()

        payload = core_entries(CORE)
        config_dir = os.path.join(PAYLOAD, "config")
        if not os.path.isdir(config_dir):
            raise SystemExit("config directory not found: " + config_dir)
        for fn in sorted(os.listdir(config_dir)):
            p = os.path.join(config_dir, fn)
            if os.path.isfile(p):
                payload["assets/BepInEx/config/" + fn] = open(p, "rb").read()
        payload["assets/BepInEx/core/Be5Bootstrap.dll"] = boot
        if HOOKS_OVERRIDE:
            payload["assets/BepInEx/core/HOOKS-Assembly-CSharp.dll"] = open(
                HOOKS_OVERRIDE, "rb"
            ).read()
        if INCLUDE_PLUGINS and os.path.isdir(PLUGINS_DIR):
            for fn in sorted(os.listdir(PLUGINS_DIR)):
                if fn.endswith(".dll"):
                    payload["assets/BepInEx/plugins/" + fn] = open(
                        os.path.join(PLUGINS_DIR, fn), "rb"
                    ).read()
        if with_patcher:
            if not PATCHER or not os.path.isfile(PATCHER):
                raise SystemExit(
                    "--patcher needs BE5ModFolderLoader.dll: set BE5_PATCHER_DLL"
                )
            payload["assets/BepInEx/patchers/BE5ModFolderLoader.dll"] = open(
                PATCHER, "rb"
            ).read()

        tmp = out_apk + ".tmp"
        t0 = time.time()
        try:
            with zipfile.ZipFile(tmp, "w", allowZip64=True) as zout:
                for info in zin.infolist():
                    if is_signature_entry(info.filename):
                        continue
                    if info.filename in ("classes.dex", "lib/%s/libbe5hook.so" % ABI):
                        continue
                    if info.filename in payload:
                        continue
                    zi = zipfile.ZipInfo(info.filename, info.date_time)
                    zi.compress_type = info.compress_type
                    zi.external_attr = info.external_attr
                    zi.create_system = info.create_system
                    with zin.open(info) as fsrc, zout.open(zi, "w") as fdst:
                        shutil.copyfileobj(fsrc, fdst, 1 << 20)

                add_file(zout, "classes.dex", dex, zipfile.ZIP_STORED)
                add_file(zout, "lib/%s/libbe5hook.so" % ABI, so, zipfile.ZIP_STORED)
                for arc, data in payload.items():
                    add_file(zout, arc, data, zipfile.ZIP_DEFLATED)

            os.replace(tmp, out_apk)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        print("[repack] %s -> %s (%.0fs, payload=%d)" % (in_apk, out_apk, time.time() - t0, len(payload)))


def sign(apk):
    if not SDK_BT:
        raise SystemExit(
            "zipalign/apksigner not found: pass --sdk-build-tools or set "
            "ANDROID_BUILD_TOOLS"
        )
    if not os.path.isfile(KEYSTORE):
        raise SystemExit("keystore not found: pass --keystore or create " + KEYSTORE)
    env = dict(os.environ)
    if JDK:
        env["JAVA_HOME"] = JDK
        env["PATH"] = os.path.join(JDK, "bin") + os.pathsep + env.get("PATH", "")
    zipalign = "zipalign.exe" if os.name == "nt" else "zipalign"
    apksigner = "apksigner.bat" if os.name == "nt" else "apksigner"
    aligned = apk + ".aligned"
    subprocess.run(
        [os.path.join(SDK_BT, zipalign), "-f", "-p", "4", apk, aligned],
        check=True,
        env=env,
    )
    os.replace(aligned, apk)
    signed = apk + ".signed"
    subprocess.run(
        [
            os.path.join(SDK_BT, apksigner),
            "sign",
            "--ks",
            KEYSTORE,
            "--ks-pass",
            "pass:" + KEY_PASS,
            "--ks-key-alias",
            KEY_ALIAS,
            "--min-sdk-version",
            MIN_SDK,
            "--out",
            signed,
            apk,
        ],
        check=True,
        env=env,
    )
    os.replace(signed, apk)
    subprocess.run(
        [os.path.join(SDK_BT, apksigner), "verify", apk], check=True, env=env
    )
    print("[sign] ok ->", apk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--sign", action="store_true")
    ap.add_argument("--patcher", action="store_true")
    ap.add_argument("--dex", default=None)
    ap.add_argument("--abi", default="armeabi-v7a")
    ap.add_argument("--so", required=True, help="libbe5hook.so for the selected ABI")
    ap.add_argument(
        "--core",
        required=True,
        help="built BepInEx core directory",
    )
    ap.add_argument(
        "--payload",
        required=True,
        help="asset root containing config/",
    )
    ap.add_argument("--boot", required=True, help="Be5Bootstrap.dll")
    ap.add_argument("--plugins-dir", default=None)
    ap.add_argument("--no-plugins", action="store_true")
    ap.add_argument(
        "--no-payload-hooks",
        action="store_true",
        help="do not include HOOKS-Assembly-CSharp.dll from the core directory",
    )
    ap.add_argument(
        "--tools",
        default=None,
        help="tool directory with build-tools/ and jdk/ (env: BE5_TOOLS)",
    )
    ap.add_argument("--sdk-build-tools", default=None)
    ap.add_argument("--jdk", default=None)
    ap.add_argument("--keystore", default=None)
    ap.add_argument("--alias", default=None)
    ap.add_argument("--ks-pass", default=None)
    ap.add_argument("--min-sdk", default=None)
    ap.add_argument(
        "--hooks",
        default=None,
        help="HOOKS-Assembly-CSharp.dll to put in assets/BepInEx/core/ (overrides payload)",
    )
    a = ap.parse_args()
    global DEX_OVERRIDE, HOOKS_OVERRIDE, ABI, SO, CORE, PAYLOAD, BOOT
    global PLUGINS_DIR, INCLUDE_PLUGINS, EXCLUDE_PAYLOAD_HOOKS
    global SDK_BT, JDK, KEYSTORE, KEY_ALIAS, KEY_PASS, MIN_SDK
    DEX_OVERRIDE = a.dex
    HOOKS_OVERRIDE = a.hooks
    ABI = a.abi
    SO = a.so
    CORE = a.core
    PAYLOAD = a.payload
    BOOT = a.boot
    if a.plugins_dir:
        PLUGINS_DIR = a.plugins_dir
    if a.no_plugins:
        INCLUDE_PLUGINS = False
    if a.no_payload_hooks:
        EXCLUDE_PAYLOAD_HOOKS = True
    if a.tools:
        os.environ["BE5_TOOLS"] = a.tools
        if not a.sdk_build_tools:
            SDK_BT = _find_build_tools()
        if not a.jdk:
            JDK = _find_jdk()
        if not a.keystore:
            KEYSTORE = _find_keystore()
    if a.sdk_build_tools:
        SDK_BT = a.sdk_build_tools
    if a.jdk:
        JDK = a.jdk
    if a.keystore:
        KEYSTORE = a.keystore
    if a.alias:
        KEY_ALIAS = a.alias
    if a.ks_pass:
        KEY_PASS = a.ks_pass
    if a.min_sdk:
        MIN_SDK = a.min_sdk
    repack(a.input, a.output, with_patcher=a.patcher)
    if a.sign:
        sign(a.output)


if __name__ == "__main__":
    main()
