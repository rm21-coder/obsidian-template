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

import os
from pathlib import Path

# N11 (2026-10-23): on Windows mkdir(mode=0o700) writes an owner-only ACL
# (Python 3.12.4+); made from an elevated prompt, the owner is the
# Administrators group and the user's own tasks cannot use the folder. There
# the folder inherits the user's own profile ACL instead.
_PRIVATE_DIR_MODE = 0o700 if os.name == "posix" else 0o777

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


# Where podcast_watch retires a drop (done/, failed/ with a sibling
# <name>.error.log). Restricted here too, so an install whose folders predate
# the owner-only modes is brought into line by the next --apply.
RETIRE_SUBDIRS = ("done", "failed")


def _owner_only(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_dirs() -> list[Path]:
    """Create every drop folder, returning them. Idempotent.

    The installers call this (via --apply) before wiring up a watcher, so a
    freshly installed agent has somewhere to watch instead of logging a
    missing-directory error on every tick until the first drop arrives.

    Owner-only (5278632): drops are voice transcripts and podcast audio, and
    a bare mkdir left them 0755 under the default umask -- readable by every
    other local account. Existing folders are chmodded, not just new ones;
    mkdir's mode is masked by the umask and never applies to a folder that
    already exists.
    """
    root = drop_root()
    root.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    _owner_only(root, 0o700)
    created = []
    for folder in sorted(DIR_NAMES.values()):
        path = root / folder
        path.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)
        _owner_only(path, 0o700)
        for sub in RETIRE_SUBDIRS:
            retired = path / sub
            if retired.is_symlink() or not retired.is_dir():
                continue
            _owner_only(retired, 0o700)
            for f in retired.iterdir():
                if f.is_file() and not f.is_symlink():
                    _owner_only(f, 0o600)
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
