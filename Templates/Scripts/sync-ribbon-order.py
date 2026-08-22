#!/usr/bin/env python3
"""sync-ribbon-order.py - sync the tracked ribbon order config into Obsidian's workspace.json.

The ribbon icon order is persisted by Obsidian in workspace.json under the
'left-ribbon' key. workspace.json itself is per-machine state (open files,
tab layout, scroll positions) and stays gitignored - but the ribbon order
is workflow-relevant and worth versioning.

This script provides two directions:

  --apply  (default)
    Read .obsidian/ribbon-config.json (tracked) and merge it into
    .obsidian/workspace.json's 'left-ribbon' key. Other keys in
    workspace.json are preserved.

  --export
    Read .obsidian/workspace.json's 'left-ribbon' key and write it to
    .obsidian/ribbon-config.json. Use this after drag-reordering icons
    in Obsidian to capture the new order, then commit ribbon-config.json
    to the repo.

Important: Obsidian should be QUIT before running --apply. Obsidian writes
workspace.json on quit (and periodically while running), so if it's open
when this script runs, the merged ribbon order may be clobbered.

Usage
-----
  sync-ribbon-order.py                  # apply tracked order to workspace.json
  sync-ribbon-order.py --export         # capture current order to ribbon-config.json
  sync-ribbon-order.py --vault PATH     # override vault location
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vault", default=os.path.expanduser("~/Obsidian"),
                   help="Path to the Obsidian vault (default: ~/Obsidian)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--export", action="store_true",
                   help="capture left-ribbon FROM workspace.json INTO ribbon-config.json")
    g.add_argument("--apply", action="store_true",
                   help="(default) merge left-ribbon FROM ribbon-config.json INTO workspace.json")
    args = p.parse_args()

    vault = Path(args.vault)
    ws_path = vault / ".obsidian" / "workspace.json"
    cfg_path = vault / ".obsidian" / "ribbon-config.json"

    if args.export:
        if not ws_path.exists():
            print(f"workspace.json not found: {ws_path}", file=sys.stderr)
            return 1
        with open(ws_path) as f:
            ws = json.load(f)
        ribbon = ws.get("left-ribbon")
        if not ribbon:
            print("no left-ribbon block in workspace.json", file=sys.stderr)
            return 1
        with open(cfg_path, "w") as f:
            json.dump(ribbon, f, indent=2)
        print(f"exported left-ribbon to {cfg_path}")
        keys = list(ribbon.get("hiddenItems", {}).keys())
        print(f"  {len(keys)} items in order; first 3: {keys[:3]}")
        return 0

    # --apply is the default
    if not cfg_path.exists():
        print(f"ribbon-config.json not found: {cfg_path}", file=sys.stderr)
        print("nothing to apply; commit ribbon-config.json to the repo first", file=sys.stderr)
        return 1
    with open(cfg_path) as f:
        ribbon = json.load(f)

    if ws_path.exists():
        with open(ws_path) as f:
            ws = json.load(f)
    else:
        ws = {}

    ws["left-ribbon"] = ribbon

    with open(ws_path, "w") as f:
        json.dump(ws, f, indent=2)
    print(f"applied left-ribbon to {ws_path}")
    keys = list(ribbon.get("hiddenItems", {}).keys())
    print(f"  {len(keys)} items in order; first 3: {keys[:3]}")
    print("  Quit and reopen Obsidian to see the new ribbon order")
    return 0


if __name__ == "__main__":
    sys.exit(main())
