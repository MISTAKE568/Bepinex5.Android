#!/usr/bin/env python3
"""patch_dex.py — move the BepInEx shim load into UnityPlayer.loadNative().

Why this patch is required
--------------------------
`System.loadLibrary("be5hook")` must run **after** `libmain.so` is loaded and
**before** `initJni()` starts the engine:

    UnityPlayer.<init>
        loadNative(path)      -> System.load(".../libmain.so")   [libmain + libunity + libmono]
        System.loadLibrary("be5hook")                             [<-- we inject here]
        initJni(context)      -> nativeInit, mono_jit_init, assembly loads, Awake...

If the shim is loaded from `UnityPlayerActivity.onCreate` (before `new UnityPlayer`),
libmain/libunity/libmono are not loaded yet and the shim cannot install its
dlsym/GOT hooks in time.

This script:
  1. baksmali the input dex
  2. removes any existing `loadLibrary("be5hook")` block (old injection point)
  3. inserts the call at the successful end of native library loading inside
     `UnityPlayer.loadNative`:
       - newer Unity: after the merge label following `System.load(...)`
       - older Unity: after `NativeLoader.load(...)` returns true
  4. smali the result back

The label name (`:goto_1c` in Unity 2020.3) is discovered dynamically, so the
script is not tied to one offset.

Requirements: a JRE, baksmali.jar and smali.jar (same major version, e.g. 2.5.x).

Usage:
  python patch_dex.py --input classes.dex --output classes.dex \
      --baksmali baksmali.jar --smali smali.jar [--java java] [--keep-smali DIR]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

ACTIVITY = "com/unity3d/player/UnityPlayerActivity.smali"
PLAYER = "com/unity3d/player/UnityPlayer.smali"

INJECT_BLOCK = (
    '    const-string {reg}, "be5hook"\n'
    "\n"
    "    invoke-static {{{reg}}}, Ljava/lang/System;->loadLibrary(Ljava/lang/String;)V\n"
    "\n"
)

# matches an injected loadLibrary("be5hook") pair, with or without blank lines
INJECT_RE = re.compile(
    r'^[ \t]*const-string[ \t]+(?P<reg>[vp]\d+),[ \t]*"be5hook"[ \t]*\r?\n'
    r'(?:[ \t]*\r?\n)?'
    r'[ \t]*invoke-static[ \t]+\{(?P=reg)\},[ \t]*Ljava/lang/System;->loadLibrary'
    r'\(Ljava/lang/String;\)V[ \t]*\r?\n'
    r'(?:[ \t]*\r?\n)?',
    re.MULTILINE,
)


def run(cmd):
    print("[dex] " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def strip_existing(text):
    """Remove every loadLibrary("be5hook") block (old onCreate injection)."""
    new, n = INJECT_RE.subn("", text)
    if n:
        print("[dex] removed %d existing be5hook injection(s)" % n)
    return new


LOADNATIVE_RE = re.compile(
    r'^\.method[^\n]*\bloadNative\('
    r'(?P<sig>Ljava/lang/String;|Landroid/content/pm/ApplicationInfo;)'
    r'\)Ljava/lang/String;',
    re.MULTILINE,
)


def find_loadnative_block(text):
    """Return (start, end) character offsets of UnityPlayer.loadNative."""
    m = LOADNATIVE_RE.search(text)
    if not m:
        raise SystemExit(
            "loadNative(String) or loadNative(ApplicationInfo) not found in " + PLAYER)
    end = text.find("\n.end method", m.end())
    if end < 0:
        raise SystemExit("unterminated loadNative method")
    return m.start(), end


def _skip_line_break(text, pos):
    if text.startswith("\r\n", pos):
        return pos + 2
    if text.startswith("\n", pos):
        return pos + 1
    return pos


def _skip_blank_lines(text, pos):
    while True:
        new_pos = _skip_line_break(text, pos)
        if new_pos == pos:
            return pos
        pos = new_pos


def inject_into_loadnative(text):
    start, end = find_loadnative_block(text)
    block = text[start:end]

    # Newer Unity: System.load(".../libmain.so") inside the try block.
    # Insert after the merge label reached on success.
    load_m = re.search(
        r'^[ \t]*invoke-static[ \t]+\{[vp]\d+\},[ \t]*Ljava/lang/System;->load'
        r'\(Ljava/lang/String;\)V[ \t]*$',
        block, re.MULTILINE)
    if load_m:
        # first `goto :label` after the load call, then the label definition itself
        goto_m = re.search(r'^[ \t]*goto[ \t]+:(?P<label>\w+)[ \t]*$',
                           block[load_m.end():], re.MULTILINE)
        if not goto_m:
            raise SystemExit("no merge label after System.load(...)")
        label = goto_m.group("label")

        label_re = re.compile(r'^[ \t]*:' + re.escape(label) + r'[ \t]*$', re.MULTILINE)
        label_m = label_re.search(block)
        if not label_m:
            raise SystemExit("merge label :%s not found" % label)

        insert_at = start + label_m.end()
        # keep the blank line that follows the label, insert after it
        insert_at = _skip_line_break(text, insert_at)

        injected = text[:insert_at] + INJECT_BLOCK.format(reg="v1") + text[insert_at:]
        print("[dex] inserted loadLibrary(\"be5hook\") after :%s in loadNative" % label)
        return injected

    # Older Unity: NativeLoader.load(path) returns a boolean. Insert after the
    # `if-eqz` success fallthrough, before the engine is initialised.
    loader_m = re.search(
        r'^[ \t]*invoke-static[ \t]+\{[vp]\d+\},[ \t]*'
        r'Lcom/unity3d/player/NativeLoader;->load\(Ljava/lang/String;\)Z[ \t]*$',
        block, re.MULTILINE)
    if not loader_m:
        raise SystemExit("System.load(...) or NativeLoader.load(...) not found in loadNative")

    tail = block[loader_m.end():]
    if_m = re.search(r'^[ \t]*if-eqz[ \t]+[vp]\d+,[ \t]*:\w+[ \t]*$',
                     tail, re.MULTILINE)
    if not if_m:
        raise SystemExit("no if-eqz success check after NativeLoader.load(...)")

    insert_at = start + loader_m.end() + if_m.end()
    insert_at = _skip_line_break(text, insert_at)
    insert_at = _skip_blank_lines(text, insert_at)

    injected = text[:insert_at] + INJECT_BLOCK.format(reg="v1") + text[insert_at:]
    print("[dex] inserted loadLibrary(\"be5hook\") after NativeLoader.load success in loadNative")
    return injected


def patch_smali_dir(smali_dir):
    activity = os.path.join(smali_dir, ACTIVITY)
    player = os.path.join(smali_dir, PLAYER)
    for p in (activity, player):
        if not os.path.isfile(p):
            raise SystemExit("missing " + p)

    with open(activity, "r", encoding="utf-8") as f:
        a = f.read()
    a2 = strip_existing(a)
    if a2 != a:
        with open(activity, "w", encoding="utf-8", newline="") as f:
            f.write(a2)

    with open(player, "r", encoding="utf-8") as f:
        p = f.read()
    p2 = strip_existing(p)
    if "be5hook" in p2:
        print("[dex] loadNative already patched, leaving as is")
        return
    p2 = inject_into_loadnative(p2)
    with open(player, "w", encoding="utf-8", newline="") as f:
        f.write(p2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="input classes.dex")
    ap.add_argument("--output", required=True, help="output classes.dex")
    ap.add_argument("--baksmali", default=os.environ.get("BAKSMALI_JAR", "baksmali.jar"))
    ap.add_argument("--smali", default=os.environ.get("SMALI_JAR", "smali.jar"))
    ap.add_argument("--java", default=os.environ.get("JAVA", "java"))
    ap.add_argument("--keep-smali", default=None, help="copy the patched smali tree here")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="be5dex_")
    try:
        smali_dir = os.path.join(tmp, "smali")
        run([args.java, "-jar", args.baksmali, "d", args.input, "-o", smali_dir])
        patch_smali_dir(smali_dir)
        if args.keep_smali:
            if os.path.isdir(args.keep_smali):
                shutil.rmtree(args.keep_smali)
            shutil.copytree(smali_dir, args.keep_smali)
        run([args.java, "-jar", args.smali, "a", smali_dir, "-o", args.output])
        print("[dex] wrote %s (%d bytes)" % (args.output, os.path.getsize(args.output)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
