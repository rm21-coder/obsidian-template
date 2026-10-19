#!/usr/bin/env python3
"""add_people_fields.py - give existing People notes the template's newer
email fields.

The People template gained `Email-Work-2` (a second work address) and
`Email-Priority` (read by the email-tasks job) after many vaults already held
hundreds of People notes. Templates only apply to new notes, so this inserts
the missing keys into each existing note's frontmatter in template order:
`Email-Work-2:` (empty) after `Email-Work:`, `Email-Priority: false` after
`Email-Work-2:`. An `Email-Priority:` left empty by an earlier version of this
script is set to `false`: Obsidian draws an empty checkbox in its no-value
state, which looks checked (2026-10-18). Nothing else in the note changes: no
other value is read or rewritten, a checked box is kept, the body is
untouched, and a note with no `Email-Work:` line is left alone and counted.

Quit Obsidian first: it rewrites the frontmatter of notes changed under it.
The script refuses to write while it is running (--force overrides).

    python3 add_people_fields.py            # dry run: counts only
    python3 add_people_fields.py --write    # apply

Idempotent. Stdlib only, Python 3.9-compatible.
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent.parent
# (key to add, key it goes after, value it gets). Order matters: Email-Priority follows
# Email-Work-2, which may itself have just been added.
FIELDS = (("Email-Work-2", "Email-Work", ""), ("Email-Priority", "Email-Work-2", "false"))


def _has_key(lines: list, key: str) -> int:
    """Index of the top-level `key:` line, or -1."""
    for i, line in enumerate(lines):
        if line == key + ":" or line.startswith(key + ": ") or line.startswith(key + ":\t"):
            return i
    return -1


def add_fields(text: str):
    """(new_text, keys_added), or (None, reason) when the note is skipped."""
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    if not text.startswith("---" + nl):
        return None, "no frontmatter"
    end = text.find(nl + "---", 3)
    if end < 0:
        return None, "unterminated frontmatter"
    head = text[len("---" + nl):end]
    lines = head.split(nl)
    added = []
    for key, after, value in FIELDS:
        at = _has_key(lines, key)
        if at >= 0:
            if value and lines[at].strip() == key + ":":
                lines[at] = "%s: %s" % (key, value)
                added.append(key)
            continue
        anchor = _has_key(lines, after)
        if anchor < 0:
            return None, "no %s line" % after
        # A value continued on indented lines (a YAML list) stays with its key.
        at = anchor + 1
        while at < len(lines) and lines[at][:1] in (" ", "\t", "-") and lines[at].strip():
            at += 1
        lines.insert(at, ("%s: %s" % (key, value)) if value else key + ":")
        added.append(key)
    if not added:
        return text, []
    return "---" + nl + nl.join(lines) + text[end:], added


def obsidian_running():
    """True, False, or None when the check itself could not run (no pgrep or
    tasklist, or it failed): the caller then refuses, as if it were running."""
    try:
        if platform.system() == "Windows":
            done = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe", "/NH"],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode != 0:
                return None
            return "obsidian.exe" in done.stdout.lower()
        rc = subprocess.run(["pgrep", "-x", "Obsidian"], capture_output=True,
                            timeout=10).returncode
        return {0: True, 1: False}.get(rc)     # pgrep: 0 found, 1 none, else error
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--vault", default=None)
    parser.add_argument("--write", action="store_true", help="apply (default: dry run)")
    parser.add_argument("--force", action="store_true", help="write even if Obsidian is running")
    args = parser.parse_args(argv)
    vault = Path(args.vault).expanduser() if args.vault else VAULT_ROOT
    people = vault / "People"
    if not people.is_dir():
        print("no People folder under %s" % vault)
        return 1
    if args.write and not args.force:
        running = obsidian_running()
        if running is None:
            print("Could not tell whether Obsidian is running. Quit it, then re-run with "
                  "--write --force")
            return 1
        if running:
            print("Obsidian is running: quit it first (it rewrites notes changed under it), "
                  "or pass --force")
            return 1

    counts = {"Email-Work-2": 0, "Email-Priority": 0}
    changed = current = 0
    skipped = []
    for note in sorted(people.rglob("*.md")):
        try:
            text = note.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            skipped.append((note, "unreadable"))
            continue
        new, added = add_fields(text)
        if new is None:
            skipped.append((note, added))
            continue
        if not added:
            current += 1
            continue
        changed += 1
        for key in added:
            counts[key] += 1
        if args.write:
            tmp = note.with_name(note.name + ".tmp")
            tmp.write_bytes(new.encode("utf-8"))
            os.replace(str(tmp), str(note))

    verb = "updated" if args.write else "would update"
    print("%s %d note(s): +Email-Work-2 in %d, +Email-Priority in %d; %d already current"
          % (verb, changed, counts["Email-Work-2"], counts["Email-Priority"], current))
    for note, why in skipped:
        print("  skipped %s: %s" % (note.relative_to(vault), why))
    if not args.write and changed:
        print("dry run: nothing written. Quit Obsidian, then re-run with --write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
