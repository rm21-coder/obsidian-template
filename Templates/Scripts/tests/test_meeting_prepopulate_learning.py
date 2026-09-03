"""meeting_prepopulate: zero-participant handling and the learning store.

These paths decide whether a calendar entry becomes a note at all, and until
now they were covered only by throwaway sandbox runs. The failure they guard
against is silent in both directions:

  - over-suppress and a real meeting produces no note, which is discovered by
    walking into it with nowhere to type;
  - over-generate and the vault fills with notes for haircuts.

Neither shows up as an error, in a log, or on the dashboard — the consumer
exits 0 either way. So the invariants are pinned here instead.

The learning store is the part most worth protecting: it is keyed on a
normalized subject rather than the uid, because a repeating block gets a fresh
uid every occurrence. Get the key wrong and the store either never matches
(learning silently does nothing) or over-matches (one deletion suppresses
unrelated meetings).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from collections import Counter
from zoneinfo import ZoneInfo

import pytest

import meeting_prepopulate as mp

TZ = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 9, 3, 8, 0, tzinfo=TZ)


@pytest.fixture
def learning_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate MEETINGS_DIR and the learning store under tmp_path.

    Both are module-level constants resolved at import from the real vault, so
    without this a test run would read and write the user's actual notes.
    """
    meetings = tmp_path / "Meetings"
    meetings.mkdir()
    state = tmp_path / ".state"
    state.mkdir()
    monkeypatch.setattr(mp, "MEETINGS_DIR", meetings)
    monkeypatch.setattr(mp, "STATE_DIR", state)
    monkeypatch.setattr(mp, "LEARNING_FILE", state / "meeting_block_learning.json")
    return tmp_path


def meeting(subject: str, *, hint: str = "solo", body: str = "",
            start: str = "2026-09-03T14:00:00Z",
            end: str = "2026-09-03T15:00:00Z",
            location: dict | None = None) -> dict:
    """Build the handoff-shaped dict the consumer's gates consume."""
    return {
        "uid": "uid-" + subject.lower().replace(" ", "-"),
        "subject": subject,
        "body_preview": body,
        "is_all_day": False,
        "is_cancelled": False,
        "is_private_appointment": False,
        "my_response_status": "organizer",
        "producer_classification_hint": {"class": hint},
        "start": start,
        "end": end,
        "location": location or {"display": None, "is_teams_meeting": False,
                                 "teams_join_url": None},
        "attendees": [],
        "attendee_counts": {"required_non_declined_non_resource": 0},
        "organizer": {"email": "me@example.edu", "display_name": "Me",
                      "is_me": True, "is_group_mailbox": False},
    }


class TestBlockKey:
    """The normalization contract the learning store depends on."""

    def test_repeating_entry_collapses_to_one_key(self) -> None:
        """Flight numbers and confirmation codes differ every occurrence. If
        they survived into the key, deleting one flight note would teach
        nothing about the next one and the store would grow without ever
        matching."""
        a = mp.block_key("Acme Air flight 2712 to Denver (AAA111)")
        b = mp.block_key("Acme Air flight 1149 to Denver (BBB222)")
        assert a == b
        assert a == "acme air flight to denver"

    def test_punctuation_variants_collapse(self) -> None:
        """Humans type the same block inconsistently."""
        assert mp.block_key("PTO - offsite") == mp.block_key("PTO- offsite")
        assert mp.block_key("PTO  -  offsite") == mp.block_key("pto offsite")

    def test_outlook_prefixes_are_stripped(self) -> None:
        assert mp.block_key("FYI: Budget Review") == mp.block_key("Budget Review")
        assert mp.block_key("Reminder: Budget Review") == mp.block_key("Budget Review")

    def test_distinct_meetings_keep_distinct_keys(self) -> None:
        """The dangerous direction. Over-collapsing means one deletion
        suppresses unrelated meetings, so assert these stay separate."""
        keys = {mp.block_key(s) for s in (
            "Roadmap Discussion",
            "Platform Steering Committee",
            "Vendor talk",
            "Sam stopping by",
            "Budget Review",
        )}
        assert len(keys) == 5

    def test_empty_and_missing_subjects_are_safe(self) -> None:
        assert mp.block_key("") == ""
        assert mp.block_key(None) == ""

    def test_key_is_stable_across_calls(self) -> None:
        s = "Acme Air flight 2712 to Denver (AAA111)"
        assert mp.block_key(s) == mp.block_key(s)


class TestPersonalBlockSeed:
    """The seed vocabulary, which only ever sees zero-participant events."""

    @pytest.mark.parametrize("subject", [
        "PTO", "PTO - offsite", "OOO Friday",
        "Acme Air flight 2712 to Denver", "Fly to Portland",
        "Hold", "Blocked", "Focus time",
        "Work on slide examples", "Finish up the dry run",
        "Car appt", "Dental work at 9am", "Haircut at 1",
        "Internet repair", "Window installers",
        # plurals: a \b after a singular stem silently fails to match these
        "Two deliveries", "Flights to Denver",
        "Car repairs", "Dentist appointments",
    ])
    def test_blocks_are_recognized(self, subject: str) -> None:
        assert mp.looks_like_personal_block(subject) is True

    @pytest.mark.parametrize("subject", [
        "Roadmap Discussion",
        "Vendor talk",
        "Sam stopping by",
        "Budget Review",
        "1:1 with the director",
    ])
    def test_real_meetings_are_not_blocked(self, subject: str) -> None:
        """The expensive direction: a false positive here silently deletes a
        meeting from the vault, and nothing reports it."""
        assert mp.looks_like_personal_block(subject) is False

    def test_empty_subject_is_not_a_block(self) -> None:
        """An untitled entry is unresolved, not personal — it should reach the
        flagged-note path rather than being suppressed."""
        assert mp.looks_like_personal_block("") is False
        assert mp.looks_like_personal_block(None) is False


class TestJoinLinkRescue:
    """A solo block carrying a hosted-meeting URL is a meeting."""

    @pytest.mark.parametrize("body", [
        "Join Zoom Meeting https://example.zoom.us/j/93230782357",
        "https://acme.zoom.us/j/97333972362?pwd=abc",
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x",
        "https://teams.live.com/meet/9312345",
        "https://meet.google.com/abc-defg-hij",
        "https://acme.webex.com/meet/someone",
    ])
    def test_join_urls_in_body_are_detected(self, body: str) -> None:
        assert mp.has_join_link(meeting("Working session", body=body)) is True

    def test_structured_location_fields_are_detected(self) -> None:
        assert mp.has_join_link(meeting("X", location={
            "display": None, "is_teams_meeting": True, "teams_join_url": None})) is True
        assert mp.has_join_link(meeting("X", location={
            "display": None, "is_teams_meeting": False,
            "teams_join_url": "https://teams.microsoft.com/l/meetup-join/1"})) is True
        assert mp.has_join_link(meeting("X", location={
            "display": "https://example.zoom.us/j/1", "is_teams_meeting": False,
            "teams_join_url": None})) is True

    def test_no_link_is_not_rescued(self) -> None:
        assert mp.has_join_link(meeting("Hold")) is False
        assert mp.has_join_link(meeting("Hold", body="in my office")) is False
        assert mp.has_join_link(meeting("Hold", location={
            "display": "Pratt 5th floor", "is_teams_meeting": False,
            "teams_join_url": None})) is False

    def test_redacted_body_hides_the_link(self) -> None:
        """Documented limitation, pinned so it changes deliberately: the
        producer redacts bodies for private/confidential sensitivity, so a
        link only present there cannot be seen and the event stays solo."""
        m = meeting("Sensitive sync", body="[redacted: sensitivity=private]")
        assert mp.has_join_link(m) is False

    def test_gate_keeps_a_solo_meeting_with_a_link(self) -> None:
        m = meeting("Working session",
                    body="Join Zoom Meeting https://example.zoom.us/j/1")
        assert mp.should_skip_meeting(m, NOW, TZ, False) is None

    def test_gate_still_drops_a_plain_block(self) -> None:
        assert mp.should_skip_meeting(meeting("PTO"), NOW, TZ, False) == "solo-block"


class TestAmbiguousBlocksGetNotes:
    """Unresolved zero-participant events must not be dropped silently."""

    def test_unknown_subject_is_not_skipped(self) -> None:
        assert mp.should_skip_meeting(meeting("Vendor talk"), NOW, TZ, False) is None

    def test_gate_order_learned_beats_seed(self) -> None:
        """A learned suppression must win over the seed heuristic, and report
        its own reason so the log says which rule fired."""
        learned = {"suppress": {mp.block_key("Vendor talk"): {}}, "participants": {}}
        assert mp.should_skip_meeting(
            meeting("Vendor talk"), NOW, TZ, False, None, learned) == "learned-block"

    def test_join_link_beats_a_learned_suppression(self) -> None:
        """Certainty outranks history: if an entry now carries a join link it
        is a meeting, whatever was learned when it did not."""
        learned = {"suppress": {mp.block_key("Vendor talk"): {}}, "participants": {}}
        m = meeting("Vendor talk", body="https://example.zoom.us/j/1")
        assert mp.should_skip_meeting(m, NOW, TZ, False, None, learned) is None

    def test_others_pto_is_still_always_skipped(self) -> None:
        m = meeting("Janet PTO", hint="personal_block")
        assert mp.should_skip_meeting(m, NOW, TZ, False) == "personal-block-others-pto"


class TestInProgressIsCurrent:
    """Past meetings are judged on the end time, not the start."""

    def test_meeting_under_way_is_kept(self) -> None:
        """Judging on the start refused to create a note for the meeting the
        user was sitting in, on every run after the hour struck."""
        m = meeting("Working session",
                    body="https://example.zoom.us/j/1",
                    start="2026-09-03T12:00:00Z",   # 08:00 EDT
                    end="2026-09-03T13:00:00Z")     # 09:00 EDT
        now = dt.datetime(2026, 9, 3, 8, 24, tzinfo=TZ)
        assert mp.should_skip_meeting(m, now, TZ, False) is None

    def test_finished_meeting_is_skipped(self) -> None:
        m = meeting("Working session", body="https://example.zoom.us/j/1",
                    start="2026-09-03T12:00:00Z", end="2026-09-03T13:00:00Z")
        now = dt.datetime(2026, 9, 3, 11, 0, tzinfo=TZ)
        assert mp.should_skip_meeting(m, now, TZ, False) == "in-the-past"

    def test_unparseable_end_falls_back_to_start(self) -> None:
        m = meeting("Working session", body="https://example.zoom.us/j/1",
                    start="2026-09-03T12:00:00Z")
        del m["end"]
        now = dt.datetime(2026, 9, 3, 11, 0, tzinfo=TZ)
        assert mp.should_skip_meeting(m, now, TZ, False) == "in-the-past"


class TestReadNotePeople:

    def _write(self, env: Path, name: str, text: str) -> Path:
        p = mp.MEETINGS_DIR / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_the_people_list(self, learning_env: Path) -> None:
        p = self._write(learning_env, "n.md",
                        '---\ntype: Ad-hoc\npeople:\n'
                        '  - "[[Ackerman, Dana]]"\n  - "[[Baptiste, Yusuf]]"\n'
                        'tags: []\n---\n')
        assert mp.read_note_people(p) == ["[[Ackerman, Dana]]", "[[Baptiste, Yusuf]]"]

    def test_empty_list_is_not_none(self, learning_env: Path) -> None:
        """[] means 'asked, still unanswered'; None means 'could not read'.
        Conflating them would make a missing note look like an empty one and
        suppress nothing."""
        p = self._write(learning_env, "n.md",
                        '---\ntype: Ad-hoc\npeople:\ntags: []\n---\n')
        assert mp.read_note_people(p) == []

    def test_flag_tag_does_not_bleed_into_people(self, learning_env: Path) -> None:
        """The flagged-note format puts a `tags:` list immediately after an
        empty `people:`. Scanning must stop at the next frontmatter key or
        `needs-attendees` would be harvested as a person."""
        p = self._write(learning_env, "n.md",
                        '---\ntype: Ad-hoc\npeople:\n'
                        'tags:\n  - needs-attendees\n'
                        'classification: confidential\n---\n')
        assert mp.read_note_people(p) == []

    def test_group_list_does_not_bleed_into_people(self, learning_env: Path) -> None:
        p = self._write(learning_env, "n.md",
                        '---\ntype: Group\ngroup:\n  - "[[Steering]]"\n'
                        'people:\n  - "[[Nguyen, Chris]]"\ntags: []\n---\n')
        assert mp.read_note_people(p) == ["[[Nguyen, Chris]]"]

    def test_missing_file_and_no_frontmatter_return_none(self, learning_env: Path) -> None:
        assert mp.read_note_people(mp.MEETINGS_DIR / "absent.md") is None
        p = self._write(learning_env, "plain.md", "just a body\n")
        assert mp.read_note_people(p) is None


class TestLearningStore:

    def test_missing_store_loads_empty_shape(self, learning_env: Path) -> None:
        d = mp.load_learning()
        assert d["suppress"] == {} and d["participants"] == {}

    def test_corrupt_store_warns_and_recovers(self, learning_env: Path,
                                              caplog: pytest.LogCaptureFixture) -> None:
        mp.LEARNING_FILE.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            d = mp.load_learning()
        assert d["suppress"] == {} and d["participants"] == {}
        assert "learning store unreadable" in caplog.text
        assert str(mp.LEARNING_FILE) in caplog.text

    def test_partial_store_is_backfilled(self, learning_env: Path) -> None:
        """A hand-edited store missing a key must not KeyError the consumer."""
        mp.LEARNING_FILE.write_text('{"suppress": {}}', encoding="utf-8")
        d = mp.load_learning()
        assert d["participants"] == {}

    def test_save_round_trips(self, learning_env: Path) -> None:
        d = mp.load_learning()
        d["suppress"]["vendor talk"] = {"subject": "Vendor talk"}
        mp.save_learning(d, dry_run=False)
        assert mp.load_learning()["suppress"]["vendor talk"]["subject"] == "Vendor talk"

    def test_dry_run_writes_nothing(self, learning_env: Path) -> None:
        d = mp.load_learning()
        d["suppress"]["x"] = {}
        mp.save_learning(d, dry_run=True)
        assert not mp.LEARNING_FILE.exists()


class TestHarvestLearning:
    """The feedback loop: the user's edits become durable rules."""

    def _state(self, **over) -> dict:
        entry = {
            "filename": "2026-09-03 0815.md",
            "generated_at": "2026-09-03T05:01:00-04:00",
            "low_confidence": True,
            "block_key": mp.block_key("Roadmap Discussion"),
            "subject": "Roadmap Discussion",
            "status": "active",
        }
        entry.update(over)
        return {"uid-1": entry}

    def test_deleted_note_teaches_suppression(self, learning_env: Path) -> None:
        state, learned, c = self._state(), mp.load_learning(), Counter()
        mp.harvest_learning(state, learned, False, c)
        key = mp.block_key("Roadmap Discussion")
        assert key in learned["suppress"]
        assert learned["suppress"][key]["subject"] == "Roadmap Discussion"
        assert c["learned-suppress"] == 1
        assert state["uid-1"]["status"] == "suppressed"

    def test_suppression_then_fires_in_the_gate(self, learning_env: Path) -> None:
        """End to end: the rule harvested from a deletion must actually stop
        the next occurrence, which arrives with a different uid."""
        learned, c = mp.load_learning(), Counter()
        mp.harvest_learning(self._state(), learned, False, c)
        nxt = meeting("Roadmap Discussion")
        nxt["uid"] = "a-different-uid"
        assert mp.should_skip_meeting(
            nxt, NOW, TZ, False, None, learned) == "learned-block"

    def test_filled_people_are_adopted(self, learning_env: Path) -> None:
        (mp.MEETINGS_DIR / "2026-09-03 0815.md").write_text(
            '---\ntype: Ad-hoc\npeople:\n  - "[[Ackerman, Dana]]"\ntags: []\n---\n',
            encoding="utf-8")
        learned, c = mp.load_learning(), Counter()
        mp.harvest_learning(self._state(), learned, False, c)
        key = mp.block_key("Roadmap Discussion")
        assert learned["participants"][key]["people"] == ["[[Ackerman, Dana]]"]
        assert c["learned-participants"] == 1
        assert key not in learned["suppress"]

    def test_flagged_but_untouched_note_teaches_nothing(self, learning_env: Path) -> None:
        """The common case: the note exists and the user has not filled it in
        yet. Neither rule should be created, or an unanswered flag would be
        mistaken for an answer."""
        (mp.MEETINGS_DIR / "2026-09-03 0815.md").write_text(
            '---\ntype: Ad-hoc\npeople:\ntags:\n  - needs-attendees\n---\n',
            encoding="utf-8")
        learned, c = mp.load_learning(), Counter()
        mp.harvest_learning(self._state(), learned, False, c)
        assert learned["suppress"] == {} and learned["participants"] == {}

    def test_normal_notes_are_never_harvested(self, learning_env: Path) -> None:
        """Only notes this pipeline flagged are inspected. A deleted ordinary
        meeting note must not suppress that meeting forever."""
        state = self._state(low_confidence=False)
        learned, c = mp.load_learning(), Counter()
        mp.harvest_learning(state, learned, False, c)
        assert learned["suppress"] == {} and learned["participants"] == {}

    def test_entry_without_a_key_is_skipped(self, learning_env: Path) -> None:
        """State written before subject-keyed learning existed has no
        block_key; it must be ignored, not crash the run."""
        state = self._state()
        del state["uid-1"]["block_key"]
        learned, c = mp.load_learning(), Counter()
        mp.harvest_learning(state, learned, False, c)
        assert learned["suppress"] == {}

    def test_harvest_is_idempotent(self, learning_env: Path) -> None:
        """Runs every 30 minutes; re-learning an unchanged answer must not
        re-log or churn the store."""
        (mp.MEETINGS_DIR / "2026-09-03 0815.md").write_text(
            '---\npeople:\n  - "[[Ackerman, Dana]]"\ntags: []\n---\n',
            encoding="utf-8")
        learned = mp.load_learning()
        first = Counter()
        mp.harvest_learning(self._state(), learned, False, first)
        second = Counter()
        mp.harvest_learning(self._state(), learned, False, second)
        assert first["learned-participants"] == 1
        assert second["learned-participants"] == 0

    def test_changed_people_update_the_rule(self, learning_env: Path) -> None:
        note = mp.MEETINGS_DIR / "2026-09-03 0815.md"
        note.write_text('---\npeople:\n  - "[[Ackerman, Dana]]"\ntags: []\n---\n',
                        encoding="utf-8")
        learned = mp.load_learning()
        mp.harvest_learning(self._state(), learned, False, Counter())
        note.write_text('---\npeople:\n  - "[[Ackerman, Dana]]"\n'
                        '  - "[[Baptiste, Yusuf]]"\ntags: []\n---\n', encoding="utf-8")
        c = Counter()
        mp.harvest_learning(self._state(), learned, False, c)
        key = mp.block_key("Roadmap Discussion")
        assert learned["participants"][key]["people"] == [
            "[[Ackerman, Dana]]", "[[Baptiste, Yusuf]]"]
        assert c["learned-participants"] == 1

    def test_harvest_persists_to_disk(self, learning_env: Path) -> None:
        mp.harvest_learning(self._state(), mp.load_learning(), False, Counter())
        on_disk = json.loads(mp.LEARNING_FILE.read_text(encoding="utf-8"))
        assert mp.block_key("Roadmap Discussion") in on_disk["suppress"]


class TestFlaggedNoteRendering:

    def test_flag_emits_the_tag(self) -> None:
        out = mp.render_meeting_file(
            meeting("Vendor talk"), "Ad-hoc", None, [], "2026-09-03T09:00",
            needs_attendees=True)
        assert "tags:\n  - needs-attendees" in out
        assert "title: Vendor talk" in out

    def test_unflagged_note_keeps_empty_tags(self) -> None:
        """The tagger pipeline owns tags on ordinary notes; only the flag path
        may seed one."""
        out = mp.render_meeting_file(
            meeting("Budget Review"), "Ad-hoc", None, [], "2026-09-03T09:00")
        assert "tags: []" in out
        assert "needs-attendees" not in out
