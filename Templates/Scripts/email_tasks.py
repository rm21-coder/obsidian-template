#!/usr/bin/env python3
"""email_tasks.py - turn what people ask of you by email into #task lines.

Each weekday morning a confined, headless Claude Code session (``claude -p``)
reads the mail received since the last run through the Microsoft 365 MCP
connector and returns the requests waiting on you as JSON. It has exactly two
tools, the connector's mail search and its item reader, and nothing that can
write, run commands or reach the web. This script then hands that JSON to
``email_task_transform.py``, the only writer, which appends one
``- [ ] ... #task`` line per request to ``Actions/Email Tasks.md``, where the
To-Do page and the Morning Dashboard already pick tasks up.

An inbox is text anyone can write, so the session is confined exactly as the
meeting pull's is (``meeting_pull.producer_command``) and treats every email as
data. What a crafted email can still do: steer which mail the session reads
and what short task text it returns. It cannot run anything, write anything
itself, or put a link in the vault that is not the message's own Outlook link.
See docs/Email-Tasks.md for the full design and its limits.

Scheduled by ``com.obsidian.email-tasks.plist`` (macOS) or the ``email-tasks``
task in ``windows/schedules.psd1``; both off until set up. Validate by hand::

    python3 email_tasks.py --dry-run     # render the prompt, call nothing
    python3 email_tasks.py               # real run

Everything person-specific is read from ``.config/email_tasks.json``
(gitignored; see email_tasks.example.json). Stdlib only, Python 3.9-compatible.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import email_task_transform as T  # noqa: E402
import meeting_pull as mp  # noqa: E402  (the session machinery is shared)

VAULT_ROOT = SCRIPTS_DIR.parent.parent
DEFAULT_SEARCH_TOOL = "outlook_email_search"
DEFAULT_READ_TOOL = "read_resource"
DEFAULT_NOTE = "Actions/Email Tasks.md"
REQUIRED_KEYS = ("display_name", "email")
EXIT_AUTH = mp.EXIT_AUTH
EXIT_NO_NETWORK = mp.EXIT_NO_NETWORK
TIMEOUT_SEC = int(os.environ.get("EMAIL_TASKS_TIMEOUT", "900"))
HANDLED_KEEP_DAYS = 30
DEFAULT_LOOKBACK_HOURS = 24
MAX_LOOKBACK_HOURS = 96

# Shape of the reply, enforced by the CLI (--json-schema).
REQUESTS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"requests": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(T.KINDS)},
            "sender_address": {"type": "string"},
            "sender_name": {"type": "string"},
            "task": {"type": "string"},
            "due": {"type": "string"},
            "weblink": {"type": "string"},
            "subject": {"type": "string"},
            "senior_title": {"type": "boolean"},
            "phishing_suspect": {"type": "boolean"},
            "needs_review": {"type": "boolean"},
        },
        "required": ["message_id", "kind", "sender_address", "task"],
    }},
        "flagged": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "sender_address": {"type": "string"},
                "sender_name": {"type": "string"},
                "weblink": {"type": "string"},
            },
            "required": ["message_id", "sender_address"],
        }}},
    "required": ["requests"],
})


def log(message: str) -> None:
    print("%s email_tasks: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def die(message: str) -> None:
    log("ERROR: %s" % message)
    raise SystemExit(1)


def state_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "obsidian-email-tasks" / "state.json"


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"handled": {}}
    if not isinstance(state, dict) or not isinstance(state.get("handled"), dict):
        return {"handled": {}}
    return state


def save_state(path: Path, state: dict, today: _dt.date) -> None:
    cutoff = (today - _dt.timedelta(days=HANDLED_KEEP_DAYS)).isoformat()
    state["handled"] = {k: v for k, v in state["handled"].items() if str(v) >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def load_config(path: Path) -> dict:
    if not path.is_file():
        die("config not found: %s (copy email_tasks.example.json there and fill it "
            "in; see docs/Email-Tasks.md)" % path)
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        die("config %s is not valid JSON: %s" % (path, exc))
    if not isinstance(config, dict):
        die("config %s must be a JSON object" % path)
    missing = [k for k in REQUIRED_KEYS if not str(config.get(k, "")).strip()]
    if missing:
        die("config %s is missing required key(s): %s" % (path, ", ".join(missing)))
    if not T.clean_address(config["email"]):
        die("config 'email' is not an email address")
    return config


def own_addresses(config: dict) -> set:
    addrs = [config.get("email")] + list(config.get("other_addresses") or [])
    return {a for a in (T.clean_address(x) for x in addrs) if a}


def tool_name(config: dict, key: str, default: str) -> str:
    """A plain identifier, never one of the connector tools this job is denied
    (the same rule, for the same reason, as meeting_pull.tool_name)."""
    value = config.get(key) or default
    if not isinstance(value, str) or not mp._TOOL_NAME.fullmatch(value):
        die("config %r must be a single tool-name part, not %r" % (key, value))
    if key == "mcp_prefix":
        if not (value.startswith("mcp__") and len(value) > 5 and not value.endswith("_")):
            die("config 'mcp_prefix' must look like mcp__<server>, not %r" % value)
    elif value in denied_connector_tools(DEFAULT_SEARCH_TOOL, DEFAULT_READ_TOOL):
        die("config %r names %r, a connector tool this session is denied" % (key, value))
    return value


def denied_connector_tools(search: str, read: str) -> list:
    """Every other Microsoft 365 connector tool, calendar included."""
    names = set(mp.OTHER_M365_TOOLS) | {"outlook_calendar_search"}
    return sorted(names - {search, read})


def tools(config: dict) -> tuple:
    prefix = tool_name(config, "mcp_prefix", mp.DEFAULT_MCP_PREFIX)
    search = tool_name(config, "search_tool", DEFAULT_SEARCH_TOOL)
    read = tool_name(config, "read_tool", DEFAULT_READ_TOOL)
    allowed = ["%s__%s" % (prefix, search), "%s__%s" % (prefix, read)]
    denied = [n for n in list(mp.DENIED_BUILTINS)
              + ["%s__%s" % (prefix, t) for t in denied_connector_tools(search, read)]
              if n not in allowed]
    return " ".join(allowed), " ".join(denied)


def session_command(claude: str, allowed: str, denied: str) -> list:
    """meeting_pull.producer_command's flags with this job's schema: restricted,
    dontAsk, exactly the two mail tools, everything else removed, structured
    output, no transcript kept (it would hold the mail)."""
    return [claude, "-p",
            "--input-format", "stream-json",
            "--restricted",
            "--permission-mode", "dontAsk",
            "--allowedTools", allowed,
            "--disallowedTools", denied,
            "--json-schema", REQUESTS_SCHEMA,
            "--output-format", "stream-json", "--verbose",
            "--no-session-persistence"]


def window(state: dict, config: dict, now: _dt.datetime) -> tuple:
    """(after, before) in UTC ISO: since the last successful run, bounded."""
    cap = max(1, min(int(config.get("max_lookback_hours") or MAX_LOOKBACK_HOURS),
                     MAX_LOOKBACK_HOURS))
    after = now - _dt.timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    last = state.get("last_success")
    if isinstance(last, str):
        try:
            prev = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
            if prev.tzinfo is not None and prev <= now:
                after = prev
        except ValueError:
            pass
    after = max(after, now - _dt.timedelta(hours=cap))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return after.astimezone(_dt.timezone.utc).strftime(fmt), now.astimezone(_dt.timezone.utc).strftime(fmt)


def priority_addresses(config: dict, vault: Path) -> list:
    """Senders whose mail is always read in full: the configured high
    senders and every People note with Email-Priority checked."""
    addrs = {T.clean_address(a) for a in config.get("high_priority_senders") or []}
    addrs |= {a for a, level in T.people_priorities(vault).items() if level == "high"}
    return sorted(a for a in addrs if a)


def render_prompt(template: str, config: dict, allowed: str, after: str, before: str,
                  priority=()) -> str:
    search, read = allowed.split()
    values = {
        "SEARCH_TOOL": search, "READ_TOOL": read,
        "AFTER_DATETIME": after, "BEFORE_DATETIME": before,
        "USER_NAME": str(config["display_name"]),
        "USER_ADDRESSES": ", ".join(sorted(own_addresses(config))),
        "TEAM_MAX": str(int(config.get("team_ask_max_recipients") or 10)),
        "PRIORITY_SENDERS": ", ".join(priority) or "(none)",
    }
    for key, value in values.items():
        template = template.replace("{{%s}}" % key, value)
    return template


def extract_requests(stdout: str, search_tool: str) -> dict:
    """The reply object, after the same checks meeting_pull.extract_events
    makes: the session had the search tool and completed a call to it, so an
    empty list means no requests, not a connector that never loaded."""
    envelope, init, calls = mp._read_session(stdout)
    if init is None:
        raise mp.ProducerOutputError("the CLI did not report its session")
    if envelope.get("is_error"):
        raise mp.ProducerOutputError("the CLI reported an error")
    names = init.get("tools")
    if isinstance(names, list) and search_tool not in names:
        raise mp.ProducerOutputError(
            "the session has no tool named %s (MCP servers: %s), so no mail was read"
            % (search_tool, mp._servers(init)))
    if calls is not None and not calls.get(search_tool):
        raise mp.ProducerOutputError(
            "the session never completed a call to %s, so no mail was read" % search_tool)
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = envelope.get("result")
    if not isinstance(result, str):
        raise mp.ProducerOutputError("the CLI returned no result")
    return mp._first_json_object(result)


def write_tasks(raw: dict, config: dict, state: dict, vault: Path, today: _dt.date) -> int:
    """Validate, render and append; record what was handled. Returns the count."""
    handled = set(state["handled"])
    own = own_addresses(config)
    reqs = T.validate(raw, handled, own)
    flagged = T.validate_flagged(raw, handled, own)
    lines = T.render(reqs, config, T.people_priorities(vault), today) + T.render_flagged(flagged)
    if lines:
        T.append_tasks(vault / str(config.get("note") or DEFAULT_NOTE), lines, today)
    for r in reqs + flagged:
        state["handled"][r["message_id"]] = today.isoformat()
    if flagged:
        log("%d email(s) flagged as trying to instruct the session" % len(flagged))
    return len(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--config", default=os.environ.get("EMAIL_TASKS_CONFIG"))
    parser.add_argument("--prompt", default=os.environ.get("EMAIL_TASKS_PROMPT"))
    parser.add_argument("--vault", default=None)
    parser.add_argument("--claude", default=os.environ.get("EMAIL_TASKS_CLAUDE"))
    parser.add_argument("--dry-run", action="store_true",
                        help="render the prompt and print the command; call nothing")
    parser.add_argument("--from-json", metavar="FILE",
                        help="write tasks from a saved reply instead of running a session")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser() if args.config else SCRIPTS_DIR / ".config" / "email_tasks.json"
    prompt_path = Path(args.prompt).expanduser() if args.prompt else SCRIPTS_DIR / "email_tasks_prompt.txt"
    vault = Path(args.vault).expanduser().resolve() if args.vault else VAULT_ROOT
    config = load_config(config_path)
    allowed, denied = tools(config)
    spath = state_path()
    state = load_state(spath)
    now = _dt.datetime.now(_dt.timezone.utc)
    today = _dt.date.today()

    if args.from_json:
        raw = json.loads(Path(args.from_json).read_text(encoding="utf-8-sig"))
        n = write_tasks(raw, config, state, vault, today)
        save_state(spath, state, today)
        log("wrote %d task(s) from %s" % (n, args.from_json))
        return 0

    after, before = window(state, config, now)
    prompt = render_prompt(prompt_path.read_text(encoding="utf-8"), config, allowed, after, before,
                           priority_addresses(config, vault))
    if args.dry_run:
        log("dry run - would invoke: claude -p --input-format stream-json --restricted "
            "--permission-mode dontAsk --allowedTools %r --disallowedTools <%d tools> "
            "--output-format stream-json --verbose, prompt on stdin after %ds"
            % (allowed, len(denied.split()), mp.CONNECTOR_WAIT_SEC))
        log("window %s .. %s; note %s" % (after, before, config.get("note") or DEFAULT_NOTE))
        print(prompt)
        return 0

    if not mp.network_ready():
        log("the API host is not reachable; not attempting the run")
        return EXIT_NO_NETWORK
    claude = mp.find_claude(args.claude)
    command = mp.keep_awake(session_command(claude, allowed, denied))
    for attempt in (1, 2):
        log("starting (window %s .. %s, attempt %d/2)" % (after, before, attempt))
        workdir = tempfile.mkdtemp(prefix="email_tasks_cwd_")
        try:
            done = mp.run_session(command, prompt, mp.CONNECTOR_WAIT_SEC * attempt,
                                  TIMEOUT_SEC, workdir)
        except subprocess.TimeoutExpired:
            log("session exceeded %ds and was killed" % TIMEOUT_SEC)
            continue
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        # Never echo the stream: it holds the mail. failure_text keeps only
        # the CLI's own words.
        words = mp.failure_text(done.stdout, done.stderr)
        if done.returncode != 0:
            if words.strip():
                print(words.rstrip()[-2000:], flush=True)
            if mp.ORG_POLICY_RE.search(words):
                log("FATAL: the Claude CLI was refused because %s" % mp.ORG_POLICY_ADVICE)
                return EXIT_AUTH
            if mp.AUTH_FAILURE_RE.search(words):
                log("FATAL: the Claude CLI cannot authenticate: run `claude`, "
                    "sign in with /login, then re-run this")
                return EXIT_AUTH
            log("session exited %d" % done.returncode)
            continue
        try:
            raw = extract_requests(done.stdout or "", allowed.split()[0])
            n = write_tasks(raw, config, state, vault, today)
        except (mp.ProducerOutputError, ValueError) as exc:
            log("the session returned no usable reply: %s" % exc)
            continue
        state["last_success"] = before
        save_state(spath, state, today)
        log("done: %d new task(s)" % n)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
