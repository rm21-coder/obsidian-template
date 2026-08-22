#!/usr/bin/env python3
"""
disclosure_check.py — disclosure-aware export gate for the vault.

The classification property (Knowledge/Data Classification.md) only earns its
keep if something refuses to act on it. `obsidian-rag-sync.py` was the first
consumer and gates one tier. This is the second: it refuses to export, copy or
attach vault content whose tier exceeds the audience it is bound for.

AUDIENCES — each names a ceiling, not a wish:

    public    ceiling `public`             leaving the organization entirely: a
                                           public repo, a conference deck, a site
    internal  ceiling `internal-use-only`  circulating inside the organization
    cleared   ceiling `confidential`       a named, cleared distribution — an
                                           executive committee, a review group

`restricted` is never exportable at any audience. It is the tier for PHI, PII
and credentials; if a genuine need exists, move the specific content, not the
note.

WHAT MAKES THIS MORE THAN A GREP — transclusion:

    An Obsidian embed `![[Some Note]]` pulls that note's BODY into the exporting
    note. Exporting note A therefore discloses everything A embeds, however A
    itself is classified. This gate resolves embeds recursively and judges the
    whole closure. Plain links `[[Some Note]]` do not carry content and are
    reported for information only.

FAIL CLOSED: a note with a missing or unrecognised `classification` blocks.
That is the same posture rag-sync takes, and for the same reason — an unlabeled
note is an unreviewed note, not a safe one.

Usage:
    # will these files clear a given audience?
    python3 disclosure_check.py check NOTE... --audience public

    # copy what is allowed to a destination, refusing the whole export if
    # anything is blocked (add --skip-blocked to export the rest with a manifest)
    python3 disclosure_check.py export NOTE... --to DIR --audience internal

    # deliberate exception — always logged, never available for `restricted`
    python3 disclosure_check.py check NOTE --audience public --override "reason"

Exit codes:  0 clear · 1 blocked · 2 usage/error
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()

TIERS = ["public", "internal-use-only", "confidential", "restricted"]
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

AUDIENCES = {
    "public":   "public",
    "internal": "internal-use-only",
    "cleared":  "confidential",
}

# No audience raises the ceiling this high. Kept separate from AUDIENCES so that
# adding an audience can never accidentally make restricted material exportable.
NEVER_EXPORTABLE = "restricted"

MAX_EMBED_DEPTH = 6

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_CLASS_RE = re.compile(r"(?m)^classification\s*:[ \t]*(.+?)[ \t]*$")
# Embeds carry content; plain links do not. The leading '!' is the whole
# difference and is why they are matched separately.
_EMBED_RE = re.compile(r"!\[\[([^\]\n|#]+)(?:[#|][^\]\n]*)?\]\]")
_LINK_RE = re.compile(r"(?<!!)\[\[([^\]\n|#]+)(?:[#|][^\]\n]*)?\]\]")
_ATTACHMENT_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|bmp|pdf|mp4|mov|m4a|mp3|wav|canvas|base)$", re.I)


def note_tier(path: Path) -> str | None:
    """The note's declared tier, or None when absent or unrecognised."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = _FM_RE.match(text)
    if not m:
        return None
    c = _CLASS_RE.search(m.group(1))
    if not c:
        return None
    val = c.group(1).strip().strip('"').strip("'").lower()
    return val if val in TIER_RANK else None


def _index_vault() -> dict[str, Path]:
    """Map lowercase note stem AND vault-relative path -> file, for wikilinks."""
    index: dict[str, Path] = {}
    for md in VAULT_ROOT.rglob("*.md"):
        rel = md.relative_to(VAULT_ROOT)
        if any(p.startswith(".") for p in rel.parts):
            continue
        index.setdefault(md.stem.lower(), md)
        index[rel.with_suffix("").as_posix().lower()] = md
    return index


def resolve_link(target: str, index: dict[str, Path]) -> Path | None:
    key = target.strip().lower()
    return index.get(key) or index.get(Path(key).name)


def embed_closure(path: Path, index: dict[str, Path]) -> tuple[list[Path], list[str]]:
    """Every note reachable from `path` by embeds, plus unresolved embed targets.

    Recursive because an embed of an embed still lands in the exported bytes.
    """
    seen: set[Path] = set()
    unresolved: list[str] = []
    frontier = [(path, 0)]
    while frontier:
        current, depth = frontier.pop()
        if depth >= MAX_EMBED_DEPTH:
            continue
        try:
            text = current.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for target in _EMBED_RE.findall(text):
            if _ATTACHMENT_EXT.search(target):
                unresolved.append(f"{target} (attachment — cannot be classified)")
                continue
            resolved = resolve_link(target, index)
            if resolved is None:
                unresolved.append(f"{target} (unresolved)")
            elif resolved not in seen and resolved != path:
                seen.add(resolved)
                frontier.append((resolved, depth + 1))
    return sorted(seen), unresolved


def evaluate(paths: list[Path], ceiling: str,
             unclassified_as: str | None = None) -> list[dict]:
    """Judge each note and everything it transcludes against the ceiling."""
    index = _index_vault()
    limit = TIER_RANK[ceiling]
    results: list[dict] = []

    for path in paths:
        tier = note_tier(path) or unclassified_as
        embedded, unresolved = embed_closure(path, index)

        reasons: list[str] = []
        worst = tier
        if tier is None:
            reasons.append("unclassified — no usable `classification` value")
        elif TIER_RANK[tier] > limit:
            reasons.append(f"note is `{tier}`, above the `{ceiling}` ceiling")

        for dep in embedded:
            dep_tier = note_tier(dep) or unclassified_as
            rel = dep.relative_to(VAULT_ROOT)
            if dep_tier is None:
                reasons.append(f"embeds unclassified note `{rel}`")
                worst = None if worst is None else worst
            elif TIER_RANK[dep_tier] > limit:
                reasons.append(f"embeds `{dep_tier}` note `{rel}`")
                if worst is not None and TIER_RANK[dep_tier] > TIER_RANK[worst]:
                    worst = dep_tier

        results.append({
            "path": path,
            "tier": tier,
            "effective": worst,
            "embedded": embedded,
            "unresolved": unresolved,
            "links": sorted({t for t in _LINK_RE.findall(
                path.read_text(encoding="utf-8", errors="replace"))}),
            "reasons": reasons,
            "blocked": bool(reasons),
            "restricted": (tier == NEVER_EXPORTABLE
                           or any(note_tier(d) == NEVER_EXPORTABLE for d in embedded)),
        })
    return results


def audit(action: str, audience: str, results: list[dict],
          override: str | None) -> None:
    """Record the decision. An export gate that leaves no trace is a suggestion."""
    try:
        import security_common
        security_common.append_alert({
            "control": "disclosure-check",
            "action": action,
            "audience": audience,
            "override": override or "",
            "cleared": [str(r["path"].relative_to(VAULT_ROOT))
                        for r in results if not r["blocked"]],
            "blocked": {str(r["path"].relative_to(VAULT_ROOT)): r["reasons"]
                        for r in results if r["blocked"]},
        })
    except Exception as exc:
        print(f"  Warning: audit record not written: {exc}", file=sys.stderr)


def report(results: list[dict], ceiling: str, audience: str) -> None:
    for r in results:
        try:
            name = r["path"].relative_to(VAULT_ROOT)
        except ValueError:
            name = r["path"]
        if r["blocked"]:
            print(f"  BLOCKED  {name}")
            for reason in r["reasons"]:
                print(f"           - {reason}")
        else:
            print(f"  clear    {name}  [{r['tier']}]")
        if r["embedded"]:
            print(f"           transcludes {len(r['embedded'])} note(s): "
                  + ", ".join(str(d.relative_to(VAULT_ROOT)) for d in r["embedded"][:4])
                  + (" ..." if len(r["embedded"]) > 4 else ""))
        for u in r["unresolved"]:
            print(f"           ? embed {u}")


def gather(raw: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in raw:
        p = Path(item)
        if not p.is_absolute():
            p = VAULT_ROOT / p
        if p.is_dir():
            out.extend(sorted(p.rglob("*.md")))
        elif p.is_file():
            out.append(p.resolve())
        else:
            print(f"Error: no such path: {item}", file=sys.stderr)
            sys.exit(2)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refuse to export vault content above an audience's ceiling.")
    parser.add_argument("mode", choices=["check", "export"])
    parser.add_argument("paths", nargs="+", help="Notes or directories")
    parser.add_argument("--audience", choices=sorted(AUDIENCES), required=True)
    parser.add_argument("--treat-unclassified", choices=TIERS, metavar="TIER",
                        help="Tier to assume for a note carrying no "
                             "`classification` value. Omitted, an unclassified "
                             "note BLOCKS — an unlabeled note is an unreviewed "
                             "one. Set this only where something other than the "
                             "label already establishes the tier: repo "
                             "documentation in an already-public checkout, for "
                             "instance. Never set it against the live vault.")
    parser.add_argument("--vault", type=str,
                        help="Override the vault root. Needed when gating a "
                             "checkout or staging copy rather than the live vault.")
    parser.add_argument("--to", type=str, help="Destination directory (export mode)")
    parser.add_argument("--skip-blocked", action="store_true",
                        help="Export what is allowed instead of refusing entirely; "
                             "withheld files are listed in WITHHELD.md")
    parser.add_argument("--override", type=str, metavar="REASON",
                        help="Proceed despite the ceiling. Logged. Never lifts the "
                             "bar on `restricted`.")
    args = parser.parse_args()

    global VAULT_ROOT
    if args.vault:
        VAULT_ROOT = Path(args.vault).expanduser().resolve()

    ceiling = AUDIENCES[args.audience]
    paths = gather(args.paths)
    if not paths:
        print("No notes matched.", file=sys.stderr)
        return 2

    print(f"Disclosure check · audience `{args.audience}` "
          f"· ceiling `{ceiling}` · {len(paths)} note(s)\n")
    if args.treat_unclassified:
        print(f"  (unclassified notes are being treated as "
              f"`{args.treat_unclassified}`)\n")
    results = evaluate(paths, ceiling, args.treat_unclassified)
    report(results, ceiling, args.audience)

    blocked = [r for r in results if r["blocked"]]
    restricted = [r for r in results if r["restricted"]]

    if args.override and restricted:
        print("\nREFUSED: --override does not apply to `restricted` content.")
        print("  Move the specific material out of the note instead of exporting it.")
        audit(args.mode + ":refused-restricted", args.audience, results, args.override)
        return 1

    overridden = []
    if args.override and blocked:
        print(f"\nOVERRIDE IN EFFECT — reason: {args.override}")
        print("  Recorded to the security audit log.")
        overridden, blocked = blocked, []

    print()
    if blocked and not (args.mode == "export" and args.skip_blocked):
        print(f"BLOCKED — {len(blocked)} of {len(results)} note(s) exceed the "
              f"`{ceiling}` ceiling. Nothing was exported.")
        audit(args.mode + ":blocked", args.audience, results, args.override)
        return 1

    if args.mode == "check":
        if overridden:
            # Never report an overridden export as clean — the whole value of
            # the audit trail is that the exception stays visible.
            print(f"PROCEEDING UNDER OVERRIDE — {len(overridden)} note(s) exceed "
                  f"`{ceiling}` and were allowed anyway.")
            audit("check:overridden", args.audience, results, args.override)
        else:
            print(f"CLEAR — all {len(results)} note(s) are within `{ceiling}`.")
            audit("check:clear", args.audience, results, args.override)
        return 0

    if not args.to:
        print("Error: export mode requires --to DIR", file=sys.stderr)
        return 2

    dest = Path(args.to).expanduser().resolve()
    if dest.is_relative_to(VAULT_ROOT):
        # Exporting into the vault would re-import the copy on the next pass and
        # give the same content a second, divergent classification.
        print(f"Error: --to must be outside the vault ({dest})", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)

    # Overridden notes export — loudly. The earlier banner announced the
    # override; silently withholding those exact files anyway (the pre-fix
    # behavior) made the tool lie about what it did. The exception stays
    # visible: reported here, flagged in the audit record, absent from
    # WITHHELD.md because it genuinely shipped.
    overridden_paths = {r["path"] for r in overridden}
    allowed = [r for r in results
               if not r["blocked"] or r["path"] in overridden_paths]
    if overridden:
        print(f"PROCEEDING UNDER OVERRIDE — exporting {len(overridden)} "
              f"note(s) that exceed `{ceiling}`.")
    for r in allowed:
        rel = r["path"].relative_to(VAULT_ROOT)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(r["path"], target)

    withheld = [r for r in results
                if r["blocked"] and r["path"] not in overridden_paths]
    if withheld:
        lines = ["# Withheld from this export", "",
                 f"Audience `{args.audience}` · ceiling `{ceiling}`", ""]
        for r in withheld:
            lines.append(f"- `{r['path'].relative_to(VAULT_ROOT)}`")
            lines += [f"    - {reason}" for reason in r["reasons"]]
        (dest / "WITHHELD.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Exported {len(allowed)} note(s) to {dest}")
    if withheld:
        print(f"Withheld {len(withheld)} — see {dest / 'WITHHELD.md'}")
    audit("export:done", args.audience, results, args.override)
    return 0


if __name__ == "__main__":
    sys.exit(main())
