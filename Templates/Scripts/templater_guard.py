#!/usr/bin/env python3
"""
templater_guard.py -- stop text from outside the vault running as Templater code.

Templater runs commands in two places this workflow cannot switch off:

  * dynamic commands -- <%+ ... %>, and <%*+ ... %> as JavaScript -- in the
    rendered text of EVERY note shown in reading view, from a post-processor
    no setting or folder limits;
  * every command in a new note, when "Trigger Templater on new file
    creation" is on (off by default in this template since 2026-10-01).

The scripts here write other people's words into notes: invite subjects and
attendee names, clipped pages, video and podcast transcripts, converted
documents. Any of them can carry "<%". neutralize() puts a zero-width space
between the "<" and the "%", so the text still reads the same and Templater no
longer sees a command.

The rendered text is what the dynamic post-processor reads, so encoded forms
count too: "&lt;%", "&#60;%", "<&#37;", and a backslash-escaped "\\<" or "\\%" all
render as "<%". A comment or tag between the two characters already separates them
into different DOM text nodes, which Templater does not join.

Templater is retired (2026-10-03), but an install that still loads it keeps
the protection, and other plugins run code from note text too. neutralize()
also defuses those triggers in outside text:

  * fenced blocks a plugin renders -- ```tasks (its "filter/sort/group by
    function" lines are JavaScript, and Tasks has no switch for them),
    ```dataview / ```dataviewjs, every ```meta-bind-*, ```mdm (Metadata Menu),
    ```excalidraw-script-install (a button that downloads a script) and
    Obsidian's own ```base (Bases formulas can build an image URL from note
    properties). A zero-width space goes between the fence and its language,
    so the block shows as plain code. The language is matched after decoding
    entities and backslash escapes, ignoring case and leading whitespace (JS
    trim() also drops U+FEFF), anywhere on a line: list items and quotes hold
    fences too;
  * a language that smuggles in a second CSS class -- reading view gives a
    block the class "language-<info up to the first space or tab>", and the
    DOM splits a class attribute on LF, FF and CR too, so "x\flanguage-tasks"
    or "x&#10;language-tasks" is found as a tasks block. Such an info string
    has its form feeds turned into spaces and its "&" escaped;
  * inline code a plugin reads -- `= ...` and `$= ...` (Dataview queries;
    plain `=` is on by default and its results render as markdown, a remote
    image URL included) and `INPUT[`, `VIEW[`, `BUTTON[` (Meta Bind). Both
    plugins trim the code's text, so the zero-width space goes straight after
    the opening backticks;
  * Dataview queries inside code blocks -- Dataview runs `=` / `$=` found in a
    code block too (its default), so a line starting with one, after quote
    markers and indentation, gets the space; a line of only "=" is a heading
    underline and is left alone;
  * raw HTML <code> tags, which those plugins read the same way and whose text
    is entity-decoded and can be split across child tags: "<code" becomes
    literal text;
  * the frontmatter keys that make Excalidraw open a note as a drawing and
    offer to run its onload script.

A zero-width space survives both JS trim() and Python strip(). The cost of
over-matching is real but bounded: the space lands inside someone else's code
too, so an Excel formula like `=SUM(A1:A9)` or an HTML sample with <code>,
copied out of a clipped page, no longer pastes cleanly.

Use it on text from outside, never on a person's own note: a template or note
that deliberately uses these plugins must keep working.
"""
from __future__ import annotations

import html
import re

ZWSP = "\u200b"

_LT = r"(?:<|&lt;?|&#0*60;?|&#x0*3c;?)"
_PCT = r"(?:\\?%|&percnt;?|&#0*37;?|&#x0*25;?)"   # "\%" is a markdown escape
_OPENER = re.compile(rf"({_LT})({_PCT})", re.I)


# Code block languages that installed plugins, or Obsidian itself (Bases),
# render. Prefixes, lower case.
PLUGIN_BLOCKS = ("tasks", "dataview", "meta-bind", "mdm", "excalidraw", "base")
# The language runs to the line end or the next fence-like run, so a second
# run on the same line is checked too.
_FENCE = re.compile(r"(`{3,}|~{3,})([^\r\n`~]*(?:[`~]{1,2}[^\r\n`~]+)*)")
_BACKSLASH_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
# What JS trim() removes and str.strip() may not.
_TRIM = " \t\n\r\f\v\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006" \
        "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
# Obsidian's reading view gives a fenced block the class "language-<info up to
# the first space or tab>", and the DOM splits a class attribute on LF, FF and
# CR as well -- so "x\flanguage-tasks" or "x&#10;language-tasks" adds a second
# class, "language-tasks", which is what plugins look up.
_CLASS_SPLIT = re.compile(r"[\n\r\f]")
_INLINE = re.compile(r"(`+)(?=[\s\ufeff]*(?:\$?=|(?:INPUT|VIEW|BUTTON)\[))", re.I)
# Dataview also runs `=` queries found inside code blocks (its default), on the
# block's text after JS trim(). A line that starts, after quote markers and
# anything trim() removes, with "=" or "$=" gets the space. The one exception
# is a heading underline: a line of only "=" under a line of text that is not
# a fence -- inside a code block a bare "=" line can start a query that
# continues on the next line, and a code block cannot follow text directly.
_JS_SPACE = "\t\v\f \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
# A line starts after LF or a lone CR: Obsidian turns every CR into a line
# break before parsing.
_BLOCK_QUERY = re.compile(rf"(?:^|(?<=[\n\r]))([>{_JS_SPACE}]*)(?=\$?=)")
_SETEXT = re.compile(r"=+[ \t]*")
_FENCE_LINE = re.compile(r"`{3,}|~{3,}")
_HTML_CODE = re.compile(r"<(?=code(?![\w-]))", re.I)
# Excalidraw opens a note as a drawing, and offers to run its onload script,
# from frontmatter keys starting "excalidraw-". A YAML double-quoted key can
# spell them with escapes ("\x65xcalidraw-plugin"), in block style, in
# a flow mapping or after "? ", so any double-quoted key holding a backslash
# is defused too. Values are left alone: titles with escaped quotes are common.
_EXCALIDRAW_KEY = re.compile(r"(excalidraw)(?=-|\\)", re.I)
_QUOTED = r'(?:[^"\\\r\n]|\\.)*' 
_ESCAPED_KEY = re.compile(
    rf'(")(?!\u200b)(?={_QUOTED}\\{_QUOTED}"[ \t]*:)'                # "k\x": v, {"k\x": v}
    rf'|(?:^|(?<=[\n\r]))([ \t]*\?[ \t]*")(?!\u200b)(?={_QUOTED}\\)')  # ? "k\x"


def _block_query(m: re.Match) -> str:
    text = m.string
    line = re.split(r"[\r\n]", text[m.end():], maxsplit=1)[0]
    if _SETEXT.fullmatch(line) and m.start() > 0:
        before = text[:m.start()]
        before = before[:-2] if before.endswith("\r\n") else before[:-1]
        prev = re.split(r"[\r\n]", before)[-1]
        if prev.strip(_TRIM + ">") and not _FENCE_LINE.search(prev):
            return m.group(0)
    return m.group(1) + ZWSP


def _decoded(info: str) -> str:
    return html.unescape(_BACKSLASH_ESCAPE.sub(r"\1", info))


def _info_language(info: str) -> str:
    return _decoded(info).strip(_TRIM).strip().lower()


def _fence(m: re.Match) -> str:
    fence, info = m.group(1), m.group(2)
    if _CLASS_SPLIT.search(_decoded(info)):
        # Disarm the split itself: entities stop decoding, a raw form feed
        # becomes a space (which ends the language).
        info = info.replace("&", "&amp;").replace("\f", " ")
        return fence + ZWSP + info
    if _info_language(info).startswith(PLUGIN_BLOCKS):
        return fence + ZWSP + info
    return m.group(0)


def neutralize(text: str) -> str:
    """`text` with every Templater command opener, in any rendered form,
    split by a zero-width space, and every plugin code trigger (see the module
    docstring) defused the same way. Idempotent."""
    if not text:
        return text
    text = _OPENER.sub(lambda m: m.group(1) + ZWSP + m.group(2), text)
    text = _FENCE.sub(_fence, text)
    text = _INLINE.sub(lambda m: m.group(1) + ZWSP, text)
    text = _BLOCK_QUERY.sub(_block_query, text)
    text = _EXCALIDRAW_KEY.sub(lambda m: m.group(1) + ZWSP, text)
    text = _ESCAPED_KEY.sub(lambda m: (m.group(1) or m.group(2)) + ZWSP, text)
    return _HTML_CODE.sub("<" + ZWSP, text)


def is_neutral(text: str) -> bool:
    """True when neutralize() would change nothing."""
    return neutralize(text or "") == (text or "")
