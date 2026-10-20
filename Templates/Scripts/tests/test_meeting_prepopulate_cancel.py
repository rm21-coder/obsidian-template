"""
test_meeting_prepopulate_cancel.py -- a cancellation never destroys the user's notes.

Until 2026-10-20 any organizer's cancellation, or the user declining, made
delete_cancelled() unlink the meeting note, including whatever prep the user
had written in it. Obsidian's File Recovery does not capture an external
delete, so the text was simply gone (finding 5277583). The same delete was
then read by harvest_learning() as "the user deleted this flagged note", which
suppressed the subject for good (finding 5268417).

Now an untouched note (body still the pipeline's own) moves to the vault's
.trash/, and a note the user wrote in stays put with a `status:` line and a
fixed-text banner. These drive process_handoff() for real against a throwaway
vault, then the helpers directly for the edge cases.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import meeting_prepopulate as mp

TZ = "America/New_York"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    for d in ("People", "Groups", "Meetings"):
        (root / d).mkdir(parents=True)
    state = tmp_path / ".state"
    state.mkdir()
    monkeypatch.setattr(mp, "PEOPLE_DIR", root / "People")
    monkeypatch.setattr(mp, "PEOPLE_UNRESOLVED_DIR", root / "People" / "_Unresolved")
    monkeypatch.setattr(mp, "GROUPS_DIR", root / "Groups")
    monkeypatch.setattr(mp, "MEETINGS_DIR", root / "Meetings")
    monkeypatch.setattr(mp, "MEETINGS_SERIES_DIR", root / "Meetings" / "Series")
    monkeypatch.setattr(mp, "STATE_DIR", state)
    monkeypatch.setattr(mp, "STATE_FILE", state / "seen.json")
    monkeypatch.setattr(mp, "LEARNING_FILE", state / "learning.json")
    monkeypatch.setattr(mp, "CONFIG_FILE", tmp_path / "no-config.json")
    return root


def _meeting(uid: str = "uid-1", *, cancelled: bool = False,
             response: str = "accepted", subject: str = "Budget review") -> dict:
    start = (dt.datetime.now(ZoneInfo(TZ)) + dt.timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(hours=1)
    utc = dt.timezone.utc
    return {
        "uid": uid, "subject": subject, "body_preview": "",
        "start": start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_all_day": False, "is_cancelled": cancelled,
        "is_private_appointment": False, "my_response_status": response,
        "producer_classification_hint": {"class": "individual"},
        "organizer": {"email": "bob@example.edu", "display_name": "Bob Builder",
                      "is_me": False, "is_group_mailbox": False},
        "attendees": [
            {"email": "ada@example.edu", "display_name": "Ada Example",
             "type": "required", "response_status": "accepted", "is_resource": False},
            {"email": "bob@example.edu", "display_name": "Bob Builder",
             "type": "required", "response_status": "accepted", "is_resource": False},
        ],
        "attendee_counts": {"required_non_declined_non_resource": 2},
        "location": {"display": None, "is_teams_meeting": False, "teams_join_url": None},
    }


class _Record:
    def __init__(self, meetings: list[dict]) -> None:
        self.id = "test-handoff"
        self.handle = None
        self.payload = {"user": {"email": "ada@example.edu", "timezone": TZ},
                        "meetings": meetings, "contacts": []}


def _run(*meetings: dict) -> None:
    assert mp.process_handoff(_Record(list(meetings)), None, dry_run=False,
                              no_move=True) == 0


def _state() -> dict:
    return json.loads(mp.STATE_FILE.read_text(encoding="utf-8"))


def _note(vault: Path) -> Path:
    (note,) = [p for p in (vault / "Meetings").glob("*.md")]
    return note


class TestCancellationEndToEnd:

    def test_an_untouched_note_moves_to_the_trash(self, vault: Path) -> None:
        _run(_meeting())
        note = _note(vault)
        generated = note.read_text(encoding="utf-8")
        assert _state()["uid-1"]["body_sha256"] == mp._body_digest(generated)

        _run(_meeting(cancelled=True))
        assert not note.exists()
        trashed = vault / ".trash" / note.name
        assert trashed.read_text(encoding="utf-8") == generated
        assert _state()["uid-1"]["status"] == "cancelled"
        assert _state()["uid-1"]["cancel_action"] == "trashed"

    def test_an_operator_edited_note_is_kept_with_a_banner(self, vault: Path) -> None:
        _run(_meeting())
        note = _note(vault)
        note.write_text(note.read_text(encoding="utf-8") + "\n## Prep\n- my notes\n",
                        encoding="utf-8")

        _run(_meeting(cancelled=True))
        text = note.read_text(encoding="utf-8")
        assert "- my notes" in text
        fm, body = mp._split_frontmatter(text)
        assert "\nstatus: cancelled\n" in fm
        assert body.lstrip("\n").startswith(mp.CANCEL_FENCE_START + "\n> [!warning] Cancelled\n"
                               "> The organizer cancelled this meeting. Kept because "
                               "this note has your edits")
        assert not (vault / ".trash").exists()
        assert _state()["uid-1"]["cancel_action"] == "kept"

    def test_a_decline_keeps_an_edited_note_as_declined(self, vault: Path) -> None:
        _run(_meeting())
        note = _note(vault)
        note.write_text(note.read_text(encoding="utf-8") + "prep\n", encoding="utf-8")

        _run(_meeting(response="declined"))
        text = note.read_text(encoding="utf-8")
        assert "\nstatus: declined\n" in mp._split_frontmatter(text)[0]
        assert "> [!warning] Declined\n> You declined this meeting." in text
        assert text.rstrip().endswith("prep")

    def test_a_decline_trashes_an_untouched_note(self, vault: Path) -> None:
        _run(_meeting())
        note = _note(vault)
        _run(_meeting(response="declined"))
        assert not note.exists()
        assert (vault / ".trash" / note.name).exists()

    def test_a_kept_note_is_restored_when_the_meeting_comes_back(self, vault: Path) -> None:
        _run(_meeting())
        note = _note(vault)
        edited = note.read_text(encoding="utf-8") + "prep\n"
        note.write_text(edited, encoding="utf-8")
        _run(_meeting(cancelled=True))
        _run(_meeting())
        text = note.read_text(encoding="utf-8")
        assert mp.CANCEL_FENCE_START not in text
        assert "status:" not in text
        assert text.rstrip().endswith("prep")
        assert [p.name for p in (vault / "Meetings").glob("*.md")] == [note.name]
        assert _state()["uid-1"]["status"] == "active"

    def test_harvest_does_not_learn_from_the_pipelines_own_cancellation(
            self, vault: Path) -> None:
        """A flagged (no-attendee) note moved to the trash by a cancellation is
        not the user deleting it (finding 5268417)."""
        solo = _meeting(subject="Roadmap Discussion")
        solo["attendees"] = []
        solo["attendee_counts"] = {"required_non_declined_non_resource": 0}
        _run(solo)
        assert _state()["uid-1"]["low_confidence"] is True
        _run(dict(solo, is_cancelled=True))
        _run(dict(solo, uid="uid-2"))          # next run harvests first
        learned = json.loads(mp.LEARNING_FILE.read_text(encoding="utf-8"))
        assert learned["suppress"] == {}
        assert _state()["uid-1"]["status"] == "cancelled"
        assert len(list((vault / "Meetings").glob("*.md"))) == 1


class TestRetireHelpers:

    @pytest.fixture
    def meetings(self, vault: Path) -> Path:
        return vault / "Meetings"

    def test_a_trash_name_collision_does_not_overwrite(self, meetings: Path) -> None:
        trash = meetings.parent / ".trash"
        trash.mkdir()
        (trash / "2026-10-21 1000.md").write_text("older")
        note = meetings / "2026-10-21 1000.md"
        note.write_text("---\ntype: Individual\n---\n")
        assert mp.delete_cancelled({"filename": note.name}, "cancelled",
                                   dry_run=False, changes=[]) == "trashed"
        assert (trash / "2026-10-21 1000.md").read_text() == "older"
        assert (trash / "2026-10-21 1000 1.md").read_text() == "---\ntype: Individual\n---\n"

    def test_a_body_differing_from_the_recorded_digest_is_kept(self, meetings: Path) -> None:
        note = meetings / "2026-10-21 1000.md"
        note.write_text("---\ntype: Individual\n---\n")
        prev = {"filename": note.name, "body_sha256": mp._body_digest(note.read_text())}
        note.write_text("---\ntype: Individual\n---\nmy notes\n")
        changes: list[str] = []
        assert mp.delete_cancelled(prev, "cancelled", False, changes) == "kept"
        assert changes == ["CANCELLED (cancelled) -> kept `2026-10-21 1000.md` (has your edits)"]
        assert note.read_text().endswith("my notes\n")

    def test_frontmatter_rewrites_alone_do_not_count_as_edits(self, meetings: Path) -> None:
        """Obsidian rewrites the frontmatter of an externally changed note."""
        note = meetings / "2026-10-21 1000.md"
        note.write_text("---\ntype: Individual\n---\n")
        prev = {"filename": note.name, "body_sha256": mp._body_digest(note.read_text())}
        note.write_text("---\ntype: Individual\ntags: []\n---\n")
        assert mp.delete_cancelled(prev, "cancelled", False, []) == "trashed"

    def test_a_symlinked_trash_is_refused_and_the_note_kept(
            self, meetings: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (meetings.parent / ".trash").symlink_to(elsewhere)
        note = meetings / "2026-10-21 1000.md"
        note.write_text("---\ntype: Individual\n---\n")
        assert mp.delete_cancelled({"filename": note.name}, "cancelled", False, []) == "kept"
        assert note.exists() and not list(elsewhere.iterdir())

    def test_dry_run_moves_and_writes_nothing(self, meetings: Path) -> None:
        note = meetings / "2026-10-21 1000.md"
        note.write_text("---\ntype: Individual\n---\nmine\n")
        assert mp.delete_cancelled({"filename": note.name}, "cancelled", True, []) == "kept"
        assert note.read_text() == "---\ntype: Individual\n---\nmine\n"

    def test_the_redirect_stub_goes_to_the_trash_too(self, meetings: Path) -> None:
        stub = meetings / "2026-10-20 0900.md"
        stub.write_text(mp._redirect_stub("2026-10-21 1000", "2026-10-20T08:00:00"))
        note = meetings / "2026-10-21 1000.md"
        note.write_text("---\ntype: Individual\n---\n")
        mp.delete_cancelled({"filename": note.name, "rescheduled_from": "2026-10-20 0900"},
                            "cancelled", False, [])
        assert not stub.exists()
        assert (meetings.parent / ".trash" / stub.name).exists()

    def test_harvest_skips_cancelled_and_suppressed_entries(self, vault: Path) -> None:
        learned, c = mp.load_learning(), Counter()
        state = {u: {"filename": "2026-10-21 1000.md", "low_confidence": True,
                     "block_key": "roadmap discussion", "subject": "Roadmap Discussion",
                     "status": s} for u, s in (("a", "cancelled"), ("b", "suppressed"))}
        mp.harvest_learning(state, learned, False, c)
        assert learned["suppress"] == {}
        assert c["learned-suppress"] == 0
        assert state["a"]["status"] == "cancelled"
