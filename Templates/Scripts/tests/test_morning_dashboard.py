"""
test_morning_dashboard.py — the dashboard's job-health diagnosis.

These cover the helpers that decide WHY a pipeline job is unhealthy. They
exist because of one morning the dashboard got that wrong: an always-on job
was crash-looping, and the dashboard reported "loaded but not running" and
then guessed at a cause -- that the job's venv, shared with the tagger,
probably needed rebuilding. The venv was perfectly healthy. The real cause was
sitting in two places nobody was reading: `launchctl print` said runs = 7152
with last exit code = 0 (a program returning immediately, over and over), and
the tail of the job's stderr said "gateway host ... did not resolve", i.e. the
laptop was off the VPN. Nothing needed inferring; it needed reading.

So the rule these tests enforce is that a cause is reported only after it has
been checked, and that not knowing is reported as not knowing. A confident
wrong answer sends someone at the wrong component, which costs more than
silence.

_plist_load is here for a related failure: reporting a real state as an
absence. plistlib is stricter than launchd, so a perfectly loadable agent
could vanish from the section entirely, announced only by a warning on a
stderr log that nobody reads.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import morning_dashboard as md


ON_MACOS = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# _plist_load -- agree with launchd about what is loadable.
# ---------------------------------------------------------------------------

# plutil and launchd accept both of these; plistlib (expat) rejects both.
# The continuation is what Pulse Secure's installer actually writes -- given
# the vault marker here because the lenient fallback is reserved for our own
# plists, a real third-party one being covered by the silence test below.
VAULT_DOCTYPE_CONTINUATION = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" \\
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>com.example.voice-cleanup</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/Users/someone/Obsidian/Templates/Scripts/.venv/bin/python3</string>
\t\t<string>/Users/someone/Obsidian/Templates/Scripts/voice_cleanup.py</string>
\t</array>
</dict>
</plist>
"""

DOUBLE_DASH_COMMENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- installed by hand -- see the setup guide -->
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>com.example.ours</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/Users/someone/Obsidian/Templates/Scripts/.venv/bin/python3</string>
\t\t<string>/Users/someone/Obsidian/Templates/Scripts/thing.py</string>
\t</array>
</dict>
</plist>
"""


def test_a_well_formed_plist_is_parsed_by_the_strict_parser(tmp_path):
    p = tmp_path / "good.plist"
    p.write_text(DOUBLE_DASH_COMMENT.replace(
        "<!-- installed by hand -- see the setup guide -->", ""),
        encoding="utf-8")

    assert md._plist_load(p)["Label"] == "com.example.ours"


@pytest.mark.skipif(not ON_MACOS, reason="plutil is macOS-only")
def test_a_doctype_continuation_is_parsed_by_the_fallback(tmp_path,
                                                          allow_subprocess):
    """The Pulse Secure case: expat says no, launchd says fine.

    Before the fallback existed this returned None, which drops the job from
    pipeline health -- a loaded, running agent reported as absent.
    """
    p = tmp_path / "com.example.voice-cleanup.plist"
    p.write_text(VAULT_DOCTYPE_CONTINUATION, encoding="utf-8")

    # Confirm the premise rather than assuming it.
    import plistlib
    with pytest.raises(Exception):
        with p.open("rb") as fh:
            plistlib.load(fh)

    assert md._plist_load(p)["Label"] == "com.example.voice-cleanup"


@pytest.mark.skipif(not ON_MACOS, reason="plutil is macOS-only")
def test_a_double_dash_comment_is_parsed_by_the_fallback(tmp_path,
                                                         allow_subprocess):
    """The cause the original docstring named, now actually handled."""
    p = tmp_path / "com.example.ours.plist"
    p.write_text(DOUBLE_DASH_COMMENT, encoding="utf-8")

    assert md._plist_load(p)["Label"] == "com.example.ours"


def test_an_unparseable_third_party_plist_is_silent(tmp_path, monkeypatch,
                                                    capsys):
    """No warning for an agent that was never going to be in this section.

    ~/Library/LaunchAgents is shared with every other installer on the
    machine. Telling the operator a VPN helper is "missing from pipeline
    health" is noise, and noise is what gets a warning ignored when it
    finally matters. It must also cost nothing: the fallback parser is
    reserved for our own plists, so this must not spawn one process per
    third-party agent on every run.
    """
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: pytest.fail("should not shell out"))
    p = tmp_path / "net.vendor.thing.plist"
    p.write_text("this is not a plist at all", encoding="utf-8")

    assert md._plist_load(p) is None
    assert capsys.readouterr().err == ""


def test_an_unparseable_vault_plist_is_reported(tmp_path, monkeypatch, capsys):
    """One of ours failing to parse is worth saying out loud."""
    class Failed:
        returncode = 1
        stdout = b""
        stderr = b"plutil: unrecognised format"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Failed())
    p = tmp_path / "com.obsidian.ours.plist"
    p.write_text(f"garbage {md.PIPELINE_MARKER} garbage", encoding="utf-8")

    assert md._plist_load(p) is None
    err = capsys.readouterr().err
    assert "com.obsidian.ours.plist" in err
    assert "missing from pipeline health" in err


def test_the_report_survives_the_exception_being_unbound(tmp_path, monkeypatch,
                                                         capsys):
    """Python unbinds an `except ... as` target at the end of its block.

    The strict parser's error is captured under a second name for exactly
    this reason; referencing the original would raise NameError here and lose
    the whole diagnosis.
    """
    class Failed:
        returncode = 1
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Failed())
    p = tmp_path / "com.obsidian.ours.plist"
    p.write_text(f"garbage {md.PIPELINE_MARKER}", encoding="utf-8")

    md._plist_load(p)

    err = capsys.readouterr().err
    assert "NameError" not in err
    # Both parsers' verdicts reach the operator.
    assert "plutil also failed" in err


def test_a_missing_plist_is_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: pytest.fail("should not shell out"))

    assert md._plist_load(tmp_path / "nope.plist") is None
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# _launchctl_print -- the crash-loop signature.
# ---------------------------------------------------------------------------

# Trimmed from real output. The nested `state = active` lines are the trap:
# they belong to the coalitions, not the job, and an unanchored pattern reads
# one of them as the job's state.
PRINT_OUTPUT = """gui/503/com.voice-cleanup = {
\tactive count = 0
\tpath = /Users/someone/Library/LaunchAgents/com.voice-cleanup.plist
\tstate = spawn scheduled

\tprogram = /Users/someone/Obsidian/Templates/Scripts/.venv/bin/python3
\tminimum runtime = 10
\texit timeout = 5
\truns = 7152
\tlast exit code = 0

\tresource coalition = {
\t\tID = 1360
\t\tstate = active
\t\tactive count = 1
\t}
}
"""


def _fake_print(out: str, rc: int = 0):
    class Result:
        returncode = rc
        stdout = out.encode()
        stderr = b""
    return lambda *a, **kw: Result()


def test_print_parses_the_crash_loop_signature(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_print(PRINT_OUTPUT))

    got = md._launchctl_print("com.voice-cleanup")

    assert got["runs"] == 7152
    assert got["exit"] == 0
    assert got["min_runtime"] == 10


def test_print_is_not_fooled_by_nested_state_lines(monkeypatch):
    """The job's state, not a coalition's."""
    monkeypatch.setattr(subprocess, "run", _fake_print(PRINT_OUTPUT))

    assert md._launchctl_print("com.voice-cleanup")["state"] == "spawn scheduled"


def test_a_never_run_job_reports_none_not_zero(monkeypatch):
    """"Never exited" and "exited 0" are different facts.

    Defaulting a missing field to 0 would invent a clean exit for a job that
    has never run, which is the input the crash-loop test keys on.
    """
    monkeypatch.setattr(subprocess, "run", _fake_print(
        "gui/503/com.example = {\n\tstate = running\n\truns = 1\n}\n"))

    got = md._launchctl_print("com.example")

    assert got["runs"] == 1
    assert got["exit"] is None


def test_print_failure_yields_all_unknown(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_print("", rc=1))

    assert md._launchctl_print("com.nope") == {
        "state": None, "runs": None, "exit": None, "min_runtime": None}


def test_print_survives_the_binary_being_absent(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("no launchctl here")

    monkeypatch.setattr(subprocess, "run", boom)

    assert md._launchctl_print("com.example")["runs"] is None


# ---------------------------------------------------------------------------
# cli_auth_defect -- the one cause that cannot clear itself.
#
# Exists because last_error_line() provably misses it: the producer wrapper
# logs its own summary after the CLI speaks, so on 2026-09-04 the dashboard
# quoted "producer exited 1 - no handoff written" while the line above it
# named both the cause and the fix.
# ---------------------------------------------------------------------------

def test_an_expired_session_is_found_under_the_wrappers_summary(tmp_path):
    """The exact shape of the log that was misdiagnosed: the CLI's message,
    then the wrapper's less informative last word on top of it."""
    log = tmp_path / "meeting-pull.log"
    log.write_text(textwrap.dedent("""\
        2026-09-04 06:32:02 meeting_pull: starting (attempt 3/3)
        Failed to authenticate: OAuth session expired and could not be refreshed
        2026-09-04 06:32:03 meeting_pull: producer exited 1 - no handoff written
        """), encoding="utf-8")

    verdict = md.cli_auth_defect(str(log))
    assert verdict is not None
    assert "OAuth session expired" in verdict
    # The generic signal picks the symptom, which is the whole point.
    assert "producer exited 1" in md.last_error_line(str(log))


def test_the_verdict_names_the_human_step(tmp_path):
    """A scheduled job can never sign itself in, so a diagnosis that does not
    say what the operator must do is useless here."""
    log = tmp_path / "j.log"
    log.write_text("Not logged in - Please run /login\n", encoding="utf-8")

    verdict = md.cli_auth_defect(str(log))
    assert "/login" in verdict and "claude" in verdict
    assert verdict.startswith("Verified:")


@pytest.mark.parametrize("line", [
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "Not logged in - Please run /login",
    "API Error: authentication_error",
    "Error: invalid_api_key",
    "HTTP 403 Unauthorized",
])
def test_known_signatures_are_recognized(tmp_path, line):
    log = tmp_path / "j.log"
    log.write_text(line + "\n", encoding="utf-8")
    assert md.cli_auth_defect(str(log)) is not None


@pytest.mark.parametrize("line", [
    "API Error: Can't reach the API server (ENOTFOUND)",
    "Your computer went to sleep mid-response",
    "2026-09-03 05:01:15 meeting_pull: done",
    "gateway host api.ai.example.edu did not resolve",
])
def test_other_failures_are_not_called_auth(tmp_path, line):
    """The expensive direction. Naming auth on a transient network failure
    sends the operator to re-login for a problem a retry would have cleared,
    and this signal deliberately outranks the generic one."""
    log = tmp_path / "j.log"
    log.write_text(line + "\n", encoding="utf-8")
    assert md.cli_auth_defect(str(log)) is None


def test_a_clean_or_missing_log_reports_nothing(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    assert md.cli_auth_defect(str(empty)) is None
    assert md.cli_auth_defect(str(tmp_path / "absent.log")) is None


def test_the_hint_does_not_name_a_cause():
    """The rule stated above PIPELINE_HINTS, asserted rather than trusted:
    this hint carried two guessed causes in the same file as the rule
    forbidding them."""
    hint = md.PIPELINE_HINTS["com.meeting-pull"]
    for guess in ("re-auth", "Usual causes", "no longer match"):
        assert guess not in hint, f"hint names a cause: {guess!r}"
    assert "meeting-pull.log" in hint


# ---------------------------------------------------------------------------
# last_error_line -- read the log instead of telling someone to read it.
# ---------------------------------------------------------------------------

def test_the_last_distinct_line_is_the_diagnosis(tmp_path):
    """A crash-looping job repeats one line thousands of times."""
    log = tmp_path / "job.err"
    log.write_text(
        ("10:00:00 [WARNING] Skipped: gateway host api.ai.example.edu did not "
         "resolve\n") * 500, encoding="utf-8")

    assert "did not resolve" in md.last_error_line(str(log))


def test_traceback_scaffolding_is_skipped_for_the_exception(tmp_path):
    """The frame lines and the caret name nothing; the last line does."""
    log = tmp_path / "job.err"
    log.write_text(textwrap.dedent("""\
        Traceback (most recent call last):
          File "/x/voice_cleanup.py", line 249, in <module>
            main()
            ~~~~^^
        anthropic.NotFoundError: Error code: 404 - model: claude-old
        """), encoding="utf-8")

    assert md.last_error_line(str(log)).startswith("anthropic.NotFoundError")


def test_a_huge_log_is_not_read_whole(tmp_path):
    """The log that prompted this was 19MB."""
    log = tmp_path / "job.err"
    filler = "x" * 999 + "\n"
    with log.open("w", encoding="utf-8") as fh:
        for _ in range(400):
            fh.write(filler)
        fh.write("the actual last error\n")

    assert log.stat().st_size > 128 * 1024
    assert md.last_error_line(str(log)) == "the actual last error"


def test_a_long_line_is_truncated(tmp_path):
    log = tmp_path / "job.err"
    log.write_text("E" * 5000 + "\n", encoding="utf-8")

    assert len(md.last_error_line(str(log))) <= 300


def test_an_empty_or_missing_log_yields_none(tmp_path):
    empty = tmp_path / "empty.err"
    empty.write_text("\n  \n\n", encoding="utf-8")

    assert md.last_error_line(str(empty)) is None
    assert md.last_error_line(str(tmp_path / "absent.err")) is None


# ---------------------------------------------------------------------------
# venv_defect -- only a checked defect, never an inferred one.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_venv_cache():
    """The verdict is memoised per interpreter for the life of a run."""
    md._VENV_VERDICT.clear()
    yield
    md._VENV_VERDICT.clear()


def test_a_missing_interpreter_is_a_verified_defect(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: pytest.fail("nothing to probe"))

    verdict = md.venv_defect(str(tmp_path / ".venv" / "bin" / "python3"))

    assert verdict and verdict.startswith("Verified:")
    assert "does not exist" in verdict


def test_an_interpreter_that_cannot_import_the_sdk_is_a_defect(tmp_path,
                                                               monkeypatch):
    interp = tmp_path / "python3"
    interp.write_text("#!/bin/sh\n", encoding="utf-8")

    class Failed:
        returncode = 1
        stdout = b""
        stderr = b"ModuleNotFoundError: No module named 'anthropic'"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Failed())

    verdict = md.venv_defect(str(interp))

    assert verdict and "cannot import" in verdict


def test_a_healthy_venv_reports_nothing(tmp_path, monkeypatch):
    """The whole point: a working venv must never be named as the cause."""
    interp = tmp_path / "python3"
    interp.write_text("#!/bin/sh\n", encoding="utf-8")

    class Ok:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Ok())

    assert md.venv_defect(str(interp)) is None


def test_a_probe_that_fails_for_its_own_reasons_reports_nothing(tmp_path,
                                                                monkeypatch):
    """Not knowing is reported as not knowing, not as a defect."""
    interp = tmp_path / "python3"
    interp.write_text("#!/bin/sh\n", encoding="utf-8")

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="python3", timeout=30)

    monkeypatch.setattr(subprocess, "run", timeout)

    assert md.venv_defect(str(interp)) is None


def test_the_verdict_is_probed_once_per_interpreter(tmp_path, monkeypatch):
    """A dozen jobs share one venv; the probe costs a subprocess."""
    interp = tmp_path / "python3"
    interp.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = {"n": 0}

    class Ok:
        returncode = 0
        stdout = b""
        stderr = b""

    def counted(*a, **kw):
        calls["n"] += 1
        return Ok()

    monkeypatch.setattr(subprocess, "run", counted)

    md.venv_defect(str(interp))
    md.venv_defect(str(interp))

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# The endpoint-config reader -- an allowlist, over a file full of secrets.
# ---------------------------------------------------------------------------

def test_only_endpoint_keys_are_copied_out_of_the_env_file(tmp_path,
                                                           monkeypatch):
    """~/dev/secrets/.env also holds credentials.

    Nothing here should pull a secret into the dashboard's environment just to
    learn whether a hostname resolves.
    """
    env = tmp_path / ".env"
    env.write_text(
        "LLM_BASE_URL=https://api.ai.example.edu\n"
        "LLM_API_KEY_NAME=SOME_KEY_NAME\n"
        "ANTHROPIC_API_KEY=REDACTED-must-not-be-read\n"
        "SOURCE_MAIL_APP_PASSWORD=hunter2\n",
        encoding="utf-8")
    monkeypatch.setattr(md, "SECRETS_ENV", env)
    for key in ("LLM_BASE_URL", "LLM_API_KEY_NAME", "ANTHROPIC_API_KEY",
                "SOURCE_MAIL_APP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    md._load_endpoint_env()

    assert os.environ["LLM_BASE_URL"] == "https://api.ai.example.edu"
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "SOURCE_MAIL_APP_PASSWORD" not in os.environ


def test_an_existing_environment_value_wins(tmp_path, monkeypatch):
    """Matches load_dotenv's default, so an override stays an override."""
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=https://from-the-file.example.edu\n",
                   encoding="utf-8")
    monkeypatch.setattr(md, "SECRETS_ENV", env)
    monkeypatch.setenv("LLM_BASE_URL", "https://from-the-shell.example.edu")

    md._load_endpoint_env()

    assert os.environ["LLM_BASE_URL"] == "https://from-the-shell.example.edu"


def test_a_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    """A fresh clone has no ~/dev/secrets/.env; the check degrades to silence."""
    monkeypatch.setattr(md, "SECRETS_ENV", tmp_path / "absent" / ".env")

    md._load_endpoint_env()  # must not raise
