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
    # core.quotePath=false and explicit UTF-8: by default git C-quotes any
    # path with a non-ASCII byte ("People/Jos\303\251.md"), and the quoted
    # string names no file -- a note named after an accented name was
    # dropped from both scans (review, 2026-10-04).
    return subprocess.run(("git", "-c", "core.quotePath=false") + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="surrogateescape", check=False).stdout


def _diff_path(header: str) -> str | None:
    """The path in a `+++ ` diff header, or None for /dev/null. Git still
    C-quotes a path holding a tab, newline, quote or backslash."""
    rest = header[4:]
    if rest == "/dev/null":
        return None
    if rest.startswith('"') and rest.endswith('"'):
        rest = _c_unquote(rest[1:-1])
    return rest[2:] if rest.startswith("b/") else rest


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
              '"': 34, "\\": 92}


def _c_unquote(body: str) -> str:
    """Undo git's C-quoting at the byte level: octal escapes are raw UTF-8
    bytes, and unescaped characters may be any Unicode (with quotePath off),
    so they are re-encoded as UTF-8 rather than Latin-1."""
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567" and i + 3 < len(body) + 0 and body[i + 1:i + 4].isdigit():
                out.append(int(body[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _name_lines(paths: list[str]) -> list[tuple[str, int, str]]:
    """Each path as a line of its own (line 0): a file NAMED after a real
    person publishes the name as surely as its contents would."""
    return [(p, 0, p) for p in paths if not skip_path(p)]


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
    # --text: a file git thinks is binary (one NUL byte) showed only "Binary
    # files differ", and none of its added lines was scanned. ACMRT: a
    # symlink replaced by a file is a type change.
    diff = run_git("diff", "--cached", "--unified=0", "--no-color", "--text",
                   "--diff-filter=ACMRT")
    names = [p for p in run_git("diff", "--cached", "--name-only", "-z",
                                "--diff-filter=ACMRT").split("\0") if p]
    out: list[tuple[str, int, str]] = _name_lines(names)
    path, lineno = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = _diff_path(line)
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


def _decodings(raw: bytes, encoding: str | None = None) -> list[str]:
    """Every reading of bytes git stores verbatim. Always UTF-8 with each byte
    that is not valid UTF-8 read as Latin-1 -- per byte, so one stray Latin-1
    byte cannot garble a UTF-8 name next to it (review rounds 5-7). Plus the
    declared encoding (a commit's or tag's `encoding` header) when it decodes
    and reads differently: in addition, never instead, because git writes the
    bytes it was given under whatever i18n.commitEncoding says, so a declared
    UTF-16 or ISO-8859-1 can sit over UTF-8 text and decode "cleanly" into
    garbage (review round 8, 2026-10-04)."""
    text = raw.decode("utf-8", "surrogateescape")
    texts = [re.sub("[\udc80-\udcff]", lambda m: chr(ord(m.group()) - 0xDC00), text)]
    if encoding:
        try:
            declared = raw.decode(encoding)
        except Exception:       # unknown, malformed (a NUL), or not this text
            declared = None
        if declared is not None and declared != texts[0]:
            texts.append(declared)
    return texts


# A header line is a lowercase key and a value; anything else ends the header
# block, as a hand-built object with a stray "\r" separator line showed (round 7).
_HEADER_RE = re.compile(rb"^[a-z][a-z0-9-]* ")

# Header lines that carry no free text: hashes and the object type, each
# skipped only in its strict form.
_HASH_LINE = re.compile(r"^(?:(?:tree|parent|object) [0-9a-f]{40,64}"
                        r"|type (?:commit|tree|blob|tag))$")
# An identity line is skipped only when it is the maintainer's own, as git is
# configured to write it: a colleague's patch applied with `git am`, or a
# commit made under a work address, publishes that identity (round 8).
_IDENT_LINE = re.compile(r"^(?:author|committer|tagger) ([^<>\n]*<[^<>\n]*>) \d+ [+-]\d{4}$")


def _own_identity() -> str | None:
    name = run_git("config", "user.name").strip()
    email = run_git("config", "user.email").strip()
    return f"{name} <{email}>" if name and email else None


def _parse_object(raw: bytes) -> tuple[list[list[bytes]], bytes]:
    """A tag or commit object's header blocks (a header line and its
    continuation lines, unindented) and its message, as raw bytes."""
    lines = raw.split(b"\n")
    blocks: list[list[bytes]] = []
    n = len(lines)
    for k, line in enumerate(lines):
        if line.startswith(b" ") and blocks:
            blocks[-1].append(line[1:])
        elif _HEADER_RE.match(line):
            blocks.append([line])
        else:
            n = k
            break
    rest = lines[n:]
    if rest and rest[0] == b"":
        rest = rest[1:]            # the blank separator line itself
    return blocks, b"\n".join(rest)


def _scan_object(raw: bytes, label: str, own: str | None,
                 out: list[tuple[str, int, str]]) -> str | None:
    """Queue a tag or commit object's headers and message for scanning, and
    any tag object a `mergetag` header embeds (merging a signed tag copies it
    whole, round 5). Returns a tag's target object, else None."""
    blocks, body = _parse_object(raw)
    encoding = next((b[0][9:].decode("latin-1") for b in blocks
                     if b[0].startswith(b"encoding ") and len(b) == 1), None)
    target, mergetags = None, 0
    for block in blocks:
        first = _decodings(block[0])[0]
        if len(block) == 1:
            m = re.match(r"^object ([0-9a-f]{40,64})$", first)
            if m:
                target = m.group(1)
            if _HASH_LINE.match(first):
                continue
            ident = _IDENT_LINE.match(first)
            if ident and own is not None and ident.group(1) == own:
                continue
            if first.startswith("encoding "):
                continue
            if first.startswith("tag "):
                for text in _decodings(block[0][4:], encoding):
                    out.append((f"{label} name", 1, text))
                continue
        if block[0].startswith(b"mergetag "):
            mergetags += 1
            embedded = b"\n".join([block[0][9:]] + block[1:])
            _scan_object(embedded, f"{label} mergetag {mergetags}", own, out)
            continue
        key = first.partition(" ")[0]
        for i, line in enumerate(block, 1):
            for text in _decodings(line, encoding):
                out.append((f"{label} {key} header", i, text))
    for text in _decodings(body, encoding):
        for i, line in enumerate(text.splitlines(), 1):
            out.append((f"{label} message", i, line))
    return target


def _cat_file(kind: str, sha: str) -> bytes:
    return subprocess.run(("git", "cat-file", kind, sha), capture_output=True,
                          check=False).stdout


# The separators-as-spaces form of a ref name is matched as whole words only:
# as a substring, a branch "visual-green-ci" matched a two-word name entry
# whose first word is the last letters of "visual" (round 8).
REF_WORDS = "pushed ref name (as words)"


class RangeError(RuntimeError):
    """A pushed range git could not list."""


def range_added_lines(rev_range: str,
                      ref_names: list[str] | None = None) -> list[tuple[str, int, str]]:
    """Added lines and names from every commit in a rev-list range: what a
    push publishes, intermediate commits included (a name committed and then
    removed is still in the pushed history)."""
    out: list[tuple[str, int, str]] = []
    own = _own_identity()
    # The ref names a push creates are published too: a branch or tag name,
    # including a lightweight tag's and a renamed push's `src:refs/tags/X`
    # (review round 6, 2026-10-04).
    # A ref name cannot hold a space, so a name entry ("First Last") is also
    # tried against the name with its separators read as spaces (round 7).
    for name in ref_names or []:
        out.append(("pushed ref name", 1, name))
        out.append((REF_WORDS, 1, re.sub(r"[-_./]+", " ", name)))
    listing = subprocess.run(("git", "rev-list", *rev_range.split()), capture_output=True,
                             text=True, encoding="utf-8", check=False)
    if listing.returncode != 0:
        # Fail closed: an unlistable range is not a clean one.
        raise RangeError(listing.stderr.strip() or f"git rev-list {rev_range} failed")
    # Annotated tags being pushed: their names and messages are published too
    # -- every tag in a tag-on-tag chain, and the new side of an updated tag
    # ref, whose range lists no commits at all (review rounds 4-5, 2026-10-04).
    tips: list[str] = []
    for word in rev_range.split():
        if word.startswith(("-", "^")):
            continue
        if "..." in word:
            tips += [side or "HEAD" for side in word.split("...", 1)]
        elif ".." in word:
            tips.append(word.split("..", 1)[1] or "HEAD")
        else:
            tips.append(word)
    for tip in tips:
        obj, seen = tip, set()
        while obj not in seen and run_git("cat-file", "-t", obj).strip() == "tag":
            seen.add(obj)
            target = _scan_object(_cat_file("tag", obj), f"tag {obj[:9]}", own, out)
            if not target:
                break
            obj = target
    for commit in listing.stdout.split():
        # The commit message is published with the commit (review round 3,
        # 2026-10-04), and so is every header. Author and committer fields are
        # not scanned: they are the maintainer's own published identity, on
        # every commit. Everything else is: a merged signed tag's whole object
        # sits in a `mergetag` header (round 5), and signature armor carries
        # free-text Comment: lines (round 6).
        _scan_object(_cat_file("commit", commit), f"commit {commit[:9]}", own, out)
        # -m / --diff-merges=separate: a merge's own changes, per parent.
        names = [p for p in dict.fromkeys(run_git(
            "diff-tree", "-m", "--no-commit-id", "-r", "-z", "--root",
            "--name-only", "--diff-filter=ACMRT", commit).split("\0")) if p]
        out += _name_lines(names)
        diff = run_git("show", "--format=", "--unified=0", "--no-color", "--text",
                       "--diff-merges=separate", "--diff-filter=ACMRT", "--root", commit)
        path, lineno = None, 0
        for line in diff.splitlines():
            if line.startswith("+++ "):
                path = _diff_path(line)
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
        listing = run_git("ls-files", "-z", "--cached", "--others",
                          "--exclude-standard")
        paths = [p for p in listing.split("\0") if p]
    out: list[tuple[str, int, str]] = _name_lines(paths)
    for p in paths:
        if skip_path(p):
            continue
        try:
            # errors="replace": a Latin-1 or otherwise non-UTF-8 file was
            # skipped outright; its ASCII content (addresses, most names) is
            # what the scan needs, and survives the replacement.
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            out.append((p, i, line))
    return out


def scan(lines, rules, allowed) -> list[tuple[str, int, str, str]]:
    """Return (path, line_no, rule, excerpt) for every hit."""
    findings = []
    for path, lineno, text in lines:
        for label, pat in rules:
            if path == REF_WORDS:
                pat = re.compile(rf"(?<![^\W_])(?:{pat.pattern})(?![^\W_])", pat.flags)
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
    mode.add_argument("--range", metavar="REVS",
                      help="scan every commit in a git rev-list range (pre-push hook)")
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
    ap.add_argument("--ref-name", action="append", default=[], metavar="REF",
                    help="--range: a ref name the push publishes (repeatable)")
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

    if args.range:
        try:
            lines = range_added_lines(args.range, args.ref_name)
        except RangeError as exc:
            print(f"check_identity_leak: cannot list commits in {args.range!r}: {exc}",
                  file=sys.stderr)
            return 1
    elif args.staged:
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
        scope = ("pushed" if args.range else
                 "staged" if args.staged else "worktree")
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
