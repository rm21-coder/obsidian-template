"""
test_mcp_meeting_transform.py -- one bad event cannot cost the whole day.

The events are model output from a session that reads invite bodies, so an
outsider's invite can steer it into emitting `"dateTime": "TBD"`, a string
organizer or a non-object attendee. Until 2026-10 any of those raised out of
build_handoff: no handoff was written, so no meeting notes at all that day,
and a recurring invite repeated it daily (M-DASH #93). Now the bad event is
skipped with a warning in notes[] and the rest of the day goes through.
"""
from __future__ import annotations

import copy

import pytest

import mcp_meeting_transform as mt

USER = {"display_name": "Ada Example", "email": "ada@example.edu",
        "tenant": "example.edu", "timezone": "America/New_York"}
WEEK = {"start": "2026-10-05", "end": "2026-10-05"}
DOMAINS = frozenset({"example.edu"})

GOOD = {
    "id": "good-1", "subject": "Budget review", "bodyPreview": "agenda",
    "organizer": {"name": "Bob Builder", "address": "bob@example.edu"},
    "attendees": [
        {"name": "Ada Example", "address": "ada@example.edu",
         "type": "required", "responseStatus": "accepted"},
        {"name": "Bob Builder", "address": "bob@example.edu",
         "type": "required", "responseStatus": "accepted"},
    ],
    "start": {"dateTime": "2026-10-05T15:00:00.0000000",
              "timeZone": "Eastern Standard Time"},
    "end": {"dateTime": "2026-10-05T15:30:00.0000000",
            "timeZone": "Eastern Standard Time"},
}


def _bad(**override) -> dict:
    ev = copy.deepcopy(GOOD)
    ev["id"] = "bad-1"
    ev["attendees"].append({"name": "Eve Planted", "address": "eve@example.edu",
                            "type": "required", "responseStatus": "accepted"})
    ev.update(override)
    return ev


def _warning(payload: dict) -> str:
    warnings = [n["text"] for n in payload["notes"] if n["level"] == "warning"]
    assert len(warnings) == 1, payload["notes"]
    return warnings[0]


@pytest.mark.parametrize("override, reason", [
    ({"start": {"dateTime": "TBD", "timeZone": "UTC"}}, "ValueError"),
    ({"start": "tomorrow at 3"}, "TypeError: start is not an object"),
    ({"end": {"dateTime": 20261005, "timeZone": "UTC"}}, "AttributeError"),
    ({"attendees": "everyone"}, "TypeError: attendees is not a list"),
    # Valid ISO, but the UTC conversion leaves datetime's range.
    ({"start": {"dateTime": "9999-12-31T23:00:00",
                "timeZone": "Pacific Standard Time"}}, "OverflowError"),
    ({"start": {"dateTime": "0001-01-01T00:30:00",
                "timeZone": "Asia/Tokyo"}}, "OverflowError"),
])
def test_malformed_event_is_skipped_and_the_rest_kept(override, reason, capsys):
    payload = mt.build_handoff([_bad(**override), copy.deepcopy(GOOD)],
                               USER, WEEK, DOMAINS)

    assert [m["uid"] for m in payload["meetings"]] == ["good-1"]
    text = _warning(payload)
    assert text.startswith("Skipped 1 malformed event(s); the rest of the day")
    assert "'bad-1'" in text and reason in text
    assert "WARNING: skipped malformed event 'bad-1'" in capsys.readouterr().err
    # Nothing from the half-processed bad event leaks into contacts.
    assert "eve@example.edu" not in {c["email"] for c in payload["contacts"]}


def test_non_object_event_is_skipped_by_position():
    payload = mt.build_handoff(["not an event", copy.deepcopy(GOOD)],
                               USER, WEEK, DOMAINS)

    assert [m["uid"] for m in payload["meetings"]] == ["good-1"]
    assert "#0 (TypeError: event is not an object)" in _warning(payload)


def test_wrong_typed_organizer_and_attendee_are_treated_as_absent():
    ev = copy.deepcopy(GOOD)
    ev["organizer"] = "Bob Builder <bob@example.edu>"
    ev["attendees"].append("Mallory <m@example.edu>")

    payload = mt.build_handoff([ev], USER, WEEK, DOMAINS)

    (meeting,) = payload["meetings"]
    assert meeting["organizer"]["email"] is None
    assert [a["email"] for a in meeting["attendees"]] == [
        "ada@example.edu", "bob@example.edu"]
    assert not [n for n in payload["notes"] if n["level"] == "warning"]


def test_well_formed_day_is_unchanged():
    payload = mt.build_handoff([copy.deepcopy(GOOD)], USER, WEEK, DOMAINS)

    (meeting,) = payload["meetings"]
    assert meeting["start"] == "2026-10-05T19:00:00Z"
    assert meeting["end"] == "2026-10-05T19:30:00Z"
    assert meeting["duration_minutes"] == 30
    assert meeting["producer_classification_hint"]["class"] == "individual"
    assert meeting["organizer"]["email"] == "bob@example.edu"
    assert [c["email"] for c in payload["contacts"]] == [
        "ada@example.edu", "bob@example.edu"]
    assert [n["level"] for n in payload["notes"]] == ["info"]
