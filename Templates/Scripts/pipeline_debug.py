#!/usr/bin/env python3
# pipeline_debug.py -- read-only health dump for the vault's launchd pipelines.
#
# For every LaunchAgent whose program runs a Templates/Scripts script it prints:
#   loaded?  last exit status  running state  log age  interpreter/script
#   existence  and the tail of the stderr/stdout logs (the actual error).
#
# Safe to run anytime: it only reads plists, stats log files, runs
# `launchctl list`, and tails logs. It changes nothing.
#
# Run:  /usr/bin/python3 '~/Obsidian/Templates/Scripts/pipeline_debug.py'

import os
import re
import glob
import time
import plistlib
import subprocess
from pathlib import Path

HOME = Path.home()
LA = HOME / "Library" / "LaunchAgents"
MARKER = "/Obsidian/Templates/Scripts/"


def launchctl(label):
    try:
        p = subprocess.run(["/bin/launchctl", "list", label], capture_output=True)
    except Exception as e:
        return None, None, "launchctl error: %s" % e
    if p.returncode != 0:
        return False, None, "NOT LOADED"
    out = p.stdout.decode(errors="ignore")
    m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
    exit_code = int(m.group(1)) if m else None
    running = bool(re.search(r'"PID"\s*=\s*\d+', out))
    return True, exit_code, ("running now" if running else "idle")


def fmt_age(path):
    try:
        secs = time.time() - os.stat(path).st_mtime
    except OSError:
        return "no file"
    h = secs / 3600.0
    if h < 1:
        return "%dm ago" % int(secs // 60)
    if h < 48:
        return "%.1fh ago" % h
    return "%.1fd ago" % (h / 24.0)


def tail(path, n=12):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    return data.decode(errors="ignore").splitlines()[-n:]


def main():
    found = 0
    for pp in sorted(glob.glob(str(LA / "*.plist"))):
        try:
            with open(pp, "rb") as f:
                pl = plistlib.load(f)
        except Exception:
            continue
        args = pl.get("ProgramArguments") or []
        if not any(MARKER in str(a) for a in args):
            continue
        found += 1
        label = pl.get("Label") or Path(pp).stem
        interp = str(args[0]) if args else ""
        script = ""
        for a in args[1:]:
            if str(a).endswith(".py"):
                script = str(a)
                break

        loaded, exit_code, run_state = launchctl(label)
        print("=" * 72)
        print("LABEL:    %s" % label)
        print("loaded:   %s    last exit: %s    (%s)" % (loaded, exit_code, run_state))
        print("interp:   %s  [%s]" % (interp, "exists" if os.path.exists(interp) else "MISSING"))
        if script:
            print("script:   %s  [%s]" % (script, "exists" if os.path.exists(script) else "MISSING"))
        for key in ("StandardOutPath", "StandardErrorPath"):
            lp = pl.get(key)
            if lp:
                print("%s: %s  (%s)" % (key, lp, fmt_age(lp)))
        for lbl, key in (("stderr", "StandardErrorPath"), ("stdout", "StandardOutPath")):
            p = pl.get(key)
            if not p:
                continue
            t = tail(p)
            if t:
                print("  --- %s tail: %s ---" % (lbl, p))
                for line in t:
                    print("    " + line)
    print("=" * 72)
    if not found:
        print("No vault pipelines found in %s" % LA)
    else:
        print("%d vault pipeline(s) inspected." % found)


if __name__ == "__main__":
    main()
