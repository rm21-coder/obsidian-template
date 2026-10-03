"""plugin_drift.py - list the enabled plugins whose installed copy is not the pinned one.

Called by the updaters (macOS update.sh, Windows update.ps1) to decide whether
the plugins need reinstalling from installers/plugin-pins.json. A plugin counts
as drifted when any pinned file is missing or does not match its pinned
SHA256 -- every file, so a same-version re-pin (upstream re-uploaded a bundle
under the same tag) or a hand-replaced main.js is caught too. The one
exception is QuickAdd's main.js: the installers patch it after install
(quickadd_patch.py), so it never matches its pin; its manifest still does.

Deciding from what is installed, not from what the last pull changed, matters
in two cases the pull alone misses: a re-run after a failed download (the pull
has nothing new, yet the plugin is still old), and an install that drifted
from its pins some other way (updated by hand in Obsidian, or never updated).
Plugins listed as enabled but with no pin are left alone: the installers warn
about those already.

Usage: python3 plugin_drift.py <vault>
Prints one plugin id per line, nothing when every plugin matches. Exit 0 on
success, 2 when the pins or the enabled list cannot be read.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


# Files the installers change after installing them, so never equal to the pin.
PATCHED_LOCALLY = {("quickadd", "main.js")}


def drifted(vault: Path) -> list[str]:
    pins = json.loads((vault / "installers" / "plugin-pins.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in pins}
    enabled = json.loads((vault / ".obsidian" / "community-plugins.json").read_text(encoding="utf-8"))
    out = []
    for pid in enabled:
        pin = by_id.get(pid) if isinstance(pid, str) else None
        if pin is None:
            continue
        for name, meta in pin["files"].items():
            if (pid, name) in PATCHED_LOCALLY:
                continue
            try:
                got = hashlib.sha256((vault / ".obsidian" / "plugins" / pid / name).read_bytes()).hexdigest()
            except OSError:
                got = None
            if got != meta["sha256"].lower():
                out.append(pid)
                break
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: plugin_drift.py <vault>", file=sys.stderr)
        return 2
    try:
        ids = drifted(Path(sys.argv[1]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"plugin_drift: cannot read pins or plugin list: {exc}", file=sys.stderr)
        return 2
    for pid in ids:
        print(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
