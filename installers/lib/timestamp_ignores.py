"""timestamp_ignores.py - keep the update-time-on-edit plugin off the files
this repository tracks.

The template ships update-time-on-edit enabled. With no settings it ignores
nothing, so whenever Obsidian sees a tracked document change -- a git pull
counts -- the plugin writes created:/updated: frontmatter into it. A 2026-10-15
Windows run found CLAUDE.md, ONBOARDING.md, README.md and a docs/ page
modified that way: update.ps1 then refuses over local changes, an upstream
edit to the same file conflicts on the next pull, and the CLAUDE.md edit is an
integrity finding.

The fix is the plugin's own ignore list (ignoreGlobalFolder, matched as a
path prefix, so a file path works as well as a folder). It cannot ship as a
tracked data.json: the plugin rewrites that file on every stamp, so a tracked
copy would itself be a local change. This helper merges the entries into
whatever data.json is there instead, keeping every other setting and every
entry the user added, and creates the file when there is none (the plugin
fills in its defaults around it on load).

Called by installers/components/32-timestamp-ignores.sh and, on Windows, by
Invoke-TimestampIgnores in common.ps1 -- one implementation for both. Run it
with Obsidian closed: the plugin writes its in-memory settings back on the
next stamp, which would drop the merge.

Usage: python3 timestamp_ignores.py <vault>
Prints exactly one of: ADDED:<n> | ALREADY | NO_PLUGIN | UNREADABLE
Stdlib only, Python 3.9-compatible.
"""
import json
import os
import sys
from pathlib import Path

PLUGIN = "update-time-on-edit"

# Everything the repository tracks outside the content folders, plus the one
# tracked note inside them. test_platform_hardening.py fails if a tracked
# Markdown file is not covered.
TRACKED = [
    ".github",
    ".obsidian",
    "Templates",
    "Z_attachments",
    "docs",
    "installers",
    "CLAUDE.md",
    "ONBOARDING.md",
    "README.md",
    "Z_archive/Completed Tasks.md",
]


def merge(settings: dict) -> int:
    """Add the missing TRACKED entries to settings in place; the count added."""
    current = settings.get("ignoreGlobalFolder")
    if isinstance(current, str):        # the plugin's first version kept one string
        current = [current] if current else []
    elif not isinstance(current, list):
        current = []
    missing = [p for p in TRACKED if p not in current]
    settings["ignoreGlobalFolder"] = current + missing
    return len(missing)


def main(argv: list) -> str:
    plugin_dir = Path(argv[0]) / ".obsidian" / "plugins" / PLUGIN
    if not plugin_dir.is_dir():
        return "NO_PLUGIN"
    path = plugin_dir / "data.json"
    settings = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "UNREADABLE"
        if not isinstance(settings, dict):
            return "UNREADABLE"
    added = merge(settings)
    if not added and path.is_file():
        return "ALREADY"
    tmp = path.with_name("data.json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(path))
    return "ADDED:%d" % added


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: timestamp_ignores.py <vault>\n")
        sys.exit(2)
    print(main(sys.argv[1:]))
