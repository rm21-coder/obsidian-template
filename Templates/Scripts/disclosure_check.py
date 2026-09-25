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
import json
import os
import re
import shutil
import sys
import unicodedata
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classification_tier  # noqa: E402

VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()

TIERS = list(classification_tier.TIERS)
TIER_RANK = dict(classification_tier.TIER_RANK)

AUDIENCES = {
    "public":   "public",
    "internal": "internal-use-only",
    "cleared":  "confidential",
}

# No audience raises the ceiling this high. Kept separate from AUDIENCES so that
# adding an audience can never accidentally make restricted material exportable.
NEVER_EXPORTABLE = "restricted"

MAX_EMBED_DEPTH = 6

# ---------------------------------------------------------------------------
# FAIL CLOSED ON WHAT THE GATE CANNOT SEE (decided 2026-09-25).
#
# The first versions tried to judge exactly what Obsidian renders and treated
# anything they could not resolve as "advisory -- nothing exists to leak".
# That premise only holds if resolution is never stricter than Obsidian's, and
# the adversarial review of the M-DASH fixes showed it always was, in some
# form: `![[Note.md]]`, NFD filenames, markdown `![](Note.md)` embeds,
# relative paths, duplicate note names, `\|` in tables, symlinked folders,
# canvases, Excalidraw drawings, query blocks. Each was a restricted note
# exported under a "clear" verdict. Matching Obsidian form by form is a race
# the gate loses to the next form, so the rule is now:
#
#   * every reference that could render note content is extracted, including
#     markdown embeds, canvas nodes and every link inside a drawing;
#   * a reference that matches several notes brings ALL of them into the
#     closure -- the gate need not guess which one Obsidian would pick;
#   * a note reference that resolves to nothing BLOCKS (override allowed: the
#     operator can see the target does not exist);
#   * dynamic content -- query blocks and bases, which render other notes'
#     content or properties by query -- BLOCKS, overridable with a logged
#     reason: the operator can look at what it renders, the gate cannot;
#   * content the gate tried to evaluate and could not -- an unreadable
#     dependency, an unparseable canvas, a closure past MAX_EMBED_DEPTH, a
#     declared tier it does not recognise -- BLOCKS and cannot be overridden,
#     because a restricted note may be behind it.
#
# Binary media (images, audio, PDF) stay advisory: they carry no tier, and
# blocking them would block every note with a picture.
# ---------------------------------------------------------------------------

_WIKI_EMBED_RE = re.compile(r"!\[\[([^\]\n]+?)\]\]")
_WIKI_LINK_RE = re.compile(r"(?<!!)\[\[([^\]\n]+?)\]\]")
_MD_EMBED_RE = re.compile(r"!\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)")
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_MEDIA_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|bmp|avif|heic|tiff?|pdf|mp4|mov|webm|mkv|"
    r"m4a|mp3|wav|ogg|flac|opus)$", re.I)
_QUERY_FENCE_RE = re.compile(
    r"(?m)^[ \t]*(`{3,}|~{3,})[ \t]*(dataview|dataviewjs|tasks|base|query|bases)\b")
_INLINE_QUERY_RE = re.compile(r"`\$?=[^`\n]+`")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def note_tier(path: Path) -> str | None:
    """The note's declared tier (most restrictive if several), or None when
    absent, unrecognised or unreadable."""
    try:
        tier, _unknown = classification_tier.effective(_read(path))
    except (OSError, UnicodeDecodeError):
        return None
    return tier


def _tier_detail(path: Path) -> tuple[str | None, list[str], str | None]:
    """(tier, unrecognised declared values, read error or None)."""
    try:
        text = _read(path)
    except (OSError, UnicodeDecodeError) as exc:
        return None, [], type(exc).__name__
    tier, unknown = classification_tier.effective(text)
    return tier, unknown, None


def _link_key(text: str) -> str:
    """One spelling per note, for index keys and link targets.

    NFC because macOS filenames and typed link text can differ in Unicode
    normalization while naming the same note. Lowercase because Obsidian
    resolves links case-insensitively.
    """
    return unicodedata.normalize("NFC", text).strip().lower()


def _strip_md(key: str) -> str:
    return key[:-3] if key.endswith(".md") else key


class VaultIndex:
    """Every file in the vault, reachable by the ways a link can name it.

    Symlinked folders are followed (Obsidian indexes them), with a guard
    against cycles. Dot-folders are skipped, as Obsidian does.
    """

    def __init__(self, root: Path):
        self.root = root
        self.by_rel: dict[str, Path] = {}          # "folder/note" (no .md) or "folder/file.ext"
        self.by_name: dict[str, list[Path]] = {}   # "note" (no .md) or "file.ext"
        seen_real: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen_real:
                dirnames[:] = []
                continue
            seen_real.add(real)
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                p = Path(dirpath) / fn
                rel = p.relative_to(root).as_posix()
                rk = _link_key(rel)
                nk = _link_key(fn)
                if rk.endswith(".md"):
                    rk, nk = rk[:-3], nk[:-3]
                self.by_rel[rk] = p
                self.by_name.setdefault(nk, []).append(p)

    def resolve(self, target: str, source: Path) -> list[Path]:
        """Every file `target` could mean, seen from `source`. Empty if none.

        Deliberately generous: returning a candidate Obsidian would not pick
        costs a spurious block at worst; missing the one it would pick
        exported restricted content.
        """
        key = _link_key(target)
        if not key:
            return []
        found: list[Path] = []
        for k in dict.fromkeys([key, _strip_md(key)]):
            if "/" in k or k.startswith("."):
                try:
                    src_dir = source.parent.relative_to(self.root).as_posix()
                except ValueError:
                    src_dir = ""
                rel_to_src = _link_key(os.path.normpath(os.path.join(src_dir, k)))
                for cand in (rel_to_src, _strip_md(rel_to_src), k.lstrip("./"), _strip_md(k.lstrip("./"))):
                    if cand in self.by_rel:
                        found.append(self.by_rel[cand])
                tail = "/" + _strip_md(k).lstrip("./")
                found += [p for rk, p in self.by_rel.items() if ("/" + rk).endswith(tail)]
            else:
                found += self.by_name.get(k, [])
        return list(dict.fromkeys(found))


def _index_vault() -> VaultIndex:
    return VaultIndex(VAULT_ROOT)


def _wiki_target(inner: str) -> str:
    # "\|" is the pipe Obsidian requires inside a table; it separates the
    # alias just like "|" does. It used to be read as part of the target,
    # which then resolved to nothing and was waved through as advisory.
    inner = inner.replace("\\|", "|")
    return re.split(r"[|#^]", inner, maxsplit=1)[0].strip()


def _is_drawing(path: Path, text: str) -> bool:
    return path.name.lower().endswith(".excalidraw.md") or \
        re.search(r"(?m)^excalidraw-plugin\s*:", text[:2000]) is not None


def references(path: Path, text: str) -> tuple[list[str], list[str]]:
    """(targets whose content renders into this note, dynamic constructs --
    queries -- whose rendered content the gate cannot evaluate)."""
    targets = [_wiki_target(m) for m in _WIKI_EMBED_RE.findall(text)]
    for raw in _MD_EMBED_RE.findall(text):
        raw = raw[1:-1] if raw.startswith("<") else raw
        if _URL_SCHEME_RE.match(raw):
            continue                       # remote resource, not vault content
        targets.append(urllib.parse.unquote(raw).split("#", 1)[0])
    if _is_drawing(path, text):
        # A drawing renders the notes it references; Excalidraw records them
        # as plain `[[Note]]` links, so here every link is an embed.
        targets += [_wiki_target(m) for m in _WIKI_LINK_RE.findall(text)]
    opaque = []
    if _QUERY_FENCE_RE.search(text):
        opaque.append("contains a query block, which renders other notes' "
                      "content the gate cannot evaluate")
    if _INLINE_QUERY_RE.search(text):
        opaque.append("contains an inline query, which renders content the "
                      "gate cannot evaluate")
    return [t for t in targets if t], opaque


def _canvas_references(path: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """(file nodes as (target, source-for-resolution), embedded text refs)."""
    try:
        data = json.loads(_read(path))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"canvas could not be parsed: {type(exc).__name__}") from exc
    files: list[str] = []
    texts: list[str] = []
    for node in data.get("nodes", []) if isinstance(data, dict) else []:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "file" and isinstance(node.get("file"), str):
            files.append(node["file"])
        elif node.get("type") == "text" and isinstance(node.get("text"), str):
            texts.append(node["text"])
    return files, texts


def embed_closure(path: Path, index: VaultIndex
                  ) -> tuple[list[Path], list[str], list[str], list[str], list[str]]:
    """Everything whose content renders into `path`, recursively.

    Returns (notes reached, unresolved references, media attachments,
    dynamic: query blocks and bases -- overridable,
    incomplete: dependencies that exist or may exist but could not be judged
    -- not overridable). Recursive because an embed of an embed still lands
    in what is shown.
    """
    seen: set[Path] = set()
    unresolved: list[str] = []
    media: list[str] = []
    dynamic: list[str] = []
    incomplete: list[str] = []
    frontier: list[tuple[Path, int]] = [(path, 0)]

    def reach(target: str, source: Path, depth: int) -> None:
        if _MEDIA_EXT.search(target):
            media.append(f"{target} (media attachment — cannot be classified)")
            return
        cands = index.resolve(target, source)
        if not cands:
            unresolved.append(target)
            return
        for c in cands:
            if c in seen or c == path:
                continue
            if depth + 1 > MAX_EMBED_DEPTH:
                incomplete.append(f"{target} (beyond embed depth {MAX_EMBED_DEPTH})")
                continue
            seen.add(c)
            frontier.append((c, depth + 1))

    while frontier:
        current, depth = frontier.pop()
        suffix = current.name.lower()
        if suffix.endswith(".base"):
            dynamic.append(f"{current.name} (a base renders other notes' "
                           "properties by query; check what it shows)")
            continue
        if suffix.endswith(".canvas"):
            try:
                files, texts = _canvas_references(current)
            except ValueError as exc:
                incomplete.append(f"{current.name} ({exc})")
                continue
            for f in files:
                reach(f, index.root / "_", depth)       # canvas paths are vault-relative
            for t in texts:
                refs, opaque = references(current, t)
                dynamic += [f"{current.name}: {o}" for o in opaque]
                for r in refs:
                    reach(r, current, depth)
            continue
        if _MEDIA_EXT.search(suffix) or not suffix.endswith(".md"):
            if current != path and not suffix.endswith(".md"):
                incomplete.append(f"{current.name} (embedded file of a kind "
                                  "the gate cannot evaluate)")
            continue
        try:
            text = _read(current)
        except (OSError, UnicodeDecodeError) as exc:
            incomplete.append(f"{current.name} (unreadable: {type(exc).__name__})")
            continue
        refs, opaque = references(current, text)
        dynamic += [f"{current.name}: {o}" if current != path else o for o in opaque]
        for r in refs:
            reach(r, current, depth)
    notes = sorted(p for p in seen if p.name.lower().endswith(".md"))
    return notes, sorted(set(unresolved)), media, dynamic, incomplete


def _rel(p: Path) -> str:
    try:
        return p.relative_to(VAULT_ROOT).as_posix()
    except ValueError:
        return str(p)


def evaluate(paths: list[Path], ceiling: str,
             unclassified_as: str | None = None) -> list[dict]:
    """Judge each note and everything it transcludes against the ceiling."""
    index = _index_vault()
    limit = TIER_RANK[ceiling]
    results: list[dict] = []

    for path in paths:
        declared, unknown, err = _tier_detail(path)
        tier = declared or (None if (unknown or err) else unclassified_as)
        embedded, unresolved, media, dynamic, incomplete = embed_closure(path, index)
        if err:
            incomplete.insert(0, f"note itself is unreadable ({err})")
        if unknown and not declared:
            incomplete.insert(0, f"note declares an unrecognised tier `{unknown[0]}`")

        reasons: list[str] = []
        worst = tier
        if tier is None and not (unknown or err):
            reasons.append("unclassified — no usable `classification` value")
        elif tier is not None and TIER_RANK[tier] > limit:
            reasons.append(f"note is `{tier}`, above the `{ceiling}` ceiling")

        dep_restricted = False
        for dep in embedded:
            dt_, du, de = _tier_detail(dep)
            rel = _rel(dep)
            if de:
                incomplete.append(f"{rel} (unreadable: {de})")
                continue
            if du and not dt_:
                incomplete.append(f"{rel} declares an unrecognised tier `{du[0]}`")
                continue
            dep_tier = dt_ or unclassified_as
            if dep_tier is None:
                reasons.append(f"embeds unclassified note `{rel}`")
                continue
            dep_restricted |= dep_tier == NEVER_EXPORTABLE
            if TIER_RANK[dep_tier] > limit:
                reasons.append(f"embeds `{dep_tier}` note `{rel}`")
                if worst is not None and TIER_RANK[dep_tier] > TIER_RANK[worst]:
                    worst = dep_tier

        for u in unresolved:
            reasons.append(f"embed `{u}` does not resolve to any note in the "
                           "vault — the gate cannot confirm what it shows")
        for d in dict.fromkeys(dynamic):
            reasons.append(f"dynamic content: {d}")
        for gap in incomplete:
            reasons.append(f"could not evaluate: {gap}")

        try:
            links_text = _read(path)
        except (OSError, UnicodeDecodeError):
            links_text = ""
        results.append({
            "path": path,
            "tier": tier,
            "effective": worst,
            "embedded": embedded,
            "unresolved": unresolved,
            "media": media,
            "links": sorted({_wiki_target(t) for t in _WIKI_LINK_RE.findall(links_text)}),
            "reasons": reasons,
            "blocked": bool(reasons),
            # An incomplete closure cannot be overridden: override is for
            # "I know this is fine to share", and the gate cannot know what
            # it could not read -- a restricted note may be behind the gap.
            "restricted": (tier == NEVER_EXPORTABLE or bool(incomplete)
                           or dep_restricted),
            "dynamic": list(dict.fromkeys(dynamic)),
            "incomplete": incomplete,
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
            "cleared": [_rel(r["path"]) for r in results if not r["blocked"]],
            "blocked": {_rel(r["path"]): r["reasons"]
                        for r in results if r["blocked"]},
        })
    except Exception as exc:
        print(f"  Warning: audit record not written: {exc}", file=sys.stderr)


def report(results: list[dict], ceiling: str, audience: str) -> None:
    for r in results:
        name = _rel(r["path"])
        if r["blocked"]:
            print(f"  BLOCKED  {name}")
            for reason in r["reasons"]:
                print(f"           - {reason}")
        else:
            print(f"  clear    {name}  [{r['tier']}]")
        if r["embedded"]:
            print(f"           transcludes {len(r['embedded'])} note(s): "
                  + ", ".join(_rel(d) for d in r["embedded"][:4])
                  + (" ..." if len(r["embedded"]) > 4 else ""))
        for m in r["media"]:
            print(f"           · {m}")


def gather(raw: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in raw:
        p = Path(item)
        if not p.is_absolute():
            p = VAULT_ROOT / p
        p = _inside_vault(p, item)
        if p.is_dir():
            out.extend(_inside_vault(q, str(q)) for q in sorted(p.rglob("*.md")))
        elif p.is_file():
            out.append(p)
        else:
            print(f"Error: no such path: {item}", file=sys.stderr)
            sys.exit(2)
    return out


def _inside_vault(p: Path, item: str) -> Path:
    """`p` as a path inside the vault, or exit 2 before anything is judged.

    A note outside the vault used to be evaluated, reported CLEAR with the
    audit record silently not written, and -- for a symlink resolving outside
    -- copied before the export crashed on relative_to, leaving a partial
    export and no audit trail (M-DASH [37]). The gate governs this vault.
    """
    absolute = Path(os.path.abspath(p))
    for cand in (absolute, absolute.resolve()):
        if cand == VAULT_ROOT or cand.is_relative_to(VAULT_ROOT):
            return cand
    print(f"Error: {item} is outside the vault ({VAULT_ROOT})", file=sys.stderr)
    sys.exit(2)


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
        rel = r["path"].relative_to(VAULT_ROOT)   # gather() guarantees this
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(r["path"], target)

    withheld = [r for r in results
                if r["blocked"] and r["path"] not in overridden_paths]
    if withheld:
        lines = ["# Withheld from this export", "",
                 f"Audience `{args.audience}` · ceiling `{ceiling}`", ""]
        for r in withheld:
            lines.append(f"- `{_rel(r['path'])}`")
            lines += [f"    - {reason}" for reason in r["reasons"]]
        (dest / "WITHHELD.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Exported {len(allowed)} note(s) to {dest}")
    if withheld:
        print(f"Withheld {len(withheld)} — see {dest / 'WITHHELD.md'}")
    audit("export:done", args.audience, results, args.override)
    return 0


if __name__ == "__main__":
    sys.exit(main())
