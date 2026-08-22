#!/usr/bin/env python3
"""Refresh photo placeholders in Obsidian Groups/*.md files.

Conservative rule: ONLY swap `placeholder-person.png` -> a real photo file when
a matching photo now exists in Z_attachments/. Never modifies any line whose
photo reference is something other than `placeholder-person.png` -- even if the
referenced file is missing -- to avoid clobbering hand-edited references or
references the user is intentionally leaving for visibility.

Filename convention recognized:
    LastName-FirstName.{png,jpg,jpeg,webp}
where spaces in either name part may be replaced with `-` or `_`. Matching is
case-insensitive and tolerant of all three separators.

Idempotent. Designed to be safe to run nightly.

Usage:
    python3 refresh_groups.py [--dry-run]
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Vault root is the parent of the directory this script lives in
# (script lives at <vault>/Z_attachments/refresh_groups.py).
# Override with OBSIDIAN_VAULT env var for testing.
VAULT = Path(os.environ.get("OBSIDIAN_VAULT") or Path(__file__).resolve().parent.parent)
GROUPS_DIR = VAULT / "Groups"
ATTACH_DIR = VAULT / "Z_attachments"
PLACEHOLDER = "placeholder-person.png"
EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def normalize(s: str) -> str:
    """Collapse spaces / underscores / hyphens to a single hyphen, lowercased."""
    return re.sub(r"[\s_\-]+", "-", s).strip("-").lower()


def build_photo_index(attach_dir: Path) -> dict[str, str]:
    """normalized_basename -> actual filename. Prefers names without numeric '-N'
    suffix when multiple files map to the same normalized key (e.g. prefer
    `Doe-Jane.png` over `Doe-Jane-1.png`)."""
    index: dict[str, str] = {}
    for p in attach_dir.iterdir():
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        key = normalize(p.stem)
        if key in index:
            existing = index[key]
            existing_has_suffix = bool(re.search(r"-\d+$", Path(existing).stem))
            new_has_suffix = bool(re.search(r"-\d+$", p.stem))
            if existing_has_suffix and not new_has_suffix:
                index[key] = p.name
            # else keep existing
        else:
            index[key] = p.name
    return index


# Match a placeholder line: ![[placeholder-person.png|40]] [[Last, First]] (with optional |alias)
PLACEHOLDER_LINE = re.compile(
    r'(!\[\[)placeholder-person\.png(\|40\]\]\s+\[\[)([^\]\|#]+?)(\|[^\]]+)?(\]\])'
)


def refresh_file(fp: Path, photo_index: dict[str, str]) -> list[tuple[str, str]]:
    """Returns list of (person_link, new_photo) tuples for upgrades made."""
    text = fp.read_text()
    upgrades: list[tuple[str, str]] = []

    def sub(m: re.Match) -> str:
        prefix, mid, link_target, alias, suffix = m.groups()
        if "," not in link_target:
            return m.group(0)
        last, first = (p.strip() for p in link_target.split(",", 1))
        key = normalize(f"{last}-{first}")
        new_photo = photo_index.get(key)
        if not new_photo:
            return m.group(0)
        upgrades.append((link_target.strip(), new_photo))
        return f"{prefix}{new_photo}{mid}{link_target}{alias or ''}{suffix}"

    new_text = PLACEHOLDER_LINE.sub(sub, text)
    if new_text != text:
        fp.write_text(new_text)
    return upgrades


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if not GROUPS_DIR.is_dir() or not ATTACH_DIR.is_dir():
        print(f"ERROR: expected vault dirs missing under {VAULT}", file=sys.stderr)
        return 1

    photo_index = build_photo_index(ATTACH_DIR)
    print(f"Indexed {len(photo_index)} photo(s) in {ATTACH_DIR}")
    print(f"Scanning {GROUPS_DIR}{' (dry-run)' if dry_run else ''}...\n")

    total_files_changed = 0
    total_upgrades = 0
    for fp in sorted(GROUPS_DIR.glob("*.md")):
        if dry_run:
            text = fp.read_text()
            preview: list[tuple[str, str]] = []
            for m in PLACEHOLDER_LINE.finditer(text):
                _, _, link_target, _, _ = m.groups()
                if "," not in link_target:
                    continue
                last, first = (p.strip() for p in link_target.split(",", 1))
                key = normalize(f"{last}-{first}")
                new = photo_index.get(key)
                if new:
                    preview.append((link_target.strip(), new))
            upgrades = preview
        else:
            upgrades = refresh_file(fp, photo_index)

        if upgrades:
            total_files_changed += 1
            total_upgrades += len(upgrades)
            print(f"  {fp.name}")
            for person, photo in upgrades:
                print(f"    {person}: placeholder-person.png -> {photo}")

    print(
        f"\n{'Would upgrade' if dry_run else 'Upgraded'} {total_upgrades} photo "
        f"reference(s) across {total_files_changed} file(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
