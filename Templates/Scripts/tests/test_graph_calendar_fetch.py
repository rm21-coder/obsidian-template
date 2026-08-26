"""graph_calendar_fetch: event flattening, pagination, token lifecycle.

No network: urllib is monkeypatched everywhere. The contract that matters
is byte-compatibility with mcp_meeting_transform.py's expected input shape
(flat attendee/organizer objects), because the transform is the shared,
already-validated half of the pipeline.
"""
from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

import graph_calendar_fetch as gcf


# ---------------------------------------------------------------------------
# flatten_event — the shape contract with mcp_meeting_transform
# ---------------------------------------------------------------------------

GRAPH_EVENT = {
    "id": "AAMk123",
    "subject": "Budget review",
    "start": {"dateTime": "2026-08-19T13:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-08-19T14:00:00.0000000", "timeZone": "UTC"},
    "isAllDay": False,
    "attendees": [
        {"type": "required",
         "status": {"response": "accepted", "time": "0001-01-01T00:00:00Z"},
         "emailAddress": {"name": "Jane Doe", "address": "jane@example.edu"}},
        {"type": "optional",
         "status": {"response": "none", "time": "0001-01-01T00:00:00Z"},
         "emailAddress": {"name": "Grp-Finance", "address": "grp-fin@example.edu"}},
    ],
    "organizer": {"emailAddress": {"name": "Rich Example",
                                   "address": "rich@example.edu"}},
    "bodyPreview": "agenda...",
    "sensitivity": "normal",
    "isCancelled": False,
    "categories": [],
    "location": {"displayName": "Conf Room 5"},
}


class TestFlattenEvent:

    def test_attendees_are_flattened_to_transform_shape(self) -> None:
        out = gcf.flatten_event(GRAPH_EVENT)
        a = out["attendees"][0]
        assert a == {"address": "jane@example.edu", "name": "Jane Doe",
                     "type": "required", "responseStatus": "accepted"}

    def test_organizer_is_flattened(self) -> None:
        out = gcf.flatten_event(GRAPH_EVENT)
        assert out["organizer"] == {"address": "rich@example.edu",
                                    "name": "Rich Example"}

    def test_missing_status_defaults_to_none(self) -> None:
        ev = dict(GRAPH_EVENT)
        ev["attendees"] = [{"emailAddress": {"address": "x@example.edu"}}]
        a = gcf.flatten_event(ev)["attendees"][0]
        assert a["responseStatus"] == "none"
        assert a["type"] == "required"

    def test_passthrough_fields_survive(self) -> None:
        out = gcf.flatten_event(GRAPH_EVENT)
        for f in ("id", "subject", "start", "end", "bodyPreview",
                  "sensitivity", "isCancelled", "location"):
            assert out[f] == GRAPH_EVENT[f]

    def test_original_event_is_not_mutated(self) -> None:
        before = json.dumps(GRAPH_EVENT, sort_keys=True)
        gcf.flatten_event(GRAPH_EVENT)
        assert json.dumps(GRAPH_EVENT, sort_keys=True) == before

    def test_flattened_event_feeds_the_real_transform(
            self, tmp_path, allow_subprocess) -> None:
        """End-to-end shape check against the actual transform module —
        if mcp_meeting_transform's field expectations ever change, this is
        the test that notices."""
        import subprocess as sp
        import sys as _sys
        raw = {
            "user": {"display_name": "Rich Example", "email": "rich@example.edu",
                     "tenant": "Example U", "timezone": "America/New_York"},
            "week": {"start": "2026-08-19", "end": "2026-08-19"},
            "events": [gcf.flatten_event(GRAPH_EVENT)],
        }
        inp = tmp_path / "raw.json"
        inp.write_text(json.dumps(raw))
        out_dir = tmp_path / "drop"
        p = sp.run([_sys.executable,
                    str(gcf.SCRIPTS_DIR / "mcp_meeting_transform.py"),
                    "--input", str(inp), "--out-dir", str(out_dir),
                    "--run-date", "2026-08-19",
                    "--tenant-domains", "example.edu",
                    "--exclude-attendees", ""],
                   capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        written = list(out_dir.glob("*"))
        assert written, "transform wrote nothing"


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

def test_fetch_events_follows_nextlink(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://graph.microsoft.com/v1.0/me/calendarView?p=1":
            {"value": [{"id": "1"}],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?p=2"},
        "https://graph.microsoft.com/v1.0/me/calendarView?p=2":
            {"value": [{"id": "2"}]},
    }
    seen = []

    def fake_get(url, token, prefer_tz=None):
        seen.append(url)
        return pages[url]

    monkeypatch.setattr(gcf, "_graph_get", fake_get)
    # bypass the param-building first URL by patching urlencode's output
    monkeypatch.setattr(gcf.urllib.parse, "urlencode", lambda q: "p=1")
    events = gcf.fetch_events("tok", "2026-08-19T00:00:00", "2026-08-20T00:00:00")
    assert [e["id"] for e in events] == ["1", "2"]
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# Prefer: outlook.timezone — the all-day-event date fence
# ---------------------------------------------------------------------------

class TestPreferTimezone:
    """Without the header Graph answers in UTC and an all-day event comes back
    as 00:00Z, which the consumer converts to 20:00 the PREVIOUS day. These
    assert the header, not merely that some request went out."""

    def _capture(self, monkeypatch):
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(req)
            return io.BytesIO(json.dumps({"value": []}).encode())

        monkeypatch.setattr(gcf.urllib.request, "urlopen", fake_urlopen)
        return sent

    def test_calendarview_requests_the_window_timezone(self, monkeypatch) -> None:
        sent = self._capture(monkeypatch)
        gcf.fetch_events("tok", "2026-08-19T00:00:00-04:00",
                         "2026-08-20T00:00:00-04:00",
                         tz_name="America/New_York")
        assert len(sent) == 1
        assert sent[0].get_header("Prefer") == 'outlook.timezone="America/New_York"'

    def test_no_prefer_header_when_no_zone_given(self, monkeypatch) -> None:
        sent = self._capture(monkeypatch)
        gcf.fetch_events("tok", "2026-08-19T00:00:00", "2026-08-20T00:00:00")
        assert sent[0].get_header("Prefer") is None

    def test_local_midnight_all_day_keeps_its_date_through_the_transform(self) -> None:
        """The end-to-end reason the header exists: Graph's reply under the
        header is local midnight, and that must survive to_utc_iso ->
        local-time conversion on the SAME calendar day."""
        import datetime as dt
        from zoneinfo import ZoneInfo
        import mcp_meeting_transform as mt

        with_header = mt.to_utc_iso("2026-08-19T00:00:00.0000000",
                                    "America/New_York")
        assert with_header == "2026-08-19T04:00:00Z"
        local = (dt.datetime.strptime(with_header, "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=dt.timezone.utc)
                 .astimezone(ZoneInfo("America/New_York")))
        assert local.date() == dt.date(2026, 8, 19)

        # the pre-fix behaviour, kept as the contrast that motivates the header
        without_header = mt.to_utc_iso("2026-08-19T00:00:00.0000000", "UTC")
        slipped = (dt.datetime.strptime(without_header, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=dt.timezone.utc)
                   .astimezone(ZoneInfo("America/New_York")))
        assert slipped.date() == dt.date(2026, 8, 18)


# ---------------------------------------------------------------------------
# token lifecycle
# ---------------------------------------------------------------------------

class TestTokenLifecycle:

    def _post_factory(self, responses):
        calls = []

        def fake_post(url, fields):
            calls.append((url, fields))
            return responses.pop(0)
        return fake_post, calls

    def test_refresh_rotates_stored_token(self, monkeypatch) -> None:
        import secret_store
        stored = {"GRAPH_REFRESH_TOKEN": "old-rt"}
        monkeypatch.setattr(secret_store, "get_secret",
                            lambda n, **k: stored.get(n))
        monkeypatch.setattr(secret_store, "set_secret",
                            lambda n, v: stored.__setitem__(n, v) or True)
        fake_post, calls = self._post_factory(
            [({"access_token": "at", "refresh_token": "new-rt"}, None)])
        monkeypatch.setattr(gcf, "_form_post", fake_post)
        at = gcf.get_access_token({})
        assert at == "at"
        assert stored["GRAPH_REFRESH_TOKEN"] == "new-rt"
        assert calls[0][1]["grant_type"] == "refresh_token"

    def test_expired_refresh_token_points_at_reauth(self, monkeypatch) -> None:
        import secret_store
        monkeypatch.setattr(secret_store, "get_secret", lambda n, **k: "rt")
        fake_post, _ = self._post_factory([(None, {"error": "invalid_grant"})])
        monkeypatch.setattr(gcf, "_form_post", fake_post)
        with pytest.raises(SystemExit):
            gcf.get_access_token({})

    def test_no_stored_token_points_at_auth(self, monkeypatch) -> None:
        import secret_store
        monkeypatch.setattr(secret_store, "get_secret", lambda n, **k: None)
        with pytest.raises(SystemExit):
            gcf.get_access_token({})

    def test_client_id_and_authority_overridable(self) -> None:
        assert gcf._client_id({}) == gcf.DEFAULT_CLIENT_ID
        assert gcf._client_id({"graph_client_id": "abc"}) == "abc"
        assert "organizations" in gcf._authority({})
        assert "contoso.edu" in gcf._authority({"graph_auth_tenant": "contoso.edu"})
