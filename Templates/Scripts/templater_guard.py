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
    ```dataview / ```dataviewjs, every ```meta-bind-*, ```mdm (Metadata Menu)
    and ```excalidraw-script-install (a button that downloads a script). A
    zero-width space goes between the fence and its language, so the block
    shows as plain code. The language is matched after decoding entities and
    backslash escapes, ignoring case and leading whitespace (JS trim() also
    drops U+FEFF), anywhere on a line: list items and quotes hold fences too;
  * inline code a plugin reads -- `= ...` and `$= ...` (Dataview queries;
    plain `=` is on by default and its results render as markdown, a remote
    image URL included) and `INPUT[`, `VIEW[`, `BUTTON[` (Meta Bind). Both
    plugins trim the code's text, so the zero-width space goes straight after
    the opening backticks;
  * raw HTML <code> tags, which those plugins read the same way and whose text
    is entity-decoded and can be split across child tags: "<code" becomes
    literal text.

A zero-width space survives both JS trim() and Python strip(). Over-matching
only adds an invisible character to someone else's text.

Use it on text from outside, never on a person's own note: a template or note
that deliberately uses these plugins must keep working.
"""
from __future__ import annotations

import html
import re

ZWSP = "​"

_LT = r"(?:<|&lt;?|&#0*60;?|&#x0*3c;?)"
_PCT = r"(?:\\?%|&percnt;?|&#0*37;?|&#x0*25;?)"   # "\%" is a markdown escape
_OPENER = re.compile(rf"({_LT})({_PCT})", re.I)


# Code block languages that installed plugins render (prefixes, lower case).
PLUGIN_BLOCKS = ("tasks", "dataview", "meta-bind", "mdm", "excalidraw")
# The language runs to the line end or the next fence-like run, so a second
# run on the same line is checked too.
_FENCE = re.compile(r"(`{3,}|~{3,})([^\r\n`~]*(?:[`~]{1,2}[^\r\n`~]+)*)")
_BACKSLASH_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
# What JS trim() removes and str.strip() may not.
_TRIM = " \t\n\r\f\v\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006" \
        "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_INLINE = re.compile(r"(`+)(?=[\s\ufeff]*(?:\$?=|(?:INPUT|VIEW|BUTTON)\[))", re.I)
_HTML_CODE = re.compile(r"<(?=code(?![\w-]))", re.I)


def _info_language(info: str) -> str:
    info = html.unescape(_BACKSLASH_ESCAPE.sub(r"\1", info))
    return info.strip(_TRIM).strip().lower()


def _fence(m: re.Match) -> str:
    if _info_language(m.group(2)).startswith(PLUGIN_BLOCKS):
        return m.group(1) + ZWSP + m.group(2)
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
    return _HTML_CODE.sub("<" + ZWSP, text)


def is_neutral(text: str) -> bool:
    text = text or ""
    return (_OPENER.search(text) is None
            and not any(_info_language(m.group(2)).startswith(PLUGIN_BLOCKS)
                        for m in _FENCE.finditer(text))
            and _INLINE.search(text) is None
            and _HTML_CODE.search(text) is None)
