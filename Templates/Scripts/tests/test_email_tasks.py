"""
test_email_tasks.py -- the mail session reads mail and nothing else, and what
it returns reaches the vault only as plain task lines.

An inbox is text anyone can write. These pin both halves of the hybrid design:
the session's privileges (two mail tools, everything else removed, the same
confinement as the meeting pull), and the transform that is the only writer
(no markup, no links but the message's own Outlook link, no Tasks markers an
email chose, priority from the user's data rather than the email's say).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import email_task_transform as T
import email_tasks as et
import meeting_pull as mp
import templater_guard

TODAY = dt.date(2026, 10, 19)
CONFIG = {"display_name": "Ada Example", "email": "ada@example.edu"}
SEARCH = "mcp__claude_ai_Microsoft_365__outlook_email_search"
READ = "mcp__claude_ai_Microsoft_365__read_resource"
LINK = "https://outlook.office.com/mail/inbox/id/AAMk1"


def _req(**over) -> dict:
    base = {"message_id": "mail:///messages/AAMk1", "kind": "request",
            "sender_address": "bob@example.edu", "sender_name": "Bob Builder",
            "task": "Review the Q3 budget draft", "due": "", "weblink": LINK,
            "senior_title": False, "phishing_suspect": False}
    base.update(over)
    return base


def _lines(*reqs, config=None, people=None, handled=()) -> list:
    valid = T.validate({"requests": list(reqs)}, set(handled), {"ada@example.edu"})
    return T.render(valid, config or CONFIG, people or {}, TODAY)


# ---------------------------------------------------------------- the transform

class TestTaskText:
    def test_a_plain_request_becomes_one_task_line(self) -> None:
        assert _lines(_req()) == [
            "- [ ] Review the Q3 budget draft, from Bob Builder "
            "([open in Outlook](%s)) #task" % LINK]

    @pytest.mark.parametrize("hostile", [
        "Pay now [[Secrets]] #urgent",
        "Approve <script>alert(1)</script>",
        "Sign `rm -rf ~` here",
        "Click [here](https://evil.example/x)",
        "Open obsidian://open?vault=x&file=y",
        "Run javascript:alert(1)",
        "Due \U0001f4c5 2020-01-01 ⏫",
        "<% tp.system.prompt() %>",
        "{{ dataview }} | table",
    ])
    def test_markup_links_and_task_markers_never_survive(self, hostile: str) -> None:
        [line] = _lines(_req(task=hostile, weblink=""))
        body = line[len("- [ ] "):-len(" #task")]
        for bad in ("[[", "](", "#urgent", "<", ">", "`", "://", "obsidian:",
                    "javascript:", "\U0001f4c5", "⏫", "{{", "|", "<%"):
            assert bad not in body, (bad, line)

    def test_prose_keeps_its_colon(self) -> None:
        [line] = _lines(_req(task="Budget review: send comments by Friday"))
        assert "Budget review: send comments by Friday" in line

    def test_one_line_and_bounded(self) -> None:
        [line] = _lines(_req(task="a\nb\r\nc d " + "x" * 500,
                             sender_name="N" * 200))
        assert "\n" not in line and " " not in line
        assert len(line) < T.MAX_TASK_CHARS + T.MAX_NAME_CHARS + 200

    def test_the_block_written_is_neutralized(self, tmp_path: Path) -> None:
        note = tmp_path / "Actions" / "Email Tasks.md"
        T.append_tasks(note, ["- [ ] x <%* tR %> #task"], TODAY)
        assert "<%*" not in note.read_text(encoding="utf-8")
        assert templater_guard.ZWSP in note.read_text(encoding="utf-8")


class TestLinks:
    @pytest.mark.parametrize("link", [
        "http://outlook.office.com/mail/x",            # not https
        "https://outlook.office.com.evil.example/x",    # look-alike host
        "https://evil.example/?u=outlook.office.com",
        "https://outlook.office.com/x) [more](https://evil.example",
        "javascript:alert(1)",
        "https://outlook.office.com/a b",
        "",
        None,
        42,
    ])
    def test_only_the_messages_own_outlook_link_is_kept(self, link) -> None:
        [line] = _lines(_req(weblink=link))
        assert "open in Outlook" not in line and "evil" not in line

    @pytest.mark.parametrize("host", T.OUTLOOK_HOSTS)
    def test_each_outlook_host_is_accepted(self, host: str) -> None:
        [line] = _lines(_req(weblink="https://%s/mail/id/1" % host))
        assert "([open in Outlook](https://%s/mail/id/1))" % host in line


class TestValidate:
    def test_the_reply_must_carry_a_requests_list(self) -> None:
        for raw in ({}, {"requests": "x"}, [], None):
            with pytest.raises(ValueError, match="no 'requests' list"):
                T.validate(raw, set(), set())

    @pytest.mark.parametrize("over", [
        {"message_id": "https://evil.example/x"},
        {"message_id": "mail:///messages/" + "a" * 600},
        {"kind": "invoice"},
        {"sender_address": "not an address"},
        {"sender_address": "ada@example.edu"},      # the user's own send
        {"task": "  [[ ]] ## "},                     # nothing left after cleaning
    ])
    def test_malformed_or_own_requests_are_dropped(self, over: dict) -> None:
        assert _lines(_req(**over)) == []

    def test_duplicates_and_already_handled_are_dropped(self) -> None:
        assert len(_lines(_req(), _req())) == 1
        assert _lines(_req(), handled=("mail:///messages/AAMk1",)) == []

    def test_approvals_come_first_and_team_asks_are_marked(self) -> None:
        lines = _lines(_req(message_id="mail:///messages/1", kind="team_ask", task="Team thing"),
                       _req(message_id="mail:///messages/2", kind="request", task="Mine"),
                       _req(message_id="mail:///messages/3", kind="approval", task="Sign it"))
        assert [l.split(",")[0] for l in lines] == [
            "- [ ] Sign it", "- [ ] Mine", "- [ ] Team thing (team ask)"]

    def test_a_suspected_phish_is_flagged_to_verify(self) -> None:
        [line] = _lines(_req(kind="approval", phishing_suspect=True, task="Sign the NDA"))
        assert line.startswith("- [ ] Verify (possible phishing): Sign the NDA")

    def test_flags_must_be_real_booleans(self) -> None:
        [line] = _lines(_req(phishing_suspect="true", senior_title="yes"),
                        config=dict(CONFIG, title_priority=True))
        assert "Verify" not in line and T.PRIORITY_MARK["medium"] not in line


class TestDueDates:
    @pytest.mark.parametrize("due,kept", [
        ("2026-10-23", True), ("2027-10-19", True),
        ("2028-01-01", False), ("2026-02-30", False), ("next Friday", False),
        ("2026-10-23 ⏫", False), (None, False), (20261023, False),
    ])
    def test_only_a_real_date_near_today(self, due, kept: bool) -> None:
        [line] = _lines(_req(due=due))
        assert ("\U0001f4c5 " in line) is kept
        if kept:
            assert line.endswith("\U0001f4c5 %s #task" % due)


class TestPriority:
    def test_configured_high_senders_win(self) -> None:
        [line] = _lines(_req(), config=dict(CONFIG, high_priority_senders=["BOB@example.edu"]),
                        people={"bob@example.edu": "none"})
        assert T.PRIORITY_MARK["high"] in line

    def test_people_notes_set_the_level_and_none_overrides_title(self, tmp_path: Path) -> None:
        people = tmp_path / "People"
        people.mkdir()
        (people / "Bob.md").write_text(
            "---\nEmail-Work: bob@example.edu\nEmail-Priority: medium\n---\n", encoding="utf-8")
        (people / "Cy.md").write_text(
            "---\nEmail-Work-2: 'cy@example.edu'\nEmail-Priority: none\n---\n", encoding="utf-8")
        (people / "Bad.md").write_text(
            "---\nEmail-Work: dee@example.edu\nEmail-Priority: urgent\n---\n", encoding="utf-8")
        prio = T.people_priorities(tmp_path)
        assert prio == {"bob@example.edu": "medium", "cy@example.edu": "none"}
        cfg = dict(CONFIG, title_priority=True)
        [bob] = _lines(_req(), config=cfg, people=prio)
        [cy] = _lines(_req(sender_address="cy@example.edu", senior_title=True), config=cfg, people=prio)
        assert T.PRIORITY_MARK["medium"] in bob
        assert not any(m in cy for m in T.PRIORITY_MARK.values())

    def test_a_checked_box_is_high_and_an_unchecked_one_is_no_opinion(self, tmp_path: Path) -> None:
        people = tmp_path / "People"
        people.mkdir()
        for name, addr, value in (("A", "a@example.edu", "true"), ("B", "b@example.edu", "false"),
                                  ("C", "c@example.edu", ""), ("D", "d@example.edu", "True")):
            (people / (name + ".md")).write_text(
                "---\nEmail-Work: %s\nEmail-Priority: %s\n---\n" % (addr, value), encoding="utf-8")
        assert T.people_priorities(tmp_path) == {"a@example.edu": "high", "d@example.edu": "high"}

    def test_a_senior_title_counts_only_when_configured(self) -> None:
        [off] = _lines(_req(senior_title=True))
        [on] = _lines(_req(senior_title=True), config=dict(CONFIG, title_priority=True))
        assert T.PRIORITY_MARK["medium"] not in off and T.PRIORITY_MARK["medium"] in on


class TestAppend:
    def test_creates_a_confidential_note_and_appends_under_todays_heading(self, tmp_path: Path) -> None:
        note = tmp_path / "Actions" / "Email Tasks.md"
        T.append_tasks(note, ["- [ ] one #task"], TODAY)
        T.append_tasks(note, ["- [ ] two #task"], TODAY)
        T.append_tasks(note, ["- [ ] three #task"], TODAY + dt.timedelta(days=1))
        text = note.read_text(encoding="utf-8")
        assert text.startswith("---\nclassification: confidential\n")
        assert text.count("## 2026-10-19") == 1 and "## 2026-10-20" in text
        assert text.index("one") < text.index("two") < text.index("## 2026-10-20") < text.index("three")
        assert not (note.parent / "Email Tasks.md.tmp").exists()


# ---------------------------------------------------------------- the session

class TestConfinement:
    def _cmd(self, config=None) -> list:
        allowed, denied = et.tools(config or CONFIG)
        return et.session_command("claude", allowed, denied)

    def test_exactly_the_two_mail_tools_are_allowed(self) -> None:
        cmd = self._cmd()
        assert cmd[cmd.index("--allowedTools") + 1].split() == [SEARCH, READ]

    def test_every_builtin_and_other_connector_tool_is_removed(self) -> None:
        cmd = self._cmd()
        denied = set(cmd[cmd.index("--disallowedTools") + 1].split())
        assert set(mp.DENIED_BUILTINS) <= denied
        for t in ("Bash", "Write", "Read", "WebFetch", "WebSearch", "ReadMcpResourceTool"):
            assert t in denied
        prefix = mp.DEFAULT_MCP_PREFIX
        for t in ("outlook_calendar_search", "teams_list_chats", "sharepoint_search", "chat_message_search"):
            assert "%s__%s" % (prefix, t) in denied
        assert SEARCH not in denied and READ not in denied

    def test_the_meeting_pull_flags_are_all_present(self) -> None:
        cmd = self._cmd()
        for flag in ("--restricted", "--no-session-persistence", "--verbose"):
            assert flag in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        assert cmd[cmd.index("--input-format") + 1] == "stream-json"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert json.loads(cmd[cmd.index("--json-schema") + 1])["required"] == ["requests"]
        assert not any("Ada" in a for a in cmd), "the prompt goes on stdin, not argv"

    @pytest.mark.parametrize("key,value", [
        ("search_tool", "x Read"), ("read_tool", "teams_list_chats"),
        ("search_tool", "outlook_calendar_search"), ("mcp_prefix", "mcp_x"),
    ])
    def test_a_config_cannot_widen_the_session(self, key: str, value: str) -> None:
        with pytest.raises(SystemExit):
            et.tools(dict(CONFIG, **{key: value}))


def _stream(requests, *, tools=(SEARCH, READ), calls=(SEARCH, READ)) -> str:
    lines = [{"type": "system", "subtype": "init", "tools": ["StructuredOutput", *tools],
              "mcp_servers": [{"name": "claude.ai Microsoft 365", "status": "connected"}]}]
    for n, name in enumerate(calls):
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu%d" % n, "name": name, "input": {}}]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu%d" % n, "is_error": False, "content": "x"}]}})
    lines.append({"type": "result", "subtype": "success", "is_error": False, "result": "",
                  "structured_output": {"requests": requests}})
    return "\n".join(json.dumps(m) for m in lines) + "\n"


class TestExtract:
    def test_a_real_stream_yields_its_requests(self) -> None:
        assert et.extract_requests(_stream([_req()]), SEARCH) == {"requests": [_req()]}

    def test_no_search_tool_is_an_error_not_an_empty_inbox(self) -> None:
        with pytest.raises(mp.ProducerOutputError, match="no tool named"):
            et.extract_requests(_stream([], tools=(), calls=()), SEARCH)

    def test_no_completed_search_is_an_error_not_an_empty_inbox(self) -> None:
        with pytest.raises(mp.ProducerOutputError, match="never completed a call"):
            et.extract_requests(_stream([], calls=()), SEARCH)

    def test_a_bare_envelope_is_refused(self) -> None:
        bare = json.dumps({"type": "result", "is_error": False, "result": '{"requests": []}'})
        with pytest.raises(mp.ProducerOutputError, match="did not report its session"):
            et.extract_requests(bare, SEARCH)


class TestWindowAndState:
    NOW = dt.datetime(2026, 10, 19, 11, 30, tzinfo=dt.timezone.utc)

    def test_first_run_reads_one_day(self) -> None:
        assert et.window({"handled": {}}, CONFIG, self.NOW) == ("2026-10-18T11:30:00Z", "2026-10-19T11:30:00Z")

    def test_later_runs_start_at_the_last_success_but_never_beyond_the_cap(self) -> None:
        assert et.window({"last_success": "2026-10-16T11:30:00Z"}, CONFIG, self.NOW)[0] == "2026-10-16T11:30:00Z"
        assert et.window({"last_success": "2026-09-01T00:00:00Z"}, CONFIG, self.NOW)[0] == "2026-10-15T11:30:00Z"
        assert et.window({"last_success": "2026-09-01T00:00:00Z"},
                         dict(CONFIG, max_lookback_hours=10000), self.NOW)[0] == "2026-10-15T11:30:00Z"

    @pytest.mark.parametrize("bad", ["garbage", "2026-10-18T00:00:00", "2027-01-01T00:00:00Z", 5])
    def test_a_bad_or_future_last_success_falls_back_to_one_day(self, bad) -> None:
        assert et.window({"last_success": bad}, CONFIG, self.NOW)[0] == "2026-10-18T11:30:00Z"

    def test_handled_ids_are_pruned_after_thirty_days(self, tmp_path: Path) -> None:
        path = tmp_path / "s" / "state.json"
        et.save_state(path, {"handled": {"old": "2026-09-01", "new": "2026-10-18"}}, TODAY)
        assert et.load_state(path)["handled"] == {"new": "2026-10-18"}

    def test_a_corrupt_state_file_starts_clean(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        assert et.load_state(path) == {"handled": {}}


class TestMain:
    def _setup(self, tmp_path: Path, monkeypatch) -> tuple:
        cfg = tmp_path / "email_tasks.json"
        cfg.write_text(json.dumps(CONFIG), encoding="utf-8")
        monkeypatch.setattr(et, "state_path", lambda: tmp_path / "state" / "state.json")
        return cfg, tmp_path / "vault"

    def test_from_json_writes_once_and_remembers(self, tmp_path: Path, monkeypatch) -> None:
        cfg, vault = self._setup(tmp_path, monkeypatch)
        reply = tmp_path / "reply.json"
        reply.write_text(json.dumps({"requests": [_req()]}), encoding="utf-8")
        args = ["--config", str(cfg), "--vault", str(vault), "--from-json", str(reply)]
        assert et.main(args) == 0 and et.main(args) == 0
        assert (vault / "Actions" / "Email Tasks.md").read_text(encoding="utf-8").count("#task") == 1

    def test_dry_run_calls_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        cfg, vault = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(mp, "run_session", lambda *a, **k: pytest.fail("ran a session"))
        assert et.main(["--config", str(cfg), "--vault", str(vault), "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert SEARCH in out and "{{" not in out

    def test_a_good_session_writes_and_advances(self, tmp_path: Path, monkeypatch) -> None:
        cfg, vault = self._setup(tmp_path, monkeypatch)
        import subprocess
        monkeypatch.setattr(mp, "network_ready", lambda: True)
        monkeypatch.setattr(mp, "find_claude", lambda o: "claude")
        monkeypatch.setattr(mp, "run_session", lambda c, p, w, t, cwd: subprocess.CompletedProcess(
            c, 0, _stream([_req()]), ""))
        assert et.main(["--config", str(cfg), "--vault", str(vault)]) == 0
        state = et.load_state(tmp_path / "state" / "state.json")
        assert "mail:///messages/AAMk1" in state["handled"] and state["last_success"].endswith("Z")

    def test_a_session_that_read_no_mail_writes_nothing_and_does_not_advance(
            self, tmp_path: Path, monkeypatch) -> None:
        cfg, vault = self._setup(tmp_path, monkeypatch)
        import subprocess
        monkeypatch.setattr(mp, "network_ready", lambda: True)
        monkeypatch.setattr(mp, "find_claude", lambda o: "claude")
        monkeypatch.setattr(mp, "CONNECTOR_WAIT_SEC", 0)
        monkeypatch.setattr(mp, "run_session", lambda c, p, w, t, cwd: subprocess.CompletedProcess(
            c, 0, _stream([], tools=(), calls=()), ""))
        assert et.main(["--config", str(cfg), "--vault", str(vault)]) == 1
        assert not (vault / "Actions" / "Email Tasks.md").exists()
        assert not (tmp_path / "state" / "state.json").exists()

    def test_missing_config_names_the_example(self, tmp_path: Path, capsys) -> None:
        with pytest.raises(SystemExit):
            et.main(["--config", str(tmp_path / "nope.json")])
        assert "email_tasks.example.json" in capsys.readouterr().out
