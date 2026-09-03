#!/usr/bin/env python3
"""check_identity_leak.py — refuse commits that publish real identities.

This repo is public. gitleaks (see security-checks.sh) covers credentials, but
a credential is not the only thing that must not be published: tenant email
domains, colleagues' names, internal meeting titles and tenant-specific
hostnames are all harmless-looking strings that gitleaks has no opinion about.
They reach a public commit through docstrings, comments, test fixtures and
example data — none of which feel like "publishing" while they are being
written.

Two rule classes, deliberately different in kind:

  deny-list   Exact local values (tenant domains, real names). Precise, so it
              hard-fails. Lives in `identity-denylist.local`, which is
              GITIGNORED -- a committed list of the names you are protecting
              would publish them, which is the whole failure this guards
              against. Generate it with --init.

  emails      Any real-looking address whose domain is not a reserved or
              conventional example domain. Generic, so it ships in the repo;
              the allowed domains live in `identity-allowed-domains.txt`,
              which is safe to publish because it names only placeholders.

Usage:
    check_identity_leak.py --staged        # added lines in the index (the hook)
    check_identity_leak.py --worktree      # tracked + untracked, not ignored
    check_identity_leak.py --files A B     # specific paths
    check_identity_leak.py --init          # build the local deny-list

Exit status is 1 when anything is found, so it can gate a commit. Override a
single commit with `git commit --no-verify`, and only when you know why.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent
DENYLIST = LIB_DIR / "identity-denylist.local"
ALLOWED_DOMAINS = LIB_DIR / "identity-allowed-domains.txt"

# Files whose whole purpose is to hold these strings, plus anything binary-ish.
#
# The scanner's own test file is here for the same reason as the lists: it has
# to contain addresses and names that trip these rules, or there would be no
# way to prove the rules fire at all. That is a real hole -- a genuine leak
# parked in that one file would pass -- accepted knowingly because the
# alternative is allow-listing the fixture domains, which blunts the rule
# everywhere instead of in one small reviewed file.
#
# For the same reason, do not quote a fixture address in a comment here: this
# file is NOT self-excluded, and the first draft of this very comment tripped
# the scanner on itself.
SELF_EXCLUDE = {DENYLIST.name, ALLOWED_DOMAINS.name, "identity-denylist.example",
                "test_check_identity_leak.py"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".svg", ".zip",
                 ".woff", ".woff2", ".ttf", ".ico", ".excalidraw"}

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def run_git(*args: str) -> str:
    return subprocess.run(("git",) + args, capture_output=True, text=True,
                          check=False).stdout


def load_allowed_domains() -> set[str]:
    """Domains an example address may legitimately use."""
    if not ALLOWED_DOMAINS.is_file():
        return set()
    out = set()
    for line in ALLOWED_DOMAINS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out


def domain_allowed(domain: str, allowed: set[str]) -> bool:
    domain = domain.lower().rstrip(".")
    if domain in allowed:
        return True
    # A suffix entry (".example", ".test") allows every domain under it.
    return any(domain.endswith(a) for a in allowed if a.startswith("."))


def load_denylist() -> list[tuple[str, re.Pattern]]:
    """Compile the local deny-list. Literal by default; `re:` prefix for regex.

    Absence is not an error -- a fresh clone has no local values to protect
    yet -- but it is reported, because a silently empty deny-list would look
    exactly like a passing scan.
    """
    if not DENYLIST.is_file():
        return []
    rules = []
    for raw in DENYLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            body = line[3:].strip()
            try:
                rules.append((body, re.compile(body, re.I)))
            except re.error as e:
                print(f"check_identity_leak: bad regex in {DENYLIST.name}: "
                      f"{body!r} ({e})", file=sys.stderr)
        else:
            rules.append((line, re.compile(re.escape(line), re.I)))
    return rules


def skip_path(path: str) -> bool:
    name = Path(path).name
    return name in SELF_EXCLUDE or Path(path).suffix.lower() in SKIP_SUFFIXES


def staged_added_lines() -> list[tuple[str, int, str]]:
    """Added lines in the index, as (path, line_no, text).

    Scanning the index rather than the working tree is the point: a new file is
    untracked until `git add`, so a working-tree scan run before staging sees
    nothing and reports clean. That exact sequence is how identity strings got
    within one keystroke of a public commit three times in one session.
    """
    diff = run_git("diff", "--cached", "--unified=0", "--no-color",
                   "--diff-filter=ACMR")
    out: list[tuple[str, int, str]] = []
    path, lineno = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if path and not skip_path(path):
                out.append((path, lineno, line[1:]))
            lineno += 1
    return out


def worktree_lines(paths: list[str] | None = None) -> list[tuple[str, int, str]]:
    """Every line of tracked and untracked-but-not-ignored files."""
    if paths is None:
        listing = run_git("ls-files", "--cached", "--others",
                          "--exclude-standard")
        paths = [p for p in listing.splitlines() if p]
    out: list[tuple[str, int, str]] = []
    for p in paths:
        if skip_path(p):
            continue
        try:
            text = Path(p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            out.append((p, i, line))
    return out


def scan(lines, rules, allowed) -> list[tuple[str, int, str, str]]:
    """Return (path, line_no, rule, excerpt) for every hit."""
    findings = []
    for path, lineno, text in lines:
        for label, pat in rules:
            m = pat.search(text)
            if m:
                findings.append((path, lineno, f"deny-list: {label}",
                                 text.strip()[:120]))
                break
        for m in EMAIL_RE.finditer(text):
            if not domain_allowed(m.group(1), allowed):
                findings.append((path, lineno,
                                 f"real-looking address: {m.group(0)}",
                                 text.strip()[:120]))
    return findings


SOURCE_VAULT_MARKER = "# source-vault: "


def read_source_vault() -> Path | None:
    """Where --init last read People/ from, recorded in the deny-list header.

    Stored in the file rather than re-derived so the staleness check needs no
    config and no arguments -- it has to run inside a pre-commit hook, where
    anything requiring setup would simply not run.
    """
    if not DENYLIST.is_file():
        return None
    for line in DENYLIST.read_text(encoding="utf-8").splitlines():
        if line.startswith(SOURCE_VAULT_MARKER):
            val = line[len(SOURCE_VAULT_MARKER):].strip()
            if val:
                return Path(val).expanduser()
        if not line.startswith("#"):
            break          # header is over; rules must not redirect the check
    return None


def stale_people(vault: Path | None) -> int:
    """How many People notes are newer than the deny-list.

    A colleague added after the last --init has no rules at all, and nothing
    about a clean scan reveals that -- the protection for that name simply
    does not exist. Comparing mtimes is the cheapest signal that says
    "regenerate".

    Deliberately a warning and not a block: a stale list is a gap in coverage,
    not a leak, and refusing a commit over it would train the operator to pass
    --no-verify, which also disables the check that catches real leaks.
    """
    if vault is None or not DENYLIST.is_file():
        return 0
    people = vault / "People"
    if not people.is_dir():
        return 0
    cutoff = DENYLIST.stat().st_mtime
    n = 0
    try:
        with os.scandir(people) as it:
            for entry in it:
                if entry.name.endswith(".md") and entry.is_file():
                    if entry.stat().st_mtime > cutoff:
                        n += 1
    except OSError:
        return 0
    return n


def warn_if_stale() -> None:
    """Print a non-fatal staleness note. Runs even under --quiet: surfacing
    this during an ordinary commit is the entire reason it exists."""
    vault = read_source_vault()
    n = stale_people(vault)
    if n:
        print(f"check_identity_leak: NOTE -- {n} People note(s) in "
              f"{vault / 'People'} are newer than the deny-list.",
              file=sys.stderr)
        print("  Names added since the last --init are not protected yet. "
              "Refresh with:", file=sys.stderr)
        print(f"    {Path(__file__).name} --init", file=sys.stderr)
        print("  (warning only -- this does not block the commit)",
              file=sys.stderr)


def init_denylist(vault: Path, config: Path) -> int:
    """Build the local deny-list from values already on this machine.

    Sources are things the operator already has and already keeps out of the
    repo: the meeting-pull config's tenant domains and identity, and the
    People/ note names in the real vault. Nothing is invented and nothing is
    committed.
    """
    entries: set[str] = set()

    if config.is_file():
        try:
            cfg = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
        for d in cfg.get("tenant_domains") or []:
            if d:
                entries.add(str(d).strip().lower())
        for k in ("email", "display_name", "tenant"):
            v = str(cfg.get(k) or "").strip()
            if v:
                entries.add(v)
        email = str(cfg.get("email") or "")
        if "@" in email:
            local = email.split("@", 1)[0]
            if len(local) >= 5:
                entries.add(local)

    people = vault / "People"
    if people.is_dir():
        for note in people.glob("*.md"):
            stem = note.stem.strip()
            # Only full-name forms. A bare surname would match common words
            # and a hook that cries wolf is a hook people bypass.
            if "," not in stem:
                continue
            last, _, first = (x.strip() for x in stem.partition(","))
            first = first.split()[0] if first else ""
            if len(last) < 3 or len(first) < 2:
                continue
            entries.add(f"{last}, {first}")
            entries.add(f"{first} {last}")

    if not entries:
        print("check_identity_leak: --init found nothing to protect. Checked:\n"
              f"  config: {config}\n  vault:  {people}", file=sys.stderr)
        return 1

    header = [
        "# identity-denylist.local -- GITIGNORED, never commit this file.",
        "#",
        "# Strings that must not reach a public commit: tenant domains, real",
        "# names, tenant hostnames. Generated by:",
        "#     installers/lib/check_identity_leak.py --init",
        "#",
        "# One value per line, matched case-insensitively as a literal.",
        "# Prefix with 're:' for a regular expression. '#' starts a comment.",
        "# Hand-edit freely; --init merges rather than overwrites.",
        "#",
        "# The marker below tells later runs where to look for People notes",
        "# added since this file was built. Keep it inside the header block.",
        SOURCE_VAULT_MARKER + str(vault),
        "",
    ]
    existing = []
    if DENYLIST.is_file():
        for line in DENYLIST.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                existing.append(s)
    merged = sorted(entries | set(existing), key=str.lower)
    DENYLIST.write_text("\n".join(header + merged) + "\n", encoding="utf-8")
    print(f"check_identity_leak: wrote {len(merged)} rule(s) to {DENYLIST}")
    print(f"  ({len(merged) - len(existing)} new, {len(existing)} kept)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true",
                      help="scan added lines in the index (what the hook uses)")
    mode.add_argument("--worktree", action="store_true",
                      help="scan tracked + untracked-not-ignored files entirely")
    mode.add_argument("--files", nargs="+", metavar="PATH",
                      help="scan these paths entirely")
    mode.add_argument("--init", action="store_true",
                      help="generate/refresh the local deny-list and exit")
    ap.add_argument("--vault", default=str(Path.home() / "Obsidian"),
                    help="--init: real vault root (default: ~/Obsidian)")
    ap.add_argument("--config", default=None,
                    help="--init: meeting_pull.json path")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when clean")
    args = ap.parse_args()

    if args.init:
        vault = Path(args.vault).expanduser()
        config = (Path(args.config).expanduser() if args.config
                  else vault / "Templates" / "Scripts" / ".config" / "meeting_pull.json")
        return init_denylist(vault, config)

    rules = load_denylist()
    allowed = load_allowed_domains()

    if args.staged:
        lines = staged_added_lines()
    elif args.worktree:
        lines = worktree_lines()
    else:
        lines = worktree_lines(args.files)

    findings = scan(lines, rules, allowed)

    warn_if_stale()

    if findings:
        print("check_identity_leak: real identities in content about to be "
              "published:", file=sys.stderr)
        for path, lineno, rule, excerpt in findings:
            print(f"  {path}:{lineno}: {rule}", file=sys.stderr)
            print(f"      {excerpt}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Replace them with example values. If a hit is a false positive, "
              "add the domain to", file=sys.stderr)
        print(f"  {ALLOWED_DOMAINS.name}", file=sys.stderr)
        print("or narrow the deny-list rule. Override once with "
              "`git commit --no-verify`.", file=sys.stderr)
        return 1

    if not args.quiet:
        scope = "staged" if args.staged else "worktree"
        print(f"check_identity_leak: {scope} content clean "
              f"({len(rules)} deny-list rule(s), {len(lines)} line(s) scanned).")
        if not rules:
            # An empty deny-list passes everything and looks identical to a
            # real pass, so say so rather than implying coverage.
            print("  note: no local deny-list. Only the generic address rule "
                  "ran. Build one with --init.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
