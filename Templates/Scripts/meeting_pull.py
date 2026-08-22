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
import json
import os
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
        die("config not found: %s (run: ./install.sh --only 54-meeting-pull)" % path)
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
    tokens = {
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
    """Build the --allowedTools list.

    A headless session cannot answer a permission prompt, so every MCP tool the
    prompt uses has to be named here up front -- and named exactly as the CLI
    exposes it (`claude mcp list`), which is not always how a desktop client
    labels the same connector.
    """
    prefix = config.get("mcp_prefix") or DEFAULT_MCP_PREFIX
    search_tool = config.get("search_tool") or DEFAULT_SEARCH_TOOL
    read_tool = config.get("read_tool") or DEFAULT_READ_TOOL
    tools = ["Bash", "Write", "%s__%s" % (prefix, search_tool), "%s__%s" % (prefix, read_tool)]
    return " ".join(tools)


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
            log("dry run - would invoke: claude -p <prompt> --allowedTools %r" % tools)
            log("drop folder: %s" % out_dir)
            print(prompt)
            return 0
        producer_cmd = None  # built below, after skip-if-fresh
    else:
        die("config 'producer' must be 'claude' or 'graph', not %r" % producer)

    if args.skip_if_fresh and handoff_exists_for_today(out_dir):
        log("today's handoff already exists in %s - nothing to do" % out_dir)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    if producer == "claude":
        claude = find_claude(args.claude)
        producer_cmd = [claude, "-p", prompt, "--allowedTools", tools]
    command = keep_awake(producer_cmd)

    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        log("starting (producer=%s, out_dir=%s, attempt %d/%d)" % (producer, out_dir, attempt, attempts))
        completed = subprocess.run(command)
        if completed.returncode == 0:
            log("done")
            return 0
        log("producer exited %d - no handoff written" % completed.returncode)
        # A handoff can exist despite a non-zero exit: the transform runs before
        # the CLI's final message, so a session that dies at the very end has
        # already delivered the goods. Re-pulling would be harmless but wasteful.
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
