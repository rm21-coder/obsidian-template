#!/usr/bin/env python3
"""
strip_ads.py — Remove ad blocks left behind by NYT, WSJ, golf.com, and similar
web clippers.

Targets three families of cruft:

1. NYT three-line block:
       Advertisement

       [SKIP ADVERTISEMENT](https://www.nytimes.com/.../#after-story-ad-N)

2. WSJ shorthand (just the marker, no SKIP link):
       Advertisement

3. Embedded ad payloads — <iframe> and <video> tags whose attributes
   identify them as ads. Specifically:
     - <iframe ... title="3rd party ad content" ...></iframe>
     - <iframe ... title="Advertisement" ...></iframe>
     - <iframe ... aria-label="Advertisement" ...></iframe>
     - <video  ... title="Advertisement" ...></video>
   These signals only appear on advertising payloads — legitimate embeds
   (Instagram, Ceros video, paywall offers, instructional videos) use other
   titles and are left untouched.

The "Advertisement" marker line is matched only when it appears alone
(`^Advertisement\\s*$`) so the word is safe inside prose.

Surrounding blank lines are collapsed only when something was actually
removed, so files without ads are returned byte-identical (no mtime churn).

The script is idempotent.

Usage:
    strip_ads.py FILE_OR_DIR [FILE_OR_DIR ...]
        [--dry-run]   show what would change without writing
        [--quiet]     only print files that were modified
        [--verbose]   print every file considered

Exit codes:
    0 = success (whether or not anything was changed)
    1 = at least one path could not be processed
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Vault root, derived from this script's location (Templates/Scripts/strip_ads.py
# -> vault). Lets the tool default to <vault>/Clippings when called with no path,
# so the scheduled job needs no machine-specific path baked into it. On macOS the
# LaunchAgent still passes an explicit path, so its behavior is unchanged.
VAULT_ROOT = Path(__file__).resolve().parent.parent.parent

# A line containing exactly "Advertisement" (NYT/WSJ marker line).
AD_HEADER = re.compile(r"^Advertisement\s*$")

# A "[SKIP ADVERTISEMENT](...)" line. The link target may be a fragment
# (#after-recipeDetail-mid-1) or a full URL.
SKIP_LINK = re.compile(r"^\[SKIP ADVERTISEMENT\]\([^)]*\)\s*$", re.IGNORECASE)

# An <iframe> tag that advertises itself as advertising. Three signals,
# any one is sufficient: title="3rd party ad content", title="Advertisement",
# or aria-label="Advertisement". `[^>]*?` keeps the attribute scan inside
# the opening tag; `.*?</iframe>` (DOTALL) handles iframes that span lines.
AD_IFRAME = re.compile(
    r'<iframe\b[^>]*?'
    r'(?:title="3rd party ad content"|title="Advertisement"|aria-label="Advertisement")'
    r'[^>]*?>.*?</iframe>',
    re.IGNORECASE | re.DOTALL,
)

# A <video> tag tagged as an advertisement (e.g. golf.com ad pre-rolls).
AD_VIDEO = re.compile(
    r'<video\b[^>]*?title="Advertisement"[^>]*?>.*?</video>',
    re.IGNORECASE | re.DOTALL,
)

# Collapse runs of 3+ consecutive blank lines down to a single blank line.
EXTRA_BLANKS = re.compile(r"\n{3,}")


def strip_ads(text: str) -> str:
    """Return `text` with ad blocks removed.

    Idempotent and surgical: if no ad markers are found, the original text is
    returned unchanged (including any pre-existing runs of blank lines —
    we don't normalize whitespace in files we have no business touching).
    """
    removed = False

    # Pass 1 — strip embedded ad payloads (iframes, videos) anywhere in
    # the text. These can span lines or sit inline with legitimate content,
    # so this is a regex-on-whole-text pass.
    new_text, n_iframe = AD_IFRAME.subn("", text)
    new_text, n_video = AD_VIDEO.subn("", new_text)
    if n_iframe or n_video:
        removed = True
        text = new_text

    # Pass 2 — strip the line-anchored "Advertisement" / "[SKIP ADVERTISEMENT]"
    # marker block.
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Case 1: standalone SKIP link with no preceding "Advertisement"
        # (defensive — usually paired, but strip either half if present).
        if SKIP_LINK.match(line):
            removed = True
            i += 1
            continue

        # Case 2: "Advertisement" header — possibly followed by SKIP link.
        if AD_HEADER.match(line):
            removed = True
            # Look ahead, skipping blank lines, for a SKIP link to swallow too.
            j = i + 1
            while j < n and lines[j].strip() == "":
                j += 1
            if j < n and SKIP_LINK.match(lines[j]):
                i = j + 1  # consume header + blanks + skip link
            else:
                i += 1     # consume just the lone "Advertisement"
            continue

        out.append(line)
        i += 1

    # Untouched: hand back the original verbatim.
    if not removed:
        return text

    cleaned = "\n".join(out)

    # Preserve a trailing newline if the original had one.
    if text.endswith("\n") and not cleaned.endswith("\n"):
        cleaned += "\n"

    # Collapse the gaps left behind by removed lines (only when we removed
    # something, so we don't reflow blank-line runs that the user intended).
    cleaned = EXTRA_BLANKS.sub("\n\n", cleaned)

    return cleaned


def process_file(path: Path, *, dry_run: bool, quiet: bool, verbose: bool) -> bool:
    """Process one .md file. Returns True on success, False on error."""
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR  {path}: {exc}", file=sys.stderr)
        return False

    cleaned = strip_ads(original)
    if cleaned == original:
        if verbose:
            print(f"ok     {path}")
        return True

    if dry_run:
        print(f"would clean  {path}")
        return True

    try:
        path.write_text(cleaned, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR  {path}: {exc}", file=sys.stderr)
        return False

    if not quiet:
        print(f"cleaned      {path}")
    return True


def iter_targets(paths: list[Path]):
    """Yield .md files from the given paths (files or directories)."""
    for p in paths:
        if p.is_file():
            if p.suffix.lower() == ".md":
                yield p
        elif p.is_dir():
            yield from sorted(p.rglob("*.md"))
        else:
            print(f"skip   {p} (not a file or directory)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path,
                    help="Files or directories to process. Defaults to <vault>/Clippings.")
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    ap.add_argument("--quiet", action="store_true", help="Only print modified files.")
    ap.add_argument("--verbose", action="store_true", help="Print every file considered.")
    args = ap.parse_args(argv)

    if not args.paths:
        args.paths = [VAULT_ROOT / "Clippings"]

    ok = True
    for md in iter_targets(args.paths):
        if not process_file(md, dry_run=args.dry_run, quiet=args.quiet, verbose=args.verbose):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
