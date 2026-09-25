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


def declared(text: str) -> list[str]:
    """Every value declared for `classification` in the frontmatter, in order.

    A key with an empty value followed by an indented line is YAML's
    multi-line form, which this does not interpret: it is reported as the
    literal "(multi-line value)" so a gate treats it as unrecognised.
    """
    fm = frontmatter(text)
    if fm is None:
        return []
    out: list[str] = []
    lines = fm.splitlines()
    for m in _KEY_RE.finditer(fm):
        v = _value(m.group(1))
        if not v:
            line_no = fm.count("\n", 0, m.start())
            nxt = lines[line_no + 1] if line_no + 1 < len(lines) else ""
            if nxt[:1] in (" ", "\t"):
                v = "(multi-line value)"
        out.append(v)
    return out


def effective(text: str) -> tuple[str | None, list[str]]:
    """(most restrictive recognised tier or None, unrecognised declared values).

    An empty declared value counts as no value at all.
    """
    values = [v for v in declared(text) if v]
    known = [v for v in values if v in TIER_RANK]
    unknown = [v for v in values if v not in TIER_RANK]
    tier = max(known, key=TIER_RANK.__getitem__) if known else None
    return tier, unknown
