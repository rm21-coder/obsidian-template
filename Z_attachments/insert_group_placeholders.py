#!/usr/bin/env python3
"""Insert the photo placeholder prefix on truly-bare member lines in Groups/*.md.

Companion to refresh_groups.py. That script only ever SWAPS an existing
`placeholder-person.png` reference for a real photo; it intentionally never
touches a line that lacks the placeholder. This script fills the one gap that
leaves: a newly-added member written as a bare wikilink, e.g.

    [[Doe, Jane]]

has no image at all, so refresh_groups.py can never act on it. This script
prepends the placeholder so the next refresh pass can upgrade it to the real
photo:

    [[Doe, Jane]]  ->  ![[placeholder-person.png|40]] [[Doe, Jane]]

DELIBERATELY NARROW RULE -- this is why it is a separate script rather than
folded into refresh_groups.py:

  * It ONLY matches a line that is *exactly* a person wikilink and nothing
    else: `[[Last, First]]` (an optional `|alias` is allowed).
  * The link target MUST contain a comma (the `Last, First` convention) and
    MUST NOT contain `#` -- so MOC links and base embeds like
    `![[Meetings.base#Group]]` are never matched.
  * A line that already begins with an image embed (`![[...]]`) is never
    touched, because the regex is anchored to a line that starts with `[[`.

It therefore can only ADD a placeholder to a line that has no image whatsoever.
It can never modify, downgrade, or clobber an existing photo reference. This is
the conservative property that the earlier overzealous refresh logic violated.

Idempotent. Safe to run nightly, before refresh_groups.py.

Usage:
    python3 insert_group_placeholders.py [--dry-run]
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Vault root is the parent of the directory this script lives in
# (script lives at <vault>/Z_attachments/insert_group_placeholders.py).
# Override with OBSIDIAN_VAULT env var for testing.
VAULT = Path(os.environ.get("OBSIDIAN_VAULT") or Path(__file__).resolve().parent.parent)
GROUPS_DIR = VAULT / "Groups"
ATTACH_DIR = VAULT / "Z_attachments"
PLACEHOLDER = "placeholder-person.png"
PREFIX = f"![[{PLACEHOLDER}|40]] "

# A bare member line: the whole line is just a person wikilink.
#   group 1 = link target (must contain a comma, no '#', no '!', no '|')
#   group 2 = optional alias including the leading '|'
# Anchored with ^ and $ (MULTILINE) so a leading '![[' image embed never matches.
BARE_LINE = re.compile(
    r'^\[\[([^\]\|#!]+,[^\]\|#!]+?)(\|[^\]]+)?\]\][ \t]*$',
    re.MULTILINE,
)


def insert_file(fp: Path, dry_run: bool) -> list[str]:
    """Prepend the placeholder prefix to bare member lines.

    Returns the list of person link-targets that were (or would be) prefixed.
    """
    text = fp.read_text()
    inserted: list[str] = []

    def sub(m: re.Match) -> str:
        link_target = m.group(1).strip()
        inserted.append(link_target)
        # Rebuild the line verbatim, just with the placeholder prefix in front.
        return f"{PREFIX}{m.group(0)}"

    new_text = BARE_LINE.sub(sub, text)
    if inserted and not dry_run and new_text != text:
        fp.write_text(new_text)
    return inserted


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if not GROUPS_DIR.is_dir() or not ATTACH_DIR.is_dir():
        print(f"ERROR: expected vault dirs missing under {VAULT}", file=sys.stderr)
        return 1

    print(f"Scanning {GROUPS_DIR}{' (dry-run)' if dry_run else ''}...\n")

    total_files_changed = 0
    total_inserts = 0
    for fp in sorted(GROUPS_DIR.glob("*.md")):
        inserted = insert_file(fp, dry_run)
        if inserted:
            total_files_changed += 1
            total_inserts += len(inserted)
            print(f"  {fp.name}")
            for person in inserted:
                print(f"    {person}: (bare) -> {PREFIX.strip()} {{person}}")

    print(
        f"\n{'Would insert' if dry_run else 'Inserted'} {total_inserts} placeholder "
        f"prefix(es) across {total_files_changed} file(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
