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

Use it on text from outside, never on a person's own note: a template or note
that deliberately uses Templater must keep working.
"""
from __future__ import annotations

import re

ZWSP = "​"

_LT = r"(?:<|&lt;?|&#0*60;?|&#x0*3c;?)"
_PCT = r"(?:\\?%|&percnt;?|&#0*37;?|&#x0*25;?)"   # "\%" is a markdown escape
_OPENER = re.compile(rf"({_LT})({_PCT})", re.I)


def neutralize(text: str) -> str:
    """`text` with every Templater command opener, in any rendered form,
    split by a zero-width space. Idempotent."""
    if not text:
        return text
    return _OPENER.sub(lambda m: m.group(1) + ZWSP + m.group(2), text)


def is_neutral(text: str) -> bool:
    return _OPENER.search(text or "") is None
