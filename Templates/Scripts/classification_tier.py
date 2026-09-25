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

# Frontmatter is delimited exactly as Obsidian delimits it (read from its
# metadata worker, round 3 of the adversarial review, 2026-09-25): the file
# starts with a "---" line (a BOM is tolerated), and the block ends at the
# first later line that STARTS with "---" -- "----" and "--- end" included.
# A reader that ended only at a bare "---" kept reading into what Obsidian
# shows as body text, where a decoy `classification: public` was waiting.
_OPEN_RE = re.compile(r"\A\ufeff?---\n")


def frontmatter(text: str) -> str | None:
    """The leading YAML block without its fences, or None."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not _OPEN_RE.match(text):
        return None
    body = text[_OPEN_RE.match(text).end():]
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("---"):
            return "\n".join(lines[:i])
    return None


# The ONE form of declaration this reader accepts. Three rounds of review
# showed that interpreting anything more -- comments beyond this, duplicate
# spellings, nesting, multi-line and list values, other delimiters -- is a
# YAML parser in regex form, and it lost to Obsidian's parser every round.
_TIER_ALT = "|".join(re.escape(x) for x in TIERS)
_STRICT_RE = re.compile(
    rf"^classification:[ \t]+(?:({_TIER_ALT})|\"({_TIER_ALT})\"|'({_TIER_ALT})')"
    r"[ \t]*(?:[ \t]#[^\n]*)?$", re.I)
_PLAIN_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_ .-]*?)[ \t]*:(?:[ \t]|$)")
_SIBLING_KEY_RE = re.compile(r"classification_[a-z0-9_]+")   # the classifier's own keys
UNREADABLE = "(frontmatter the gate cannot read reliably)"


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def declared(text: str) -> list[str]:
    """Every tier declared, plus UNREADABLE for anything this reader will not
    interpret. Rules, each from a bypass the review proved:

      * `classification: <tier>` at column 0, optionally quoted, optionally
        followed by a " # comment", is the only accepted declaration. Any
        other line whose key is `classification` -- empty, list, folded,
        flow, tagged, multi-line, different case, confusable spelling -- is
        UNREADABLE. So is an accepted declaration followed by an indented
        line, which YAML folds into the value.
      * Any other key whose folded name contains "classif" is UNREADABLE,
        except the classifier's own plain `classification_<word>` keys.
      * A line at column 0 that is not a plain `key:` line, a list item or a
        comment -- quoted, escaped, tagged, anchored, flow or complex keys --
        is UNREADABLE, as is an indented line before any key (YAML then reads
        the whole indented block as the top-level mapping).

    Measured on the maintainer's 2,836-note vault before adoption: every note
    that carries a tier uses the accepted form, and no note is UNREADABLE.
    """
    fm = frontmatter(text)
    if fm is None:
        return []
    out: list[str] = []
    lines = fm.split("\n")
    seen_key = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            if not seen_key:
                out.append(UNREADABLE)
            continue
        if line.startswith("-"):
            if not seen_key:
                out.append(UNREADABLE)
            continue
        m = _PLAIN_KEY_RE.match(line)
        if not m:
            out.append(UNREADABLE)
            continue
        seen_key = True
        key = m.group(1)
        if key == "classification":
            sm = _STRICT_RE.match(line)
            nxt = next((ln for ln in lines[idx + 1:]
                        if ln.strip() and not ln.strip().startswith("#")), "")
            if not sm or nxt[:1] in (" ", "\t"):
                out.append(UNREADABLE)
            else:
                out.append((sm.group(1) or sm.group(2) or sm.group(3)).lower())
        elif "classif" in _fold(key) and not _SIBLING_KEY_RE.fullmatch(key):
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
