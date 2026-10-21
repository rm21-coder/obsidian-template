#!/usr/bin/env python3
"""Scheduler entry point for the group-photos job.

Runs the two group-photo scripts beside this file in sequence, mirroring the
macOS com.obsidian.group-photos LaunchAgent's `insert && refresh` shell
pipeline. insert must run first: it adds the placeholder to any newly-added
member with no image yet, so a member who already has a matching headshot
file gets upgraded to the real photo in the same pass rather than waiting
for the next night.

Each step runs under `python -I` (stdlib only; nothing from the current
directory, the script's folder or PYTHON* variables is loaded), as the macOS
plist does. The scripts moved here from Z_attachments/ on 2026-10-20
(finding 5278622): code a scheduled job runs belongs in the hashed scripts
folder, not the synced attachments folder.
"""
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
STEPS = ("insert_group_placeholders.py", "refresh_groups.py")


def main() -> int:
    for name in STEPS:
        script = SCRIPTS / name
        result = subprocess.run([sys.executable, "-I", str(script), *sys.argv[1:]])
        if result.returncode != 0:
            print(f"ERROR: {name} exited {result.returncode}", file=sys.stderr)
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
