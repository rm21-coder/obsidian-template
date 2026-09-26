"""
test_meeting_prepopulate_injection.py -- invite text cannot write frontmatter.

Meeting subjects and attendee display names are authored by whoever sends the
invite. The frontmatter writer quoted them on a handful of YAML metacharacters
and escaped nothing, so a `"` plus a newline closed the string and started new
lines -- `classification: public` among them, above the note's own
`classification: confidential`. The export gate and RAG sync read the first
classification line. Found by the adversarial review of the M-DASH fixes,
2026-09-25, together with two availability defects (an over-long name
stalled every handoff; seen-state names reached unlink()).

Assertions are made on the PARSED result, with two parsers that disagree about
duplicate keys (first-match regex, PyYAML last-wins): both must see exactly
one classification, and it must be the real one.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

import disclosure_check as dc
import meeting_prepopulate as mp

NOW = "2026-09-25T08:00:00-04:00"

INJECTIONS = [
    'Zed Q"\nclassification: public\nx: "',
    "Budget\nclassification: public",
    "Budget\r\nclassification: public",
    "Budget classification: public",
    "Budget\x85classification: public",
    'x" \n---\nclassification: public\n---\n"',
    "true", "null", "- item", "#comment", "12:30",
]


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    return text[4:text.index("\n---", 4)]


def _classification_lines(fm: str) -> list[str]:
    return [ln for ln in fm.splitlines() if ln.startswith("classification")]


def _assert_single_real_tier(text: str, tier: str | None, tmp_path: Path) -> None:
    fm = _frontmatter(text)
    parsed = yaml.safe_load(fm)          # must still be valid YAML
    assert isinstance(parsed, dict), parsed
    lines = _classification_lines(fm)
    if tier is None:
        assert lines == [], lines
        return
    assert lines == [f"classification: {tier}"], lines
    assert parsed["classification"] == tier
    note = tmp_path / "n.md"
    note.write_text(text, encoding="utf-8")
    assert dc.note_tier(note) == tier


@pytest.mark.parametrize("payload", INJECTIONS)
def test_attendee_display_name_cannot_write_frontmatter(tmp_path: Path, payload: str) -> None:
    contact = {"email": "zed@example.com", "display_name": payload,
               "title": payload, "company": payload, "phone": payload}
    text = mp.render_people_stub("Zed Q", contact, payload, "handoff", NOW)
    _assert_single_real_tier(text, "confidential", tmp_path)


@pytest.mark.parametrize("payload", INJECTIONS)
def test_meeting_subject_cannot_write_frontmatter(tmp_path: Path, payload: str) -> None:
    text = mp.render_meeting_file({"subject": payload}, "Ad-hoc", None,
                                  [payload, "Pat Quinn"], NOW)
    _assert_single_real_tier(text + "---\n" if not text.rstrip().endswith("---") else text,
                             "confidential", tmp_path)


@pytest.mark.parametrize("payload", INJECTIONS)
def test_series_root_subject_cannot_write_frontmatter(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(mp, "MEETINGS_DIR", tmp_path / "Meetings")
    m = {"subject": payload, "series_uid": payload, "recurrence_human": payload,
         "rrule_raw": payload}
    mp.ensure_series_root(m, "Meetings/Series/s", [payload], NOW, dry_run=False)
    text = (tmp_path / "Meetings" / "Series" / "s.md").read_text(encoding="utf-8")
    _assert_single_real_tier(text, None, tmp_path)
    body = text.split("\n---\n", 1)[1]
    assert "\nclassification:" not in body


def test_hostile_email_is_never_written_as_an_address(tmp_path: Path) -> None:
    contact = {"email": "a@example.com\nclassification: public"}
    text = mp.render_people_stub("A B", contact, "A B", "handoff", NOW)
    _assert_single_real_tier(text, "confidential", tmp_path)


# ---------------------------------------------------------------------------
# Availability: one outsider-authored name must not stall the pipeline.

@pytest.fixture
def people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "vault" / "People"
    d.mkdir(parents=True)
    monkeypatch.setattr(mp, "PEOPLE_DIR", d)
    monkeypatch.setattr(mp, "PEOPLE_UNRESOLVED_DIR", d / "_Unresolved")
    return d


def _resolve(display: str) -> str:
    stem, _status = mp.resolve_or_create_person(
        {"display_name": display, "email": ""}, {}, mp.PeopleIndex(), NOW,
        dry_run=False, counters=Counter())
    return stem


@pytest.mark.parametrize("display", ["Ab " * 200, "É" * 300, "漢字" * 100])
def test_over_long_display_name_still_creates_a_stub(people: Path, display: str) -> None:
    """Over 255 bytes raised "File name too long", the handoff was never
    acked, and every later run failed on the same invite."""
    stem = _resolve(display)
    assert len(stem.encode("utf-8")) <= mp._MAX_STEM_BYTES
    assert list(people.rglob("*.md")), "no stub was written"


@pytest.mark.parametrize("display", ["CON", "nul", "Com1", "LPT9", "aux.md"])
def test_windows_device_names_are_not_used_as_stems(people: Path, display: str) -> None:
    stem = _resolve(display)
    assert stem.split(".")[0].strip().upper() not in mp._RESERVED_STEMS


# ---------------------------------------------------------------------------
# Seen-state names reach read/write/unlink: only pipeline-shaped names do.

@pytest.fixture
def meetings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "vault" / "Meetings"
    d.mkdir(parents=True)
    monkeypatch.setattr(mp, "MEETINGS_DIR", d)
    return d


@pytest.mark.parametrize("name", ["../../victim.txt", "../victim.md", "/abs/victim.md",
                                  "2026-09-25 0900.md/../../victim.txt", "victim.md"])
def test_cancel_cannot_delete_outside_the_owned_note_shape(
        meetings: Path, tmp_path: Path, name: str) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (meetings.parent / "victim.md").write_text("keep")
    (meetings / "victim.md").write_text("keep")
    mp.delete_cancelled({"filename": name, "rescheduled_from": name}, "cancelled",
                        dry_run=False, changes=[])
    assert victim.read_text() == "keep"
    assert (meetings.parent / "victim.md").exists()
    assert (meetings / "victim.md").exists(), "a non-pipeline note was deleted"


def test_cancel_still_deletes_a_pipeline_owned_note(meetings: Path) -> None:
    owned = meetings / "2026-09-25 0900-2.md"
    owned.write_text("---\nclassification: confidential\n---\n")
    mp.delete_cancelled({"filename": owned.name}, "cancelled", dry_run=False, changes=[])
    assert not owned.exists()


def test_reschedule_cannot_overwrite_outside_meetings(meetings: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.md"
    victim.write_text("keep")
    import datetime as dt
    out = mp.apply_reschedule({"filename": "../victim.md"},
                              dt.datetime(2026, 9, 26, 10, 0), NOW,
                              dry_run=False, changes=[])
    assert out is None
    assert victim.read_text() == "keep"


# ---------------------------------------------------------------------------
# Round 2: a wrong-typed handoff must not crash every run or withhold the ack.

import handoff_source as hs


@pytest.mark.parametrize("payload_patch", [
    {"meetings": "x"}, {"contacts": "x"}, {"user": ["x"]},
])
def test_wrong_container_types_are_a_schema_error(payload_patch: dict) -> None:
    payload = {"schema_version": next(iter(hs.SUPPORTED_SCHEMA_VERSIONS)),
               **{k: [] for k in hs.REQUIRED_TOP_LEVEL if k != "schema_version"}}
    payload.update(payload_patch)
    with pytest.raises(hs.HandoffError, match="must be a JSON"):
        hs.validate_schema(payload)


@pytest.mark.parametrize("m", [
    "x", None, 42, {"uid": ["a"]}, {"uid": "u", "subject": {"x": 1}},
    {"uid": "u", "attendees": "bob"}, {"uid": "u", "attendees": [1, 2]},
])
def test_malformed_meeting_is_named(m) -> None:
    assert mp._meeting_shape_problem(m)


def test_well_formed_meeting_passes_the_shape_check() -> None:
    assert mp._meeting_shape_problem({"uid": "u", "subject": "s", "start": "2026-09-25T09:00:00",
                                      "end": "2026-09-25T10:00:00",
                                      "attendees": [{"email": "a@example.com"}]}) is None


def test_stub_path_fits_windows_max_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows ARM64 test laptop, 2026-09-25: vault path + People/ + a 200-byte
    stem exceeded MAX_PATH, the write failed, the handoff stalled. The stem is
    now fitted to the directory it lands in."""
    deep = tmp_path / ("v" * 60) / ("w" * 60) / "People"
    deep.mkdir(parents=True)
    monkeypatch.setattr(mp, "PEOPLE_DIR", deep)
    monkeypatch.setattr(mp, "PEOPLE_UNRESOLVED_DIR", deep / "_Unresolved")
    monkeypatch.setattr(mp.sys, "platform", "win32")
    stem = _resolve("Ab " * 200)
    written = list(deep.rglob("*.md"))
    assert written, "no stub was written"
    assert all(len(str(p.resolve())) <= mp._WIN_MAX_PATH for p in written), \
        [len(str(p.resolve())) for p in written]


def test_a_stub_that_cannot_be_written_is_skipped_not_raised(people: Path, monkeypatch) -> None:
    """Whatever the cause, a failed stub write must not propagate: that is
    what withheld the handoff's ack."""
    real = Path.write_text
    def boom(self, *a, **k):
        if self.parent.name in ("People", "_Unresolved"):
            raise FileNotFoundError(2, "path too long")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", boom)
    stem, status = mp.resolve_or_create_person(
        {"display_name": "Pat Quinn", "email": ""}, {}, mp.PeopleIndex(), NOW,
        dry_run=False, counters=Counter())
    assert (stem, status) == (None, "stub-write-failed")
