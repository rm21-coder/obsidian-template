"""plugin_drift.py - list the enabled plugins whose installed copy is not the pinned one.

Called by the updaters (macOS update.sh, Windows update.ps1) to decide whether
the plugins need reinstalling from installers/plugin-pins.json. A plugin counts
as drifted when its manifest.json is missing or does not match the pinned
SHA256. The manifest is compared rather than main.js because the installers
patch QuickAdd's main.js after install (quickadd_patch.py), so main.js never
matches its pin; a release always changes the manifest's version.

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


def drifted(vault: Path) -> list[str]:
    pins = json.loads((vault / "installers" / "plugin-pins.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in pins}
    enabled = json.loads((vault / ".obsidian" / "community-plugins.json").read_text(encoding="utf-8"))
    out = []
    for pid in enabled:
        pin = by_id.get(pid) if isinstance(pid, str) else None
        if pin is None:
            continue
        want = pin["files"]["manifest.json"]["sha256"].lower()
        manifest = vault / ".obsidian" / "plugins" / pid / "manifest.json"
        try:
            got = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError:
            got = None
        if got != want:
            out.append(pid)
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
