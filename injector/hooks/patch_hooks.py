#!/usr/bin/env python3
"""Resolve HOOKS-Assembly-CSharp.dll for an APK build.

Order of attempts:

  1. extract the game's official MMHOOK from the APK:
     assets/ModLoader/HOOKS/HOOKS-Assembly-CSharp.dll
  2. if absent, extract assets/bin/Data/Managed/ and generate one with
     MonoMod.HookGen from Assembly-CSharp.dll
  3. if generation is unavailable or fails, print a hint and skip
     (exit 0) so the rest of the build can continue

A skip means mods that reference HOOKS-Assembly-CSharp (On.* / IL.*) may
fail to resolve it.  Pass --strict to make a skip fatal.

Outputs:

  <out>/HOOKS-Assembly-CSharp.dll   when successful
  <out>/hookgen.json                always (status/source/hash/reason)
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

NAME = "HOOKS-Assembly-CSharp.dll"
OFFICIAL = "assets/ModLoader/HOOKS/" + NAME
HOOKGEN_NAMES = (
    "MonoMod.RuntimeDetour.HookGen.exe",
    "HookGen_net35.exe",
    "MonoMod.RuntimeDetour.HookGen",
    "HookGen_net35",
)
MONO_NAMES = ("mono", "mono.exe")

HINT = (
    "Install MonoMod.RuntimeDetour.HookGen (22.7.31.1, lib/net35) and pass "
    "--hookgen <MonoMod.RuntimeDetour.HookGen.exe> (plus --mono <mono> on "
    "Windows), or use a base APK that ships "
    "assets/ModLoader/HOOKS/HOOKS-Assembly-CSharp.dll. The build can "
    "continue, but mods using On.*/IL.* may fail to resolve "
    "HOOKS-Assembly-CSharp."
)


def log(msg):
    print("[hooks] " + msg, flush=True)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def looks_like_dotnet(data):
    return len(data) > 1024 and data[:2] == b"MZ" and b"BSJB" in data


def write_json(path, result):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def find_official(z):
    names = z.namelist()
    if OFFICIAL in names:
        return OFFICIAL
    candidates = sorted(
        (n for n in names if n.endswith("/HOOKS/" + NAME)), key=len
    )
    return candidates[0] if candidates else None


def find_managed_prefix(z):
    names = z.namelist()
    exact = "assets/bin/Data/Managed/Assembly-CSharp.dll"
    if exact in names:
        return "assets/bin/Data/Managed/"
    candidates = sorted(
        (n for n in names if n.endswith("/Managed/Assembly-CSharp.dll")),
        key=len,
    )
    if not candidates:
        return None
    return candidates[0][: -len("Assembly-CSharp.dll")]


def extract_managed(z, prefix, dest):
    """Extract every .dll under prefix; HookGen needs the reference assemblies."""
    count = 0
    for info in z.infolist():
        if not info.filename.startswith(prefix):
            continue
        if not info.filename.endswith(".dll"):
            continue
        rel = info.filename[len(prefix) :]
        if not rel or rel.endswith("/"):
            continue
        dst = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with z.open(info) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        count += 1
    return count


def resolve_tool(arg, env_name, names):
    value = arg or os.environ.get(env_name)
    if value:
        path = os.path.abspath(os.path.expanduser(value))
        if os.path.isdir(path):
            for name in names:
                candidate = os.path.join(path, name)
                if os.path.isfile(candidate):
                    return candidate
            return None
        return path if os.path.isfile(path) else None
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def generate(assembly, hookgen, mono, out_dir):
    """Generate into a temp dir, validate, and return the bytes."""
    tmp_dir = tempfile.mkdtemp(prefix=".hookgen-", dir=out_dir)
    dst = os.path.join(tmp_dir, NAME)
    cmd = ([mono] if mono else []) + [
        hookgen,
        os.path.abspath(assembly),
        dst,
    ]
    log("generating: " + " ".join(cmd))
    try:
        # HookGen resolves its own MonoMod.* dependencies from the working
        # directory.  Running it from the game's Managed/ directory makes it
        # fail with "MonoMod.RuntimeDetour not found!" because the game does
        # not ship those assemblies.
        proc = subprocess.run(
            cmd,
            cwd=os.path.dirname(os.path.abspath(hookgen)),
            capture_output=True,
            text=True,
        )
        if proc.stdout.strip():
            log("HookGen stdout:\n" + proc.stdout.strip())
        if proc.stderr.strip():
            log("HookGen stderr:\n" + proc.stderr.strip())
        if proc.returncode != 0:
            raise RuntimeError("HookGen exited with %d" % proc.returncode)
        if not os.path.isfile(dst):
            raise RuntimeError("HookGen produced no output")
        data = open(dst, "rb").read()
        if not looks_like_dotnet(data):
            raise RuntimeError("HookGen output is not a .NET assembly")
        return data
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def finish(result, out_dir, json_path, strict):
    status = result["status"]
    if status == "skipped":
        stale = os.path.join(out_dir, NAME)
        if os.path.isfile(stale):
            os.remove(stale)
            log("removed stale " + stale)
        log("SKIP: " + result.get("reason", "unknown"))
        log("hint: " + HINT)
    else:
        log(
            "%s %s (%d bytes, sha256=%s)"
            % (
                status,
                result["path"],
                result["size"],
                result["sha256"],
            )
        )
        if status == "generated":
            log(
                "WARNING: this MMHOOK was generated, not extracted from the game; "
                "mods compiled against the official HOOKS may not bind."
            )
    write_json(json_path, result)
    if status == "skipped" and strict:
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Resolve HOOKS-Assembly-CSharp.dll for an APK build."
    )
    ap.add_argument(
        "--apk",
        default=None,
        help="base APK (omit when using --assembly directly)",
    )
    ap.add_argument("--out", default="out/hookgen", help="output directory")
    ap.add_argument(
        "--assembly",
        default=None,
        help="use this Assembly-CSharp.dll instead of extracting Managed/ from the APK",
    )
    ap.add_argument(
        "--hookgen",
        default=None,
        help="HookGen executable (or directory); env HOOKGEN",
    )
    ap.add_argument(
        "--mono",
        default=None,
        help="mono executable (or directory); env MONO",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when HOOKS cannot be obtained (default: skip, exit 0)",
    )
    ap.add_argument(
        "--keep-temp",
        action="store_true",
        help="keep the extracted Managed/ directory for debugging",
    )
    a = ap.parse_args()

    if not a.apk and not a.assembly:
        ap.error("--apk or --assembly is required")

    apk = os.path.abspath(a.apk) if a.apk else None
    out_dir = os.path.abspath(a.out)
    if apk and not os.path.isfile(apk):
        raise SystemExit("[hooks] APK not found: " + apk)
    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "hookgen.json")
    result = {
        "status": "skipped",
        "apk": apk or "",
        "out": out_dir,
    }
    tmp_managed = None
    assembly = None

    try:
        hookgen = resolve_tool(a.hookgen, "HOOKGEN", HOOKGEN_NAMES)
        mono = resolve_tool(a.mono, "MONO", MONO_NAMES)

        if apk:
            with zipfile.ZipFile(apk) as z:
                member = find_official(z)
                if member:
                    data = z.read(member)
                    if looks_like_dotnet(data):
                        dst = os.path.join(out_dir, NAME)
                        with open(dst, "wb") as f:
                            f.write(data)
                        result.update(
                            status="extracted",
                            source=member,
                            path=dst,
                            size=len(data),
                            sha256=sha256_bytes(data),
                        )
                        return finish(result, out_dir, json_path, a.strict)
                    log("official HOOKS is not a .NET assembly; trying generation")

                # Generation fallback: extract Managed/ unless --assembly was given.
                if not a.assembly:
                    prefix = find_managed_prefix(z)
                    if not prefix:
                        result["reason"] = (
                            "APK has no assets/ModLoader/HOOKS/%s and no "
                            "Managed/Assembly-CSharp.dll" % NAME
                        )
                        return finish(result, out_dir, json_path, a.strict)
                    if not hookgen:
                        result["reason"] = (
                            "HookGen not found (pass --hookgen or set HOOKGEN)"
                        )
                        return finish(result, out_dir, json_path, a.strict)
                    tmp_managed = tempfile.mkdtemp(prefix="be5-managed-")
                    count = extract_managed(z, prefix, tmp_managed)
                    if count == 0:
                        result["reason"] = "no .dll files under " + prefix
                        return finish(result, out_dir, json_path, a.strict)
                    assembly = os.path.join(tmp_managed, "Assembly-CSharp.dll")
                    log(
                        "extracted %d managed DLLs to %s"
                        % (count, tmp_managed)
                    )

        if a.assembly:
            assembly = os.path.abspath(a.assembly)

        if not assembly or not os.path.isfile(assembly):
            result["reason"] = "Assembly-CSharp.dll not found: %s" % assembly
            return finish(result, out_dir, json_path, a.strict)

        if not hookgen:
            result["reason"] = (
                "HookGen not found (pass --hookgen or set HOOKGEN)"
            )
            return finish(result, out_dir, json_path, a.strict)
        if not mono:
            log("mono not found; trying to run HookGen directly")

        try:
            data = generate(assembly, hookgen, mono, out_dir)
        except Exception as e:  # noqa: BLE001 - a failed generation is non-fatal
            result["reason"] = "HookGen failed: %s" % e
            result["hookgen"] = hookgen
            result["mono"] = mono
            return finish(result, out_dir, json_path, a.strict)

        dst = os.path.join(out_dir, NAME)
        with open(dst, "wb") as f:
            f.write(data)
        result.update(
            status="generated",
            source="HookGen",
            path=dst,
            size=len(data),
            sha256=sha256_bytes(data),
            assembly=assembly,
            hookgen=hookgen,
            mono=mono,
        )
        return finish(result, out_dir, json_path, a.strict)
    except zipfile.BadZipFile as e:
        raise SystemExit("[hooks] not a valid APK/zip: %s (%s)" % (apk, e))
    finally:
        if tmp_managed and not a.keep_temp:
            shutil.rmtree(tmp_managed, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
