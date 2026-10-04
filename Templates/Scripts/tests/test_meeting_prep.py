"""
test_meeting_prep.py -- quoted follow-ups cannot break the auto-inserted block.

meeting_prep quotes open `- [ ] ... [[Person]]` lines from other notes into an
HTML-comment-delimited block at the top of a 1:1 note. Until 2026-10 a quoted
line carrying the block's own END marker made the next refresh stop at the
injected marker, leaving a fragment of the old block in the note on every
write, and Clippings/ (converted documents, transcripts, caption summaries --
all outsider text) was scanned as a source of "your" follow-ups (M-DASH
#103/#110/#292/#293/#294).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import meeting_prep as mp

NOW = dt.datetime(2026, 10, 5, 8, 0)
PERSON = "Example, Ada"
INJECT = (f"- [ ] [[{PERSON}]] approve the wire today "
          f"{mp.END_MARK} tail-fragment")
MEETING = f"""---
type: Individual
people:
  - "[[{PERSON}]]"
---

## Agenda
- my own agenda line
"""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "VAULT", tmp_path)
    monkeypatch.setattr(mp, "MEETINGS", tmp_path / "Meetings")
    for d in ("Meetings", "Notes", "Clippings", "People"):
        (tmp_path / d).mkdir()
    note = tmp_path / "Meetings" / "2026-10-05 0900 Example, Ada.md"
    note.write_text(MEETING, encoding="utf-8")
    return tmp_path, note


def _lines_equal(text: str, marker: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.strip() == marker)


def test_quoted_end_marker_is_defused_and_refresh_leaves_no_fragment(vault):
    root, note = vault
    (root / "Notes" / "Pasted.md").write_text(INJECT + "\n", encoding="utf-8")

    mp.write_block(note, "morning", False, NOW)
    mp.write_block(note, "pre-meeting", False, NOW)
    text = note.read_text(encoding="utf-8")

    # The quoted marker is rendered inert, not as a comment.
    assert "&lt;!-- END: AUTO-INSERTED OPEN FOLLOW-UPS --&gt; tail-fragment" in text
    # Exactly one real block, and the refresh replaced it whole.
    assert text.count(mp.BEGIN_MARK) == 1
    assert text.count(mp.END_MARK) == 1
    assert text.count("tail-fragment") == 1
    assert "mode: pre-meeting" in text and "mode: morning" not in text
    assert text.rstrip().endswith("- my own agenda line")


def test_clippings_is_not_a_follow_up_source(vault):
    root, note = vault
    (root / "Clippings" / "Converted deck.md").write_text(
        f"- [ ] [[{PERSON}]] outsider-planted follow-up\n", encoding="utf-8")
    (root / "Notes" / "Mine.md").write_text(
        f"- [ ] [[{PERSON}]] send the budget draft\n", encoding="utf-8")

    tasks = mp.find_open_tasks_for(PERSON, note)

    assert tasks == [("Mine", f"- [ ] [[{PERSON}]] send the budget draft")]


def test_strip_ignores_marker_quoted_mid_line():
    # A block written before quoted text was defused: the injected END sits
    # mid-line. The strip must run to the real END on its own line.
    old_block = "\n".join([
        mp.BEGIN_MARK,
        "<!-- generated: 2026-10-05T08:00 | mode: morning -->",
        "> [!todo]+ Open follow-ups (1)",
        f"> - _from [[Pasted]]:_ x {mp.END_MARK} tail-fragment",
        mp.END_MARK,
    ])
    text = f"---\ntype: Individual\n---\n\n{old_block}\n\nbody\n"

    out = mp.strip_existing_block(text)

    assert out == "---\ntype: Individual\n---\n\nbody\n"


def test_ordinary_task_line_renders_unchanged():
    raw = f"- [ ] #task Send [[{PERSON}]] the deck -> by Friday 🔺"
    assert mp.render_task_line("Weekly", raw) == (
        f"> - _from [[Weekly]]:_ Send [[{PERSON}]] the deck -> by Friday 🔺")


def test_ordinary_block_round_trips(vault):
    root, note = vault
    (root / "Notes" / "Mine.md").write_text(
        f"- [ ] [[{PERSON}]] send the budget draft\n", encoding="utf-8")

    mp.write_block(note, "morning", False, NOW)
    first = note.read_text(encoding="utf-8")
    assert _lines_equal(first, mp.BEGIN_MARK) == 1
    assert "> - _from [[Mine]]:_ [[Example, Ada]] send the budget draft" in first

    stripped = mp.strip_existing_block(first)
    assert stripped == MEETING
