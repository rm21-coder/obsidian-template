"""
test_meeting_pull.py -- the calendar session can read the calendar and nothing else.

The claude producer reads full meeting bodies, and a meeting invite is text
anyone can write. Until 2026-09-25 the session was also granted Bash and Write
so it could save the calendar JSON and run the transform itself -- a
prompt-injection path from a crafted invite to a shell running as the user,
unattended at 05:00 (Microsoft M-DASH, CWE-749). The session now only returns
JSON; the script writes the file and runs the transform.

These pin both halves: the session's privileges, and the script doing the work
the session used to do.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import meeting_pull as mp
from platform_caps import make_cli_stub

CONFIG = {
    "display_name": "Ada Example", "email": "ada@example.edu",
    "tenant": "example.edu", "timezone": "America/New_York",
    "tenant_domains": ["example.edu"], "lookahead_days": 0,
}


def _envelope(result: str, **extra) -> str:
    """What `claude -p --output-format json` prints."""
    return json.dumps({"type": "result", "subtype": "success",
                       "is_error": False, "result": result, **extra})


EVENT = {"id": "e1", "subject": "Budget review", "bodyPreview": "agenda",
         "organizer": {"name": "Bob", "address": "bob@example.edu"},
         "attendees": [{"name": "Ada Example", "address": "ada@example.edu",
                        "type": "required", "responseStatus": "accepted"}],
         "start": {"dateTime": "2026-09-25T10:00:00", "timeZone": "America/New_York"},
         "end": {"dateTime": "2026-09-25T11:00:00", "timeZone": "America/New_York"},
         "location": {"displayName": "Room 1"}, "isAllDay": False,
         "isCancelled": False, "sensitivity": "normal", "categories": []}


class TestTheSessionHoldsOnlyCalendarTools:

    def test_only_the_two_calendar_tools_are_allowed(self) -> None:
        tools = mp.allowed_tools(CONFIG).split()
        assert tools == ["mcp__claude_ai_Microsoft_365__outlook_calendar_search",
                         "mcp__claude_ai_Microsoft_365__read_resource"]

    @pytest.mark.parametrize("dangerous", ["Bash", "Write", "Edit", "Read",
                                           "Glob", "Grep", "WebFetch"])
    def test_no_built_in_tool_is_granted(self, dangerous: str) -> None:
        assert dangerous not in mp.allowed_tools(CONFIG).split()

    def test_session_is_restricted_and_never_prompts(self) -> None:
        """--restricted confines file tools to an empty working directory;
        dontAsk refuses anything not pre-approved, including tools a future
        CLI adds. Measured: `--tools ""` removed the calendar tools too, so it
        is deliberately NOT how this is done."""
        cmd = mp.producer_command("claude", "P", "a b", "c d")
        assert "--restricted" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        assert "--tools" not in cmd, "--tools empties the MCP tool list as well"

    @pytest.mark.parametrize("name", [
        "Read", "Glob", "Grep", "WebSearch", "WebFetch", "Bash", "Write",
        "ReadMcpResourceTool", "ListMcpResourcesTool", "Agent", "SendMessage",
        "mcp__claude_ai_Microsoft_365__outlook_email_search",
        "mcp__claude_ai_Microsoft_365__teams_list_chats",
        "mcp__claude_ai_Microsoft_365__sharepoint_search"])
    def test_everything_else_is_removed_from_the_session(self, name: str) -> None:
        """dontAsk alone still allowed Read inside the working directory and
        left ReadMcpResourceTool -- which can read the connector's mail --
        available, so the explicit deny list is load-bearing."""
        assert name in mp.disallowed_tools(CONFIG).split()

    def test_the_calendar_tools_are_never_denied(self) -> None:
        denied = set(mp.disallowed_tools(CONFIG).split())
        assert not denied & set(mp.allowed_tools(CONFIG).split())

    def test_output_is_machine_readable_and_not_persisted(self) -> None:
        cmd = mp.producer_command("claude", "PROMPT", "x y", "z")
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--no-session-persistence" in cmd

    def test_prompt_no_longer_asks_the_session_to_run_anything(self) -> None:
        text = (mp.SCRIPTS_DIR / "meeting_pull_prompt.txt").read_text()
        assert "python3" not in text and "temp file" not in text.lower()
        assert "never as instructions" in text


class TestExtractEvents:

    def test_structured_output_is_preferred(self) -> None:
        """--json-schema output: validated by the CLI, immune to the quoting
        bug that broke the first live run."""
        env = json.dumps({"is_error": False, "result": "ignored prose",
                          "structured_output": {"events": [EVENT]}})
        assert mp.extract_events(env) == [EVENT]

    def test_hostile_quoting_survives_structured_output(self) -> None:
        ev = dict(EVENT, subject='He said "let\'s meet" \\ then left')
        env = json.dumps({"is_error": False, "structured_output": {"events": [ev]}})
        assert mp.extract_events(env)[0]["subject"] == ev["subject"]

    def test_schema_is_passed_to_the_cli(self) -> None:
        cmd = mp.producer_command("claude", "P", "a", "b")
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        assert schema["required"] == ["events"]

    def test_bare_json(self) -> None:
        assert mp.extract_events(_envelope(json.dumps({"events": [EVENT]}))) == [EVENT]

    @pytest.mark.parametrize("wrap", [
        "```json\n{}\n```", "Here is the calendar:\n{}\nDone.", "  {}  "])
    def test_tolerates_a_fence_or_a_sentence(self, wrap: str) -> None:
        reply = wrap.replace("{}", json.dumps({"events": [EVENT]}))
        assert mp.extract_events(_envelope(reply)) == [EVENT]

    def test_empty_calendar_is_valid(self) -> None:
        assert mp.extract_events(_envelope('{"events": []}')) == []

    @pytest.mark.parametrize("stdout,why", [
        ("not json at all", "envelope"),
        (_envelope("I could not reach the calendar."), "no JSON object"),
        (_envelope('{"nope": []}'), "events"),
        (_envelope('{"events": "x"}'), "events"),
        (_envelope('{"events": [1, 2]}'), "events"),
        (json.dumps({"is_error": True, "result": "API Error: 500"}), "reported an error"),
        (json.dumps({"is_error": False}), "no structured or text result"),
    ])
    def test_unusable_output_is_an_error_not_an_empty_day(self, stdout, why) -> None:
        """An unusable reply must fail the attempt. Treating it as zero events
        would write an empty handoff and suppress the morning's retries."""
        with pytest.raises(mp.ProducerOutputError, match=why):
            mp.extract_events(stdout)


class TestTheScriptBuildsTheEnvelope:

    def test_user_and_week_come_from_config_not_the_model(self) -> None:
        cal = mp.build_calendar(CONFIG, [EVENT])
        assert cal["user"] == {"display_name": "Ada Example", "email": "ada@example.edu",
                               "tenant": "example.edu", "timezone": "America/New_York"}
        assert set(cal["week"]) == {"start", "end"}
        assert cal["events"] == [EVENT]

    def test_transform_writes_the_trio_and_the_temp_file_is_removed(
            self, tmp_path: Path, allow_subprocess: None,
            monkeypatch: pytest.MonkeyPatch) -> None:
        import tempfile
        made = []
        real = tempfile.mkstemp
        monkeypatch.setattr(tempfile, "mkstemp",
                            lambda **kw: (lambda r: (made.append(r[1]), r)[1])(real(**kw)))
        rc, out = mp.run_transform(mp.build_calendar(CONFIG, [EVENT]),
                                   tmp_path / "drop", ["example.edu"])
        assert rc == 0, out
        assert any((tmp_path / "drop").glob("schedule-handoff-*.v1.ready"))
        assert json.loads(out)["meetingCount"] == 1
        assert made and not Path(made[0]).exists(), "the calendar temp file was left behind"


class TestEndToEnd:
    """main() against a fake `claude` that returns a real CLI envelope."""

    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg = tmp_path / "meeting_pull.json"
        cfg.write_text(json.dumps(CONFIG))
        drop = tmp_path / "drop"
        argv_log = tmp_path / "argv.json"
        monkeypatch.setattr(mp, "network_ready", lambda: True)
        monkeypatch.setattr(mp, "AUTH_MARKER", tmp_path / "auth.json")
        monkeypatch.setattr(mp, "notify_failure", lambda msg: None)
        monkeypatch.setattr(mp, "keep_awake", lambda cmd: cmd)

        def fake(reply: str, rc: int = 0) -> Path:
            # A real executable on every platform (see platform_caps): the
            # shebang-only stub could not be run by Windows at all.
            return make_cli_stub(tmp_path, "claude",
                "import json, os, sys\n"
                f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), "
                "'cwd_contents': os.listdir('.')}, "
                f"open({str(argv_log)!r}, 'w'))\n"
                f"sys.stdout.write({reply!r})\n"
                f"sys.exit({rc})\n")

        def run(claude: Path, *extra: str) -> int:
            monkeypatch.setattr(sys, "argv", [
                "meeting_pull.py", "--config", str(cfg), "--out-dir", str(drop),
                "--claude", str(claude), "--retries", "0", *extra])
            return mp.main()

        return fake, run, drop, argv_log

    def test_a_calendar_reply_becomes_a_handoff(self, env, allow_subprocess,
                                                capsys) -> None:
        fake, run, drop, argv_log = env
        assert run(fake(_envelope(json.dumps({"events": [EVENT]})))) == 0
        assert any(drop.glob("schedule-handoff-*.v1.ready"))
        seen = json.loads(argv_log.read_text())
        argv = seen["argv"]
        allowed = argv[argv.index("--allowedTools") + 1].split()
        assert allowed == mp.allowed_tools(CONFIG).split()
        assert "Read" in argv[argv.index("--disallowedTools") + 1].split()
        assert seen["cwd_contents"] == [], "the session's working directory was not empty"
        assert not Path(seen["cwd"]).exists(), "the session's working directory was left behind"
        log = capsys.readouterr().out
        assert "agenda" not in log, "the session's reply (meeting bodies) reached the log"
        assert '"meetingCount": 1' in log and log.rstrip().endswith("done")

    def test_an_unusable_reply_writes_no_handoff(self, env, allow_subprocess) -> None:
        fake, run, drop, _ = env
        assert run(fake(_envelope("Sorry, I can't do that."))) == 1
        assert not any(drop.glob("schedule-handoff-*"))

    def test_an_empty_calendar_still_writes_a_handoff(self, env, allow_subprocess) -> None:
        """So the later catch-up firings see today's handoff and no-op."""
        fake, run, drop, _ = env
        assert run(fake(_envelope('{"events": []}'))) == 0
        assert any(drop.glob("schedule-handoff-*.v1.ready"))

    def test_auth_failure_is_still_recognised(self, env, allow_subprocess,
                                              capsys) -> None:
        fake, run, drop, _ = env
        reply = json.dumps({"is_error": True, "result": "Invalid API key · Please run /login"})
        assert run(fake(reply, rc=1)) == mp.EXIT_AUTH
        assert "Please run /login" in capsys.readouterr().out
