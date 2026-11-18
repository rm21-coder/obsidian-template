#!/usr/bin/env python3
"""
add_classification.py

Backfills the `classification` frontmatter property across the vault so that
every note carries a data classification flag (public, internal-use-only,
confidential, restricted) — your own scheme; see e.g.
Knowledge/Data Classification.md if you keep one. This script ONLY adds the
property when missing; never overwrites an existing value.

Defaults, most specific first:
    any note with an external `source:` URL
                      → public               (external published content,
                                               wherever the note happens to live)
    Clippings/        → public               (external published content)
    Meetings/         → confidential         (sensitive by class)
    People/           → confidential         (accumulates personal detail)
    everything else   → internal-use-only    (the working default)

Skipped entirely:
    Templates/, .obsidian/, .trash/, Z_attachments/, Z_archive/

Backups are written outside the vault to
    ~/.local/share/obsidian-rag-sync/classification-backup/<timestamp>/
mirroring the vault structure, so a full revert is `rsync -a backup/ vault/`.

Usage:
    python3 add_classification.py /path/to/vault              # dry-run
    python3 add_classification.py /path/to/vault --apply      # actually modify
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Top-level folders to skip entirely (paths are vault-relative).
SKIP_TOP = {"Templates", ".obsidian", ".trash", "Z_attachments", "Z_archive"}

# Folder → default classification value. Anything not listed defaults to
# internal-use-only.
FOLDER_DEFAULT = {
    # Sensitive by class, not by content — see classify_notes.FOLDER_BASELINE.
    "Meetings": "confidential",
    "People": "confidential",
}
GLOBAL_DEFAULT = "internal-use-only"

VALID = {"public", "internal-use-only", "confidential", "restricted"}

BACKUP_ROOT = (
    Path.home() / ".local" / "share" / "obsidian-rag-sync" / "classification-backup"
)

# Frontmatter is found, and an existing tier read, exactly as every gate
# finds and reads them: classification_tier (v2.6 F22). The backfill's own
# regex needed "---" at offset 0, so a note starting with a byte-order mark
# looked untiered and got a lower block prepended above its real one.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import classification_tier  # noqa: E402

_LINE_SPLIT = re.compile(r"(\r\n|\r|\n)")
_OPEN_LINE = re.compile(r"\A\ufeff{0,2}---\Z")

# A frontmatter `source:` (or `url:`) pointing at an http(s) address — the mark
# of a web clipping. Matched against the frontmatter body only, so a link in the
# note's prose can't reclassify it.
EXTERNAL_SOURCE_RE = re.compile(r"(?m)^(?:source|url)\s*:\s*\"?https?://", re.IGNORECASE)


def default_for(rel: Path, text: str = "") -> str:
    # An external source URL is a stronger signal than the folder: a web
    # clipping is externally published content whether it landed in Clippings/
    # or somewhere else. Folders drift over time; the `source:` key doesn't.
    if EXTERNAL_SOURCE_RE.search(text):
        return "public"
    if rel.parts and rel.parts[0] in FOLDER_DEFAULT:
        return FOLDER_DEFAULT[rel.parts[0]]
    return GLOBAL_DEFAULT


def already_has(text: str) -> bool:
    """True when the note declares anything about its tier that the strict
    reader sees -- a recognised value, or one it cannot read. Either way the
    backfill has no business adding a second declaration."""
    return bool(classification_tier.declared(text))


class Refused(ValueError):
    """The rewrite would not read as exactly the tier it set."""


def insert_classification(text: str, value: str) -> str | None:
    """Return new text with classification inserted, or None if no change needed.

    Three cases:
      1. The note already declares a tier, readable or not → None (no-op).
      2. It has frontmatter (as classification_tier.frontmatter finds it: up
         to two byte-order marks, closed by the first line starting "---")
         but no tier → the key goes in as the last line before the close,
         with the note's own line ending.
      3. No frontmatter → a minimal `--- classification: X ---` block is
         prepended.

    Raises Refused unless the result reads as exactly `value` and no lower
    than before (classification_tier.tier_not_lowered).
    """
    if already_has(text):
        return None
    new = None
    if classification_tier.frontmatter(text) is not None:
        parts = _LINE_SPLIT.split(text)        # line, sep, line, sep, ...
        lines, seps = parts[0::2], parts[1::2] + [""]
        if _OPEN_LINE.match(lines[0]):
            close = next((i for i in range(1, len(lines))
                          if lines[i].startswith("---")), None)
            if close is not None:
                lf = seps[0]
                lines.insert(close, f"classification: {value}")
                seps.insert(close, lf)
                new = "".join(ln + sp for ln, sp in zip(lines, seps))
    if new is None:
        # The note's own first line ending, so a CRLF note stays CRLF (RC14).
        found = _LINE_SPLIT.search(text)
        nl = found.group(0) if found else "\n"
        minimal = f"---{nl}classification: {value}{nl}---{nl}"
        # If the file is empty or starts with content, add a separating blank line.
        new = minimal + (nl + text if text and not text.startswith(("\n", "\r")) else text)
    if not classification_tier.tier_not_lowered(text, new, expected=value):
        raise Refused(f"the result does not read as `{value}`")
    return new


def should_skip_path(rel: Path) -> bool:
    return bool(rel.parts) and rel.parts[0] in SKIP_TOP


def process_file(
    file_path: Path, vault_root: Path, backup_root: Path, apply: bool
) -> tuple[str, Path, str | None]:
    """Return (status, rel, value_used).

    status ∈ {"modified", "skip-has-class", "skip-unreadable", "skip-too-large",
              "skip-folder", "error", "refused"}
    """
    rel = file_path.relative_to(vault_root)
    if should_skip_path(rel):
        return ("skip-folder", rel, None)

    try:
        # Untranslated, so the note keeps its own line endings (review RC4).
        text = classification_tier.read_note(file_path, newline="")
    except classification_tier.NoteTooLarge:
        return ("skip-too-large", rel, None)        # reported by path (RC13)
    except Exception:
        return ("error", rel, None)

    # Something is declared but no gate can evaluate it: left alone, and
    # reported by path, since every gate will refuse it (review RC5).
    tier, unknown = classification_tier.effective(text)
    if classification_tier.declared(text) and (tier is None or unknown):
        return ("skip-unreadable", rel, None)

    # Only the frontmatter is consulted for the source-URL signal, so a link in
    # the note's prose can't reclassify it.
    value = default_for(rel, classification_tier.frontmatter(text) or "")
    if value not in VALID:
        return ("error", rel, None)

    try:
        new_text = insert_classification(text, value)
    except Refused:
        return ("refused", rel, value)
    if new_text is None:
        return ("skip-has-class", rel, None)

    if apply:
        backup_path = backup_root / rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, backup_path)
        with open(file_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)

    return ("modified", rel, value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vault", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify files (default is dry-run).",
    )
    parser.add_argument(
        "--show-samples",
        type=int,
        default=5,
        help="Number of sample modifications to print.",
    )
    parser.add_argument(
        "--folder",
        action="append",
        metavar="NAME",
        help="Limit to this top-level folder (repeatable). Default: the whole "
             "vault. Useful for backfilling one folder at a time.",
    )
    args = parser.parse_args()

    vault = args.vault.expanduser().resolve()
    if not vault.is_dir():
        print(f"Vault not found: {vault}", file=sys.stderr)
        return 1

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = BACKUP_ROOT / ts

    if args.apply:
        backup_root.mkdir(parents=True, exist_ok=True)
        print(f"APPLY mode. Backups → {backup_root}")
    else:
        print("DRY-RUN. No changes will be made. Re-run with --apply to commit.")
        print(f"(would back up to {backup_root})")
    print()

    by_value: dict[str, int] = {}
    by_folder: dict[str, dict[str, int]] = {}
    samples: list[tuple[Path, str]] = []
    error_count = 0
    skip_folder_count = 0
    skip_has_count = 0

    for md in sorted(vault.rglob("*.md")):
        if args.folder:
            parts = md.relative_to(vault).parts
            if not parts or parts[0] not in args.folder:
                continue
        status, rel, value = process_file(md, vault, backup_root, args.apply)
        top = rel.parts[0] if rel.parts else "<root>"
        if status == "skip-folder":
            skip_folder_count += 1
            continue
        f = by_folder.setdefault(
            top, {"modified": 0, "skip-has-class": 0, "error": 0}
        )
        if status == "refused":
            print(f"REFUSED (the write would not read as `{value}`): {rel}")
            status = "error"
        elif status == "skip-unreadable":
            print(f"UNREADABLE TIER (left alone; every gate refuses it, fix by hand): {rel}")
            status = "error"
        elif status == "skip-too-large":
            print(f"TOO LARGE (over the {classification_tier.MAX_NOTE_BYTES}-byte read cap; "
                  f"left alone, tier it by hand): {rel}")
            status = "error"
        f[status] = f.get(status, 0) + 1
        if status == "modified":
            by_value[value] = by_value.get(value, 0) + 1
            if len(samples) < args.show_samples:
                samples.append((rel, value or ""))
        elif status == "skip-has-class":
            skip_has_count += 1
        elif status == "error":
            error_count += 1

    total_mod = sum(c["modified"] for c in by_folder.values())

    print("=== Per-folder counts ===")
    print(
        f"{'folder':<24}{'modified':>10}{'has-class':>12}{'errors':>10}"
    )
    for folder in sorted(by_folder):
        counts = by_folder[folder]
        print(
            f"{folder:<24}{counts['modified']:>10}"
            f"{counts['skip-has-class']:>12}{counts.get('error', 0):>10}"
        )
    print(
        f"{'TOTAL':<24}{total_mod:>10}{skip_has_count:>12}{error_count:>10}"
    )

    if by_value:
        print("\n=== By classification value (modified only) ===")
        for v in ("public", "internal-use-only", "confidential", "restricted"):
            if v in by_value:
                print(f"  {v:<22}{by_value[v]:>10}")

    if samples:
        print(f"\n=== Sample modifications (first {len(samples)}) ===")
        for rel, v in samples:
            print(f"  {rel}  → classification: {v}")

    print()
    if not args.apply and total_mod > 0:
        print(
            f"To apply, re-run with --apply. {total_mod} files would be modified."
        )
        print(f"Backup destination would be: {backup_root}")
    elif args.apply:
        print(f"Modified {total_mod} files. Backups at: {backup_root}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
