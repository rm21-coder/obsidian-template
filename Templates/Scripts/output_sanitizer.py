"""
output_sanitizer.py -- make outside text inert before it is filed as a note.

Obsidian acts on some markdown the moment a note is opened, with no click:
a remote image or an HTML tag loads its URL (and a URL can carry data out),
and a Mermaid diagram can load an image or a stylesheet. A link with a
scheme other than http(s) -- obsidian:, file:, ms-word:, javascript:, a
plugin's own handler -- acts on a single click. sanitize_report() defuses
all of these, everywhere in the text, code included, by inserting a
zero-width space the renderer does not show:

  "!" + "["                           -> an image no longer loads
      (including "![[": CommonMark lets an image description hold a link,
      so "![[x](u)](u2)" is a remote image)
  "<" + letter, "/", "!" or "?"       -> no HTML tag can open
  a scheme other than http(s) + ":"   -> no link, autolink or reference
      definition can carry it; entity and backslash spellings of the colon
      are caught, and so are schemes spelled with entities or escapes
      ("obsidia&#110;://", "obsidian\\-dashboard://")
  a fence whose language is mermaid   -> the block shows as plain code
      (and themeCSS / @import inside a %%{init}%% directive are broken too)

It does not parse markdown. Two review rounds broke a version that tried to
(where code spans end, which brackets open a link): an unmatched backtick
shielded a remote image, a blockquoted reference definition hid an ms-word:
link. Text inside http(s) URLs is left alone so citations keep working.

Run templater_guard.neutralize() after it, as for any outside text: this
module handles what the renderer loads, that one what plugins run.

The cost: model tags and ports such as "llama3.1:8b" are left alone, but
mailto: links and <https://...> autolinks stop working, and local embeds
("![[x.png]]") are defused too -- a caller that writes its own embeds
restores exactly those (sanitize_keeping_embeds). Since the F11 fix
(2026-11-12), a scheme glued to a web URL is defused even when it is
harmless ("git+https://", "svn+https://", "view-source:https://"), a web URL
stops at "|" (so a "|" inside one, as in some font URLs, leaves the rest as
text that may be defused), and a URL glued to the word before it
("e.g.https://...") is not treated as a URL. Each costs an invisible
character in the copy, never a live link (review B6). In converted text,
defuse_task_tags breaks "#task" wherever it appears, so a URL anchor
("...#task-list"), a heading link ("[[Plan#Tasks]]") or CSS ("#taskbar")
gets the same invisible character (review C5).

Standard library only; Python 3.9 compatible (Apple's /usr/bin/python3
runs some of the callers).
"""
from __future__ import annotations

import bisect
import html
import re

ZWSP = "\u200b"
_ZWSP = ZWSP

_IMAGE_RE = re.compile(r"!(?=\[)")
# "<" before "[[" too (F16): a tool that later unlinks the wikilink (vault_lint
# --fix-links does) would otherwise turn "<[[x|img src=y]]>" into a live tag.
_HTML_TAG_RE = re.compile(r"<(?=[A-Za-z/!?]|\[\[)")
# A web URL starts only where no scheme character precedes it, so the
# "https://" inside "x-safari-https://" is not one, and stops at "|", a table
# cell separator (F11).
_WEB_URL_RE = re.compile(r"(?<![A-Za-z0-9+.\-])https?://[^\s<>\"'`()\[\]|]*", re.I)
_COLON = r"(?::|&#0*58;?|&#[xX]0*3[aA];?|&colon;|\\:)"
_ASCII_SPACE = " \t\r\n\f\v"
_SCHEME_COLON_RE = re.compile(
    # The colon must be followed by something a URI can continue with:
    # anything but ASCII whitespace (CommonMark allows NBSP and other
    # Unicode spaces inside a destination), and not a run of "*" that then
    # ends, which is a bold label such as "**Microsoft:**".
    r"(?<![A-Za-z0-9+.\-])([A-Za-z][A-Za-z0-9+.\-]*)(?=" + _COLON +
    r"(?!\*+(?:[" + _ASCII_SPACE + r"]|$))([^" + _ASCII_SPACE + r"]))")
# A colon followed by a digit is a model tag, a port or a time
# ("llama3.1:8b", "localhost:8080") far more often than a link. It is left
# alone, except for these schemes (and every ms-* Office scheme), which are
# defused whatever follows. tel:, sms:, facetime: and callto: are here
# because a digit is exactly what follows them (MDASH weekly:388); skype:,
# sip: and sips: for the same reason (review R10, 2026-10-26).
_ALWAYS_DEFUSE = ("javascript", "vbscript", "data", "file", "obsidian",
                  "tel", "sms", "facetime", "facetime-audio", "callto",
                  "skype", "sip", "sips")

# An entity or a backslash escape, as CommonMark decodes them inside a link
# destination. html.unescape decides what an "&..." token means; one it
# leaves alone is literal text. (F-20261020 5268416)
_DECODE_TOKEN_RE = re.compile(
    r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]*;?)"
    r"|\\[!-/:-@\[-`{-~]")

# Fenced code: the same matcher and language rule templater_guard uses
# (_FENCE, _info_language), copied so this module stands alone.
_FENCE_RE = re.compile(r"(`{3,}|~{3,})([^\r\n`~]*(?:[`~]{1,2}[^\r\n`~]+)*)")
_BACKSLASH_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_TRIM = " \t\n\r\f\v\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006" \
        "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_CLASS_SPLIT = re.compile(r"[\n\r\f]")
# A Mermaid directive ("%%{init: ...}%%"), to its close or the end of the
# text, and what in one can load a stylesheet. themeCSS is also a key of a
# diagram's YAML config block, so it is broken wherever it appears.
_DIRECTIVE_RE = re.compile(r"%%\{.*?(?:\}%%|\Z)", re.S)
_IMPORT_RE = re.compile(r"@(?=import)", re.I)
_THEMECSS_RE = re.compile(r"(theme)(?=css)", re.I)


def _web_spans(text: str) -> tuple:
    """The web URLs in `text`, as sorted, non-overlapping (start, end) pairs,
    and their starts for bisect."""
    spans = [(m.start(), m.end()) for m in _WEB_URL_RE.finditer(text)]
    return spans, [a for a, _b in spans]


def _inside(pos: int, spans: list, starts: list) -> bool:
    """Whether `pos` falls inside a web URL. A bisect, not a walk over every
    URL: the walk made a text with many URLs and many scheme-shaped tokens
    cost their product (review B1, 200 KB in 25 s)."""
    i = bisect.bisect_right(starts, pos) - 1
    return i >= 0 and pos < spans[i][1]


def _scheme_is_live(name: str, nxt: str) -> bool:
    name = name.lower()
    if name in ("http", "https"):
        return False
    if nxt in "0123456789" and name not in _ALWAYS_DEFUSE and \
            not name.startswith("ms-"):
        return False
    return True


def _decode_with_ends(text: str) -> tuple:
    """`text` with entities and backslash escapes decoded, and for each
    decoded character the offset in `text` just past what produced it."""
    out, ends, pos = [], [], 0
    for m in _DECODE_TOKEN_RE.finditer(text):
        for i in range(pos, m.start()):
            out.append(text[i])
            ends.append(i + 1)
        tok = m.group(0)
        rep = tok[1] if tok[0] == "\\" else html.unescape(tok)
        if rep == tok:
            for i in range(m.start(), m.end()):
                out.append(text[i])
                ends.append(i + 1)
        else:
            for ch in rep:
                out.append(ch)
                ends.append(m.end())
        pos = m.end()
    for i in range(pos, len(text)):
        out.append(text[i])
        ends.append(i + 1)
    return "".join(out), ends


def _defuse_encoded(segment: str, counts: dict) -> str:
    """Schemes spelled with entities or escapes ("obsidia&#110;://",
    "&#111;bsidian:", "obsidian\\-x://"): match on the decoded text, put the
    space in the original just after the scheme's last character."""
    if "&" not in segment and "\\" not in segment:
        return segment
    decoded, ends = _decode_with_ends(segment)
    spans, starts = _web_spans(decoded)
    cuts = []
    for m in _SCHEME_COLON_RE.finditer(decoded):
        if _inside(m.start(), spans, starts):
            continue
        if _scheme_is_live(m.group(1), m.group(2)):
            cuts.append(ends[m.end(1) - 1])
    # One join, not a slice per cut: a slice per cut is quadratic over the
    # whole text (review B1).
    out, prev = [], 0
    for at in sorted(set(cuts)):
        counts["schemes"] += 1
        out.append(segment[prev:at])
        out.append(ZWSP)
        prev = at
    out.append(segment[prev:])
    return "".join(out)


def _defuse_schemes(text: str, counts: dict) -> str:
    """Every live non-web scheme gets a zero-width space after its name.

    Matched over the whole text, and skipped only when the scheme starts
    inside a web URL (so citations keep working). The text used to be cut
    at web URLs first, which ended "ms-word:" at its colon, so the
    character after it, the "h" of "https://", was never seen and
    "ms-word:https://..." stayed live (F11)."""
    spans, starts = _web_spans(text)

    def scheme(m: re.Match) -> str:
        if _inside(m.start(), spans, starts):
            return m.group(0)
        if not _scheme_is_live(m.group(1), m.group(2)):
            return m.group(0)
        counts["schemes"] += 1
        return m.group(1) + ZWSP

    return _defuse_encoded(_SCHEME_COLON_RE.sub(scheme, text), counts)


def _defuse_diagrams(text: str, counts: dict) -> str:
    """Mermaid fences shown as plain code, the same way templater_guard
    defuses plugin blocks: a space between the fence and the language,
    matched after decoding, any case, on any line. (F-20261020 5268556)"""
    def fence(m: re.Match) -> str:
        info = m.group(2)
        decoded = html.unescape(_BACKSLASH_ESCAPE.sub(r"\1", info))
        if _CLASS_SPLIT.search(decoded):
            # "x&#10;language-mermaid" adds a second class: stop the entity
            # decoding and the form feed, as templater_guard does.
            counts["diagrams"] += 1
            info = info.replace("&", "&amp;").replace("\f", " ")
            return m.group(1) + ZWSP + info
        if info.startswith(ZWSP):
            return m.group(0)
        if decoded.strip(_TRIM).strip().lower().startswith("mermaid"):
            counts["diagrams"] += 1
            return m.group(1) + ZWSP + info
        return m.group(0)

    def directive(m: re.Match) -> str:
        d = _IMPORT_RE.sub("@" + ZWSP, m.group(0))
        if d != m.group(0):
            counts["diagrams"] += 1
        return d

    def themecss(m: re.Match) -> str:
        counts["diagrams"] += 1
        return m.group(1) + ZWSP

    text = _FENCE_RE.sub(fence, text)
    text = _DIRECTIVE_RE.sub(directive, text)
    return _THEMECSS_RE.sub(themecss, text)


def sanitize_report(text: str) -> tuple[str, dict]:
    """The text with nothing in it that loads or acts when the note is
    opened: every markdown image opener, every HTML tag opener, every
    non-http(s) scheme and every Mermaid block made inert by an invisible
    character, everywhere, code included. Links are otherwise kept whole.
    Returns the text and what changed."""
    counts = {"images": 0, "html": 0, "schemes": 0, "diagrams": 0}

    def image(m: re.Match) -> str:
        counts["images"] += 1
        return "!" + ZWSP

    def tag(m: re.Match) -> str:
        counts["html"] += 1
        return "<" + ZWSP

    text = _defuse_schemes(text, counts)
    text = _IMAGE_RE.sub(image, text)
    text = _HTML_TAG_RE.sub(tag, text)
    return _defuse_diagrams(text, counts), counts


def sanitize_keeping_embeds(text: str, names) -> tuple[str, dict]:
    """sanitize_report(), then the local embeds the caller itself wrote
    ("![[<name>]]", one per name, exact) put back. For converters that
    extract a document's images into the vault: those embeds are the
    caller's, everything else in the text is the document's."""
    text, counts = sanitize_report(text)
    for name in sorted(set(names)):
        # Not when "(" or "[" follows: "![[name]](https://...)" is a
        # CommonMark remote image whose description happens to be "[name]".
        embed = re.compile(re.escape("!" + ZWSP + "[[" + name + "]]") + r"(?![(\[])")
        text, n = embed.subn(lambda _m: "![[" + name + "]]", text)
        counts["images"] -= n
    return text, counts


# ---- converted documents: tasks and note names -------------------------------

# Wherever "#task" appears, not only as a whole word: the Tasks plugin's
# global filter is a plain string, and if it matches by substring, "(#task)",
# "x#task" or "#tasks" would still count (review B3).
_TASK_TAG_RE = re.compile(r"#(?=task)", re.I)


def defuse_task_tags(text: str) -> tuple[str, int]:
    """`text` with every "#task" broken by a zero-width space, wherever it
    appears, and how many. The Tasks plugin (globalFilter "#task") and the
    dashboard count a checkbox line as the user's own task only if it
    carries that string, so a document from outside could otherwise plant
    one (F27). The line still reads the same."""
    return _TASK_TAG_RE.subn("#" + ZWSP, text)


# Characters a note name must not carry from an outside file name into any
# page that lists vault paths (F15): markdown and HTML syntax, the wikilink
# separators, and path or control characters.
_STEM_UNSAFE_RE = re.compile(r"[`\[\]()<>&!|#^%{}*\\/:\"?~$=;\x00-\x1f\x7f]+")


# Windows device names, with or without an extension (review B7), the
# superscript digits Windows also reserves included (review C5).
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    "%s%s" % (p, n) for p in ("com", "lpt")
    for n in list("123456789") + ["\u00b9", "\u00b2", "\u00b3"]}


def avoid_device_name(name: str, max_len: int = 120) -> str:
    """`name` unless its part before the first dot is a Windows device
    name; then "-document" goes after that part (no characters the name
    filters remove), and the result stays within max_len."""
    base, dot, rest = name.partition(".")
    if base.strip().lower() not in _WINDOWS_RESERVED:
        return name
    out = base.rstrip() + "-document" + dot + rest
    return out[:max_len].rstrip(" .")


def safe_note_stem(stem: str, fallback: str = "converted") -> str:
    """A note name built from an outside file name: unsafe characters become
    spaces, runs of space collapse, and no leading dot (a hidden note that
    is still tagged and indexed, F37), and no Windows device name. Never
    empty; at most 120 characters."""
    t = _STEM_UNSAFE_RE.sub(" ", str(stem))
    t = "".join(ch for ch in t if ch.isprintable())
    t = " ".join(t.split()).lstrip(". -")
    t = t[:120].rstrip(" .")
    # Before the first dot: Windows reserves "prn.txt" as well as "prn".
    t = avoid_device_name(t)
    return t or fallback

