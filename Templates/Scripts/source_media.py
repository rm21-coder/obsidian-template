#!/usr/bin/env python3
"""
source_media.py — where source drops live, resolved in one place.

Two watchers consume drop folders (voice_cleanup, podcast_watch) and one
transport fills them (source_mail_pull). Each used to resolve its own folder
with its own copy of the path logic, which meant migrating the layout required
coordinated edits and any missed one failed silently -- the transport would
deliver into a folder nothing drained, with no error anywhere.

Layout:

    ~/SourceMedia/
    ├── VoiceInput/     voice_cleanup.py
    └── PodcastInput/   podcast_watch.py

One local root, no cloud sync client -- see docs/Source-Mail-Transport.md.
Subfolders rather than one flat folder because both drop kinds are .txt; flat,
a .txt would be ambiguous and the watchers would race for each other's input.

History worth knowing, since older commits and notes refer to both:

  - A `news` kind fed clip_article.py, which fetched article URLs shared from a
    phone. Retired 2026-08-18: the Obsidian Web Clipper clips any page you can
    open, paywalled sources included, so a queue of unread URLs earned its
    complexity for almost nothing.
  - Drops used to arrive over iCloud Drive, and this module carried a sticky
    fallback to the old iCloud and top-level folders so an install predating
    the mail transport kept working. That fallback is gone too -- every install
    is on ~/SourceMedia/ now, and a fallback nothing needs is a path that only
    ever surprises you (it is preferred whenever it exists, so a stale folder
    silently wins over the real one).
"""
from __future__ import annotations

from pathlib import Path

# Drop kind -> folder name. Kinds are the authenticated `type` values used by
# source_mail_pull.py, so the two agree by construction: a type this dict does
# not name is rejected by the transport rather than written somewhere unread.
DIR_NAMES = {
    "voice": "VoiceInput",
    "podcast": "PodcastInput",
}


def drop_root() -> Path:
    """The single local root holding every drop folder."""
    return Path.home() / "SourceMedia"


def drop_dir(kind: str) -> Path:
    """Resolve the drop folder for a kind ('voice', 'podcast')."""
    try:
        folder = DIR_NAMES[kind]
    except KeyError:
        raise ValueError(
            f"unknown drop kind {kind!r}; expected one of "
            f"{', '.join(sorted(DIR_NAMES))}") from None
    return drop_root() / folder


def ensure_dirs() -> list[Path]:
    """Create every drop folder, returning them. Idempotent.

    The installers call this (via --apply) before wiring up a watcher, so a
    freshly installed agent has somewhere to watch instead of logging a
    missing-directory error on every tick until the first drop arrives.
    """
    created = []
    for folder in sorted(DIR_NAMES.values()):
        path = drop_root() / folder
        path.mkdir(parents=True, exist_ok=True)
        created.append(path)
    return created


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Show or create the drop folders under ~/SourceMedia/.")
    p.add_argument("--apply", action="store_true",
                   help="create any missing drop folders (default: report only)")
    args = p.parse_args()

    if args.apply:
        for path in ensure_dirs():
            print(f"ready: {path}")
    else:
        for kind in sorted(DIR_NAMES):
            path = drop_dir(kind)
            print(f"  {kind:8s} -> {path}"
                  f"{'' if path.is_dir() else '   (missing; run --apply)'}")
