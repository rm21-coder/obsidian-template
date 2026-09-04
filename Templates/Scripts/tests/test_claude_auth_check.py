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
    p = tmp_path / name
    p.write_text("#!/bin/sh\n"
                 f"cat >&2 <<'EOF'\n{stderr}\nEOF\n"
                 f"exit {code}\n", encoding="utf-8")
    p.chmod(0o755)
    return str(p)


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


def _run(mod, cli: str) -> int:
    import sys
    argv = sys.argv
    sys.argv = ["claude_auth_check.py", "--claude", cli]
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
