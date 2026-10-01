"""
test_templater_guard.py -- text from outside the vault must not run as Templater code.

Templater runs dynamic commands (<%+ %>, <%*+ %> as JavaScript) in the rendered
text of every note shown in reading view, with no setting or folder limit, and
runs every command in a new note when its creation trigger is on. The scripts
write invite subjects, attendee names, clipped pages, transcripts and converted
documents into notes (found 2026-10-01). templater_guard.neutralize() splits
the opener with a zero-width space; these pin that it does, in every rendered
form, and that every writer of outside text applies it.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

import templater_guard as G

SCRIPTS = Path(G.__file__).resolve().parent

# What Templater's dynamic post-processor matches, applied to RENDERED text.
TEMPLATER_DYNAMIC = re.compile(r"(<%(?:-|_)?\s*[*~]{0,1})\+((?:.|\s)*?%>)")
TEMPLATER_ANY = re.compile(r"<%")


def rendered(text: str) -> str:
    """Roughly what the DOM text holds: entities decoded, markdown escapes gone."""
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", html.unescape(text))


@pytest.mark.parametrize("raw", [
    "<%+ tp.file.include('[[People/Secret]]') %>",
    "<%*+ require('child_process') %>",
    "<%- *+ x %>",
    "&lt;%+ x %>",
    "&lt%+ x %>",
    "&#60;%+ x %>",
    "&#x3C;%+ x %>",
    "<&#37;+ x %>",
    "<&percnt;+ x %>",
    "\\<%+ x %>",
    "<\\%+ x %>",
    "\\<\\%*+ x %>",
    "&LT;&#X25;+ x %>",
    "<% tp.date.now() %>",
])
def test_every_rendered_form_of_the_opener_is_split(raw: str) -> None:
    out = G.neutralize(raw)
    assert not TEMPLATER_DYNAMIC.search(rendered(out)), out
    assert not TEMPLATER_ANY.search(rendered(out)), out
    assert G.is_neutral(out)


def test_it_is_idempotent_and_leaves_ordinary_text_alone() -> None:
    text = "50% < 75% and 3 %> 2; a <b>tag</b>"
    assert G.neutralize(text) == text
    once = G.neutralize("<%+ x %>")
    assert G.neutralize(once) == once
    assert once.replace(G.ZWSP, "") == "<%+ x %>"      # reads the same


def test_the_clipping_cleaner_neutralizes_a_clipped_page(tmp_path: Path) -> None:
    import strip_ads
    clip = tmp_path / "Clip.md"
    clip.write_text("---\ntitle: x\n---\n\nA page. <%*+ require('child_process') %>\n",
                    encoding="utf-8")
    assert strip_ads.process_file(clip, dry_run=False, quiet=True, verbose=False)
    assert not TEMPLATER_ANY.search(rendered(clip.read_text(encoding="utf-8")))


def test_a_classifier_rationale_is_neutralized() -> None:
    import classify_notes
    assert G.is_neutral(classify_notes.yaml_quote("page says <%+ tp.file.include('x') %>"))


# Every place a script writes outside text into a note.
WRITERS = {
    "meeting_prepopulate.py": [
        "templater_guard.neutralize(_yaml_email(email) or '')",
        "path.write_text(templater_guard.neutralize(content), encoding='utf-8')",
    ],
    "youtube_summarize.py": ['templater_guard.neutralize(frontmatter + "\\n" + body)'],
    "podcast_transcribe.py": ['templater_guard.neutralize("\\n".join(lines) + "\\n")'],
    "voice_cleanup.py": ["templater_guard.neutralize(note_content)"],
    "markitdown_convert.py": ["templater_guard.neutralize(body)"],
    "markitdown_dropper.py": ["templater_guard.neutralize(body)"],
    "markitdown_cleanup.py": ["cleaned = templater_guard.neutralize(cleaned)"],
    "strip_ads.py": ["templater_guard.neutralize(strip_ads(original))"],
    "meeting_prep.py": ["templater_guard.neutralize(build_block(now, mode, attendee_tasks))"],
    "tag_clippings.py": ["line = templater_guard.neutralize("],
    "classify_notes.py": ["flat = templater_guard.neutralize(flat)"],
    "meeting_group_backfill.py": ["text = templater_guard.neutralize('\\n'.join(out))"],
    "obsidian-rag-sync.py": ['templater_guard.neutralize("\\n".join(lines))'],
}


@pytest.mark.parametrize("script", sorted(WRITERS))
def test_every_writer_of_outside_text_applies_the_guard(script: str) -> None:
    src = (SCRIPTS / script).read_text(encoding="utf-8")
    for needle in WRITERS[script]:
        assert needle in src, f"{script}: {needle}"


def test_meeting_prepopulate_guards_every_new_note_it_writes() -> None:
    """New meeting notes, People stubs and series roots: three writes."""
    src = (SCRIPTS / "meeting_prepopulate.py").read_text(encoding="utf-8")
    assert src.count("path.write_text(templater_guard.neutralize(content), encoding='utf-8')") == 3
