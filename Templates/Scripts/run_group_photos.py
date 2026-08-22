#!/usr/bin/env python3
"""Scheduler entry point for the group-photos job.

Runs the two Z_attachments group-photo scripts in sequence, mirroring the
macOS com.obsidian.group-photos LaunchAgent's `insert && refresh` shell
pipeline. insert must run first: it adds the placeholder to any newly-added
member with no image yet, so a member who already has a matching headshot
file gets upgraded to the real photo in the same pass rather than waiting
for the next night.
"""
import subprocess
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent.parent
ATTACHMENTS = VAULT / "Z_attachments"
STEPS = ("insert_group_placeholders.py", "refresh_groups.py")


def main() -> int:
    for name in STEPS:
        script = ATTACHMENTS / name
        result = subprocess.run([sys.executable, str(script), *sys.argv[1:]])
        if result.returncode != 0:
            print(f"ERROR: {name} exited {result.returncode}", file=sys.stderr)
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
