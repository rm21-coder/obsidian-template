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

# Default deny: every .md in the tree must be `classification: public`,
# except the template's own scaffolding named here. Until 2026-10-04 the gate
# audited a list of content folders and skipped everything else, so a
# confidential note archived into Z_archive/, put in a new top-level folder,
# or saved at the root was committed unaudited (review, 2026-10-04).
#
# Top-level folders that hold scaffolding, not notes. Any folder starting
# with "." is scaffolding too (.git, .obsidian, .github, .claude).
SCAFFOLD_FOLDERS = frozenset({
    "Templates", "Z_attachments", "docs", "installers",
})
# Root files that are the repo's own documentation.
SCAFFOLD_ROOT_FILES = frozenset({"README.md", "CLAUDE.md", "ONBOARDING.md"})

# Frontmatter pattern: a YAML block at the very top, delimited by ---.
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Anchored at column 0 inside the YAML block so a stray
# "classification:" in body text can't accidentally satisfy the gate.
CLASSIFICATION_RE = re.compile(r"(?mi)^classification[ \t]*:(.*)$")

REQUIRED_VALUE = "public"


def _shared_tier_reader():
    """Templates/Scripts/classification_tier.py from this checkout, the one
    reader every gate shares, or None if the checkout does not carry it (the
    inline fallback below then applies the same core rule)."""
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "Templates" / "Scripts" / "classification_tier.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_classification_tier", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 -- fall back rather than skip the audit
        return None
    return mod


def parse_classification(text: str) -> str | None:
    """Return the lowercased classification value from frontmatter, or None
    if there is no frontmatter or no `classification:` key. Strings are
    unquoted (`"public"` and `public` both resolve to `public`).
    """
    shared = _shared_tier_reader()
    if shared is not None:
        tier, unknown = shared.effective(text)
        if unknown:
            return unknown[0]
        return tier
    m = FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if not m:
        return None
    # EVERY declared value counts, not the first. With a first-match read, a
    # `classification: public` line above a real `confidential` one passed
    # this audit and the note went into a public commit. Any value other than
    # the required one is returned, so the audit fails on it. Same rule as
    # Templates/Scripts/classification_tier.py, restated here because this
    # file also runs on recipients' machines with no vault on the path.
    values = []
    for cm in CLASSIFICATION_RE.finditer(m.group(1)):
        v = cm.group(1).strip()
        if v[:1] in ("'", '"'):
            end = v.find(v[0], 1)
            v = v[1:end] if end != -1 else v[1:]
        else:
            v = re.split(r"[ \t]#", " " + v, maxsplit=1)[0]
        v = v.strip().lower()
        if v:
            values.append(v)
    if not values:
        return None
    others = [v for v in values if v != REQUIRED_VALUE]
    return others[0] if others else REQUIRED_VALUE


def is_markdown(rel_path: Path) -> bool:
    """.md in any case: Knowledge/x.MD is a note like any other."""
    return rel_path.suffix.lower() == ".md"


def should_audit(rel_path: Path) -> bool:
    """Return True unless this .md is the template's own scaffolding.

    Default deny: only the named scaffolding folders, dot-folders and the
    named root documentation files are exempt; everything else -- Z_archive/,
    a folder the template does not know, a note saved at the root -- must be
    public. Only the top level is consulted: a README.md or docs/ folder
    deeper inside a content folder is audited like any other note.
    """
    parts = rel_path.parts
    if not parts or not is_markdown(rel_path):
        return False
    if len(parts) == 1:
        return parts[0] not in SCAFFOLD_ROOT_FILES
    top = parts[0]
    return not (top in SCAFFOLD_FOLDERS or top.startswith("."))


def git_staged_files(repo_root: Path) -> list[Path]:
    """Return paths (relative to repo_root) of .md files staged for commit.

    Uses --diff-filter=ACMRT so deletions and renames-out don't appear.

    -z is load-bearing. Without it git C-quotes any path with a non-ASCII
    byte ("People/Jos\\303\\251 Garc\\303\\255a.md" under the default
    core.quotePath), the quoted string names no file, and the note was
    silently dropped: a staged confidential People/José García.md audited as
    0 files and the commit went through. Accented names come straight from
    invite text, so this is the ordinary case, not an exotic one. -z output
    is never quoted, the same reason git_ignored() uses it.

    A staged path that is not on disk is returned rather than skipped: it is
    still being committed, and audit_files() reports the unreadable file as a
    violation instead of quietly auditing less.
    """
    try:
        out = subprocess.check_output(
            # T: a symlink replaced by a real file is a type change, and
            # would otherwise not be listed at all. :(icase): .MD too.
            ["git", "diff", "--cached", "--name-only", "-z",
             "--diff-filter=ACMRT", "--", ":(icase)*.md"],
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except subprocess.CalledProcessError:
        return []
    return [Path(s) for s in out.split("\0") if s]


def git_ignored(repo_root: Path, paths: list[Path]) -> set[Path]:
    """Return the subset of `paths` that git would ignore.

    The audit exists to keep non-public content out of a PUBLIC repo, so a
    file git will never track cannot be a violation. Without this the audit
    walks the filesystem and flags local runtime output -- most visibly
    `Creations/RAG-Sync-*.md`, which the RAG sync writes on every run and
    `.gitignore` names explicitly. On any machine that has run the sync,
    `install.sh` / `install.ps1` then hard-fail at component 02 until
    `--skip-audit` is passed, which is exactly the wrong reflex to train.

    Fails OPEN (returns an empty set, so nothing is excluded) when git is
    unavailable or this is not a checkout: the audit is a safety net, and a
    missing git must not quietly shrink what it looks at.
    """
    if not paths:
        return set()
    # -z and as_posix() are both load-bearing on Windows, and the first
    # version of this filter had neither -- it silently matched nothing there
    # while appearing to work, which is the same silent-no-op failure mode it
    # was written to remove. Two separate causes:
    #
    #   1. str(Path) yields backslashes on Windows. git treats a path
    #      containing one as needing quoting and echoes it back quoted and
    #      escaped ("Creations\\RAG-Sync-....md"), which never compares equal
    #      to what was sent. as_posix() keeps git in unquoted territory.
    #   2. text=True translates \n to \r\n on stdin, so with newline
    #      delimiters git receives the CR as part of the filename. NUL
    #      delimiters have no newline to translate.
    #
    # -z also makes the output unquoted, so the parse side needs no unescaping.
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
            cwd=repo_root,
            input="\0".join(p.as_posix() for p in paths) + "\0",
            capture_output=True,
            text=True,
            # git speaks UTF-8 paths; the Windows locale codec would mangle
            # a non-ASCII name on the way in.
            encoding="utf-8",
            errors="surrogateescape",
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    # git check-ignore: 0 = some ignored, 1 = none ignored, other = error.
    if proc.returncode not in (0, 1):
        return set()
    return {Path(s) for s in proc.stdout.split("\0") if s.strip()}


def all_md_files(repo_root: Path) -> list[Path]:
    """Return paths (relative to repo_root) of all .md files in the repo.

    Gitignored files are excluded -- see git_ignored().
    """
    paths: list[Path] = []
    for f in repo_root.rglob("*"):
        if not f.is_file() or not is_markdown(Path(f.name)):
            continue
        try:
            rel = f.relative_to(repo_root)
        except ValueError:
            continue
        # Prune only top-level skipped folders (.git, Templates, docs, ...).
        # A folder that merely shares the name deeper in the tree --
        # Knowledge/docs/, People/Templates/ -- is content and is audited;
        # should_audit() makes the final call on every path kept here.
        if rel.parts[0] in SCAFFOLD_FOLDERS or rel.parts[0].startswith("."):
            continue
        paths.append(rel)
    ignored = git_ignored(repo_root, paths)
    return [p for p in paths if p not in ignored]


def staged_text(repo_root: Path, rel: Path) -> str:
    """The staged (index) copy of a file -- what the commit will contain.
    Reading the working tree instead let a partly staged file pass on a
    clean working copy while a confidential version was committed."""
    return subprocess.run(
        ["git", "show", f":{rel.as_posix()}"], cwd=repo_root, check=True,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def range_blobs(repo_root: Path, rev_range: str) -> list[tuple[str, Path]]:
    """(commit, path) for every .md added, changed or type-changed by each
    commit in `rev_range` -- what a push publishes, history included. A note
    committed and then removed or relabelled in a later commit is still
    public once pushed, so the tip alone is not enough."""
    commits = subprocess.run(
        ["git", "rev-list", *rev_range.split()], cwd=repo_root, check=True,
        capture_output=True, text=True, encoding="utf-8").stdout.split()
    out: list[tuple[str, Path]] = []
    for c in commits:
        names = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "-r", "-z", "--root",
             "--name-only", "--diff-filter=ACMRT", c, "--", ":(icase)*.md"],
            cwd=repo_root, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="surrogateescape").stdout
        out += [(c, Path(n)) for n in names.split("\0") if n]
    return out


def audit_range(repo_root: Path, rev_range: str) -> tuple[int, int]:
    """Audit every pushed version of every note. Return (violations, audited)."""
    violations = audited = 0
    for commit, rel in range_blobs(repo_root, rev_range):
        if not should_audit(rel):
            continue
        audited += 1
        try:
            text = subprocess.run(
                ["git", "show", f"{commit}:{rel.as_posix()}"], cwd=repo_root,
                check=True, capture_output=True, text=True, encoding="utf-8",
                errors="replace").stdout
        except subprocess.CalledProcessError as exc:
            print(f"VIOLATION  {rel} @ {commit[:9]}  could not read: {exc}", file=sys.stderr)
            violations += 1
            continue
        value = parse_classification(text)
        if value != REQUIRED_VALUE:
            print(f"VIOLATION  {rel} @ {commit[:9]}\n"
                  f"           classification: {value if value is not None else '(missing)'}\n"
                  f"           required:       {REQUIRED_VALUE}", file=sys.stderr)
            violations += 1
    return violations, audited


def audit_files(
    repo_root: Path, paths: list[Path], quiet: bool, staged: bool = False
) -> tuple[int, int]:
    """Audit the given paths, their staged copies when `staged`. Return
    (violations, audited_count)."""
    violations = 0
    audited = 0
    for rel in paths:
        if not should_audit(rel):
            continue
        audited += 1
        full = repo_root / rel
        try:
            text = (staged_text(repo_root, rel) if staged
                    else full.read_text(encoding="utf-8", errors="replace"))
        except (OSError, subprocess.CalledProcessError) as exc:
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
    parser.add_argument("--range", metavar="REVS",
                        help="Audit every note version the commits in a git "
                             "rev-list range add or change (pre-push mode).")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress summary line; only print violations.")
    args = parser.parse_args()

    repo_root = find_repo_root(args.repo_root)
    if not repo_root.is_dir():
        print(f"error: repo root not a directory: {repo_root}",
              file=sys.stderr)
        return 2

    if args.range:
        violations, audited = audit_range(repo_root, args.range)
        if violations or not args.quiet:
            print(f"classification audit: {audited} pushed note version(s) audited, "
                  f"{violations} violation(s).", file=sys.stderr)
        return 1 if violations else 0
    paths = git_staged_files(repo_root) if args.staged else all_md_files(repo_root)
    violations, _ = audit_files(repo_root, paths, quiet=args.quiet,
                                staged=args.staged)
    return 0 if violations == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
