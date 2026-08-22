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

# Match a frontmatter block at the very top of the file: --- ... ---
# Captures the YAML body so we can scan it for an existing `classification` key.
FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)

# Match an existing classification key anywhere in a YAML block, at column 0.
CLASS_KEY_RE = re.compile(r"(?m)^classification\s*:", re.IGNORECASE)

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


def already_has(fm_body: str) -> bool:
    return bool(CLASS_KEY_RE.search(fm_body))


def insert_classification(text: str, value: str) -> str | None:
    """Return new text with classification inserted, or None if no change needed.

    Three cases:
      1. File already has classification → return None (no-op).
      2. File has frontmatter but no classification → insert key before the
         closing `---` fence.
      3. File has no frontmatter → prepend a minimal `--- classification: X ---`
         block.
    """
    m = FM_RE.match(text)
    if m:
        fm_body = m.group(1)
        if already_has(fm_body):
            return None
        # Insert `classification: value` as a new line at the END of the YAML
        # body, just before the closing fence. We preserve whatever trailing
        # whitespace shape the YAML body has.
        new_body = fm_body.rstrip("\n") + f"\nclassification: {value}\n"
        # Reconstruct the file with the same closing-fence form as the original.
        # m.group(0) is the entire matched FM block including fences.
        original_block = m.group(0)
        # Find where the closing fence starts in original_block.
        # The block is: ---\n  <body>  \n---\n? — replace the body cleanly.
        # Determine the line-ending used.
        lf = "\r\n" if "\r\n" in original_block else "\n"
        closing = f"---{lf}" if original_block.endswith(lf) else "---"
        new_block = f"---{lf}{new_body.rstrip(chr(10))}{lf}{closing}"
        # Preserve whatever came after the closing fence (the body of the note).
        rest = text[m.end():]
        return new_block + rest

    # No frontmatter at all — prepend a minimal block.
    minimal = f"---\nclassification: {value}\n---\n"
    # If the file is empty or starts with content, add a separating blank line.
    if text and not text.startswith("\n"):
        return minimal + "\n" + text
    return minimal + text


def should_skip_path(rel: Path) -> bool:
    return bool(rel.parts) and rel.parts[0] in SKIP_TOP


def process_file(
    file_path: Path, vault_root: Path, backup_root: Path, apply: bool
) -> tuple[str, Path, str | None]:
    """Return (status, rel, value_used).

    status ∈ {"modified", "skip-has-class", "skip-folder", "error"}
    """
    rel = file_path.relative_to(vault_root)
    if should_skip_path(rel):
        return ("skip-folder", rel, None)

    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return ("error", rel, None)

    # Only the frontmatter is consulted for the source-URL signal, so a link in
    # the note's prose can't reclassify it.
    fm_match = FM_RE.match(text)
    value = default_for(rel, fm_match.group(1) if fm_match else "")
    if value not in VALID:
        return ("error", rel, None)

    new_text = insert_classification(text, value)
    if new_text is None:
        return ("skip-has-class", rel, None)

    if apply:
        backup_path = backup_root / rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, backup_path)
        file_path.write_text(new_text, encoding="utf-8")

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
