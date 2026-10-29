#!/usr/bin/env python3
"""Build the BepInEx core from ``src/BepInEx`` for maintainers and CI.

This is not a prerequisite for ordinary APK packaging: users consume the
prebuilt core from the downloaded CI asset pack.

The Android packaging step consumes ``out/core``; it never copies a checked-in
``payload/core``.  This script publishes ``BepInEx.Preloader`` (which also
builds BepInEx, BepInEx.Harmony, HarmonyXInterop and HarmonyX2Interop) and then
keeps only the assemblies and config that are actually loaded on Android.

Examples
--------
    # Build and stage out/core
    python build_core.py

    # Verify an existing out/core without rebuilding
    python build_core.py --verify-only

    # Inspect the command that would run
    python build_core.py --dry-run

Requirements: .NET SDK 8 or newer (Windows or Linux) and network access for the
first NuGet restore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(ROOT, "src", "BepInEx")
DEFAULT_OUT = os.path.join(ROOT, "out", "core")
PROJECT_REL = os.path.join("BepInEx.Preloader", "BepInEx.Preloader.csproj")
CONFIG_NAME = "BepInEx.Preloader.dll.config"
NET35_TARGETS = """<Project>
  <ItemGroup Condition="'$(TargetFramework)' == 'net35'">
    <PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies.net35" Version="1.0.3" PrivateAssets="all" />
  </ItemGroup>
</Project>
"""

# The exact runtime set consumed by repack_native.py.  XML docs, PDBs and
# Mono.Cecil.Mdb/Pdb/Rocks are deliberately excluded.
CORE_FILES = (
    "BepInEx.dll",
    "BepInEx.Preloader.dll",
    CONFIG_NAME,
    "BepInEx.Harmony.dll",
    "0Harmony.dll",
    "0Harmony20.dll",
    "HarmonyXInterop.dll",
    "Mono.Cecil.dll",
    "MonoMod.RuntimeDetour.dll",
    "MonoMod.Utils.dll",
)

# Fail early if a game-specific assembly is ever staged as "core".
FORBIDDEN_FILES = ("HOOKS-Assembly-CSharp.dll",)


class CoreBuildError(RuntimeError):
    """A user-facing core build failure."""


def log(msg):
    print("[core] " + msg, flush=True)


def warn(msg):
    print("[core] WARNING: " + msg, file=sys.stderr, flush=True)


def expand(path):
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def read_sha256sums(path):
    sums = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise CoreBuildError(
                    "%s:%d: expected '<sha256>  <name>'" % (path, line_no)
                )
            digest, name = parts[0].lower(), parts[1].lstrip("*")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise CoreBuildError("%s:%d: invalid SHA-256" % (path, line_no))
            if name in sums:
                raise CoreBuildError("%s:%d: duplicate entry %s" % (path, line_no, name))
            sums[name] = digest
    return sums


def read_source_version(src_dir):
    props = os.path.join(src_dir, "Directory.Build.props")
    if not os.path.isfile(props):
        raise CoreBuildError("Directory.Build.props not found: " + props)
    try:
        root = ET.parse(props).getroot()
    except ET.ParseError as exc:
        raise CoreBuildError("cannot parse %s: %s" % (props, exc)) from exc
    prefix = root.findtext(".//BepInExVersionPrefix")
    if not prefix:
        raise CoreBuildError("BepInExVersionPrefix missing from " + props)
    prefix = prefix.strip()

    project_json = os.path.join(ROOT, "project.json")
    if os.path.isfile(project_json):
        try:
            with open(project_json, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise CoreBuildError("cannot read project.json: %s" % exc) from exc
        upstream = ((data.get("upstream") or {}).get("version") or "").strip()
        if upstream and upstream != prefix:
            raise CoreBuildError(
                "source version %s does not match project.json upstream.version %s"
                % (prefix, upstream)
            )
    return prefix


def git_revision():
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def find_dotnet(explicit):
    candidate = explicit or os.environ.get("DOTNET") or shutil.which("dotnet")
    if not candidate:
        raise CoreBuildError(
            "dotnet not found; install .NET SDK 8+ or pass --dotnet <dotnet>"
        )
    candidate = expand(candidate)
    if not os.path.isfile(candidate):
        raise CoreBuildError("dotnet not found: " + candidate)
    return candidate


def dotnet_version(dotnet):
    try:
        return subprocess.check_output(
            [dotnet, "--version"], text=True, stderr=subprocess.STDOUT
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CoreBuildError("cannot run %s --version: %s" % (dotnet, exc)) from exc


def publish_command(dotnet, project, configuration, publish_dir, no_restore):
    cmd = [
        dotnet,
        "publish",
        project,
        "-c",
        configuration,
        "-o",
        publish_dir,
        "--nologo",
        "-v:minimal",
    ]
    if no_restore:
        cmd.append("--no-restore")
    return cmd


def ensure_net35_targets(src_dir, dry_run=False):
    """Generate the ignored Linux build input; never overwrite local customizations."""
    path = os.path.join(src_dir, "Directory.Build.targets")
    expected = NET35_TARGETS.encode("utf-8")
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isfile(path):
            raise CoreBuildError("net35 targets must be a regular file: " + path)
        with open(path, "rb") as f:
            if f.read() != expected:
                raise CoreBuildError(
                    "net35 targets differ from build_core.py template; "
                    "review the existing file before rebuilding: " + path
                )
    elif not dry_run:
        with open(path, "xb") as f:
            f.write(expected)
    log("net35 reference targets%s: %s" % (" (planned)" if dry_run else "", path))


def check_assembly(path):
    with open(path, "rb") as f:
        blob = f.read()
    if not blob.startswith(b"MZ"):
        raise CoreBuildError("not a PE/.NET assembly: " + path)
    if b"BSJB" not in blob:
        raise CoreBuildError("missing .NET metadata (BSJB): " + path)


def check_config(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as exc:
        raise CoreBuildError("cannot read %s: %s" % (path, exc)) from exc
    for needle in ("libc.so.6", "libc.so", "dllmap"):
        if needle not in text:
            raise CoreBuildError(
                "%s does not contain the Android dllmap entry (%s)"
                % (path, needle)
            )


def validate_core(directory, require_manifest=False):
    """Validate the staged folder and return a small summary."""
    directory = expand(directory)
    if not os.path.isdir(directory):
        raise CoreBuildError(
            "core directory not found: %s\n  run: python build_core.py" % directory
        )

    sums_path = os.path.join(directory, "SHA256SUMS")
    if not os.path.isfile(sums_path):
        raise CoreBuildError(
            "core/SHA256SUMS not found: %s\n  run: python build_core.py" % sums_path
        )
    sums = read_sha256sums(sums_path)
    if not sums:
        raise CoreBuildError("core/SHA256SUMS is empty: " + sums_path)

    manifest = None
    manifest_path = os.path.join(directory, "manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as exc:
            raise CoreBuildError(
                "cannot read core manifest %s: %s" % (manifest_path, exc)
            ) from exc
        if manifest.get("kind") != "bepinex-core":
            raise CoreBuildError("unexpected core manifest kind: " + manifest_path)
        files = manifest.get("files") or []
        names = [item.get("name") for item in files if isinstance(item, dict)]
        if not all(names):
            raise CoreBuildError("invalid files list in " + manifest_path)
    elif require_manifest:
        raise CoreBuildError("core manifest not found: " + manifest_path)
    else:
        names = sorted(sums)

    if len(set(names)) != len(names):
        raise CoreBuildError("duplicate file names in core manifest")
    missing_entries = [name for name in CORE_FILES if name not in sums]
    if missing_entries:
        raise CoreBuildError(
            "core/SHA256SUMS is missing: " + ", ".join(missing_entries)
        )
    missing_names = [name for name in CORE_FILES if name not in names]
    if missing_names:
        raise CoreBuildError(
            "core manifest is missing: " + ", ".join(missing_names)
        )
    extra = sorted(set(sums) - set(names))
    if manifest is not None and extra:
        raise CoreBuildError(
            "core/SHA256SUMS contains files not in manifest.json: " + ", ".join(extra)
        )

    checked = []
    for name in names:
        if os.path.basename(name) != name or name in (".", ".."):
            raise CoreBuildError("unsafe core file name: " + name)
        if name in FORBIDDEN_FILES:
            raise CoreBuildError(
                "%s is game-specific and must not be part of BepInEx core" % name
            )
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            raise CoreBuildError("core file missing: " + path)
        actual = sha256_file(path)
        expected = sums.get(name)
        if not expected:
            raise CoreBuildError("core/SHA256SUMS has no entry for " + name)
        if actual != expected:
            raise CoreBuildError(
                "core file hash mismatch: %s\n  expected %s\n  actual   %s"
                % (name, expected, actual)
            )
        if name.endswith(".dll"):
            check_assembly(path)
        elif name == CONFIG_NAME:
            check_config(path)
        checked.append(
            {"name": name, "size": os.path.getsize(path), "sha256": actual}
        )

    return {
        "path": directory,
        "files": checked,
        "sha256sums": sha256_file(sums_path),
        "manifest": manifest,
        "manifest_path": manifest_path if os.path.isfile(manifest_path) else None,
    }


def stage(src_dir, publish_dir, out_dir, source_config, version, configuration, dotnet, dotnet_ver, command):
    parent = os.path.dirname(os.path.abspath(out_dir))
    os.makedirs(parent, exist_ok=True)
    tmp_dir = out_dir + ".tmp"
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir)

    try:
        for name in CORE_FILES:
            src = os.path.join(publish_dir, name)
            if name == CONFIG_NAME and not os.path.isfile(src):
                src = source_config
            if not os.path.isfile(src):
                raise CoreBuildError(
                    "publish output is missing %s\n  looked in %s"
                    % (name, publish_dir)
                )
            shutil.copy2(src, os.path.join(tmp_dir, name))

        # Validate before writing the hash list/manifest.
        for name in CORE_FILES:
            path = os.path.join(tmp_dir, name)
            if name.endswith(".dll"):
                check_assembly(path)
            elif name == CONFIG_NAME:
                check_config(path)

        entries = []
        for name in sorted(CORE_FILES):
            path = os.path.join(tmp_dir, name)
            entries.append(
                {
                    "name": name,
                    "size": os.path.getsize(path),
                    "sha256": sha256_file(path),
                }
            )

        sums_path = os.path.join(tmp_dir, "SHA256SUMS")
        with open(sums_path, "w", encoding="utf-8", newline="\n") as f:
            for item in entries:
                f.write("%s  %s\n" % (item["sha256"], item["name"]))

        manifest = {
            "schema": 1,
            "kind": "bepinex-core",
            "built_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "root": os.path.relpath(src_dir, ROOT).replace("\\", "/"),
                "project": PROJECT_REL.replace("\\", "/"),
                "revision": git_revision(),
                "version": version,
            },
            "build": {
                "configuration": configuration,
                "dotnet": dotnet_ver,
                "command": command,
            },
            "files": entries,
            "sha256sums": sha256_file(sums_path),
        }
        write_json(os.path.join(tmp_dir, "manifest.json"), manifest)

        validate_core(tmp_dir, require_manifest=True)

        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        os.replace(tmp_dir, out_dir)
        return manifest
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def command_line(argv=None):
    ap = argparse.ArgumentParser(
        description="Build BepInEx core into out/core (runtime files only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--src", default=DEFAULT_SRC, help="src/BepInEx directory")
    ap.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help="staged core directory (default: out/core)",
    )
    ap.add_argument("--configuration", default="Release", help="build configuration")
    ap.add_argument("--dotnet", help="dotnet executable (default: PATH or DOTNET)")
    ap.add_argument("--no-restore", action="store_true", help="pass --no-restore")
    ap.add_argument(
        "--keep-publish",
        action="store_true",
        help="keep the full dotnet publish output next to --out",
    )
    ap.add_argument(
        "--verify-only",
        action="store_true",
        help="validate --out without building",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved paths/command without writing files",
    )
    return ap.parse_args(argv)


def main(argv=None):
    args = command_line(argv)
    try:
        src_dir = expand(args.src)
        out_dir = expand(args.out)
        project = os.path.join(src_dir, PROJECT_REL)
        source_config = os.path.join(
            src_dir, "BepInEx.Preloader", CONFIG_NAME
        )
        if not os.path.isfile(project):
            raise CoreBuildError("project not found: " + project)
        if not os.path.isfile(source_config):
            raise CoreBuildError("Android dllmap config not found: " + source_config)

        if args.verify_only:
            summary = validate_core(out_dir, require_manifest=True)
            log("verified %d files in %s" % (len(summary["files"]), out_dir))
            for item in summary["files"]:
                log("  %s  %s" % (item["sha256"][:12], item["name"]))
            log("SHA256SUMS: " + summary["sha256sums"])
            return 0

        version = read_source_version(src_dir)
        dotnet = find_dotnet(args.dotnet)
        dotnet_ver = dotnet_version(dotnet)
        publish_dir = out_dir + ".publish"

        cmd = publish_command(
            dotnet, project, args.configuration, publish_dir, args.no_restore
        )

        log("source:  " + src_dir)
        log("project: " + project)
        log("version: " + version)
        log("dotnet:  %s (%s)" % (dotnet, dotnet_ver))
        log("output:  " + out_dir)
        log("$ " + " ".join(cmd))

        if sys.platform.startswith("linux"):
            ensure_net35_targets(src_dir, dry_run=args.dry_run)

        if args.dry_run:
            log("dry run: no files were changed")
            return 0

        if os.path.isdir(publish_dir):
            shutil.rmtree(publish_dir)
        os.makedirs(publish_dir, exist_ok=True)

        try:
            subprocess.run(
                cmd,
                cwd=src_dir,
                check=True,
                env={
                    **os.environ,
                    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                    "DOTNET_NOLOGO": "1",
                    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
                },
            )
        except FileNotFoundError as exc:
            raise CoreBuildError("dotnet not found: " + dotnet) from exc
        except subprocess.CalledProcessError as exc:
            raise CoreBuildError(
                "dotnet publish failed with exit code %d" % exc.returncode
            ) from exc

        manifest = stage(
            src_dir,
            publish_dir,
            out_dir,
            source_config,
            version,
            args.configuration,
            dotnet,
            dotnet_ver,
            cmd,
        )
        summary = validate_core(out_dir, require_manifest=True)

        if args.keep_publish:
            log("publish output kept at " + publish_dir)
        else:
            shutil.rmtree(publish_dir, ignore_errors=True)

        log("built BepInEx %s -> %s" % (version, out_dir))
        for item in summary["files"]:
            log("  %s  %s" % (item["sha256"][:12], item["name"]))
        log("SHA256SUMS: " + os.path.join(out_dir, "SHA256SUMS"))
        log("manifest:   " + os.path.join(out_dir, "manifest.json"))
        log("revision:   " + manifest["source"]["revision"])
        return 0
    except CoreBuildError as exc:
        print("[core] ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[core] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
