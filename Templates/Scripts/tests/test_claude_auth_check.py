"""claude_auth_check: warm the CLI session, and warn while someone can act.

Written after a morning where the producer failed nine times on an expired
session and the operator learned about it from a dashboard three hours later.
The check moves that discovery to the evening before.

The assertion that matters is the one separating the two failure modes this
pipeline actually has. The log shows both: authentication (Aug 19, Sep 4) and
no route at the scheduled wake (Aug 20). Calling the second one "auth" sends
someone to re-login over a Wi-Fi association delay, and calling the first one
"network" leaves them waiting for a retry that can never succeed. So every
test here is really about which of those two answers comes out.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from platform_caps import make_cli_stub

SCRIPTS = Path(__file__).resolve().parent.parent
CHECKER = SCRIPTS / "claude_auth_check.py"


def load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Import the checker with its marker redirected into tmp_path.

    STATE_DIR/MARKER resolve next to the script, so without this a test run
    would clobber the operator's real auth state -- and a stale "ok" written
    by a test is exactly the warning this tool exists to raise."""
    spec = importlib.util.spec_from_file_location("cac", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "MARKER", tmp_path / "claude_auth_state.json")
    monkeypatch.setattr(mod, "notify", lambda *_a, **_k: None)
    return mod


def fake_cli(tmp_path: Path, name: str, stderr: str, code: int) -> str:
    # A real executable on every platform (platform_caps.make_cli_stub): the
    # old #!/bin/sh stub could not run on Windows at all.
    source = ("import sys\n"
              f"sys.stderr.write({stderr!r} + '\\n')\n"
              f"sys.exit({code})\n")
    return str(make_cli_stub(tmp_path, name, source))


@pytest.fixture
def online(monkeypatch, tmp_path):
    """A checker that believes it has a route, so the probe is what is tested."""
    m = load(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "network_ready", lambda *_a, **_k: True)
    return m


class TestSignatureSeparation:
    """Which of the two answers comes out. The whole point of the tool."""

    def test_auth_failure_is_named_auth(self, online, tmp_path,
                                        allow_subprocess) -> None:
        cli = fake_cli(tmp_path, "c-auth",
                       "Failed to authenticate: OAuth session expired and "
                       "could not be refreshed", 1)
        rc = _run(online, cli)
        assert rc == online.EXIT_AUTH
        assert json.loads(online.MARKER.read_text())["state"] == "auth-failed"

    def test_network_failure_is_not_named_auth(self, online, tmp_path,
                                               allow_subprocess) -> None:
        """Aug 20's actual failure. Reporting it as auth would send the
        operator to re-login for something a later firing cleared by itself."""
        cli = fake_cli(tmp_path, "c-net",
                       "API Error: Can't reach the API server (ENOTFOUND)", 1)
        rc = _run(online, cli)
        assert rc == online.EXIT_UNKNOWN
        assert not online.MARKER.exists()

    @pytest.mark.parametrize("line", [
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "Not logged in - Please run /login",
        "API Error: authentication_error",
        "Error: invalid_api_key",
    ])
    def test_known_auth_signatures(self, online, tmp_path, line,
                                   allow_subprocess) -> None:
        rc = _run(online, fake_cli(tmp_path, "c", line, 1))
        assert rc == online.EXIT_AUTH

    @pytest.mark.parametrize("line", [
        "API Error: Can't reach the API server (ENOTFOUND)",
        "Your computer went to sleep mid-response",
        "Error: model not found",
    ])
    def test_other_failures_stay_unknown(self, online, tmp_path, line,
                                         allow_subprocess) -> None:
        rc = _run(online, fake_cli(tmp_path, "c", line, 1))
        assert rc == online.EXIT_UNKNOWN


def _run(mod, cli: str, *extra: str) -> int:
    import sys
    argv = sys.argv
    sys.argv = ["claude_auth_check.py", "--claude", cli, *extra]
    try:
        return mod.main()
    finally:
        sys.argv = argv


class TestMarkerHandling:

    def test_success_records_ok(self, online, tmp_path, allow_subprocess) -> None:
        cli = fake_cli(tmp_path, "c-ok", "", 0)
        assert _run(online, cli) == online.EXIT_OK
        assert json.loads(online.MARKER.read_text())["state"] == "ok"

    def test_recovery_is_noticed(self, online, tmp_path, capsys,
                                 allow_subprocess) -> None:
        """Saying "working again" is worth a line: it closes a warning the
        operator has been carrying since the night before."""
        online.MARKER.write_text(json.dumps({"state": "auth-failed"}),
                                 encoding="utf-8")
        _run(online, fake_cli(tmp_path, "c-ok", "", 0))
        assert "working again" in capsys.readouterr().out

    def test_no_route_does_not_overwrite_a_warning(self, monkeypatch, tmp_path,
                                                   allow_subprocess) -> None:
        """The important one. A later run with no route must not downgrade a
        real "needs re-auth" to "unknown" -- that erases the warning while
        the problem is still there."""
        m = load(monkeypatch, tmp_path)
        m.MARKER.write_text(json.dumps({"state": "auth-failed",
                                        "detail": "expired"}), encoding="utf-8")
        monkeypatch.setattr(m, "network_ready", lambda *_a, **_k: False)
        assert _run(m, "/nonexistent/claude-would-not-be-reached") in (
            m.EXIT_UNKNOWN,)
        assert json.loads(m.MARKER.read_text())["state"] == "auth-failed"

    def test_missing_cli_is_unknown_not_auth(self, monkeypatch, tmp_path) -> None:
        m = load(monkeypatch, tmp_path)
        monkeypatch.setattr(m, "find_claude", lambda _o: None)
        assert _run(m, "ignored") == m.EXIT_UNKNOWN


class TestNetworkProbe:

    def test_unresolvable_host_is_not_ready(self, monkeypatch, tmp_path,
                                            allow_external_dns) -> None:
        m = load(monkeypatch, tmp_path)
        assert m.network_ready("nx-does-not-exist.invalid") is False


class TestSignatureParity:
    """The two scripts must agree on what an auth failure looks like."""

    def test_producer_and_checker_share_the_pattern(self, monkeypatch,
                                                    tmp_path) -> None:
        """If these drift, the evening check reports healthy on exactly the
        mornings the producer dies -- the one outcome that would make this
        tool worse than nothing."""
        m = load(monkeypatch, tmp_path)
        spec = importlib.util.spec_from_file_location(
            "mp", SCRIPTS / "meeting_pull.py")
        mp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mp)
        assert m.AUTH_FAILURE_RE.pattern == mp.AUTH_FAILURE_RE.pattern


class TestProducerNetworkGate:
    """The gate in meeting_pull.py must fail OPEN."""

    def _producer(self):
        spec = importlib.util.spec_from_file_location(
            "mp2", SCRIPTS / "meeting_pull.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_a_broken_helper_does_not_stop_the_pull(self, monkeypatch) -> None:
        """The gate is an optimisation. The one thing worse than a wasted
        05:00 attempt is a 05:00 attempt that never happens because a helper
        raised."""
        mp = self._producer()
        import builtins
        real = builtins.__import__

        def boom(name, *a, **k):
            if name == "claude_auth_check":
                raise ImportError("simulated")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", boom)
        assert mp.network_ready() is True

    def test_exit_codes_are_distinct(self) -> None:
        """Each code means a different next action, so collapsing any two
        loses the distinction the dashboard reads."""
        mp = self._producer()
        assert len({0, 1, mp.EXIT_AUTH, mp.EXIT_NO_NETWORK}) == 4


class TestSignInReminder:
    """The session's lifetime is absolute (expired 2026-10-02 and 11-08, about
    28 and 35 days after a sign-in), so warming cannot save it. What can is a
    reminder a few days ahead, which needs the sign-in date to survive every
    ordinary evening check."""

    MON = __import__("datetime").date(2026, 12, 7)      # a Monday
    FRI = __import__("datetime").date(2026, 12, 4)

    def _marker(self, signed_in, state="ok"):
        return {"state": state, "signed_in": signed_in}

    def test_not_due_before_the_threshold(self, online) -> None:
        since = self.MON - __import__("datetime").timedelta(days=online.REMIND_DAYS - 1)
        assert online.signin_reminder(self._marker(since.isoformat()), self.MON) is None

    def test_due_at_the_threshold_names_the_age_and_the_fix(self, online) -> None:
        since = self.MON - __import__("datetime").timedelta(days=online.REMIND_DAYS)
        msg = online.signin_reminder(self._marker(since.isoformat()), self.MON)
        assert msg.startswith("Claude sign-in is %d days old" % online.REMIND_DAYS)
        assert "--signed-in" in msg

    def test_friday_warns_when_the_threshold_falls_on_the_weekend(self, online) -> None:
        """Both expiries so far landed between Friday and Monday."""
        since = self.FRI - __import__("datetime").timedelta(days=online.REMIND_DAYS - 3)
        assert online.signin_reminder(self._marker(since.isoformat()), self.FRI) \
            .startswith("Claude sign-in is %d days old" % (online.REMIND_DAYS - 3))
        thursday = self.FRI - __import__("datetime").timedelta(days=1)
        assert online.signin_reminder(self._marker(since.isoformat()), thursday) is None

    def test_a_failed_session_gets_no_reminder(self, online) -> None:
        """It already has the louder "sign in now" warning."""
        assert online.signin_reminder(
            self._marker("2026-01-01", state="auth-failed"), self.MON) is None

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", 20261109])
    def test_no_usable_date_gives_no_reminder(self, online, raw) -> None:
        assert online.signin_reminder(self._marker(raw), self.MON) is None

    def test_recovery_records_the_sign_in(self, online, tmp_path, capsys,
                                          allow_subprocess) -> None:
        import datetime
        online.MARKER.write_text(json.dumps({"state": "auth-failed"}),
                                 encoding="utf-8")
        _run(online, fake_cli(tmp_path, "c-ok", "", 0))
        data = json.loads(online.MARKER.read_text())
        assert data["signed_in"] == datetime.date.today().isoformat()
        assert "sign-in recorded as" in capsys.readouterr().out

    def test_an_ordinary_check_keeps_the_recorded_date(self, online, tmp_path,
                                                       allow_subprocess) -> None:
        online.MARKER.write_text(json.dumps(self._marker("2026-11-09")),
                                 encoding="utf-8")
        _run(online, fake_cli(tmp_path, "c-ok", "", 0))
        assert json.loads(online.MARKER.read_text())["signed_in"] == "2026-11-09"

    def test_an_auth_failure_keeps_the_recorded_date(self, online, tmp_path,
                                                     allow_subprocess) -> None:
        online.MARKER.write_text(json.dumps(self._marker("2026-11-09")),
                                 encoding="utf-8")
        _run(online, fake_cli(tmp_path, "c-auth", "OAuth session expired", 1))
        data = json.loads(online.MARKER.read_text())
        assert data["state"] == "auth-failed" and data["signed_in"] == "2026-11-09"

    def test_signed_in_flag_records_a_given_date(self, online, tmp_path,
                                                 allow_subprocess) -> None:
        _run(online, fake_cli(tmp_path, "c-ok", "", 0), "--signed-in", "2026-11-09")
        assert json.loads(online.MARKER.read_text())["signed_in"] == "2026-11-09"

    def test_signed_in_flag_is_not_recorded_when_the_probe_fails(
            self, online, tmp_path, capsys, allow_subprocess) -> None:
        _run(online, fake_cli(tmp_path, "c-auth", "OAuth session expired", 1),
             "--signed-in")
        assert "signed_in" not in json.loads(online.MARKER.read_text())
        assert "--signed-in ignored: the sign-in has not taken." in capsys.readouterr().out

    @pytest.mark.parametrize("bad, marker", [
        ("2026-13-40", "wants a date as YYYY-MM-DD"),
        ("2999-01-01", "is in the future; nothing recorded"),
    ])
    def test_signed_in_flag_rejects_bad_dates(self, online, tmp_path, capsys,
                                              bad, marker) -> None:
        assert _run(online, "ignored", "--signed-in", bad) == online.EXIT_UNKNOWN
        assert marker in capsys.readouterr().out
        assert not online.MARKER.exists()

    def test_a_due_reminder_is_logged_and_still_exits_ok(
            self, online, tmp_path, capsys, allow_subprocess) -> None:
        """The session works, so the exit code must not call it a failure."""
        online.MARKER.write_text(json.dumps(self._marker("2026-01-01")),
                                 encoding="utf-8")
        assert _run(online, fake_cli(tmp_path, "c-ok", "", 0)) == online.EXIT_OK
        assert "REMINDER: Claude sign-in is" in capsys.readouterr().out
