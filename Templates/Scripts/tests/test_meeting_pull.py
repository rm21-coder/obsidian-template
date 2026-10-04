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

    @pytest.mark.parametrize("key,value", [
        ("search_tool", "outlook_calendar_search Read"),
        ("read_tool", "read_resource\tBash"),
        ("mcp_prefix", "mcp__x Write"),
        ("search_tool", ["x"]),
    ])
    def test_a_tool_name_that_would_split_into_two_is_refused(
            self, key: str, value, capsys: pytest.CaptureFixture) -> None:
        """The names are joined into --allowedTools and split back out of the
        deny list: "x Read" would allow Read and drop it from the deny list
        (review, 2026-10-03). Every use of the setting refuses it."""
        for build in (mp.allowed_tools, mp.disallowed_tools):
            with pytest.raises(SystemExit):
                build({**CONFIG, key: value})
            assert f"ERROR: config {key!r} must be a single tool-name part" in capsys.readouterr().out

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

    def test_a_config_naming_the_removed_graph_producer_is_refused(
            self, env, tmp_path, allow_subprocess, capsys) -> None:
        """graph_calendar_fetch.py left the template on 2026-09-30. A config
        still asking for it must stop and say so, not quietly run claude."""
        fake, run, drop, argv_log = env
        (tmp_path / "meeting_pull.json").write_text(json.dumps({**CONFIG, "producer": "graph"}))
        with pytest.raises(SystemExit) as exc:
            run(fake(_envelope(json.dumps({"events": [EVENT]}))))
        assert exc.value.code == 1
        assert "'graph' producer (graph_calendar_fetch.py) was removed" in capsys.readouterr().out
        assert not argv_log.exists(), "the claude session ran anyway"
        assert not any(drop.glob("schedule-handoff-*"))

    def test_an_explicit_claude_producer_still_runs(self, env, tmp_path,
                                                    allow_subprocess) -> None:
        fake, run, drop, _ = env
        (tmp_path / "meeting_pull.json").write_text(json.dumps({**CONFIG, "producer": "claude"}))
        assert run(fake(_envelope('{"events": []}'))) == 0
        assert any(drop.glob("schedule-handoff-*.v1.ready"))

    def test_auth_failure_is_still_recognised(self, env, allow_subprocess,
                                              capsys) -> None:
        fake, run, drop, _ = env
        reply = json.dumps({"is_error": True, "result": "Invalid API key · Please run /login"})
        assert run(fake(reply, rc=1)) == mp.EXIT_AUTH
        assert "Please run /login" in capsys.readouterr().out


# What the CLI printed on a colleague's Windows install, 2026-10-01, when the
# tenant does not allow Claude Code to sign in with a subscription.
ORG_POLICY_REPLY = json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 403, "api_error_code": "oauth_not_allowed_for_organization",
    "result": "Your organization has disabled Claude subscription access for "
              "Claude Code \u00b7 Use an Anthropic API key instead, or ask your "
              "admin to enable access"})


class TestWindowsFindings:
    """Two failures found on a colleague's Windows machine, 2026-10-01."""

    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        return TestEndToEnd.env.__wrapped__(self, tmp_path, monkeypatch)

    def test_a_config_written_with_a_byte_order_mark_still_loads(
            self, env, tmp_path, allow_subprocess) -> None:
        # Windows PowerShell 5.1's Set-Content -Encoding utf8 wrote every
        # installer config this way; json.loads rejected it at char 0.
        fake, run, drop, _ = env
        (tmp_path / "meeting_pull.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps(CONFIG).encode("utf-8"))
        assert run(fake(_envelope('{"events": []}'))) == 0
        assert any(drop.glob("schedule-handoff-*.v1.ready"))

    def test_an_org_policy_refusal_stops_at_once_with_its_own_advice(
            self, env, tmp_path, allow_subprocess, capsys) -> None:
        fake, run, drop, argv_log = env
        claude = fake(ORG_POLICY_REPLY, rc=1)
        # --retry-delay 0: if the refusal were treated as retryable, fail in
        # seconds rather than sleeping a minute per retry.
        assert run(claude, "--retries", "2", "--retry-delay", "0") == mp.EXIT_AUTH
        out = capsys.readouterr().out
        assert "attempt 1/3" in out and "attempt 2/3" not in out, "it retried"
        fatal = next(ln for ln in out.splitlines() if "FATAL" in ln)
        assert "does not allow Claude Code to sign in with a Claude subscription" in fatal
        assert "/login" not in fatal, "gave the expired-session advice for a tenant policy"
        assert mp.auth_block_active() == "org-policy"

    def test_the_org_policy_block_makes_later_firings_cheap_no_ops(
            self, env, tmp_path, allow_subprocess, capsys) -> None:
        fake, run, drop, argv_log = env
        claude = fake(ORG_POLICY_REPLY, rc=1)
        run(claude)
        argv_log.unlink()
        capsys.readouterr()
        assert run(claude, "--skip-if-fresh") == mp.EXIT_AUTH
        assert not argv_log.exists(), "a later firing started the CLI again"
        assert "earlier today the Claude CLI was refused because" in capsys.readouterr().out

    def test_a_marker_from_before_the_kinds_reads_as_an_expired_session(
            self, env, tmp_path) -> None:
        import datetime as _dt
        (tmp_path / "auth.json").write_text(json.dumps(
            {"date": _dt.date.today().isoformat(), "detail": "x"}))
        assert mp.auth_block_active() == "session"

    def test_the_cli_reply_is_decoded_as_utf8(self, env, monkeypatch,
                                             allow_subprocess) -> None:
        # Without encoding="utf-8", Windows decodes the reply in the ANSI code
        # page ("\u00b7" became "\u00c2\u00b7"), which can also defeat a signature match.
        fake, run, drop, _ = env
        seen: list[dict] = []
        real = mp.subprocess.run

        def spy(cmd, *a, **kw):
            seen.append(kw)
            return real(cmd, *a, **kw)
        monkeypatch.setattr(mp.subprocess, "run", spy)
        run(fake(_envelope('{"events": []}')))
        producer = seen[0]
        assert producer.get("encoding") == "utf-8"
        assert producer.get("errors") == "replace"

class TestTheLogIsWhereTheNotificationSays:
    """The failure notification names a log file, so that file has to exist
    on the platform the notification fires on.

    On Windows, Task Scheduler discards stdout, and the notification used to
    tell Windows users to open ~/Library/Logs/meeting-pull.log -- a macOS path,
    pointing at a file that was never written on any Windows machine. The task
    now runs under run_logged.py, which writes the log; this names it.
    """

    def test_windows_log_lives_under_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mp.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert mp.log_path() == tmp_path / "obsidian-logs" / "meeting-pull.log"

    def test_windows_log_is_not_inside_the_watched_security_dir(self, monkeypatch, tmp_path):
        # The integrity monitor watches obsidian-security; a log growing there
        # would raise a drift alert every weekday morning.
        monkeypatch.setattr(mp.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert "obsidian-security" not in mp.log_path().parts

    def test_macos_log_is_the_launchd_path(self, monkeypatch):
        monkeypatch.setattr(mp.sys, "platform", "darwin")
        assert mp.log_path() == Path.home() / "Library" / "Logs" / "meeting-pull.log"

    def test_windows_log_is_the_file_the_task_runner_writes(self, monkeypatch, tmp_path):
        import run_logged
        monkeypatch.setattr(mp.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert mp.log_path() == run_logged.log_path("meeting-pull")

    def test_under_the_task_runner_each_line_is_logged_once(
            self, tmp_path, monkeypatch, allow_subprocess):
        """This job used to tee its own log on win32. Once run_logged.py was
        capturing stdout, that wrote every line twice -- measured: three log()
        calls, six lines. Run the real wrapper around the real log() with the
        win32 branch live."""
        import run_logged
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        harness = tmp_path / "harness.py"
        harness.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import meeting_pull as mp\n"
            "mp.sys.platform = 'win32'   # after import: the stdlib needs the real value\n"
            "for i in range(3):\n"
            "    mp.log('probe line %%d' %% i)\n" % str(Path(mp.__file__).parent),
            encoding="utf-8")
        assert run_logged.run("meeting-pull", str(harness), []) == 0
        lines = (tmp_path / "obsidian-logs" / "meeting-pull.log").read_text(
            encoding="utf-8").splitlines()
        probes = [ln.split("meeting_pull: ", 1)[1] for ln in lines]
        assert probes == ["probe line 0", "probe line 1", "probe line 2"], lines

    def test_log_does_not_write_a_file_of_its_own(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mp.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        mp.log("anything")
        assert list(tmp_path.iterdir()) == []

    def test_failure_notification_names_the_platform_log(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mp.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        source = Path(mp.__file__).read_text(encoding="utf-8")
        assert "Library/Logs/meeting-pull.log" not in source, (
            "a hardcoded macOS log path is back in meeting_pull.py")
        assert "log_path()" in source.split("No calendar handoff written")[1][:120], (
            "the failure notification no longer names log_path()")
