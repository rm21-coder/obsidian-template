#!/usr/bin/env python3
"""
check_classification.py — audit the obsidian-template repo for content
that should not be public.

Rules
-----
The obsidian-template repo is a PUBLIC GitHub repository. Every .md file
in a "user-content" folder must explicitly carry `classification: public`
in its YAML frontmatter, per your own vault's classification scheme (see
e.g. Knowledge/Data Classification.md if you keep one). Files without
classification or with any value other than `public` are violations and
must be fixed before they can be committed.

Audited folders (user content — must be `classification: public`):
    Actions, Categories, Clippings, Creations, Daily, Excalidraw, Groups,
    Knowledge, Meetings, Notes, People, Topics

Skipped (scaffolding / config / docs — no classification check):
    .git/, .github/, .obsidian/, Templates/, Z_archive/, Z_attachments/,
    docs/, installers/, and the top-level README.md.

Modes
-----
    # audit every .md file in the repo
    ./check_classification.py [--repo-root PATH]

    # audit only files staged for commit (used by pre-commit hook)
    ./check_classification.py --staged [--repo-root PATH]

    # silent unless violations found
    ./check_classification.py --quiet

Exit codes
----------
    0   clean (no violations)
    1   violations found
    2   internal error (bad invocation, repo not found, etc.)

Remediation
-----------
For each violation, either:
  (a) Add `classification: public` to the file's frontmatter and confirm the
      content is genuinely safe to publish, OR
  (b) Move the file out of the repo (it belongs only in your private vault).

To override for a single legitimate commit:
    git commit --no-verify
This bypasses the pre-commit hook entirely. The CI side (when wired) and
the install.sh side (02-classification-audit.sh) will still flag it.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Folders whose .md files MUST be explicitly `classification: public`.
AUDITED_FOLDERS = frozenset({
    "Actions", "Categories", "Clippings", "Creations", "Daily",
    "Excalidraw", "Groups", "Knowledge", "Meetings", "Notes",
    "People", "Topics",
})

# Folders whose .md files are skipped entirely.
SKIPPED_FOLDERS = frozenset({
    ".git", ".github", ".obsidian", "Templates",
    "Z_archive", "Z_attachments",
    "docs", "installers",
})

# Files at the repo root that are skipped (scaffolding).
SKIPPED_ROOT_FILES = frozenset({"README.md"})

# Frontmatter pattern: a YAML block at the very top, delimited by ---.
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Anchored at column 0 inside the YAML block so a stray
# "classification:" in body text can't accidentally satisfy the gate.
CLASSIFICATION_RE = re.compile(r"(?m)^classification\s*:\s*(.+?)\s*$")

REQUIRED_VALUE = "public"


def parse_classification(text: str) -> str | None:
    """Return the lowercased classification value from frontmatter, or None
    if there is no frontmatter or no `classification:` key. Strings are
    unquoted (`"public"` and `public` both resolve to `public`).
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    cm = CLASSIFICATION_RE.search(m.group(1))
    if not cm:
        return None
    return cm.group(1).strip().strip('"').strip("'").lower() or None


def should_audit(rel_path: Path) -> bool:
    """Return True if this path falls under an audited folder.

    Files in skipped folders, skipped root files, and any file whose
    top-level component is not in AUDITED_FOLDERS are excluded.
    """
    parts = rel_path.parts
    if not parts:
        return False
    # Skip root-level files unless explicitly in an audited folder.
    if len(parts) == 1:
        return False
    top = parts[0]
    if top in SKIPPED_FOLDERS:
        return False
    if top not in AUDITED_FOLDERS:
        # Anything outside the known audited set is treated as scaffolding
        # and skipped. New top-level folders need to be opted IN here.
        return False
    if rel_path.name in SKIPPED_ROOT_FILES:
        return False
    return True


def git_staged_files(repo_root: Path) -> list[Path]:
    """Return paths (relative to repo_root) of .md files staged for commit.

    Uses --diff-filter=ACMR so deletions and renames-out don't appear.
    """
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only",
             "--diff-filter=ACMR", "--", "*.md"],
            cwd=repo_root,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    paths: list[Path] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        if (repo_root / p).is_file():
            paths.append(p)
    return paths


def all_md_files(repo_root: Path) -> list[Path]:
    """Return paths (relative to repo_root) of all .md files in the repo."""
    paths: list[Path] = []
    for f in repo_root.rglob("*.md"):
        try:
            rel = f.relative_to(repo_root)
        except ValueError:
            continue
        # Skip anything inside a known-skipped folder at any depth.
        if any(part in SKIPPED_FOLDERS for part in rel.parts):
            continue
        paths.append(rel)
    return paths


def audit_files(
    repo_root: Path, paths: list[Path], quiet: bool
) -> tuple[int, int]:
    """Audit the given paths. Return (violations, audited_count)."""
    violations = 0
    audited = 0
    for rel in paths:
        if not should_audit(rel):
            continue
        audited += 1
        full = repo_root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"VIOLATION  {rel}  could not read file: {exc}",
                  file=sys.stderr)
            violations += 1
            continue
        value = parse_classification(text)
        if value != REQUIRED_VALUE:
            shown = value if value is not None else "(missing)"
            print(
                f"VIOLATION  {rel}\n"
                f"           classification: {shown}\n"
                f"           required:       {REQUIRED_VALUE}\n"
                f"           fix:            add `classification: public` to the"
                f" file's frontmatter, or remove the file from this repo.",
                file=sys.stderr,
            )
            violations += 1

    if not quiet:
        print(
            f"\nclassification audit: {audited} file(s) audited, "
            f"{violations} violation(s).",
            file=sys.stderr,
        )
    return violations, audited


def find_repo_root(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    # Fall back to `git rev-parse --show-toplevel` from cwd.
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        )
        return Path(out.strip()).resolve()
    except subprocess.CalledProcessError:
        print("error: not inside a git repository and --repo-root not given.",
              file=sys.stderr)
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit the obsidian-template repo for non-public content."
    )
    parser.add_argument("--repo-root", default=None,
                        help="Path to the repo root. Defaults to git toplevel.")
    parser.add_argument("--staged", action="store_true",
                        help="Audit only files staged for commit "
                             "(pre-commit hook mode).")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress summary line; only print violations.")
    args = parser.parse_args()

    repo_root = find_repo_root(args.repo_root)
    if not repo_root.is_dir():
        print(f"error: repo root not a directory: {repo_root}",
              file=sys.stderr)
        return 2

    paths = git_staged_files(repo_root) if args.staged else all_md_files(repo_root)
    violations, _ = audit_files(repo_root, paths, quiet=args.quiet)
    return 0 if violations == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
