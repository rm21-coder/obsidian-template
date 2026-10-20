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
restores exactly those (sanitize_keeping_embeds).

Standard library only; Python 3.9 compatible (Apple's /usr/bin/python3
runs some of the callers).
"""
from __future__ import annotations

import html
import re

ZWSP = "\u200b"
_ZWSP = ZWSP

_IMAGE_RE = re.compile(r"!(?=\[)")
_HTML_TAG_RE = re.compile(r"<(?=[A-Za-z/!?])")
_WEB_URL_RE = re.compile(r"https?://[^\s<>\"'`()\[\]]*", re.I)
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
# because a digit is exactly what follows them (MDASH weekly:388).
_ALWAYS_DEFUSE = ("javascript", "vbscript", "data", "file", "obsidian",
                  "tel", "sms", "facetime", "facetime-audio", "callto")

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
    web = [(m.start(), m.end()) for m in _WEB_URL_RE.finditer(decoded)]
    cuts = []
    for m in _SCHEME_COLON_RE.finditer(decoded):
        if any(a <= m.start() < b for a, b in web):
            continue
        if _scheme_is_live(m.group(1), m.group(2)):
            cuts.append(ends[m.end(1) - 1])
    for at in sorted(set(cuts), reverse=True):
        counts["schemes"] += 1
        segment = segment[:at] + ZWSP + segment[at:]
    return segment


def _defuse_schemes(text: str, counts: dict) -> str:
    def scheme(m: re.Match) -> str:
        if not _scheme_is_live(m.group(1), m.group(2)):
            return m.group(0)
        counts["schemes"] += 1
        return m.group(1) + ZWSP

    def both(segment: str) -> str:
        return _defuse_encoded(_SCHEME_COLON_RE.sub(scheme, segment), counts)

    out, pos = [], 0
    for m in _WEB_URL_RE.finditer(text):
        out.append(both(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(both(text[pos:]))
    return "".join(out)


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
