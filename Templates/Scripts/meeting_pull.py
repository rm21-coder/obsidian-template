#!/usr/bin/env python3
"""meeting_pull.py - the PRODUCER half of meeting pre-population.

Runs a headless Claude Code session (``claude -p``) against an MCP calendar
connector, hands the result to ``mcp_meeting_transform.py``, and leaves the
schema-v1 handoff trio in the consumer's drop folder. The consumer
(``meeting_prepopulate.py``, scheduled separately) picks it up on its next
poll and writes the actual meeting notes.

Scheduled by ``com.obsidian.meeting-pull.plist`` (macOS, weekdays 05:00) or
the ``meeting-pull`` task in ``windows/schedules.psd1``. Safe to run by hand,
which is the recommended way to validate it the first time::

    python3 meeting_pull.py --dry-run   # render the prompt, call nothing
    python3 meeting_pull.py             # real run

Everything machine- or person-specific is read from
``.config/meeting_pull.json`` (gitignored; written by installer component
54-meeting-pull), never hardcoded here. Stdlib only, and deliberately free of
3.10+ syntax so the system interpreter can run it on either platform.

Full setup, the two failure modes that are easy to misdiagnose, and how to
point this at a non-Microsoft connector:
``docs/Meeting-Handoff-MCP-Producer.md``.
"""

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

# Connector defaults describe the Microsoft 365 reference implementation. They
# live here rather than in the prompt so a different connector is a config
# change, not a fork -- see "Adapting to a different MCP connector" in the docs.
DEFAULT_MCP_PREFIX = "mcp__claude_ai_Microsoft_365"
DEFAULT_SEARCH_TOOL = "outlook_calendar_search"
DEFAULT_READ_TOOL = "read_resource"

REQUIRED_KEYS = ("display_name", "email", "tenant", "timezone")

# An expired CLI session is not a transient failure. The retry and catch-up
# layers exist for a laptop that sleeps mid-run, which genuinely succeeds on a
# second attempt; authentication cannot, because it needs a human at a browser.
# Retrying it nine times across three firings -- observed 2026-09-04, still
# hammering at 08:02 over a cause established at 05:00 -- buys nothing and
# buries the one line that says what to do.
AUTH_FAILURE_RE = re.compile(
    r"OAuth session expired"
    r"|Failed to authenticate"
    r"|Not logged in"
    r"|Please run /login"
    r"|authentication_error"
    r"|invalid[_ ]api[_ ]key"
    r"|Unauthorized",
    re.I)

# Distinct from 1 so the caller can tell "needs a human" from "try again".
EXIT_AUTH = 3

# Distinct again: nothing was attempted, so nothing is wrong with the job.
EXIT_NO_NETWORK = 5

# Hard ceiling on one producer attempt. A healthy run is about 60 seconds, so
# ten minutes is failure, not slowness -- and an attempt that never returns
# holds the whole job, which is how a fourteen-hour outage started elsewhere in
# this pipeline. A timeout is a retryable outcome here: a wedged CLI session
# often succeeds on the next attempt, which is exactly what the retry loop is
# for. subprocess.run kills the child when the timeout fires.
PRODUCER_TIMEOUT_SEC = int(os.environ.get("MEETING_PULL_TIMEOUT", "600"))


def network_ready():
    """True if the API host resolves and accepts a connection.

    The 05:00 firing runs seconds after a scheduled wake, before Wi-Fi has
    necessarily associated. Aug 20 failed that way -- ENOTFOUND across three
    attempts -- and burning the retry budget on a machine with no route buries
    the honest explanation under an error that reads like a service outage.

    Fails OPEN. This gate is an optimisation, not a safety requirement: if the
    probe itself cannot run for any reason, the run proceeds exactly as it did
    before the gate existed. The one thing worse than a wasted 05:00 attempt
    is a 05:00 attempt that never happens because a helper broke.
    """
    try:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from claude_auth_check import network_ready as probe
        return probe()
    except Exception:
        return True

AUTH_MARKER = SCRIPTS_DIR / ".state" / "meeting_pull_auth_block.json"


def auth_block_active():
    """True if today's run already established that the CLI needs re-auth.

    The later catch-up firings are worth their cost only against failures that
    a retry can clear. This makes them cheap no-ops for the one failure that a
    retry never will, while still letting tomorrow try again from scratch --
    the block is dated, not permanent, so a re-auth needs no cleanup step to
    be remembered.
    """
    try:
        rec = json.loads(AUTH_MARKER.read_text())
    except (OSError, ValueError):
        return False
    return rec.get("date") == _dt.date.today().isoformat()


def set_auth_block(detail):
    try:
        AUTH_MARKER.parent.mkdir(parents=True, exist_ok=True)
        AUTH_MARKER.write_text(json.dumps({
            "date": _dt.date.today().isoformat(),
            "at": _dt.datetime.now().isoformat(timespec="seconds"),
            "detail": detail[:300],
        }, indent=2) + "\n")
    except OSError as e:
        log("could not write auth marker %s: %s" % (AUTH_MARKER, e))


def clear_auth_block():
    """Drop a stale block once a run gets through, so a later failure on the
    same day is not mistaken for the one already reported."""
    try:
        AUTH_MARKER.unlink()
    except OSError:
        pass

# Days of lookahead beyond today. 1 == today + the next working day, which is
# what the morning refresh wants: tomorrow's notes land in the vault a day
# early so there is somewhere to put prep. Override per machine in
# .config/meeting_pull.json ("lookahead_days"); 0 restores today-only.
DEFAULT_LOOKAHEAD_DAYS = 1

# Count lookahead in weekdays rather than calendar days, so Friday reaches
# Monday instead of stopping in an empty Saturday. Off ("lookahead_skips_
# weekends": false) makes the lookahead literal calendar days again -- which
# only makes sense for a calendar that is genuinely used at weekends.
DEFAULT_SKIP_WEEKENDS = True


def log(message):
    print("%s meeting_pull: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def die(message):
    log("ERROR: %s" % message)
    raise SystemExit(1)


def find_claude(override):
    """Locate the Claude CLI.

    Scheduled runs (launchd, Task Scheduler) inherit a minimal PATH that
    excludes user-level install locations, which is exactly where this CLI
    normally lives -- so PATH lookup alone is not enough.
    """
    if override:
        if Path(override).is_file():
            return override
        die("claude not executable at %s" % override)

    found = shutil.which("claude")
    if found:
        return found

    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:  # npm's global bin on Windows
        candidates += [Path(appdata) / "npm" / "claude.cmd", Path(appdata) / "npm" / "claude"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    die("Claude CLI not found; pass --claude with its full path")


def load_config(path):
    if not path.is_file():
        # install.ps1 has no per-component switch and never provisions this
        # config, so pointing a Windows operator at ./install.sh is a dead end.
        fix = ("see docs/Meeting-Handoff-MCP-Producer.md for the config shape"
               if sys.platform == "win32"
               else "run: ./install.sh --only 54-meeting-pull")
        die("config not found: %s (%s)" % (path, fix))
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die("config %s is not valid JSON: %s" % (path, exc))
    missing = [k for k in REQUIRED_KEYS if not str(config.get(k, "")).strip()]
    if missing:
        die("config %s is missing required key(s): %s" % (path, ", ".join(missing)))
    return config


def tenant_domains(config, path):
    domains = config.get("tenant_domains") or []
    if isinstance(domains, str):
        domains = [d.strip() for d in domains.split(",") if d.strip()]
    if not domains:
        die("config %s is missing required key: tenant_domains" % path)
    return domains


def last_day_of_window(first, lookahead, skip_weekends):
    """Return the inclusive last day of a window starting on `first`.

    With skip_weekends the lookahead counts weekdays, so the day after Friday
    is Monday. The *range* stays contiguous -- Saturday and Sunday remain
    inside it -- because the calendar is fetched as a single span. That costs
    nothing in practice: weekend entries are nearly always all-day PTO, which
    the consumer already drops as solo blocks.
    """
    if lookahead <= 0:
        return first
    if not skip_weekends:
        return first + _dt.timedelta(days=lookahead)
    day = first
    remaining = lookahead
    while remaining > 0:
        day += _dt.timedelta(days=1)
        if day.weekday() < 5:          # Mon-Fri
            remaining -= 1
    return day


def window_days(config, first=None):
    """Resolve the pull window to (first_day, last_day), both inclusive dates.

    Shared by BOTH producers -- meeting_pull.py renders these into the prompt,
    graph_calendar_fetch.py turns them into a Graph calendarView range -- so
    the window cannot come to depend on which producer happened to run.

    Raises ValueError rather than exiting, so each caller can report the
    problem in its own voice.
    """
    raw = config.get("lookahead_days", DEFAULT_LOOKAHEAD_DAYS)
    try:
        lookahead = int(raw)
    except (TypeError, ValueError):
        raise ValueError("config 'lookahead_days' must be an integer, not %r" % (raw,))
    if lookahead < 0:
        raise ValueError("config 'lookahead_days' must be >= 0, not %d" % lookahead)

    skip_weekends = config.get("lookahead_skips_weekends", DEFAULT_SKIP_WEEKENDS)
    if not isinstance(skip_weekends, bool):
        raise ValueError("config 'lookahead_skips_weekends' must be true or false, not %r"
                         % (skip_weekends,))

    if first is None:
        tzname = config.get("timezone") or "UTC"
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tzname)
        except Exception:
            raise ValueError("config timezone %r is not a valid IANA zone" % tzname)
        first = _dt.datetime.now(tz).date()

    return first, last_day_of_window(first, lookahead, skip_weekends)


def window_bounds(config):
    """Resolve the pull window to explicit ISO bounds.

    Returns (after_iso, before_iso, week_start, week_end).

    Computed here rather than left to the producer session, because the
    connector resolves natural-language dates on its own terms: an upper
    bound of "tomorrow" is read as the END of tomorrow. The old
    today/tomorrow pair therefore asked for two days, while the same prompt
    told the session to stamp a single-day `week` -- so whether tomorrow
    survived depended on whether the session noticed the contradiction and
    trimmed. Observed: 3 of 17 runs kept it. Explicit bounds make the
    window a config decision instead of a coin flip.

    The upper bound is exclusive at midnight, verified against the
    connector: beforeDateTime=2026-08-27T00:00:00 returns nothing on 08-27.
    """
    try:
        first, last = window_days(config)
    except ValueError as exc:
        die(str(exc))
    after = _dt.datetime.combine(first, _dt.time.min)
    before = _dt.datetime.combine(last + _dt.timedelta(days=1), _dt.time.min)
    return after.isoformat(), before.isoformat(), first.isoformat(), last.isoformat()


def render_prompt(template_path, config, config_path, out_dir):
    """Substitute the template's {{TOKEN}} placeholders.

    The template ships tokenized so the public repo carries no identity and no
    machine paths; every value comes from config or the resolved environment.
    """
    if not template_path.is_file():
        die("prompt template not found: %s" % template_path)
    transform = SCRIPTS_DIR / "mcp_meeting_transform.py"
    if not transform.is_file():
        die("transform not found: %s" % transform)

    search_tool = config.get("search_tool") or DEFAULT_SEARCH_TOOL
    read_tool = config.get("read_tool") or DEFAULT_READ_TOOL
    after_iso, before_iso, week_start, week_end = window_bounds(config)
    tokens = {
        "AFTER_DATETIME": after_iso,
        "BEFORE_DATETIME": before_iso,
        "WEEK_START": week_start,
        "WEEK_END": week_end,
        "DISPLAY_NAME": config["display_name"],
        "EMAIL": config["email"],
        "TENANT": config["tenant"],
        "TIMEZONE": config["timezone"],
        "SEARCH_TOOL": search_tool,
        "READ_TOOL": read_tool,
        "TRANSFORM_SCRIPT": str(transform),
        "OUT_DIR": str(out_dir),
        "TENANT_DOMAINS": ",".join(tenant_domains(config, config_path)),
    }
    rendered = template_path.read_text()
    for key, value in tokens.items():
        rendered = rendered.replace("{{%s}}" % key, value)
    if "{{" in rendered:
        die("unsubstituted placeholder left in rendered prompt: %s" % template_path)
    return rendered


def allowed_tools(config):
    """Build the --allowedTools list: the two calendar tools, nothing else.

    A headless session cannot answer a permission prompt, so every MCP tool the
    prompt uses has to be named here up front -- and named exactly as the CLI
    exposes it (`claude mcp list`), which is not always how a desktop client
    labels the same connector.

    This session reads full event bodies, and a meeting invite is text anyone
    can write. Until 2026-09-25 it was also granted Bash and Write so it could
    save the calendar JSON and run the transform itself -- which made a crafted
    invite a prompt-injection path to a shell running as the user, unattended
    at 05:00 (Microsoft M-DASH, CWE-749). The session now only reads the
    calendar and returns JSON; this script writes the file and runs the
    transform. See producer_command() for why removing Bash and Write alone
    would not have been enough.
    """
    prefix = config.get("mcp_prefix") or DEFAULT_MCP_PREFIX
    search_tool = config.get("search_tool") or DEFAULT_SEARCH_TOOL
    read_tool = config.get("read_tool") or DEFAULT_READ_TOOL
    return " ".join(["%s__%s" % (prefix, search_tool), "%s__%s" % (prefix, read_tool)])


# Every built-in tool this CLI can offer, removed from the session outright.
# Measured against Claude Code 2.1.278, 2026-09-25: with the other flags below
# but WITHOUT this list, Read still succeeded inside the working directory and
# the generic ReadMcpResourceTool stayed available -- and that one can read
# any resource the connector exposes, email included. Names the running CLI
# does not have are ignored, so the list errs long. It is the second line of
# defence, not the only one: see producer_command().
DENIED_BUILTINS = (
    "Agent", "Artifact", "ArtifactComments", "ArtifactData", "AskUserQuestion",
    "Bash", "BashOutput", "CronCreate", "CronDelete", "CronList", "DesignSync",
    "Edit", "EnterPlanMode", "EnterWorktree", "ExitPlanMode", "ExitWorktree",
    "Glob", "Grep", "KillShell", "ListAgents", "ListMcpResourcesTool",
    "Monitor", "MultiEdit", "NotebookEdit", "NotebookRead", "PowerShell",
    "PushNotification", "Read", "ReadMcpResourceDirTool", "ReadMcpResourceTool",
    "RemoteTrigger", "ReportFindings", "ScheduleWakeup", "SendMessage",
    "ShareOnboardingGuide", "Skill", "SlashCommand", "Task", "TaskOutput",
    "TaskStop", "TodoWrite", "ToolSearch", "WebFetch", "WebSearch", "Write",
)

# The rest of the Microsoft 365 connector. Only the calendar search and the
# event read are needed; mail, Teams and SharePoint are not this job's business.
OTHER_M365_TOOLS = (
    "chat_message_search", "find_meeting_availability", "get_me",
    "outlook_email_search", "outlook_find_available_time",
    "sharepoint_folder_search", "sharepoint_search", "teams_list_chats",
)


# The reply's shape, enforced by the CLI (--json-schema) rather than scraped
# from prose. Found live 2026-09-25: asked for bare JSON as text, the session
# returned a calendar whose event bodies contained quotes it did not escape,
# and the reply would not parse. The old flow never hit this because it passed
# the JSON through the Write tool, whose arguments the API encodes
# structurally; --json-schema restores that guarantee without the tool.
# Events are left loosely typed on purpose: their shape is the connector's.
EVENTS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"events": {"type": "array", "items": {"type": "object"}}},
    "required": ["events"],
})


def disallowed_tools(config):
    prefix = config.get("mcp_prefix") or DEFAULT_MCP_PREFIX
    allowed = set(allowed_tools(config).split())
    names = list(DENIED_BUILTINS) + ["%s__%s" % (prefix, t) for t in OTHER_M365_TOOLS]
    return " ".join(n for n in names if n not in allowed)


def producer_command(claude, prompt, tools, denied):
    """The headless CLI invocation for the claude producer.

    Four layers, each measured against the real CLI rather than assumed,
    because the obvious single flag does not do what it looks like it does:
    `--tools ""` removes the MCP tools too and leaves a session that can read
    nothing, and a non-empty `--tools` does the same.

      --restricted          no code-running tools or WebFetch, settings files
                            ignored, and file tools confined to the working
                            directory -- which is an empty private temp dir
                            (see the cwd passed by main()). Proven: a Read of
                            a file outside it was refused.
      --permission-mode dontAsk
                            anything not pre-approved is refused rather than
                            prompted for, so a tool a future CLI adds is
                            denied by default. Proven: WebSearch and the
                            connector's mail search were refused.
      --allowedTools        pre-approves exactly the two calendar tools.
      --disallowedTools     removes every other built-in and the rest of the
                            connector from the session entirely. Load-bearing:
                            dontAsk alone still allowed Read inside the working
                            directory and left ReadMcpResourceTool available.

    Removing only Bash and Write would not have been enough: Read, Grep and
    Glob run without asking in a default session, so an injected instruction
    could still have had the session read a secrets file and return it inside
    an event body, which this pipeline would carry into a vault note.

    --output-format json gives one machine-readable envelope instead of prose
    to scrape. --no-session-persistence stops the CLI saving a transcript of
    every meeting body to disk each morning.
    """
    return [claude, "-p", prompt,
            "--restricted",
            "--permission-mode", "dontAsk",
            "--allowedTools", tools,
            "--disallowedTools", denied,
            "--json-schema", EVENTS_SCHEMA,
            "--output-format", "json",
            "--no-session-persistence"]


class ProducerOutputError(ValueError):
    """The CLI returned, but not with a usable calendar."""


def _first_json_object(text):
    """The first JSON object in `text`, tolerating a code fence or a sentence
    around it -- a model asked for bare JSON does not always obey."""
    start = text.find("{")
    if start < 0:
        raise ProducerOutputError("no JSON object in the session's reply")
    try:
        obj, _end = json.JSONDecoder(strict=False).raw_decode(text, start)
    except ValueError as exc:
        raise ProducerOutputError("the session's reply was not valid JSON: %s" % exc)
    if not isinstance(obj, dict):
        raise ProducerOutputError("the session's reply was not a JSON object")
    return obj


def extract_events(stdout):
    """Return the events list from `claude -p --output-format json` stdout.

    Only `events` is taken from the model. `user` and `week` are built from
    config by build_calendar(): they are known here, and asking a model to
    copy them "verbatim" was one more thing it could get wrong.
    """
    try:
        # strict=False: event bodies carry tabs and other control characters,
        # and one probe of the real CLI returned an envelope that strict
        # parsing rejects. It only relaxes control characters inside strings.
        envelope = json.loads(stdout, strict=False)
    except ValueError:
        raise ProducerOutputError("CLI output was not the JSON envelope --output-format json promises")
    if not isinstance(envelope, dict):
        raise ProducerOutputError("CLI output was not a JSON object")
    if envelope.get("is_error"):
        raise ProducerOutputError("the CLI reported an error: %s"
                                  % str(envelope.get("result") or envelope.get("subtype"))[:300])
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        obj = structured                   # validated against EVENTS_SCHEMA
    else:
        # Fallback for a CLI that did not honour --json-schema: parse the text.
        result = envelope.get("result")
        if not isinstance(result, str):
            raise ProducerOutputError("CLI envelope carried no structured or text result")
        obj = _first_json_object(result)
    events = obj.get("events")
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        raise ProducerOutputError("the reply had no 'events' list of objects")
    return events


def build_calendar(config, events):
    """The transform's input: user and week from config, events from the model."""
    _after, _before, week_start, week_end = window_bounds(config)
    return {
        "user": {k: config[k] for k in ("display_name", "email", "tenant", "timezone")},
        "week": {"start": week_start, "end": week_end},
        "events": events,
    }


def run_transform(calendar, out_dir, domains):
    """Write the calendar to a temp file and run the transform on it.

    Returns (returncode, stdout). The temp file is private to this user and
    removed afterwards whatever happens -- it holds every meeting body.
    """
    import tempfile
    transform = SCRIPTS_DIR / "mcp_meeting_transform.py"
    fd, tmp = tempfile.mkstemp(prefix="meeting_pull_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(calendar, fh)
        completed = subprocess.run(
            [sys.executable, str(transform), "--input", tmp,
             "--out-dir", str(out_dir), "--tenant-domains", ",".join(domains)],
            capture_output=True, text=True, timeout=120)
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def resolve_out_dir(args, config):
    """Match the consumer's own resolution order: env, then config, then default.

    Getting this wrong is silent rather than loud -- the producer writes a
    perfectly valid trio into a folder nothing is watching.
    """
    if args.out_dir:
        return Path(args.out_dir).expanduser()
    env_dir = os.environ.get("MEETING_PREPOP_HANDOFF_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    if config.get("out_dir"):
        return Path(config["out_dir"]).expanduser()
    # Default drop folder — the drop folder sits OUTSIDE the vault on purpose: raw handoff JSON is
    # ingest staging, not content, and a folder under Templates/Scripts/ would be
    # carried to every device by Obsidian Sync and walked by every vault scan.
    return Path.home() / "MeetingIngest"


def handoff_exists_for_today(out_dir):
    """True if today's handoff has already been produced.

    Checks the drop folder and its _processed archive, since the consumer moves
    each trio out of the way (with a timestamp suffix) once it has run. Lets a
    retry schedule be cheap: the extra firings no-op on any morning the first
    one worked.
    """
    today = time.strftime("%Y-%m-%d")
    pattern = "schedule-handoff-%s.v1*" % today
    for folder in (out_dir, out_dir / "_processed"):
        if folder.is_dir() and any(folder.glob(pattern)):
            return True
    return False


def keep_awake(command):
    """Wrap the command so the machine cannot idle-sleep while it runs (macOS).

    An early-morning scheduled run is the single most likely thing to be killed
    mid-flight: the machine is awake only because something woke it (a
    maintenance wake, or a pmset scheduled wake), nobody is touching the
    keyboard, and the idle timer is free to put it straight back to sleep with
    the calendar read and the handoff not yet written. `caffeinate -i` holds an
    idle-sleep assertion for exactly as long as the wrapped process runs.

    Note the limits: this stops *idle* sleep, not a closed lid or an explicit
    sleep, which is why the retry and catch-up layers still matter.
    """
    if sys.platform != "darwin":
        return command
    caffeinate = shutil.which("caffeinate") or "/usr/bin/caffeinate"
    if not Path(caffeinate).is_file():
        return command
    # -i: no idle sleep. -s: no system sleep while on AC (ignored on battery).
    return [caffeinate, "-i", "-s"] + command


def notify_failure(message):
    """Best-effort desktop notification on macOS.

    The whole failure mode this guards against is silence: a producer that dies
    leaves the consumer polling happily, so nothing in the pipeline complains
    and the first sign of trouble is a morning with no meeting notes. Never let
    the notifier's own failure change the exit path.
    """
    if sys.platform != "darwin":
        return
    script = 'display notification %s with title "Meeting pull failed"' % json.dumps(message)
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--config", default=os.environ.get("MEETING_PULL_CONFIG"),
                        help="path to meeting_pull.json (default: .config/meeting_pull.json)")
    parser.add_argument("--prompt", default=os.environ.get("MEETING_PULL_PROMPT"),
                        help="path to the prompt template (default: meeting_pull_prompt.txt)")
    parser.add_argument("--out-dir", default=None,
                        help="drop folder to write the handoff into (overrides config and env)")
    parser.add_argument("--claude", default=os.environ.get("MEETING_PULL_CLAUDE"),
                        help="full path to the Claude CLI (default: auto-discovered)")
    parser.add_argument("--dry-run", action="store_true",
                        help="render the prompt and print the command; call nothing")
    parser.add_argument("--skip-if-fresh", action="store_true",
                        help="exit 0 without calling anything if today's handoff already exists "
                             "(what the scheduled retry firings pass; omit it to force a fresh pull)")
    parser.add_argument("--retries", type=int, default=2,
                        help="extra attempts if the CLI fails (default: 2). A laptop that sleeps "
                             "mid-session kills the run with the day's work half done; retrying "
                             "after the machine wakes usually just succeeds.")
    parser.add_argument("--retry-delay", type=int, default=60,
                        help="seconds to wait between attempts (default: 60)")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser() if args.config else SCRIPTS_DIR / ".config" / "meeting_pull.json"
    template_path = Path(args.prompt).expanduser() if args.prompt else SCRIPTS_DIR / "meeting_pull_prompt.txt"

    config = load_config(config_path)
    out_dir = resolve_out_dir(args, config)

    # Producer selection. "claude" (default) drives a headless Claude CLI
    # session against the M365 MCP connector — zero custom API setup, the
    # original path. "graph" calls Microsoft Graph directly via
    # graph_calendar_fetch.py — no LLM tokens, a 2-second HTTP call that
    # fits inside any wake window, but needs a one-time device-code
    # sign-in (graph_calendar_fetch.py --auth). Both feed the identical
    # deterministic transform; retries/skip-if-fresh/notify below apply
    # to either.
    producer = str(config.get("producer") or "claude").strip().lower()

    if producer == "graph":
        fetcher = SCRIPTS_DIR / "graph_calendar_fetch.py"
        producer_cmd = [sys.executable, str(fetcher),
                        "--config", str(config_path),
                        "--out-dir", str(out_dir)]
        if args.dry_run:
            log("dry run - would invoke: %s" % " ".join(producer_cmd))
            log("drop folder: %s" % out_dir)
            return 0
    elif producer == "claude":
        prompt = render_prompt(template_path, config, config_path, out_dir)
        tools = allowed_tools(config)
        if args.dry_run:
            log("dry run - would invoke: claude -p <prompt> --restricted "
                "--permission-mode dontAsk --allowedTools %r --disallowedTools "
                "<%d tools> --output-format json" % (
                    tools, len(disallowed_tools(config).split())))
            log("drop folder: %s" % out_dir)
            print(prompt)
            return 0
        producer_cmd = None  # built below, after skip-if-fresh
    else:
        die("config 'producer' must be 'claude' or 'graph', not %r" % producer)

    if args.skip_if_fresh and handoff_exists_for_today(out_dir):
        log("today's handoff already exists in %s - nothing to do" % out_dir)
        return 0

    if not network_ready():
        log("the API host is not reachable yet - most likely Wi-Fi has not "
            "associated since the scheduled wake. Not attempting the pull; "
            "the later catch-up firings will run it once there is a route.")
        return EXIT_NO_NETWORK

    if args.skip_if_fresh and auth_block_active():
        log("the Claude CLI needed re-authentication earlier today and still "
            "does as far as this job knows - not retrying. Run `claude` in a "
            "terminal, sign in with /login, then re-run this without "
            "--skip-if-fresh (marker: %s)" % AUTH_MARKER)
        return EXIT_AUTH

    out_dir.mkdir(parents=True, exist_ok=True)
    if producer == "claude":
        claude = find_claude(args.claude)
        producer_cmd = producer_command(claude, prompt, tools, disallowed_tools(config))
    command = keep_awake(producer_cmd)

    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        log("starting (producer=%s, out_dir=%s, attempt %d/%d)" % (producer, out_dir, attempt, attempts))
        # Captured rather than inherited so the auth signature can be read
        # out of it; re-emitted verbatim straight afterwards so the log keeps
        # the producer's own words, which are what the dashboard reads.
        # An empty private directory, so the file tools --restricted confines
        # to the working directory have nothing to read.
        import tempfile
        workdir = tempfile.mkdtemp(prefix="meeting_pull_cwd_")
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=PRODUCER_TIMEOUT_SEC, cwd=workdir)
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            for chunk in (exc.stdout, exc.stderr):
                if not chunk:
                    continue
                text = chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk
                if text.strip():
                    print(text.rstrip(), flush=True)
            log("producer exceeded %ds and was killed - treating as a failed "
                "attempt" % PRODUCER_TIMEOUT_SEC)
            if attempt < attempts:
                log("retrying in %ds" % args.retry_delay)
                time.sleep(args.retry_delay)
            continue
        shutil.rmtree(workdir, ignore_errors=True)
        transcript = (completed.stdout or "") + (completed.stderr or "")

        if completed.returncode == 0 and producer == "claude":
            # The session returned; now this script does what the session used
            # to do with Bash and Write. Its reply is NOT echoed to the log:
            # it contains every meeting body.
            try:
                events = extract_events(completed.stdout or "")
            except ProducerOutputError as exc:
                log("the session returned no usable calendar: %s" % exc)
                if attempt < attempts:
                    log("retrying in %ds" % args.retry_delay)
                    time.sleep(args.retry_delay)
                continue
            log("session returned %d event(s); running the transform" % len(events))
            rc, out = run_transform(build_calendar(config, events), out_dir,
                                    tenant_domains(config, config_path))
            if out.strip():
                print(out.rstrip(), flush=True)
            if rc != 0:
                log("transform exited %d - no handoff written" % rc)
                if attempt < attempts:
                    log("retrying in %ds" % args.retry_delay)
                    time.sleep(args.retry_delay)
                continue
            clear_auth_block()
            log("done")
            return 0

        if transcript.strip() and producer == "claude":
            # Failure: keep the CLI's own words (the auth signature the
            # dashboard looks for lives here), but not an unbounded reply.
            print(transcript.rstrip()[-2000:], flush=True)
        elif transcript.strip():
            print(transcript.rstrip(), flush=True)

        if completed.returncode == 0:
            clear_auth_block()
            log("done")
            return 0

        if AUTH_FAILURE_RE.search(transcript):
            detail = next((ln.strip() for ln in transcript.splitlines()
                           if AUTH_FAILURE_RE.search(ln)), "authentication failed")
            # Logged last, and deliberately so: the dashboard surfaces the
            # final line of this log, and before this the last word belonged
            # to "producer exited 1 - no handoff written" -- a symptom sitting
            # on top of the one line that names the fix.
            set_auth_block(detail)
            notify_failure("Claude CLI sign-in expired - run `claude` then /login. "
                           "No meeting notes until then.")
            log("FATAL: the Claude CLI cannot authenticate (%s). This is not "
                "retryable without a human: run `claude` in a terminal and "
                "sign in with /login, then re-run "
                "Templates/Scripts/meeting_pull.py. Skipping the remaining "
                "attempts and today's later firings." % detail)
            return EXIT_AUTH

        log("producer exited %d - no handoff written" % completed.returncode)
        # A handoff can exist despite a non-zero exit -- for the graph producer,
        # which runs the transform itself, or from an earlier attempt today.
        # Re-pulling would be harmless but wasteful.
        if handoff_exists_for_today(out_dir):
            log("today's handoff is present anyway - treating as success")
            return 0
        if attempt < attempts:
            log("retrying in %ds" % args.retry_delay)
            time.sleep(args.retry_delay)

    notify_failure("No calendar handoff written after %d attempts. See ~/Library/Logs/meeting-pull.log"
                   % attempts)
    return 1


if __name__ == "__main__":
    sys.exit(main())
