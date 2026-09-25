"""
classification_tier.py — one strict reader for a note's declared tier.

Every gate that acts on `classification:` used to parse it with its own regex,
and each regex took the FIRST match and read the rest of the line verbatim.
Two consequences, both found by the adversarial review of the Microsoft M-DASH
fixes (2026-09-25):

  * `classification: restricted   # PHI` is `restricted` to YAML, but a gate
    that kept the comment saw an unrecognised value. The export gate treated
    that as "unclassified", which --override may lift, so a restricted note
    was exported with an override. "Never lift restricted" did not hold.
  * Two `classification:` lines disagree: a first-match regex reads the first,
    PyYAML the last. Meeting pre-population wrote invite-authored text into
    frontmatter unescaped, so a crafted subject could add a `public` line
    above the real `confidential` one, and the gates believed it.

The rule here, for every gate: read ALL declared values in the frontmatter
block, apply YAML's comment and quoting rules, and take the MOST RESTRICTIVE
recognised one. Anything declared but unrecognised is reported so each gate
can fail closed on it rather than guess.

Stdlib only and import-light, so every gate can use it.
"""
from __future__ import annotations

import re
import unicodedata

TIERS = ("public", "internal-use-only", "confidential", "restricted")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

# A leading BOM is tolerated: an editor that writes one should not turn a
# classified note into an unclassified (and therefore overridable) one.
_FM_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# Case-insensitive on purpose: YAML keys are case-sensitive, so `Classification`
# is technically another key, but a gate should treat a near-miss as a claim
# about the tier and fail toward the stricter reading, not ignore it.
_KEY_RE = re.compile(r"(?mi)^classification[ \t]*:(.*)$")


def frontmatter(text: str) -> str | None:
    """The leading YAML block without its fences, or None."""
    m = _FM_RE.match(text or "")
    return m.group(1) if m else None


def _value(raw: str) -> str:
    v = raw.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        v = v[1:end] if end != -1 else v[1:]
    else:
        # A YAML comment starts at "#" preceded by whitespace.
        v = re.split(r"[ \t]#", " " + v, maxsplit=1)[0]
    return v.strip().lower()


# A top-level line this reader fully understands: a plain key, a colon, then
# a space or end of line. Plain YAML keys cannot contain escapes, quotes or
# flow syntax, which is exactly what makes them readable without a parser.
_PLAIN_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .-]*[ \t]*:(?:[ \t]|$)")
UNREADABLE = "(frontmatter the gate cannot read reliably)"


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def declared(text: str) -> list[str]:
    """Every value declared for `classification`, in order, plus UNREADABLE
    wherever the frontmatter takes a shape this reader cannot interpret.

    Round 2 of the adversarial review (2026-09-25) showed why a regex over
    "column-0 key lines" is not enough on its own: frontmatter written as a
    JSON/flow mapping -- `{"classification": "restricted", ...}` -- or with a
    quoted key is valid YAML that Obsidian reads, and the regex saw either a
    planted nested `classification: public` or nothing at all. Rather than
    grow a YAML parser (stdlib only, and it would still disagree with
    Obsidian's somewhere), anything at top level that is not a plain
    `key: value` line or a block-list item is reported as UNREADABLE, which
    every gate treats as an unrecognised tier: fail closed. Measured on the
    maintainer's 2,836-note vault, no note uses such a line.
    """
    fm = frontmatter(text)
    if fm is None:
        return []
    out: list[str] = []
    lines = fm.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        if line[:1] in (" ", "\t"):
            # Indented: a continuation, a nested mapping or a list item. A
            # nested `classification` is not the note's tier, but it is a
            # claim about it the gate cannot place -- fail closed.
            if re.match(r"^[ \t]+[\"']?classification[\"']?[ \t]*:", _fold(line)):
                out.append(UNREADABLE)
            continue
        if line.startswith("-"):
            continue                    # block-list item of the key above
        m = _KEY_RE.match(line)
        if m:
            v = _value(m.group(1))
            quoted = m.group(1).strip()[:1] in ("'", '"')
            if nxt[:1] in (" ", "\t") and not nxt.lstrip().startswith(("-", "#")):
                # YAML folds an indented next line into this value
                # ("public" + "restricted" -> "public restricted"); an
                # empty value with an indented line is a nested/multi-line
                # value. Either way, not something to read literally.
                if not v or not quoted:
                    v = "(multi-line value)"
            out.append(v)
            continue
        if not _PLAIN_KEY_RE.match(line):
            out.append(UNREADABLE)
    return out


def effective(text: str) -> tuple[str | None, list[str]]:
    """(most restrictive recognised tier or None, unrecognised declared values).

    An empty declared value counts as no value at all. Gates must treat a
    non-empty second element as "cannot evaluate", whatever the first says:
    PyYAML and Obsidian may read a different value than the one recognised.
    """
    values = [v for v in declared(text) if v]
    known = [v for v in values if v in TIER_RANK]
    unknown = [v for v in values if v not in TIER_RANK]
    tier = max(known, key=TIER_RANK.__getitem__) if known else None
    return tier, unknown
