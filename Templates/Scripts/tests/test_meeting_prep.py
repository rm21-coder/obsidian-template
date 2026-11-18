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
classification: confidential
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


def _tiered(line: str, tier: str = "confidential", extra: str = "") -> str:
    return f"---\nclassification: {tier}\n{extra}---\n\n{line}\n"


def _lines_equal(text: str, marker: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.strip() == marker)


def test_quoted_end_marker_is_defused_and_refresh_leaves_no_fragment(vault):
    root, note = vault
    (root / "Notes" / "Pasted.md").write_text(_tiered(INJECT), encoding="utf-8")

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


@pytest.mark.parametrize("folder", ["Clippings", "Creations"])   # Creations: 5278118
def test_clippings_is_not_a_follow_up_source(vault, folder):
    root, note = vault
    (root / folder).mkdir(exist_ok=True)
    (root / folder / "Converted deck.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] outsider-planted follow-up"), encoding="utf-8")
    (root / "Notes" / "Mine.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] send the budget draft"), encoding="utf-8")

    tasks = mp.find_open_tasks_for(PERSON, note, "confidential")

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
        _tiered(f"- [ ] [[{PERSON}]] send the budget draft"), encoding="utf-8")

    mp.write_block(note, "morning", False, NOW)
    first = note.read_text(encoding="utf-8")
    assert _lines_equal(first, mp.BEGIN_MARK) == 1
    assert "> - _from [[Mine]]:_ [[Example, Ada]] send the budget draft" in first

    stripped = mp.strip_existing_block(first)
    assert stripped == MEETING


# --- v2.6 F19: a source is quoted only into a note of its tier or higher ---

@pytest.mark.parametrize("src_tier,quoted", [
    ("public", True), ("internal-use-only", True), ("confidential", True),
    ("restricted", False)])
def test_a_source_ranking_above_the_meeting_is_not_quoted(vault, src_tier, quoted):
    root, note = vault
    (root / "Notes" / "Src.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] SECRET-LINE", src_tier), encoding="utf-8")

    result = mp.write_block(note, "morning", False, NOW)
    text = note.read_text(encoding="utf-8")

    assert ("SECRET-LINE" in text) is quoted
    if not quoted:
        assert result == f"skip (no open follow-ups) {note.name}"
        assert text == MEETING


@pytest.mark.parametrize("header", [
    "",                                              # no frontmatter
    "---\ntitle: x\n---\n\n",                        # untiered
    "---\nclassification: secret\n---\n\n",          # unrecognised value
    "---\nclassification: [public]\n---\n\n",        # unreadable form
])
def test_an_untiered_or_unreadable_source_is_not_quoted(vault, header):
    root, note = vault
    (root / "Notes" / "Src.md").write_text(
        f"{header}- [ ] [[{PERSON}]] SECRET-LINE\n", encoding="utf-8")

    assert mp.find_open_tasks_for(PERSON, note, "restricted") == []


def test_a_held_back_line_is_counted_never_shown(vault):
    root, note = vault
    (root / "Notes" / "Mine.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] send the budget draft"), encoding="utf-8")
    (root / "Notes" / "PHI.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] SECRET-LINE\n- [ ] [[{PERSON}]] SECRET-TWO",
                "restricted"), encoding="utf-8")

    result = mp.write_block(note, "morning", False, NOW)
    text = note.read_text(encoding="utf-8")

    assert "send the budget draft" in text
    assert "SECRET" not in text and "[[PHI]]" not in text
    assert "> 2 more held back: their notes are classified above this one" in text
    assert result == f"wrote morning block (1, 2 held back) to {note.name}"


@pytest.mark.parametrize("dest_header", [
    "---\ntype: Individual\npeople:\n",                               # untiered
    "---\ntype: Individual\nclassification: secret\npeople:\n",       # unreadable
])
def test_a_meeting_note_without_a_readable_tier_gets_no_block(vault, dest_header):
    root, note = vault
    meeting = dest_header + f'  - "[[{PERSON}]]"\n---\n\n## Agenda\n'
    note.write_text(meeting, encoding="utf-8")
    (root / "Notes" / "Mine.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] send the budget draft", "public"), encoding="utf-8")

    result = mp.write_block(note, "morning", False, NOW)

    assert result == f"skip (tier unreadable or absent) {note.name}"
    assert note.read_text(encoding="utf-8") == meeting


def test_an_old_block_is_removed_when_the_tier_stops_reading(vault):
    root, note = vault
    (root / "Notes" / "Mine.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] send the budget draft"), encoding="utf-8")
    mp.write_block(note, "morning", False, NOW)
    text = note.read_text(encoding="utf-8")
    assert mp.BEGIN_MARK in text
    note.write_text(text.replace("classification: confidential",
                                 "classification: secret"), encoding="utf-8")

    result = mp.write_block(note, "pre-meeting", False, NOW)

    assert result == f"removed block, tier unreadable {note.name}"
    assert mp.BEGIN_MARK not in note.read_text(encoding="utf-8")


def test_a_restricted_meeting_quotes_a_restricted_source(vault):
    root, note = vault
    note.write_text(MEETING.replace("confidential", "restricted"), encoding="utf-8")
    (root / "Notes" / "PHI.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] SAME-TIER", "restricted"), encoding="utf-8")

    mp.write_block(note, "morning", False, NOW)
    text = note.read_text(encoding="utf-8")

    assert "SAME-TIER" in text
    assert mp.classification_tier.effective(text) == ("restricted", [])


def test_an_oversize_source_is_not_read(vault, monkeypatch):
    root, note = vault
    monkeypatch.setattr(mp.classification_tier, "MAX_NOTE_BYTES", 200)
    (root / "Notes" / "Big.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] BIG-LINE") + "x" * 300, encoding="utf-8")

    assert mp.find_open_tasks_for(PERSON, note, "restricted") == []


# --- v2.6 F20: a job-written note is never "your" follow-up ---------------

@pytest.mark.parametrize("folder", ["Knowledge", "Notes", "Actions"])
def test_a_note_with_a_generator_marker_contributes_nothing(vault, folder):
    root, note = vault
    (root / folder).mkdir(exist_ok=True)
    (root / folder / "Weekly report.md").write_text(
        _tiered(f"- [ ] [[{PERSON}]] MODEL-WRITTEN", "confidential",
                "generator: report_job\n"), encoding="utf-8")

    assert mp.find_open_tasks_for(PERSON, note, "restricted") == []


def test_generator_in_the_body_is_not_a_marker(vault):
    root, note = vault
    (root / "Notes" / "Mine.md").write_text(
        _tiered(f"generator: notes on the backup generator\n- [ ] [[{PERSON}]] OWN-LINE"),
        encoding="utf-8")

    assert [t for _, t in mp.find_open_tasks_for(PERSON, note, "confidential")] == [
        f"- [ ] [[{PERSON}]] OWN-LINE"]
