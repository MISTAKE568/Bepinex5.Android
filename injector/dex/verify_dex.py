#!/usr/bin/env python3
"""verify_dex.py — assert that a patched classes.dex loads the shim at the right place.

Checks (all must pass):

  1. exactly one `System.loadLibrary("be5hook")` in the whole dex;
  2. it lives in `UnityPlayer.smali`, inside `loadNative(String)` or
     `loadNative(ApplicationInfo)`;
  3. it comes after native library loading in that method
     (`System.load(...)` or a successful `NativeLoader.load(...)`);
  4. `UnityPlayerActivity.smali` contains no `be5hook` (old, broken injection point);
  5. the call is not inside a `try` block that could swallow failures silently.

Usage:
  python verify_dex.py --dex classes.dex --baksmali baksmali.jar --java java
  python verify_dex.py --smali-dir <patched smali tree>
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

PLAYER = "com/unity3d/player/UnityPlayer.smali"
ACTIVITY = "com/unity3d/player/UnityPlayerActivity.smali"

CALL_RE = re.compile(
    r'const-string[ \t]+(?P<reg>[vp]\d+),[ \t]*"be5hook"[ \t]*\r?\n'
    r'(?:[ \t]*\r?\n)?'
    r'[ \t]*invoke-static[ \t]+\{(?P=reg)\},[ \t]*Ljava/lang/System;->loadLibrary'
    r'\(Ljava/lang/String;\)V[ \t]*\r?\n',
    re.MULTILINE,
)


def fail(msg):
    print("[verify] FAIL: " + msg)
    sys.exit(1)


def ok(msg):
    print("[verify] ok: " + msg)


def find_method_block(text, signature_re):
    m = re.search(r'^\.method[^\n]*' + signature_re, text, re.MULTILINE)
    if not m:
        return None
    end = text.find("\n.end method", m.end())
    if end < 0:
        return None
    return text[m.start():end]


def check_tree(smali_dir):
    player_path = os.path.join(smali_dir, PLAYER)
    if not os.path.isfile(player_path):
        fail("missing " + PLAYER)

    # 1/2. locate the single injection
    hits = []
    for dp, _, files in os.walk(smali_dir):
        for fn in files:
            if not fn.endswith(".smali"):
                continue
            p = os.path.join(dp, fn)
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                t = f.read()
            for m in CALL_RE.finditer(t):
                hits.append((os.path.relpath(p, smali_dir), m.start(), t))

    if len(hits) != 1:
        fail("expected exactly 1 loadLibrary(\"be5hook\"), found %d: %s"
             % (len(hits), [h[0] for h in hits]))
    rel, pos, text = hits[0]
    if rel.replace("\\", "/") != PLAYER:
        fail("injection is in %s, expected %s" % (rel, PLAYER))
    ok("single injection in %s" % PLAYER)

    # 3. inside loadNative, after native library loading
    block = find_method_block(
        text,
        r'\bloadNative\((?:Ljava/lang/String;|Landroid/content/pm/ApplicationInfo;)'
        r'\)Ljava/lang/String;')
    if block is None:
        fail("loadNative(String/ApplicationInfo)String not found")
    start = text.find(block)
    end = start + len(block)
    if not (start <= pos < end):
        fail("injection is outside loadNative")
    before = text[start:pos]
    if re.search(r'invoke-static[ \t]+\{[vp]\d+\},[ \t]*Ljava/lang/System;->load'
                 r'\(Ljava/lang/String;\)V', before):
        ok("injection is inside loadNative, after System.load(...)")
    elif re.search(r'invoke-static[ \t]+\{[vp]\d+\},[ \t]*'
                   r'Lcom/unity3d/player/NativeLoader;->load\(Ljava/lang/String;\)Z',
                   before) and re.search(r'^[ \t]*if-eqz[ \t]+[vp]\d+,[ \t]*:\w+[ \t]*$',
                                          before, re.MULTILINE):
        ok("injection is inside loadNative, after NativeLoader.load(...) success")
    else:
        fail("injection appears before native library loading in loadNative")

    # 4. activity must be clean
    activity_path = os.path.join(smali_dir, ACTIVITY)
    if os.path.isfile(activity_path):
        with open(activity_path, "r", encoding="utf-8", errors="replace") as f:
            if "be5hook" in f.read():
                fail("UnityPlayerActivity still mentions be5hook")
        ok("UnityPlayerActivity is clean")

    # 5. not inside a try/catch range: every `:try_start_*` / `:try_end_*` pair must not enclose it
    for m in re.finditer(r'^\s*:try_start_(?P<id>\w+)\s*$', text[start:end], re.MULTILINE):
        s = start + m.start()
        em = re.search(r'^\s*:try_end_' + re.escape(m.group("id")) + r'\s*$',
                       text[s:end], re.MULTILINE)
        if em and s < pos < s + em.end():
            fail("injection is inside try_start_%s" % m.group("id"))
    ok("injection is outside try/catch handlers")

    print("[verify] PASS")


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dex", help="patched classes.dex")
    src.add_argument("--smali-dir", help="already-decompiled smali tree")
    ap.add_argument("--baksmali", default=os.environ.get("BAKSMALI_JAR", "baksmali.jar"))
    ap.add_argument("--java", default=os.environ.get("JAVA", "java"))
    args = ap.parse_args()

    if args.smali_dir:
        check_tree(args.smali_dir)
        return

    tmp = tempfile.mkdtemp(prefix="be5verify_")
    try:
        out = os.path.join(tmp, "smali")
        subprocess.run([args.java, "-jar", args.baksmali, "d", args.dex, "-o", out],
                       check=True)
        check_tree(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
