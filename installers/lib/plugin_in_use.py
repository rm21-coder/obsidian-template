#!/usr/bin/env python3
"""plugin_in_use.py - does this vault still use a plugin the template retired?

An update removes the plugins the template has retired. Removing one whose
features a vault still uses would silently break those notes -- a Dataview
query would show as a raw code block, an Excalidraw drawing as JSON. So before
moving a retired plugin out, the updaters ask this, and leave the plugin in
place, with a warning naming what still uses it, if the answer is yes.

Usage:
    plugin_in_use.py <vault> <plugin-id>

Exit 0 and print "<n> note(s) ..." when the vault uses it; exit 1 when it does
not; exit 2 when the question cannot be answered, which callers treat as "in
use": keeping a plugin is the safe default, removing one is not.

Only plugins with a usage rule below can be judged; any other id exits 1, as
nothing in the vault is written in its syntax.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Text the ingest guard has defused carries a zero-width space inside the
# trigger. A defused copy in a clipped page is not the vault using a plugin.
ZWSP = "​"

DATAVIEW = re.compile(
    r"^[ \t]*(?:`{3,}|~{3,})[ \t]*dataview(?:js)?\b"   # a query block
    r"|`\$?=[^`\n]+`",                                   # an inline `= ...` query
    re.M | re.I)
EXCALIDRAW_FM = re.compile(r"^excalidraw-plugin:", re.M)

SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "Templates"}


def notes(vault: Path):
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            yield Path(root) / name


def dataview_uses(vault: Path) -> int:
    n = 0
    for p in notes(vault):
        if p.suffix.lower() != ".md":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in DATAVIEW.finditer(text):
            line_end = text.find("\n", m.end())
            if ZWSP not in text[m.start():line_end if line_end >= 0 else None]:
                n += 1
                break
    return n


def excalidraw_uses(vault: Path) -> int:
    n = 0
    for p in notes(vault):
        name = p.name.lower()
        if name.endswith(".excalidraw") or name.endswith(".excalidraw.md"):
            n += 1
        elif p.suffix.lower() == ".md":
            head = p.read_text(encoding="utf-8", errors="replace")[:4096]
            if EXCALIDRAW_FM.search(head):
                n += 1
    return n


RULES = {
    "dataview": (dataview_uses, "note(s) still use Dataview queries"),
    "obsidian-excalidraw-plugin": (excalidraw_uses, "Excalidraw drawing(s) in the vault"),
}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.split("\n\n", 1)[0], file=sys.stderr)
        return 2
    vault, plugin = Path(argv[1]), argv[2]
    if not vault.is_dir():
        print(f"not a directory: {vault}", file=sys.stderr)
        return 2
    rule = RULES.get(plugin)
    if rule is None:
        return 1
    try:
        n = rule[0](vault)
    except OSError as e:
        print(f"could not read the vault: {e}", file=sys.stderr)
        return 2
    if n:
        print(f"{n} {rule[1]}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
