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
import html
import json
import os
import re
import shutil
import stat
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

# Obsidian's own wiki-embed pattern is /^(!?)\[\[(.+?)]]/: a "]" inside is
# allowed, so these are too ("![[Secret|a]b]]" slipped past [^\]]).
#
# Round 3: Obsidian's matcher refuses a target that contains "[[" and
# restarts from there, so "![[Pub|the summary ![[Secret]]" embeds Secret. A
# single left-to-right match swallowed the inner embed. Matches are now taken
# at EVERY "![[" (a lookahead, so they overlap) and a target may not contain
# "[[" -- every embed Obsidian could see is seen, and a few it would not.
_WIKI_EMBED_RE = re.compile(r"(?=!\[\[((?:(?!\[\[)[^\n])+?)\]\])")
_WIKI_LINK_RE = re.compile(r"(?=(?<!!)\[\[((?:(?!\[\[)[^\n])+?)\]\])")
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_MEDIA_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|bmp|avif|heic|tiff?|pdf|mp4|mov|webm|mkv|"
    r"m4a|mp3|wav|ogg|flac|opus)$", re.I)
# Not anchored to line start: a fence inside a callout ("> ```query"), a
# blockquote or a list item still renders. Case-insensitive is stricter than
# Obsidian, which is the safe side.
_QUERY_FENCE_RE = re.compile(
    r"(`{3,}|~{3,})[ \t]*(dataview|dataviewjs|tasks|base|query|bases)\b", re.I)
_HTML_EMBED_RE = re.compile(
    r"<[^>]*\binternal-embed\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_QUOTED_RE = re.compile(r"\"([^\"\n]{1,300})\"|'([^'\n]{1,300})'")
_QUERY_TOKEN_RE = re.compile(r"\b(?:file|path)\s*:\s*(\"[^\"\n]+\"|\S+)", re.I)
_INLINE_QUERY_RE = re.compile(r"`\$?=[^`\n]+`")

# Meta Bind (obsidian-meta-bind-plugin) -- decided 2026-10-01 after two
# adversarial rounds broke both attempts to read it from the source.
#
# Its fields display and edit any note's frontmatter, render values as
# markdown, and its embed blocks render whole notes; it finds fields in the
# RENDERED text of any <code> element, so HTML comments, empty tags, emphasis
# and escapes can hide a field from any reading of the source, and it can
# build a note's name out of property values. The gate cannot model that.
#
# So the control is where Meta Bind is allowed to render at all: its own
# excludedFolders setting (watched by plugin_integrity_check). Outside that
# scope Meta Bind renders nothing and its constructs are inert. Inside it, any
# trace of Meta Bind, or any raw <code> element, makes the export
# NON-overridable: the operator cannot see everything it could render either.
# Unreadable settings, or enableJs on, put the whole vault in scope.
#
# Round 3 of the review: only Meta Bind's inline fields and its plain
# `meta-bind` block honour excludedFolders. Its embed blocks (meta-bind-embed,
# -embed-internal-1..8) and button blocks render in EVERY folder, and an embed
# renders a whole note. So any meta-bind block blocks wherever it is; only the
# inline forms depend on the scope.
MB_PLUGIN_ID = "obsidian-meta-bind-plugin"
_MB_FIELD_RE = re.compile(r"\b(?:INPUT|VIEW|BUTTON)\[")
_MB_BLOCK_RE = re.compile(r"meta-bind", re.I)
_RAW_CODE_RE = re.compile(r"<\s*code\b", re.I)
# Linear patterns only: a note can be hundreds of KB.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_MB_SETTINGS_MAX = 2 * 1024 * 1024

# Templater runs "dynamic" commands -- <%+ %>, and <%*+ %> as JavaScript -- in
# the rendered text of EVERY note shown in reading view, from a post-processor
# no setting or folder limits (review round 4). tp.file.include renders any
# other note, under a name the source need not spell out. Non-overridable
# wherever it appears. Same opening the plugin matches: <% then optional - or
# _, whitespace, optional * or ~, then +.
_TEMPLATER_DYNAMIC_RE = re.compile(r"<%[-_]?\s*[*~]?\+")
# A list item, task or not: Tasks renders task lines -- and, in tree layout,
# their child items -- under the TASK's own file path (or none), not the
# host's, so the host's scope says nothing about them.
_LIST_ITEM_RE = re.compile(r"(?m)^[ \t>]*(?:[-*+]|\d+[.)])[ \t]+.*$")


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


_TIER_CACHE: dict[Path, tuple[str | None, list[str], str | None]] = {}


def _tier_detail(path: Path) -> tuple[str | None, list[str], str | None]:
    """(tier, unrecognised declared values, read error or None). Cached for
    the duration of one evaluate() call: a query naming a folder makes every
    note in it a dependency of every note that embeds that query."""
    hit = _TIER_CACHE.get(path)
    if hit is not None:
        return hit
    _TIER_CACHE[path] = res = _tier_detail_uncached(path)
    return res


def _tier_detail_uncached(path: Path) -> tuple[str | None, list[str], str | None]:
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

    Symlinked folders are followed (Obsidian indexes them). The cycle guard
    is per branch -- a folder is not re-entered below itself -- rather than
    global, so two paths to one folder (an alias symlink) are both indexed:
    a global guard left "Alias/Secret" unresolved, and so overridable.
    Dot-folders are skipped, as Obsidian does.
    """

    MAX_FILES = 500_000

    def __init__(self, root: Path):
        self.root = root
        self.by_rel: dict[str, Path] = {}          # "folder/note" (no .md) or "folder/file.ext"
        self.by_name: dict[str, list[Path]] = {}   # "note" (no .md) or "file.ext"
        self.by_dir: dict[str, list[Path]] = {}    # "folder" -> notes anywhere below it
        self._count = 0
        self._walk(root, frozenset())

    def _walk(self, d: Path, ancestors: frozenset) -> None:
        real = os.path.realpath(d)
        if real in ancestors or self._count > self.MAX_FILES:
            return
        ancestors = ancestors | {real}
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.name.startswith("."):
                continue
            p = Path(e.path)
            try:
                if e.is_dir(follow_symlinks=True):
                    self._walk(p, ancestors)
                    continue
            except OSError:
                continue
            self._count += 1
            rel = p.relative_to(self.root).as_posix()
            rk, nk = _link_key(rel), _link_key(e.name)
            if rk.endswith(".md"):
                rk, nk = rk[:-3], nk[:-3]
                parts = rk.split("/")[:-1]
                for n in range(1, len(parts) + 1):
                    self.by_dir.setdefault("/".join(parts[:n]), []).append(p)
            self.by_rel[rk] = p
            self.by_name.setdefault(nk, []).append(p)

    def folder_notes(self, name: str) -> list[Path]:
        """Every note under a folder named by `name` (vault path or bare name)."""
        key = _link_key(name).strip("/")
        if not key:
            return []
        if key in self.by_dir:
            return self.by_dir[key]
        return [p for k, v in self.by_dir.items() if k.rsplit("/", 1)[-1] == key for p in v]

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


_MD_ESCAPABLE = re.compile(r"\\([!-/:-@\[-`{-~])")


_MAX_DEST = 4096


def _bracket_matches(text: str) -> dict[int, int]:
    """Position of every "[" -> its matching "]", in one linear pass
    (backslash escapes honoured). Scanning forward from each "![" instead was
    quadratic: 20,000 unclosed "![" took 22 s."""
    stack: list[int] = []
    match: dict[int, int] = {}
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            stack.append(i)
        elif c == "]" and stack:
            match[stack.pop()] = i
        i += 1
    return match


def _md_destinations(text: str) -> list[str]:
    """Destinations of markdown image embeds `![alt](dest "title")`.

    A small CommonMark-shaped scanner rather than one regex, because the
    forms the regex missed all render: alt text of any length and across
    lines, balanced parentheses in the destination ("Secret(1).md"), a
    parenthesised title, backslash escapes and HTML entities. Each
    destination is returned both decoded and raw; resolving both costs
    nothing and misses nothing.
    """
    out: list[str] = []
    match = _bracket_matches(text)
    i = text.find("![")
    while i != -1:
        close = match.get(i + 1)
        j = (close + 1) if close is not None else -1
        if j != -1 and text[j:j + 1] == "(":
            j += 1
            while j < len(text) and text[j] in " \t\n":
                j += 1
            raw = ""
            # Destinations are capped: a real path is well under 1 KB even
            # percent-encoded, and an unterminated one scanned to end-of-text
            # from every image made the scanner quadratic.
            lim = min(len(text), j + _MAX_DEST)
            if text[j:j + 1] == "<":
                end = text.find(">", j, lim)
                raw = text[j + 1:end] if end != -1 else ""
            else:
                k, par = j, 0
                while k < lim:
                    c = text[k]
                    if c == "\\":
                        k += 2
                        continue
                    if c in " \t\n" or (c == ")" and par == 0):
                        break
                    par += (c == "(") - (c == ")")
                    k += 1
                raw = text[j:k]
            if raw:
                decoded = urllib.parse.unquote(html.unescape(_MD_ESCAPABLE.sub(r"\1", raw)))
                out += [decoded, raw]
        i = text.find("![", i + 2)
    return out


def _query_references(text: str) -> list[str]:
    """Names a query or base mentions literally: quoted strings (Dataview
    FROM "Folder", dv.io.load("Note.md"), base filters), [[links]], and
    file:/path: search tokens. Resolved as notes AND folders, so a query that
    names a restricted note, or a folder holding one, is judged by it."""
    refs = [a or b for a, b in _QUOTED_RE.findall(text)]
    refs += [_wiki_target(m) for m in _WIKI_LINK_RE.findall(text)]
    refs += [tok.strip('"') for tok in _QUERY_TOKEN_RE.findall(text)]
    return [r.strip().strip('"').strip("'") for r in refs if r.strip()]


def _query_blocks(text: str) -> list[str]:
    """The bodies of query fences and inline queries."""
    blocks = []
    for m in _QUERY_FENCE_RE.finditer(text):
        fence = m.group(1)
        end = text.find(fence[0] * 3, m.end())
        blocks.append(text[m.end(): end if end != -1 else m.end() + 20000])
    blocks += _INLINE_QUERY_RE.findall(text)
    return blocks


def _decoded(text: str) -> str:
    """The text after the decoding CommonMark applies outside code spans --
    entities and backslash escapes, which can hide a name (meta&#45;bind,
    meta\\-bind)."""
    return _MD_ESCAPE_RE.sub(r"\1", html.unescape(text))


def _meta_bind_block(text: str) -> bool:
    """A Meta Bind block of any kind: rendered whatever the scope says."""
    return bool(_MB_BLOCK_RE.search(text) or _MB_BLOCK_RE.search(_decoded(text)))


def _meta_bind_inline(text: str) -> bool:
    """Anything that can put a Meta Bind field into a note's rendered output
    where the scope allows it: an inline field; any raw <code> element (Meta
    Bind scans the rendered text of <code>, which comments, empty tags and
    emphasis change invisibly); and any query, which renders other notes'
    text -- a Tasks query in Actions/To-Do shows task lines, code spans
    included, from any note."""
    return bool(_RAW_CODE_RE.search(text)
                or _MB_FIELD_RE.search(text) or _MB_FIELD_RE.search(_decoded(text))
                or _QUERY_FENCE_RE.search(text) or _INLINE_QUERY_RE.search(text))


_LISTDIR_CACHE: dict[Path, list[str]] = {}
_LIST_FIELD_CACHE: dict[str, list[str]] = {}


def _list_items_with_fields() -> list[str]:
    """Vault notes with a list item carrying a Meta Bind field or raw <code>:
    what a Tasks query anywhere can render where Meta Bind is active.
    Computed once per evaluate(), and only when some closure has a query."""
    if "v" not in _LIST_FIELD_CACHE:
        hits: list[str] = []
        for f in sorted(VAULT_ROOT.rglob("*.md")):
            if any(part.startswith(".") for part in f.relative_to(VAULT_ROOT).parts):
                continue
            try:
                t = _read(f)
            except (OSError, UnicodeDecodeError):
                continue
            for line in _LIST_ITEM_RE.findall(t):
                if (_RAW_CODE_RE.search(line) or _MB_FIELD_RE.search(line)
                        or _MB_FIELD_RE.search(_decoded(line))):
                    hits.append(_rel(f))
                    break
        _LIST_FIELD_CACHE["v"] = hits
    return _LIST_FIELD_CACHE["v"]


def _true_case(p: Path) -> Path:
    """`p` with each component spelled as it is on disk. APFS and NTFS are
    case-insensitive, so "templates/x.md" opens Templates/x.md; Meta Bind
    sees the real spelling, and the scope test must too."""
    try:
        rel = p.relative_to(VAULT_ROOT)
    except ValueError:
        return p
    cur = VAULT_ROOT
    for part in rel.parts:
        names = _LISTDIR_CACHE.get(cur)
        if names is None:
            try:
                names = os.listdir(cur)
            except OSError:
                return p
            _LISTDIR_CACHE[cur] = names
        if part not in names:
            key = unicodedata.normalize("NFC", part).casefold()
            hits = [n for n in names if unicodedata.normalize("NFC", n).casefold() == key]
            part = hits[0] if len(hits) == 1 else part
        cur = cur / part
    return cur


def meta_bind_scope(root: Path) -> list[str] | None:
    """Folders Meta Bind will NOT render in, or None when it may render
    anywhere (installed, but settings unreadable, or JavaScript on). An empty
    list from a vault without the plugin means "renders nowhere" -- see
    _in_meta_bind_scope."""
    plugin = root / ".obsidian" / "plugins" / MB_PLUGIN_ID
    if not plugin.is_dir():
        return ["\x00not-installed"]
    path = plugin / "data.json"
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MB_SETTINGS_MAX:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:      # absent, unreadable, deep nesting, bad encoding
        return None
    if not isinstance(data, dict) or data.get("enableJs") is not False:
        return None
    folders = data.get("excludedFolders")
    if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
        return None
    return folders


def _in_meta_bind_scope(rel: str, scope: list[str] | None) -> bool:
    """Meta Bind's own test: excluded when the file path startsWith() any
    excluded folder (meta-bind isExcludedFromRendering)."""
    if scope is None:
        return True
    if scope == ["\x00not-installed"]:
        return False
    # Excluded only when every normalisation of the path agrees: which form
    # Obsidian hands Meta Bind is not something the gate can know.
    forms = {rel, unicodedata.normalize("NFC", rel), unicodedata.normalize("NFD", rel)}
    return not any(all(r.startswith(f) for r in forms) for f in scope)


def _wiki_target(inner: str) -> str:
    # "\|" is the pipe Obsidian requires inside a table; it separates the
    # alias just like "|" does. It used to be read as part of the target,
    # which then resolved to nothing and was waved through as advisory.
    inner = inner.replace("\\|", "|")
    return re.split(r"[|#^]", inner, maxsplit=1)[0].strip()


def _is_drawing(path: Path, text: str) -> bool:
    # Anywhere in the frontmatter, not just its first 2000 characters, and
    # by the plugin's own section markers, whichever is present.
    fm = classification_tier.frontmatter(text) or ""
    return (path.name.lower().endswith(".excalidraw.md")
            or re.search(r"(?mi)^excalidraw-plugin\s*:", fm) is not None
            or "# Excalidraw Data" in text or "```compressed-json" in text)


def references(path: Path, text: str) -> tuple[list[str], list[str]]:
    """(targets whose content renders into this note, dynamic constructs --
    queries -- whose rendered content the gate cannot evaluate)."""
    targets = [_wiki_target(m) for m in _WIKI_EMBED_RE.findall(text)]
    # Raw HTML Obsidian's reading view turns into an embed. Uncertain whether
    # the sanitiser keeps src on a span; treated as an embed regardless.
    targets += [_wiki_target(m) for m in _HTML_EMBED_RE.findall(text)]
    for dest in _md_destinations(text):
        if _URL_SCHEME_RE.match(dest):
            continue                       # remote resource, not vault content
        targets.append(dest.split("#", 1)[0])
    if _is_drawing(path, text):
        if "```compressed-json" in text:
            targets.append("\x00dynamic:a compressed Excalidraw drawing, whose "
                           "embedded elements the gate cannot read")
        # A drawing renders the notes it references; Excalidraw records them
        # as plain `[[Note]]` links, so here every link is an embed.
        targets += [_wiki_target(m) for m in _WIKI_LINK_RE.findall(text)]
    for block in _query_blocks(text):
        targets += ["\x00query:" + r for r in _query_references(block)]
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


def embed_closure(path: Path, index: VaultIndex, canvases: list | None = None
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
    leaves: set[Path] = set()
    frontier: list[tuple[Path, int]] = [(path, 0)]

    def reach(target: str, source: Path, depth: int) -> None:
        if target.startswith("\x00dynamic:"):
            dynamic.append(target[len("\x00dynamic:"):])
            return
        if target.startswith("\x00query:"):
            # Named inside a query or base: judged as a leaf dependency (its
            # tier counts; it is not walked for embeds). Nothing matching is
            # normal -- query strings are mostly not note names.
            ref = target[len("\x00query:"):]
            for c in index.resolve(ref, source) + index.folder_notes(ref):
                if c.name.lower().endswith(".md") and c != path:
                    leaves.add(c)
            return
        # Resolve BEFORE deciding "media": "![[Diagram.png]]" renders the note
        # Diagram.png.md when that is what exists.
        cands = index.resolve(target, source)
        if not cands:
            if _MEDIA_EXT.search(target):
                media.append(f"{target} (media attachment — cannot be classified; not found)")
            else:
                unresolved.append(target)
            return
        for c in cands:
            if _MEDIA_EXT.search(c.name):
                media.append(f"{target} (media attachment — cannot be classified)")
                continue
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
            try:
                for r in _query_references(_read(current)):
                    reach("\x00query:" + r, current, depth)
            except (OSError, UnicodeDecodeError) as exc:
                incomplete.append(f"{current.name} (unreadable: {type(exc).__name__})")
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
    notes = sorted(p for p in seen | leaves if p.name.lower().endswith(".md"))
    if canvases is not None:
        canvases.extend(sorted(p for p in seen | {path}
                               if p.name.lower().endswith(".canvas")))
    return notes, sorted(set(unresolved)), media, dynamic, incomplete


def _rel(p: Path) -> str:
    try:
        return p.relative_to(VAULT_ROOT).as_posix()
    except ValueError:
        return str(p)


def evaluate(paths: list[Path], ceiling: str,
             unclassified_as: str | None = None) -> list[dict]:
    """Judge each note and everything it transcludes against the ceiling."""
    _TIER_CACHE.clear()
    _LISTDIR_CACHE.clear()
    _LIST_FIELD_CACHE.clear()
    index = _index_vault()
    mb_scope = meta_bind_scope(VAULT_ROOT)
    limit = TIER_RANK[ceiling]
    results: list[dict] = []

    for path in paths:
        declared, unknown, err = _tier_detail(path)
        tier = declared or (None if (unknown or err) else unclassified_as)
        canvases: list[Path] = []
        embedded, unresolved, media, dynamic, incomplete = embed_closure(path, index, canvases)
        if err:
            incomplete.insert(0, f"note itself is unreadable ({err})")
        if unknown:
            # Even beside a recognised value: YAML, PyYAML and Obsidian may
            # each read a different one (round 2 of the review).
            incomplete.insert(0, f"note declares an unrecognised tier `{unknown[0]}`")

        # Meta Bind: what it displays cannot be read from the source, so not
        # even an override may release it. Blocks render in every folder;
        # inline forms only inside its scope, and a note rendered inside an
        # in-scope host is in scope with it. Canvases are checked too: their
        # text cards render with the canvas's own path.
        host_in_scope = _in_meta_bind_scope(_rel(_true_case(path)), mb_scope)
        for n in [path, *embedded, *canvases]:
            try:
                mb_text = _read(n)
            except (OSError, UnicodeDecodeError):
                continue                    # reported as unreadable elsewhere
            if n.name.lower().endswith(".canvas"):
                try:
                    mb_text += "\n" + "\n".join(_canvas_references(n)[1])
                except ValueError:
                    pass                    # reported as unparseable elsewhere
            n_rel = _rel(_true_case(n))
            if (_TEMPLATER_DYNAMIC_RE.search(mb_text)
                    or _TEMPLATER_DYNAMIC_RE.search(_decoded(mb_text))):
                incomplete.append(f"{n_rel} contains a Templater dynamic command "
                                  "(<%+ %>), which runs when the note is shown and "
                                  "can render any other note; what it would display "
                                  "cannot be evaluated")
            if _meta_bind_block(mb_text):
                incomplete.append(f"{n_rel} contains a Meta Bind block, which "
                                  "renders other notes in every folder; what it "
                                  "would display cannot be evaluated")
            elif ((host_in_scope or _in_meta_bind_scope(n_rel, mb_scope))
                  and _meta_bind_inline(mb_text)):
                where = ("Meta Bind's settings could not be read or allow JavaScript"
                         if mb_scope is None else
                         "this folder is not in Meta Bind's excluded folders")
                incomplete.append(f"{n_rel} can show Meta Bind fields (an inline "
                                  "field, raw <code>, or a query) where Meta Bind "
                                  f"renders ({where}); what it would display "
                                  "cannot be evaluated")

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
            if du:
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
        if dynamic and mb_scope != ["\x00not-installed"]:
            carriers = _list_items_with_fields()
            if carriers:
                incomplete.append(
                    "a query here can render list items from other notes under "
                    "their own paths, and " + ", ".join(carriers[:3])
                    + (" and others" if len(carriers) > 3 else "")
                    + " carry Meta Bind fields or raw <code> in list items")
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
