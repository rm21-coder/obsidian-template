#!/usr/bin/env python3
"""
vault_lint.py — content lint for the second-brain vault. Dry-run by default.

Complements the security harness rather than overlapping it: integrity_monitor.py
watches for *hostile* change (script hashes, LaunchAgents, bulk deletion), while
this watches for *entropy* — tags that drifted off the taxonomy, notes that got
clipped twice, vault copies of scripts that fell behind the template repo.

Seven checks, in two classes.

  FIXABLE (rewritten in place by --apply, with a rollback manifest):
    dup-tags      the same tag listed twice in one note's frontmatter
    bad-tags      malformed tags ('#Golf', '- Shooting', 'AI  - AI/Tools') and
                  high-confidence remaps onto the canonical allowlist

  REPORT-ONLY (never touches a file — these need a human call):
    dup-notes     near-identical notes, confirmed by body similarity
    taxonomy      off-allowlist tags, unused allowlist tags, convention breaks
    script-drift  Templates/Scripts vs the public template repo
    schema        notes missing frontmatter keys their folder's template defines
    links         wikilinks to notes that don't exist; unreferenced People/Groups

Two further fixers, deliberately separate from --apply because they rewrite more
than a tag list:

    --fix-malformed   repairs frontmatter the auto-tagger spliced (see
                      repair_malformed); refuses anything outside that signature
    --fix-links       relinks broken wikilinks that resolve unambiguously to an
                      existing note, unlinks the rest keeping the display text

Only the frontmatter `tags:` block is ever rewritten by --apply. Bodies, other
frontmatter keys, indentation, and quoting are left byte-for-byte unchanged.
Every write is recorded in a rollback manifest.

    python3 Templates/Scripts/vault_lint.py                  # dry-run, all checks
    python3 Templates/Scripts/vault_lint.py --apply          # write the fixable ones
    python3 Templates/Scripts/vault_lint.py --only taxonomy  # one check (repeatable)
    python3 Templates/Scripts/vault_lint.py --skip links     # all but one (repeatable)
    python3 Templates/Scripts/vault_lint.py --verbose        # full lists, not samples
    python3 Templates/Scripts/vault_lint.py --json report.json
    python3 Templates/Scripts/vault_lint.py --rollback vault_lint_manifest_*.json

Before it is useful you have to tell it what "correct" means for your vault:
Knowledge/Tag Taxonomy.md is the tag allowlist, REQUIRED_KEYS below is the
frontmatter contract per folder, and EXPECTED_DRIFT records the vault/repo
differences you intend to keep. With an empty taxonomy file the tag checks
simply no-op rather than inventing a standard for you.

Exit status is 1 when any finding is reported, so a scheduled run can gate on
it — except with --exit-zero, which the bundled weekly LaunchAgent uses so that
findings don't read as a failed job. See docs/Vault-Lint.md.
"""

import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Cross-platform: force UTF-8 on stdout/stderr so printing a note title that
# contains non-ASCII characters can't crash the run on Windows, whose console
# defaults to a legacy code page (cp1252). Harmless no-op on macOS/Linux.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()  # two levels up from Templates/Scripts/
TAXONOMY = VAULT_ROOT / "Knowledge" / "Tag Taxonomy.md"

# Your clone of this template repo, for the script-drift check — the one that
# catches a vault still running last month's copy of a script. Override with
# VAULT_LINT_REPO if you keep the clone somewhere else. The check skips itself
# cleanly when the path doesn't exist, so a vault with no clone still lints.
REPO_ROOT = Path(os.environ.get(
    "VAULT_LINT_REPO", Path.home() / "dev" / "repos" / "obsidian-template")).expanduser()

# Folders holding generated, archived, or binary content — never linted.
EXCLUDE_DIRS = {"Z_archive", "Z_attachments", "Z_dashboards", "Excalidraw", ".obsidian", ".trash"}

# Frontmatter keys each folder's template defines. Only *missing* keys are
# reported; extra keys are fine (notes legitimately accrue fields over time).
REQUIRED_KEYS = {
    "People": ["categories", "classification", "tags"],
    "Meetings": ["categories", "type", "classification", "tags"],
    "Knowledge": ["tags", "classification"],
    "Creations": ["categories", "classification", "tags"],
    "Groups": ["classification", "tags"],
}

# Subtrees holding machine-generated operational output that happens to live
# under a content folder. They are logs, not notes: giving a pipeline run
# summary `categories: "[[Meetings]]"` would file it alongside real meetings in
# every Base and category view that reads that property.
# Nothing writes Meetings/_Runs any more; retained for installs that still
# carry the folder from before the run-summary note was removed.
SCHEMA_EXCLUDE = {
    "Meetings/_Runs",
}

# Values --fix-schema writes for a missing key, per folder. A key with no entry
# here is reported but never auto-filled — 'type' on a meeting is Group vs
# Individual vs Ad-hoc, which cannot be inferred from the file and would be a
# guess written into your data.
SCHEMA_DEFAULTS = {
    "People":    {"categories": ['"[[Categories/People]]"'], "tags": [], "classification": "confidential"},
    "Meetings":  {"categories": ['"[[Meetings]]"'], "tags": [], "classification": "confidential"},
    "Knowledge": {"tags": [], "classification": "internal-use-only"},
    "Creations": {"categories": ['"[[Creations]]"'], "tags": [], "classification": "internal-use-only"},
    "Groups":    {"tags": [], "classification": "internal-use-only"},
}

# --- vault-lint: config-start -------------------------------------------------
# Everything down to config-end is per-deployment settings. The script-drift
# check skips this region when comparing your vault's copy of a file against the
# repo's, so you can keep local values here without the lint reporting itself as
# drift every week. The markers work in any script the check compares, not just
# this one — wrap a local config block in them and the rest of the file is still
# compared strictly.

# Scripts that live only in your vault and have no repo counterpart on purpose —
# local wrappers, machine-specific setup helpers, anything hardcoding personal
# paths or connector IDs. Listing them here keeps them out of the drift report.
# Add your own; the commented entries below are examples from a real
# deployment (local wrapper scripts, machine-rendered configs).
VAULT_ONLY_OK = {
    # "dashboard_actions.sh",          # hardcodes local paths + connector IDs
    # "build_dashboard_actions_app.sh",
    "voice_cleanup_config.yaml",   # rendered from the .example at install time
}

# Deliberate, permanent divergences between your vault's copy of a script and
# the repo's. Each entry maps a filename to regexes; a changed diff line
# matching any of them is expected and stays quiet, while every *other* changed
# line in that same file still reports. That distinction is the point — an
# accepted exception must not become a hiding place for a real regression.
#
# Example: a vault that routes Claude calls through an institutional gateway
# while the repo stays on stock Anthropic auth, so other adopters can use their
# own keys:
#
#   _GATEWAY = [r"MY_ORG_API_KEY", r"api\.ai\.example\.edu", r"ANTHROPIC_API_KEY",
#               r"base_url", r"anthropic\.Anthropic\("]
#   EXPECTED_DRIFT = {"tag_clippings.py": _GATEWAY, "voice_cleanup.py": _GATEWAY}
#
# Note: since the llm_endpoint refactor, gateway routing is configuration
# (LLM_BASE_URL / LLM_API_KEY_NAME in your env), not forked code — a fresh
# install usually needs no entries here at all.
EXPECTED_DRIFT: dict = {}

# Repo scripts that intentionally have no vault counterpart.
REPO_ONLY_OK = {
    "seed_demo_content.py",       # seeds this template's demo dataset
    "run_group_photos.py",        # Windows Task Scheduler entry point; the macOS
                                  # LaunchAgent runs the same two scripts via sh -c
}
# --- vault-lint: config-end ---------------------------------------------------

# Applies to every file: the installer renders per-machine values into its
# templates, so a placeholder on one side and the real value on the other is
# the system working, not drift. Matched on the home directory's *name* with
# either separator, so this holds on Windows (C:\Users\me) as well as POSIX.
EXPECTED_DRIFT_GLOBAL = [r"YOUR_USERNAME",
                         r"[/\\]Users[/\\]" + re.escape(Path.home().name)]

# Tags that are template scaffolding or tooling artifacts rather than real
# topics — reported separately from genuine promotion candidates so the noise
# doesn't bury the signal.
SCAFFOLD_TAGS = {
    "classification", "note", "moc", "journal", "task", "clippings",
    "syntheses", "meetings/type", "security", "ciso",
}

DUP_BODY_SIMILARITY = 0.90  # Jaccard over body tokens, above which two notes are "the same note"

_FM_SPLIT = re.compile(r"^(---\s*\n)(.*?)(\n---\s*\n?)(.*)$", re.DOTALL)
_TAGS_BLOCK = re.compile(r"^tags:\s*$")
_TAGS_INLINE = re.compile(r"^tags:\s*\[(.*)\]\s*$")
_LIST_ITEM = re.compile(r"^\s*-\s+(.+?)\s*$")
_WIKILINK = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
_ATTACHMENT_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|bmp|pdf|mp4|mov|m4a|mp3|wav|base|canvas)$", re.I)

CHECKS = ["dup-tags", "bad-tags", "dup-notes", "taxonomy", "script-drift", "schema", "links"]
FIXABLE = {"dup-tags", "bad-tags"}


# ---------- frontmatter -------------------------------------------------------

def split_frontmatter(text):
    """(open, fm, close, body) or None if the note has no frontmatter."""
    m = _FM_SPLIT.match(text)
    return m.groups() if m else None


def read_tags(fm):
    """Tags in frontmatter order, duplicates preserved. Handles block and inline forms."""
    lines = fm.split("\n")
    for i, line in enumerate(lines):
        if _TAGS_BLOCK.match(line):
            out, j = [], i + 1
            while j < len(lines) and _LIST_ITEM.match(lines[j]):
                out.append(_LIST_ITEM.match(lines[j]).group(1).strip("\"'"))
                j += 1
            return out
        m = _TAGS_INLINE.match(line)
        if m:
            return [x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()]
    return []


def write_tags(text, new_tags):
    """Rewrite only the tags block. Returns new text, or None if nothing to write."""
    parts = split_frontmatter(text)
    if not parts:
        return None
    open_, fm, close, body = parts
    lines = fm.split("\n")
    for i, line in enumerate(lines):
        if _TAGS_BLOCK.match(line):
            j = i + 1
            while j < len(lines) and _LIST_ITEM.match(lines[j]):
                j += 1
            indent_m = re.match(r"^(\s*)-", lines[i + 1]) if j > i + 1 else None
            indent = indent_m.group(1) if indent_m else "  "
            if new_tags:
                block = [lines[i]] + ["{}- {}".format(indent, t) for t in new_tags]
            else:
                block = ["tags: []"]
            return open_ + "\n".join(lines[:i] + block + lines[j:]) + close + body
        m = _TAGS_INLINE.match(line)
        if m:
            quoted = '"' in m.group(1) or "'" in m.group(1)
            items = ['"{}"'.format(t) for t in new_tags] if quoted else new_tags
            lines[i] = "tags: [{}]".format(", ".join(items))
            return open_ + "\n".join(lines) + close + body
    return None


_ORPHAN_FRAGMENT = re.compile(r"^\d{1,2}(:\d{2})?$")
_SPLICED_TAG = re.compile(r"^(updated:\s*\S+?)\s{2,}-\s+(\S+)\s*$")


def repair_malformed(text):
    """Undo the auto-tagger's frontmatter splice. Returns new text, or None.

    The damage has one signature: a tag was written into the middle of the
    'updated:' timestamp, leaving the seconds stranded on their own line
    ('36', '1:36') and sometimes a '  - Tag' fragment welded to the timestamp.
    The stranded seconds are dropped rather than reconstructed — the split
    point varies, and Obsidian rewrites 'updated:' on its own anyway — and the
    welded tag is folded back into the tags block if it isn't already there.
    Anything that doesn't match this exact shape is left alone.
    """
    parts = split_frontmatter(text)
    if not parts:
        return None
    open_, fm, close, body = parts
    lines, out, recovered, changed = fm.split("\n"), [], [], False

    for line in lines:
        if _ORPHAN_FRAGMENT.match(line.strip()):
            changed = True
            continue
        m = _SPLICED_TAG.match(line)
        if m:
            out.append(m.group(1))
            recovered.append(m.group(2))
            changed = True
            continue
        out.append(line)

    if not changed:
        return None
    if fm_anomalies("\n".join(out)):
        return None                      # damage we don't fully understand — leave it

    new_fm = "\n".join(out)
    rebuilt = open_ + new_fm + close + body
    if recovered:
        have = read_tags(new_fm)
        missing = [t for t in recovered if t not in have]
        if missing:
            merged = write_tags(rebuilt, have + missing)
            if merged:
                rebuilt = merged
    return rebuilt if rebuilt != text else None


def fm_keys(fm):
    return {m.group(1) for m in re.finditer(r"^([A-Za-z][\w\- ]*):", fm, re.M)}


def fm_anomalies(fm):
    """Frontmatter lines that are neither a key, a list item, nor blank.

    A mangled edit can leave an orphan fragment behind (one note carries a bare
    '/Claude' line under its tags block), which is invalid YAML. Rewriting only
    the tags block of such a note would tidy the tags and leave the corruption
    in place, so any note flagged here is reported and skipped by --apply rather
    than half-fixed.
    """
    bad = []
    block_indent = None          # inside a '|' / '>' block scalar, skip its body
    for line in fm.split("\n"):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None
        if re.match(r"^\s*[A-Za-z_\"'][^:]*:\s*[|>][-+]?\s*$", line):
            block_indent = indent
            continue
        if re.match(r"^\s*-\s", line):                    # list item
            continue
        if re.match(r"^\s*[A-Za-z_\"'][^:]*:", line):     # key, possibly nested or quoted
            continue
        bad.append(line)
    return bad


def fm_scalar(fm, key):
    m = re.search(r"^{}:\s*(.+)$".format(re.escape(key)), fm, re.M)
    return m.group(1).strip().strip("\"'") if m else None


# ---------- vault walk --------------------------------------------------------

def iter_notes():
    for p in sorted(VAULT_ROOT.rglob("*.md")):
        rel = p.relative_to(VAULT_ROOT)
        if any(part in EXCLUDE_DIRS or part.startswith(".") for part in rel.parts):
            continue
        yield p, rel


def load_notes():
    """[(path, rel, text, fm, body, tags)] for every note carrying frontmatter."""
    out = []
    for p, rel in iter_notes():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        parts = split_frontmatter(text)
        if not parts:
            out.append((p, rel, text, "", text, []))
            continue
        _, fm, _, body = parts
        out.append((p, rel, text, fm, body, read_tags(fm)))
    return out


# ---------- taxonomy ----------------------------------------------------------

def load_allowlist():
    """Canonical tags from the '- Tag' bullets in Tag Taxonomy.md's body."""
    if not TAXONOMY.exists():
        return set()
    text = TAXONOMY.read_text(encoding="utf-8")
    parts = split_frontmatter(text)
    body = parts[3] if parts else text
    return {m.group(1) for m in re.finditer(r"^-\s+([A-Za-z0-9][\w/\-]*)\s*$", body, re.M)}


def canonicalize(tag, allow):
    """High-confidence normalization of one tag.

    Returns (tags, reason) where tags is the replacement list — possibly empty
    (drop it) or longer than one (a collapsed pair split back apart) — or
    (None, None) when no confident fix exists, leaving it for the taxonomy
    report. Every rule must land on a tag that is actually in the allowlist;
    a guess that doesn't is not a fix.
    """
    raw = tag.strip()
    if not raw:
        return [], "empty"
    if raw in allow:
        return None, None

    # Two tags collapsed onto one line by a bad edit: 'AI  - AI/Tools'.
    if re.search(r"\S\s+-\s+\S", raw):
        pieces = [x.strip() for x in re.split(r"\s+-\s+", raw) if x.strip()]
        if len(pieces) > 1 and all(p in allow or canonicalize(p, allow)[0] for p in pieces):
            out = []
            for p in pieces:
                fixed, _ = canonicalize(p, allow)
                out.extend([p] if fixed is None else fixed)
            return out, "split collapsed pair"

    # Leading '#' (inline-tag syntax leaking into a YAML list) or a stray '- '.
    stripped = raw.lstrip("#").strip()
    stripped = re.sub(r"^-\s+", "", stripped).strip()
    if stripped != raw and stripped in allow:
        return [stripped], "stripped '#'/'-' prefix"

    # Path doubling from a mis-scoped rename: 'Work/Work/Security' -> 'Work/Security'.
    doubled = re.sub(r"^([^/]+)/\1/", r"\1/", stripped)
    if doubled != stripped and doubled in allow:
        return [doubled], "collapsed doubled path segment"

    # Casing-only mismatch against a canonical tag.
    ci = {t.lower(): t for t in allow}
    if stripped.lower() in ci:
        return [ci[stripped.lower()]], "corrected casing"

    # A flat tag that has exactly one home in the hierarchy: 'AWS' -> 'Vendors/AWS'.
    leaves = [t for t in allow if "/" in t and t.rsplit("/", 1)[1].lower() == stripped.lower()]
    if len(leaves) == 1:
        return [leaves[0]], "reparented under {}".format(leaves[0].rsplit("/", 1)[0])

    # Singular/plural slip against the collections convention: 'GPU' -> 'GPUs'.
    for cand in (stripped + "s", stripped[:-1] if stripped.endswith("s") else None):
        if cand and cand in allow:
            return [cand], "matched plural convention"

    return None, None


def convention_breaks(tag):
    """Violations of the conventions Tag Taxonomy.md states for itself."""
    out = []
    if tag.count("/") > 1:
        out.append("depth >2")
    if " " in tag:
        out.append("contains space")
    leaf = tag.rsplit("/", 1)[-1]
    # A leading lowercase letter is fine when the name itself is styled that way
    # (eIRB, iPhone) — only an all-lowercase leaf is a real casing break.
    if leaf and leaf[0].islower() and not any(c.isupper() for c in leaf[1:]):
        out.append("not PascalCase")
    return out


# ---------- checks ------------------------------------------------------------

def check_tags(notes, allow):
    """Both fixable tag checks in one pass. Returns (dup, bad, malformed) findings."""
    dups, bads, malformed = [], [], []
    for p, rel, text, fm, _body, tags in notes:
        if not fm:
            continue
        anomalies = fm_anomalies(fm)
        if anomalies:
            malformed.append({"rel": str(rel), "path": str(p), "lines": anomalies})
        if not tags:
            continue

        fixed, removed_dups, remapped, unfixable = [], [], [], []
        seen = set()
        for tag in tags:
            new, reason = canonicalize(tag, allow) if allow else (None, None)
            if new is None:
                targets = [tag]
                if allow and tag not in allow:
                    unfixable.append(tag)
            else:
                targets = new
                remapped.append((tag, new, reason))
            for t in targets:
                if t in seen:
                    removed_dups.append(t)
                else:
                    seen.add(t)
                    fixed.append(t)

        # A note whose frontmatter has an unexplained line is never rewritten —
        # fixing its tags would leave the real corruption behind, looking clean.
        blocked = bool(anomalies)
        if removed_dups:
            dups.append({"rel": str(rel), "path": str(p), "removed": removed_dups,
                         "before": tags, "after": fixed, "blocked": blocked})
        if remapped:
            bads.append({"rel": str(rel), "path": str(p),
                         "remapped": [{"from": a, "to": b, "why": c} for a, b, c in remapped],
                         "before": tags, "after": fixed, "blocked": blocked})
        if unfixable:
            # Surfaced by the taxonomy check, not here — nothing to rewrite.
            pass
    return dups, bads, malformed


def check_taxonomy(notes, allow):
    if not allow:
        return {"error": "Tag Taxonomy.md not found or has no '- Tag' bullets"}
    used = Counter()
    for _p, _rel, _t, fm, _b, tags in notes:
        if fm:
            used.update(tags)

    off = {t: c for t, c in used.items() if t not in allow}
    fixable, promote, stray = {}, {}, {}
    for tag, count in off.items():
        new, reason = canonicalize(tag, allow)
        if new is not None:
            fixable[tag] = {"count": count, "to": new, "why": reason}
        elif tag.lower() in SCAFFOLD_TAGS or count < 3:
            stray[tag] = count
        else:
            promote[tag] = count

    breaks = {}
    for tag in allow:
        b = convention_breaks(tag)
        if b:
            breaks[tag] = b

    return {
        "allowlist_size": len(allow),
        "distinct_used": len(used),
        "off_allowlist_uses": sum(off.values()),
        "auto_fixable": fixable,
        "promotion_candidates": promote,
        "strays": stray,
        "unused_allowlist": sorted(set(allow) - set(used)),
        "convention_breaks": breaks,
    }


def _body_tokens(body):
    return set(re.findall(r"[a-z0-9]{3,}", body.lower()))


def _similar(a, b):
    ta, tb = _body_tokens(a), _body_tokens(b)
    if not ta or not tb:
        return 1.0 if ta == tb else 0.0
    return len(ta & tb) / float(len(ta | tb))


def _norm_name(s):
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"\s+\d+$", "", s)          # Obsidian's ' 1' copy suffix
    return re.sub(r"[^a-z0-9]", "", s)


def check_dup_notes(notes):
    """Candidate pairs from name/URL/title, confirmed by body similarity.

    The confirmation step is what makes this usable: 28 YouTube notes share the
    bare watch URL, 25 course notes share one product URL, and 7 camera-manual
    pages share one help URL — all distinct notes. Only near-identical bodies
    are reported.
    """
    by_name, by_url, by_title = defaultdict(list), defaultdict(list), defaultdict(list)
    index = {}
    for p, rel, _text, fm, body, _tags in notes:
        index[str(rel)] = body
        by_name[_norm_name(rel.stem)].append(str(rel))
        if not fm:
            continue
        url = fm_scalar(fm, "source") or fm_scalar(fm, "url")
        if url and len(url) > 12 and not url.startswith("markitdown"):
            base = url.split("?")[0].rstrip("/")
            if base.count("/") > 2:  # a bare host or a shared endpoint groups everything
                by_url[base].append(str(rel))
        title = fm_scalar(fm, "title")
        if title:
            key = _norm_name(title)
            if key:
                by_title[key].append(str(rel))

    seen_pairs, findings = set(), []
    for source, groups in (("name", by_name), ("source url", by_url), ("title", by_title)):
        for key, members in groups.items():
            if len(members) < 2 or len(members) > 12:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = sorted((members[i], members[j]))
                    if (a, b) in seen_pairs:
                        continue
                    sim = _similar(index[a], index[b])
                    if sim >= DUP_BODY_SIMILARITY:
                        seen_pairs.add((a, b))
                        findings.append({"a": a, "b": b, "matched_on": source,
                                         "similarity": round(sim, 3),
                                         "bytes": [len(index[a]), len(index[b])]})
    return sorted(findings, key=lambda f: -f["similarity"])


_CONFIG_START = re.compile(r"vault-lint:\s*config-start")
_CONFIG_END = re.compile(r"vault-lint:\s*config-end")


def _strip_config_blocks(text):
    """Drop lines between 'vault-lint: config-start' / 'config-end' markers.

    A vault's copy of a script often differs from the repo's only in a block of
    local settings. Enumerating those lines as regexes in EXPECTED_DRIFT is
    brittle — reindent a comment and the exception stops matching. Wrapping the
    block in markers instead says "this region is configuration" once, and the
    diff simply skips it, so the rest of the file is still compared strictly.
    """
    out, skipping = [], False
    for line in text.split("\n"):
        if _CONFIG_START.search(line):
            skipping = True
            continue
        if _CONFIG_END.search(line):
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _behavioral_signature(text, suffix):
    """A representation of what a file *does*, with comments and docs removed.

    Two files with the same signature differ only in prose, so the drift is
    documentation rather than behavior — worth knowing, but not the thing that
    silently broke the meeting pipeline. Returns None when the file can't be
    reduced confidently, in which case the caller treats the drift as
    behavioral rather than assuming it is benign.
    """
    if suffix == ".py":
        try:
            import ast
            tree = ast.parse(text)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(getattr(body[0], "value", None), ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    node.body = body[1:] or [ast.Pass()]
        return ast.dump(tree)
    if suffix in (".plist", ".xml"):
        return re.sub(r"\s+", " ", re.sub(r"<!--.*?-->", "", text, flags=re.S)).strip()
    if suffix == ".sh":
        keep = [ln for ln in text.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        return "\n".join(keep)
    return None


def check_script_drift():
    vault_dir = VAULT_ROOT / "Templates" / "Scripts"
    repo_dir = REPO_ROOT / "Templates" / "Scripts"
    if not repo_dir.is_dir():
        return {"skipped": "template repo not found at {} (set VAULT_LINT_REPO)".format(REPO_ROOT)}
    if repo_dir.resolve() == vault_dir.resolve():
        # Linting the repo itself, e.g. against its demo content. Comparing a
        # directory to itself would report "in sync" and mean nothing.
        return {"skipped": "vault and template repo are the same directory"}

    drifted, docs_only, vault_only, repo_only, expected = [], [], [], [], []
    suffixes = {".py", ".sh", ".plist", ".js", ".txt", ".yaml"}
    vault_files = {f.name for f in vault_dir.iterdir() if f.suffix in suffixes}
    repo_files = {f.name for f in repo_dir.iterdir() if f.suffix in suffixes}

    for name in sorted(vault_files & repo_files):
        vtext = (vault_dir / name).read_text(encoding="utf-8", errors="ignore")
        rtext = (repo_dir / name).read_text(encoding="utf-8", errors="ignore")
        if vtext == rtext:
            continue

        # A LaunchAgent plist in the vault is the installer's *rendered* output —
        # real paths, and per-machine EnvironmentVariables the template can't
        # carry. Diffing the text just reprints the configuration every run. What
        # actually matters is a key the template gained and the installed copy
        # never got, so compare the key sets and ignore extra local keys.
        if Path(name).suffix == ".plist":
            vkeys = set(re.findall(r"<key>([^<]+)</key>", vtext))
            rkeys = set(re.findall(r"<key>([^<]+)</key>", rtext))
            gained = rkeys - vkeys
            if gained:
                drifted.append({"file": name, "changed_lines": len(gained),
                                "unexplained_lines": len(gained),
                                "sample": ["missing <key>{}</key> the repo template defines".format(k)
                                           for k in sorted(gained)]})
            continue

        v = _strip_config_blocks(vtext).splitlines()
        r = _strip_config_blocks(rtext).splitlines()
        if v == r:
            expected.append({"file": name, "lines": 0})
            continue
        changed = [ln for ln in difflib.unified_diff(r, v, lineterm="", n=0)
                   if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
                   and ln[1:].strip()]          # a blank added/removed line says nothing
        patterns = EXPECTED_DRIFT.get(name, []) + EXPECTED_DRIFT_GLOBAL
        unexplained = [ln for ln in changed
                       if not any(re.search(pat, ln) for pat in patterns)]
        if not unexplained:
            expected.append({"file": name, "lines": len(changed)})
            continue

        suffix = Path(name).suffix
        vsig, rsig = _behavioral_signature(vtext, suffix), _behavioral_signature(rtext, suffix)
        entry = {"file": name, "changed_lines": len(changed),
                 "unexplained_lines": len(unexplained), "sample": unexplained[:6]}
        if vsig is not None and rsig is not None and vsig == rsig:
            docs_only.append(entry)
        else:
            drifted.append(entry)

    for name in sorted(vault_files - repo_files):
        if name not in VAULT_ONLY_OK:
            vault_only.append(name)
    for name in sorted(repo_files - vault_files):
        if name not in REPO_ONLY_OK:
            repo_only.append(name)

    return {"drifted": drifted, "docs_only": docs_only, "vault_only_unexplained": vault_only,
            "repo_only": repo_only, "expected_split": expected}


def check_schema(notes):
    missing = defaultdict(lambda: defaultdict(list))
    no_frontmatter = []
    for _p, rel, _text, fm, _body, _tags in notes:
        top = rel.parts[0]
        if top not in REQUIRED_KEYS:
            continue
        if any(str(rel.parent) == ex or str(rel).startswith(ex + "/")
               for ex in SCHEMA_EXCLUDE):
            continue
        if not fm:
            no_frontmatter.append(str(rel))
            continue
        keys = fm_keys(fm)
        for want in REQUIRED_KEYS[top]:
            if want not in keys:
                missing[top][want].append(str(rel))
    return {"missing": {k: dict(v) for k, v in missing.items()},
            "no_frontmatter": no_frontmatter}


def _links_by_field(text):
    """(field, target, in_code) for every wikilink.

    field is the frontmatter key the link sits under, or None in the body.
    in_code marks links inside a fence or inline-code span — documentation
    showing what a wikilink looks like, which must not be counted as broken.
    """
    parts = split_frontmatter(text)
    out = []
    if parts:
        key = None
        for line in parts[1].split("\n"):
            m = re.match(r"^([A-Za-z_][\w\- ]*):", line)
            if m:
                key = m.group(1)
            for _embed, raw in _WIKILINK.findall(line):
                out.append((key, raw, False))
        body = parts[3]
        offset = len(text) - len(body)
    else:
        body, offset = text, 0
    fenced, spans = _protected_spans(body)
    for i, line in enumerate(body.split("\n")):
        for m in _WIKILINK.finditer(line):
            in_code = i in fenced or any(s <= m.start() < e for s, e in spans.get(i, []))
            out.append((None, m.group(2), in_code))
    return out


def _protected_spans(text):
    """Line indices inside ``` fences, plus (line, start, end) inline-code spans.

    Documentation about wikilinks is full of deliberate examples — `[[Person]]`,
    `[[Lastname, First]]`, `[[${selected}]]` in a Templater snippet. They are
    illustrations, not links, and rewriting them would corrupt the docs and
    templates that explain the system.
    """
    fenced, fence = set(), False
    spans = defaultdict(list)
    for i, line in enumerate(text.split("\n")):
        if line.lstrip().startswith("```"):
            fence = not fence
            fenced.add(i)
            continue
        if fence:
            fenced.add(i)
            continue
        for m in re.finditer(r"`[^`\n]*`", line):
            spans[i].append((m.start(), m.end()))
    return fenced, spans


def _swap_name(name):
    """'Dana Ackerman' -> 'ackerman, dana' for order-insensitive matching."""
    if "," in name:
        last, _, first = name.partition(",")
        return (first.strip() + " " + last.strip()).lower()
    parts = name.split()
    if len(parts) == 2:
        return (parts[1] + ", " + parts[0]).lower()
    return None


def resolve_target(target, targets):
    """Best existing note for a broken link target, or None to unlink instead.

    Only confident, unambiguous repairs: a stray bracket, a diacritic or casing
    slip, a reversed name, or a single very-close spelling match. Four notes
    named 'King, *' make '[[king]]' ambiguous, so it is left to be unlinked
    rather than attributed to the wrong person.
    """
    cleaned = target.lstrip("[").strip()
    if cleaned != target and cleaned in targets:
        return cleaned

    lower = {t.lower(): t for t in targets}
    if cleaned.lower() in lower:
        return lower[cleaned.lower()]

    swapped = _swap_name(cleaned)
    if swapped and swapped in lower:
        return lower[swapped]

    def fold(s):
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()

    folded = {}
    for t in targets:
        folded.setdefault(fold(t), []).append(t)
    hit = folded.get(fold(cleaned))
    if hit and len(hit) == 1:
        return hit[0]

    close = difflib.get_close_matches(cleaned, list(targets), n=2, cutoff=0.88)
    if len(close) == 1:
        # A trailing disambiguator ('2026-07-14 1500-2' vs '2026-07-14 1500',
        # 'Report 3' vs 'Report') marks a *different* item, not a misspelling of
        # this one. Spelling repair must never collapse two distinct entities.
        long_, short = sorted((cleaned, close[0]), key=len, reverse=True)
        if long_.startswith(short) and re.match(r"^[\s\-_]*\d+$", long_[len(short):]):
            return None
        return close[0]
    return None


def fix_links(notes, broken):
    """Relink what resolves confidently, unlink the rest. Returns change records."""
    names = set()
    for _p, rel, _text, _fm, _body, _tags in notes:
        names.add(rel.stem)
        names.add(str(rel.with_suffix("")))

    by_source = defaultdict(set)
    for target, sources in broken.items():
        for s in sources:
            by_source[s].add(target)

    changes = []
    for source, targets_here in sorted(by_source.items()):
        rel = Path(source)
        if rel.parts and rel.parts[0] == "Templates":
            continue                      # Templater placeholders, not links
        path = VAULT_ROOT / rel
        original = path.read_text(encoding="utf-8")
        fenced, spans = _protected_spans(original)
        lines = original.split("\n")
        relinked, unlinked, skipped = [], [], []

        for i, line in enumerate(lines):
            if i in fenced:
                continue
            new_line, offset = line, 0

            def replace(m):
                raw = m.group(2)
                target = raw.split("|")[0].split("#")[0].split("^")[0].strip()
                if target not in targets_here:
                    return m.group(0)
                if any(s <= m.start() < e for s, e in spans.get(i, [])):
                    skipped.append(target)
                    return m.group(0)
                fixed = resolve_target(target, names)
                if fixed:
                    relinked.append((target, fixed))
                    return m.group(0).replace(target, fixed, 1)
                unlinked.append(target)
                display = raw.split("|", 1)[1] if "|" in raw else target
                return display

            new_line = _WIKILINK.sub(replace, line)
            if new_line != line:
                lines[i] = new_line

        new_text = "\n".join(lines)
        # A relink can turn '[[[Nguyen, Chris]]' into '[[[Nguyen, Chris]]' with
        # the target corrected but the stray bracket still there; drop it.
        new_text = re.sub(r"\[\[\[(?=[^\[\]]+\]\])", "[[", new_text)
        if new_text != original:
            changes.append({"path": str(path), "rel": source, "original": original,
                            "new": new_text, "relinked": relinked,
                            "unlinked": unlinked, "skipped": skipped})
    return changes


def fix_schema(schema_result):
    """Append missing frontmatter keys, using SCHEMA_DEFAULTS. Returns changes.

    Keys are appended at the end of the frontmatter block rather than woven into
    template order — YAML mappings are unordered, and rewriting the whole block
    to reorder it would touch notes far beyond the missing key. Only keys with a
    default are filled; anything else stays a report-only finding.
    """
    wanted = defaultdict(dict)          # path -> {key: value}
    for folder, keys in schema_result.get("missing", {}).items():
        defaults = SCHEMA_DEFAULTS.get(folder, {})
        for key, files in keys.items():
            if key not in defaults:
                continue
            for rel in files:
                wanted[rel][key] = defaults[key]

    changes = []
    for rel, keys in sorted(wanted.items()):
        path = VAULT_ROOT / rel
        original = path.read_text(encoding="utf-8")
        parts = split_frontmatter(original)
        if not parts:
            continue                     # handled by the no_frontmatter finding
        open_, fm, close, body = parts
        if fm_anomalies(fm):
            continue                     # never edit frontmatter we can't parse
        lines = fm.rstrip("\n").split("\n")
        present = fm_keys(fm)
        added = []
        for key in ("categories", "classification", "tags"):
            if key not in keys or key in present:
                continue
            value = keys[key]
            if isinstance(value, list):
                lines.append("{}:".format(key) if value else "{}: []".format(key))
                lines.extend("  - {}".format(v) for v in value)
            else:
                lines.append("{}: {}".format(key, value))
            added.append(key)
        if not added:
            continue
        new_text = open_ + "\n".join(lines) + close + body
        path.write_text(new_text, encoding="utf-8")
        changes.append({"path": str(path), "rel": rel, "original": original,
                        "added": added})
    return changes


def check_links(notes):
    targets, referenced = set(), set()
    for p, rel, _text, fm, _body, _tags in notes:
        targets.add(rel.stem)
        targets.add(str(rel.with_suffix("")))
        if fm:
            m = re.search(r"^aliases:\s*\[(.*)\]", fm, re.M)
            if m:
                for a in m.group(1).split(","):
                    a = a.strip().strip("\"'")
                    if a:
                        targets.add(a)
    # A link may point at a folder ('[[Creations]]', '[[Categories/People]]');
    # Obsidian resolves those, so they are not broken links.
    for d in VAULT_ROOT.rglob("*"):
        if d.is_dir() and not any(x in EXCLUDE_DIRS or x.startswith(".")
                                  for x in d.relative_to(VAULT_ROOT).parts):
            targets.add(d.name)
            targets.add(str(d.relative_to(VAULT_ROOT)))
    for att in (VAULT_ROOT / "Z_attachments").rglob("*"):
        if att.is_file():
            targets.add(att.stem)
            targets.add(att.name)

    broken = defaultdict(list)
    external_authors, doc_examples = set(), set()
    for _p, rel, text, _fm, _body, _tags in notes:
        for field, raw, in_code in _links_by_field(text):
            target = raw.split("|")[0].split("#")[0].split("^")[0].strip()
            if not target:
                continue
            referenced.add(target)
            referenced.add(target.rsplit("/", 1)[-1])
            if _ATTACHMENT_EXT.search(target):
                continue
            if target in targets or target.rsplit("/", 1)[-1] in targets:
                continue
            # A wikilink inside code is documentation showing the syntax, and a
            # Templater placeholder is filled in at note-creation time. Neither
            # is a link that could resolve, so neither is broken.
            if in_code or rel.parts[0] == "Templates" or re.search(r"[${}]", target):
                doc_examples.add(target)
                continue
            # Clippings link their byline as a wikilink. A recipe writer with no
            # People note is the normal case, not a broken link — counted, but
            # kept out of the list so real breaks stay visible.
            if field == "author":
                external_authors.add(target)
                continue
            broken[target].append(str(rel))

    orphans = defaultdict(list)
    for _p, rel, _text, _fm, _body, _tags in notes:
        if rel.parts[0] in ("People", "Groups") and rel.stem not in referenced:
            orphans[rel.parts[0]].append(str(rel))

    return {"broken": {k: v for k, v in sorted(broken.items(), key=lambda kv: -len(kv[1]))},
            "external_authors": len(external_authors),
            "doc_examples": len(doc_examples),
            "orphans": dict(orphans)}


# ---------- reporting ---------------------------------------------------------

def head(title):
    print("\n" + title)
    print("-" * len(title))


def sample(items, verbose, n=8):
    items = list(items)
    if verbose or len(items) <= n:
        return items, ""
    return items[:n], "    … and {} more (--verbose for all)".format(len(items) - n)


def report(results, verbose):
    findings = 0

    if "dup-tags" in results:
        r = results["dup-tags"]
        head("dup-tags — same tag listed twice in one note")
        if not r:
            print("  clean")
        else:
            findings += len(r)
            print("  {} notes, {} redundant entries".format(
                len(r), sum(len(x["removed"]) for x in r)))
            shown, more = sample(r, verbose)
            for x in shown:
                print("    {}\n        drop {}".format(x["rel"], x["removed"]))
            if more:
                print(more)

    if "bad-tags" in results:
        r = results["bad-tags"]
        head("bad-tags — malformed tags and high-confidence remaps")
        if not r:
            print("  clean")
        else:
            findings += len(r)
            print("  {} notes".format(len(r)))
            shown, more = sample(r, verbose)
            for x in shown:
                print("    {}".format(x["rel"]))
                for m in x["remapped"]:
                    print("        {!r} -> {}   ({})".format(m["from"], m["to"], m["why"]))
            if more:
                print(more)

    if results.get("malformed-fm"):
        r = results["malformed-fm"]
        findings += len(r)
        head("malformed-fm — frontmatter lines that are neither key nor list item")
        print("  {} notes — invalid YAML; --apply skips these rather than half-fixing".format(len(r)))
        shown, more = sample(r, verbose, 10)
        for x in shown:
            print("    {}".format(x["rel"]))
            for ln in x["lines"][:3]:
                print("        {!r}".format(ln))
        if more:
            print(more)

    if "taxonomy" in results:
        r = results["taxonomy"]
        head("taxonomy — drift against Tag Taxonomy.md")
        if "error" in r:
            print("  {}".format(r["error"]))
        else:
            print("  {} canonical tags, {} distinct in use, {} uses off-allowlist".format(
                r["allowlist_size"], r["distinct_used"], r["off_allowlist_uses"]))
            if r["auto_fixable"]:
                print("  auto-fixable by --apply ({}):".format(len(r["auto_fixable"])))
                for t, d in sorted(r["auto_fixable"].items(), key=lambda kv: -kv[1]["count"]):
                    print("      {:4d}  {!r} -> {}".format(d["count"], t, d["to"]))
            if r["promotion_candidates"]:
                findings += len(r["promotion_candidates"])
                print("  promotion candidates — real topics, 3+ uses, not in allowlist:")
                for t, c in sorted(r["promotion_candidates"].items(), key=lambda kv: -kv[1]):
                    print("      {:4d}  {}".format(c, t))
            if r["strays"]:
                findings += len(r["strays"])
                print("  strays — scaffolding or one-offs, likely delete:")
                shown, more = sample(sorted(r["strays"].items(), key=lambda kv: -kv[1]), verbose)
                for t, c in shown:
                    print("      {:4d}  {}".format(c, t))
                if more:
                    print(more)
            if r["unused_allowlist"]:
                print("  canonical but unused: {}".format(", ".join(r["unused_allowlist"])))
            if r["convention_breaks"]:
                findings += len(r["convention_breaks"])
                print("  allowlist entries breaking the file's own conventions:")
                for t, b in sorted(r["convention_breaks"].items()):
                    print("      {}  ({})".format(t, ", ".join(b)))

    if "dup-notes" in results:
        r = results["dup-notes"]
        head("dup-notes — near-identical notes (report only)")
        if not r:
            print("  clean")
        else:
            findings += len(r)
            print("  {} pairs at >={:.0%} body similarity".format(len(r), DUP_BODY_SIMILARITY))
            shown, more = sample(r, verbose, 12)
            for x in shown:
                print("    {:.0%}  matched on {}".format(x["similarity"], x["matched_on"]))
                print("        {}  ({} bytes)".format(x["a"], x["bytes"][0]))
                print("        {}  ({} bytes)".format(x["b"], x["bytes"][1]))
            if more:
                print(more)

    if "script-drift" in results:
        r = results["script-drift"]
        head("script-drift — vault Templates/Scripts vs template repo")
        if "skipped" in r:
            print("  skipped: {}".format(r["skipped"]))
        else:
            if r["expected_split"]:
                print("  expected divergence per EXPECTED_DRIFT, no action: {}".format(
                    ", ".join(x["file"] for x in r["expected_split"])))
            if not any((r["drifted"], r["docs_only"], r["vault_only_unexplained"], r["repo_only"])):
                print("  in sync")
            if r["drifted"]:
                print("  BEHAVIORAL — vault and repo do different things:")
            for x in r["drifted"]:
                findings += 1
                print("    {}  — {} changed lines, {} unexplained".format(
                    x["file"], x["changed_lines"], x["unexplained_lines"]))
                for ln in (x["sample"] if verbose else x["sample"][:3]):
                    print("        {}".format(ln[:110]))
            if r["docs_only"]:
                findings += len(r["docs_only"])
                print("  comments/docs only, same behavior: {}".format(
                    ", ".join(x["file"] for x in r["docs_only"])))
            if r["vault_only_unexplained"]:
                findings += len(r["vault_only_unexplained"])
                print("    vault-only, not in repo and not on the allowlist: {}".format(
                    ", ".join(r["vault_only_unexplained"])))
            if r["repo_only"]:
                findings += len(r["repo_only"])
                print("    in repo, never deployed to vault: {}".format(", ".join(r["repo_only"])))

    if "schema" in results:
        r = results["schema"]
        head("schema — frontmatter keys the folder's template defines")
        if not r["missing"] and not r["no_frontmatter"]:
            print("  clean")
        for folder, keys in sorted(r["missing"].items()):
            for key, files in sorted(keys.items(), key=lambda kv: -len(kv[1])):
                findings += 1
                print("  {}/ missing '{}': {} notes".format(folder, key, len(files)))
                shown, more = sample(files, verbose, 4)
                for f in shown:
                    print("      {}".format(f))
                if more:
                    print(more)
        if r["no_frontmatter"]:
            findings += 1
            print("  no frontmatter at all: {} notes".format(len(r["no_frontmatter"])))
            shown, more = sample(r["no_frontmatter"], verbose, 4)
            for f in shown:
                print("      {}".format(f))
            if more:
                print(more)

    if "links" in results:
        r = results["links"]
        head("links — unresolved wikilinks and unreferenced notes")
        if not r["broken"]:
            print("  no broken links")
        else:
            findings += len(r["broken"])
            print("  {} unresolved targets".format(len(r["broken"])))
            shown, more = sample(list(r["broken"].items()), verbose, 10)
            for target, sources in shown:
                print("    [[{}]]  <- {} note(s), e.g. {}".format(
                    target, len(sources), sources[0]))
            if more:
                print(more)
        ignored = []
        if r.get("external_authors"):
            ignored.append("{} author bylines (expected for clippings)".format(
                r["external_authors"]))
        if r.get("doc_examples"):
            ignored.append("{} documentation examples and template placeholders".format(
                r["doc_examples"]))
        if ignored:
            print("  (ignored: {})".format("; ".join(ignored)))
        for folder, files in sorted(r["orphans"].items()):
            findings += 1
            print("  {}/ referenced by nothing: {} notes".format(folder, len(files)))
            shown, more = sample(files, verbose, 4)
            for f in shown:
                print("      {}".format(f))
            if more:
                print(more)

    return findings


# ---------- main --------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Lint the vault for duplicates, typos, and drift.")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the fixable checks (dup-tags, bad-tags); default is dry-run")
    ap.add_argument("--fix-malformed", action="store_true",
                    help="also repair the tagger's frontmatter splice in malformed-fm notes")
    ap.add_argument("--fix-schema", action="store_true",
                    help="append missing frontmatter keys using SCHEMA_DEFAULTS; keys with no "
                         "default (e.g. a meeting's type) stay report-only rather than guessed")
    ap.add_argument("--fix-links", action="store_true",
                    help="relink broken wikilinks that resolve to an existing note, unlink the "
                         "rest; never touches Templates/, code fences, or inline code")
    ap.add_argument("--only", action="append", choices=CHECKS, metavar="CHECK",
                    help="run only this check (repeatable): " + ", ".join(CHECKS))
    ap.add_argument("--skip", action="append", choices=CHECKS, metavar="CHECK",
                    help="skip this check (repeatable)")
    ap.add_argument("--verbose", action="store_true", help="full lists instead of samples")
    ap.add_argument("--json", metavar="PATH", help="also write findings as JSON")
    ap.add_argument("--rollback", metavar="MANIFEST", help="undo a prior --apply")
    ap.add_argument("--exit-zero", action="store_true",
                    help="always exit 0 when the run completes. For scheduled runs: launchd's "
                         "status should mean 'the lint ran', not 'the vault is clean', or the "
                         "dashboard reports every finding as a failed job.")
    args = ap.parse_args()

    if args.rollback:
        manifest = json.loads(Path(args.rollback).read_text(encoding="utf-8"))
        for rec in manifest["changes"]:
            Path(rec["path"]).write_text(rec["original"], encoding="utf-8")
        print("Rolled back {} files from {}".format(len(manifest["changes"]), args.rollback))
        return 0

    active = [c for c in (args.only or CHECKS) if c not in (args.skip or [])]

    allow = load_allowlist()
    notes = load_notes()
    print("{} | vault {} | {} notes".format(
        "APPLY" if args.apply else "DRY-RUN", VAULT_ROOT, len(notes)))

    results = {}
    if "dup-tags" in active or "bad-tags" in active:
        dups, bads, malformed = check_tags(notes, allow)
        if "dup-tags" in active:
            results["dup-tags"] = dups
        if "bad-tags" in active:
            results["bad-tags"] = bads
        results["malformed-fm"] = malformed
    if "taxonomy" in active:
        results["taxonomy"] = check_taxonomy(notes, allow)
    if "dup-notes" in active:
        results["dup-notes"] = check_dup_notes(notes)
    if "script-drift" in active:
        results["script-drift"] = check_script_drift()
    if "schema" in active:
        results["schema"] = check_schema(notes)
    if "links" in active:
        results["links"] = check_links(notes)

    findings = report(results, args.verbose)

    # Apply — one write per note, merging both fixable checks so a note needing
    # each is not written twice.
    if args.apply or args.fix_malformed or args.fix_links or args.fix_schema:
        merged, skipped = {}, set()
        if args.apply:
            for key in ("dup-tags", "bad-tags"):
                for x in results.get(key, []):
                    if x.get("blocked"):
                        skipped.add(x["rel"])
                        continue
                    cur = merged.setdefault(x["path"], {"rel": x["rel"], "before": x["before"],
                                                        "after": x["after"]})
                    cur["after"] = x["after"]
        changes = []
        for path, x in sorted(merged.items()):
            original = Path(path).read_text(encoding="utf-8")
            new_text = write_tags(original, x["after"])
            if new_text is None or new_text == original:
                continue
            Path(path).write_text(new_text, encoding="utf-8")
            changes.append({"path": path, "rel": x["rel"], "before": x["before"],
                            "after": x["after"], "original": original})

        if args.fix_malformed:
            repaired = []
            for x in results.get("malformed-fm", []):
                original = Path(x["path"]).read_text(encoding="utf-8")
                new_text = repair_malformed(original)
                if new_text is None:
                    continue
                Path(x["path"]).write_text(new_text, encoding="utf-8")
                changes.append({"path": x["path"], "rel": x["rel"], "before": x["lines"],
                                "after": ["<frontmatter repaired>"], "original": original})
                repaired.append(x["rel"])
                skipped.discard(x["rel"])
            print("Repaired frontmatter in {} note(s).".format(len(repaired)))
            unrepaired = [x["rel"] for x in results.get("malformed-fm", [])
                          if x["rel"] not in repaired]
            if unrepaired:
                print("Left alone (damage outside the known signature):")
                for rel in unrepaired:
                    print("    {}".format(rel))

        if args.fix_schema:
            if "schema" not in results:
                print("--fix-schema needs the schema check; it was skipped.")
            else:
                schema_changes = fix_schema(results["schema"])
                for c in schema_changes:
                    changes.append({"path": c["path"], "rel": c["rel"],
                                    "before": ["missing keys"],
                                    "after": c["added"], "original": c["original"]})
                filled = Counter(k for c in schema_changes for k in c["added"])
                print("Schema: filled {} key(s) across {} notes{}".format(
                    sum(filled.values()), len(schema_changes),
                    " — " + ", ".join("{} x{}".format(k, n) for k, n in filled.items())
                    if filled else ""))

        if args.fix_links:
            broken = results.get("links", {}).get("broken")
            if broken is None:
                print("--fix-links needs the links check; it was skipped.")
            else:
                link_changes = fix_links(notes, broken)
                relinked = [r for c in link_changes for r in c["relinked"]]
                unlinked = [u for c in link_changes for u in c["unlinked"]]
                protected = [s for c in link_changes for s in c["skipped"]]
                for c in link_changes:
                    Path(c["path"]).write_text(c["new"], encoding="utf-8")
                    changes.append({"path": c["path"], "rel": c["rel"],
                                    "before": ["broken links"], "after": ["<links fixed>"],
                                    "original": c["original"]})
                print("Links: relinked {}, unlinked {}, across {} notes.".format(
                    len(relinked), len(unlinked), len(link_changes)))
                for was, now in sorted(set(relinked)):
                    print("    relinked  [[{}]] -> [[{}]]".format(was, now))
                for t in sorted(set(unlinked)):
                    print("    unlinked  [[{}]]".format(t))
                if protected:
                    print("    left as documentation examples: {}".format(
                        ", ".join(sorted(set(protected)))))
        if changes:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            manifest = Path(__file__).parent / "vault_lint_manifest_{}.json".format(stamp)
            manifest.write_text(json.dumps({"changes": changes}, ensure_ascii=False, indent=0),
                                encoding="utf-8")
            print("\nRewrote {} notes. Rollback manifest: {}".format(len(changes), manifest.name))
            print("Undo with:  python3 vault_lint.py --rollback {}".format(manifest))
        else:
            print("\nNothing to rewrite.")
        if skipped:
            print("Skipped {} note(s) with malformed frontmatter — fix by hand:".format(len(skipped)))
            for rel in sorted(skipped):
                print("    {}".format(rel))
    elif any(k in results for k in FIXABLE):
        print("\nDry-run. Re-run with --apply to rewrite the fixable checks. No files changed.")

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print("JSON written to {}".format(args.json))

    if args.exit_zero:
        print("\n{} finding(s). Exiting 0 (--exit-zero).".format(findings))
        return 0
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
