#!/usr/bin/env python3
"""
add-frontmatter.py

Adds minimal frontmatter to Obsidian markdown files that lack it. Designed
to improve RAG retrieval by giving the embedder a title anchor for files
that otherwise have only sparse content.

Strategy by folder:
  Actions/      title + tags: [actions]
  Groups/       title + tags: [groups]
  Topics/       title + description + tags: [topics]
  Creations/    title + tags: [creations]
  Knowledge/    title + tags: [knowledge]
  Z_archive/    title + tags: [archive]

Files that ALREADY have frontmatter (start with ---) are skipped — never
modified. Backups of every modified file are written outside the vault to
~/.local/share/obsidian-rag-sync/frontmatter-backup/<timestamp>/, mirroring
the vault structure so a full revert is `rsync -a backup/ vault/`.

Usage:
    python3 add-frontmatter.py /path/to/vault              # dry-run
    python3 add-frontmatter.py /path/to/vault --apply      # actually modify
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Folder → frontmatter generation rules
FOLDER_RULES: dict[str, dict] = {
    "Actions": {"tags": ["actions"], "description": None},
    "Groups": {"tags": ["groups"], "description": None},
    "Topics": {
        "tags": ["topics"],
        "description": "Topic aggregator: notes tagged with #{title}",
    },
    "Creations": {"tags": ["creations"], "description": None},
    "Knowledge": {"tags": ["knowledge"], "description": None},
    "Z_archive": {"tags": ["archive"], "description": None},
}

BACKUP_ROOT = Path.home() / ".local" / "share" / "obsidian-rag-sync" / "frontmatter-backup"


def has_frontmatter(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("---\n") or stripped.startswith("---\r\n")


def yaml_escape(s: str) -> str:
    """Quote a string for safe YAML scalar embedding."""
    if not s:
        return '""'
    # Escape backslashes and double quotes, wrap in double quotes
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def make_frontmatter(title: str, tags: list[str], description: str | None) -> str:
    lines = ["---"]
    lines.append(f"title: {yaml_escape(title)}")
    if description:
        lines.append(f"description: {yaml_escape(description)}")
    lines.append("tags:")
    for tag in tags:
        lines.append(f"  - {tag}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def process_file(
    file_path: Path,
    vault_root: Path,
    backup_root: Path,
    apply: bool,
) -> tuple[str, Path] | None:
    rel = file_path.relative_to(vault_root)
    if not rel.parts:
        return None
    top = rel.parts[0]
    if top not in FOLDER_RULES:
        return None

    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        return ("error", rel)

    if has_frontmatter(text):
        return ("skip-fm", rel)

    rules = FOLDER_RULES[top]
    title = file_path.stem
    description = None
    if rules["description"]:
        description = rules["description"].format(title=title)

    fm = make_frontmatter(title, rules["tags"], description)
    new_text = fm + "\n" + text  # blank-line separator after frontmatter

    if apply:
        backup_path = backup_root / rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, backup_path)
        file_path.write_text(new_text, encoding="utf-8")

    return ("modified", rel)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vault", type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="Actually modify files (default is dry-run)")
    parser.add_argument("--show-samples", type=int, default=5,
                        help="Number of sample modifications to print")
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

    by_folder: dict[str, dict[str, int]] = {}
    samples: list[tuple[Path, str]] = []

    for folder in FOLDER_RULES:
        folder_path = vault / folder
        if not folder_path.is_dir():
            continue
        by_folder[folder] = {"modified": 0, "skip-fm": 0, "error": 0}
        for md in sorted(folder_path.rglob("*.md")):
            result = process_file(md, vault, backup_root, args.apply)
            if result is None:
                continue
            status, rel = result
            by_folder[folder][status] = by_folder[folder].get(status, 0) + 1
            if status == "modified" and len(samples) < args.show_samples:
                title = rel.stem
                rules = FOLDER_RULES[folder]
                desc = rules["description"].format(title=title) if rules["description"] else None
                fm = make_frontmatter(title, rules["tags"], desc)
                samples.append((rel, fm))

    total_mod = sum(c["modified"] for c in by_folder.values())
    total_skip = sum(c["skip-fm"] for c in by_folder.values())
    total_err = sum(c.get("error", 0) for c in by_folder.values())

    print("=== Per-folder counts ===")
    print(f"{'folder':<22}{'modified':>10}{'skip (has FM)':>16}{'errors':>10}")
    for folder, counts in by_folder.items():
        print(f"{folder:<22}{counts['modified']:>10}{counts['skip-fm']:>16}{counts.get('error', 0):>10}")
    print(f"{'TOTAL':<22}{total_mod:>10}{total_skip:>16}{total_err:>10}")

    if samples:
        print(f"\n=== Sample frontmatter (first {len(samples)}) ===")
        for rel, fm in samples:
            print(f"\n--- {rel} ---")
            print(fm, end="")

    print()
    if not args.apply and total_mod > 0:
        print(f"To apply, re-run with --apply. {total_mod} files would be modified.")
        print(f"Backup destination would be: {backup_root}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
