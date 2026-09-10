#!/usr/bin/env python3
"""claude_auth_check.py - keep the CLI's session warm, and warn before 05:00.

The producer half of meeting pre-population drives a headless `claude -p`
session. When the CLI's stored OAuth session has expired, every firing that
morning fails identically and there are no meeting notes; the operator finds
out from the dashboard hours later, and the only remedy is interactive.

This runs in the evening instead, while someone is still at the keyboard, and
does two things with one trivial request:

  warm   A successful request exercises the refresh chain and rolls the access
         token forward. If the session's lifetime is sliding -- refreshed on
         use -- this alone prevents the 05:00 expiry.

  check  If the request fails on authentication, that is known the night
         before rather than discovered from an empty vault, and it is said
         plainly enough to act on without diagnosis.

Worth being honest about the limit: if the session's lifetime is absolute
rather than sliding, warming cannot prevent expiry and only the early warning
survives. That is still most of the value -- a re-login at 21:00 costs a
minute, the same re-login discovered at 08:30 has already cost the morning's
notes.

Why a real request and not `claude auth status`: status reports the stored
credential, and it is not established that it validates that credential
against the server. On the morning this was built for, the CLI held a
credential it believed in and could not use. Only a request settles it, and a
five-token prompt is the cheapest one that does.

Scheduled by com.obsidian.claude-auth-check.plist (weekdays, evening). Safe to
run by hand:

    python3 claude_auth_check.py            # warm + check
    python3 claude_auth_check.py --dry-run  # print what it would do
"""

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPTS_DIR / ".state"
MARKER = STATE_DIR / "claude_auth_state.json"

# Kept in step with meeting_pull.AUTH_FAILURE_RE on purpose: the two scripts
# must agree on what an auth failure looks like, or this one reports healthy
# on exactly the mornings the other one dies.
AUTH_FAILURE_RE = re.compile(
    r"OAuth session expired"
    r"|Failed to authenticate"
    r"|Not logged in"
    r"|Please run /login"
    r"|authentication_error"
    r"|invalid[_ ]api[_ ]key"
    r"|Unauthorized",
    re.I)

PROBE_PROMPT = "Reply with the single word: ok"
PROBE_TIMEOUT = 120

EXIT_OK = 0
EXIT_AUTH = 3
EXIT_UNKNOWN = 4          # could not reach the service; nothing proven


def log(message):
    print("%s claude_auth_check: %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def find_claude(override):
    """Locate the CLI. Scheduled runs inherit a minimal PATH that excludes the
    user-level install location where this CLI actually lives."""
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local" / "bin" / "claude",
                 home / ".claude" / "local" / "claude",
                 Path("/opt/homebrew/bin/claude"),
                 Path("/usr/local/bin/claude")):
        if cand.is_file():
            return str(cand)
    return None


def network_ready(host="api.anthropic.com", port=443, timeout=5):
    """True if `host` resolves and accepts a connection.

    Checked before the probe so a machine with no route does not get reported
    as an authentication problem. Aug 20 failed this way -- ENOTFOUND at the
    scheduled wake -- and a check that called that "auth" would send someone
    to re-login over a Wi-Fi association delay.
    """
    try:
        socket.getaddrinfo(host, port)
    except OSError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_marker():
    try:
        return json.loads(MARKER.read_text())
    except (OSError, ValueError):
        return {}


def write_marker(state, detail):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps({
            "state": state,
            "at": _dt.datetime.now().isoformat(timespec="seconds"),
            "detail": detail[:300],
        }, indent=2) + "\n")
    except OSError as e:
        log("could not write %s: %s" % (MARKER, e))


def notify(message):
    """Best-effort desktop notification. Never let it change the exit path."""
    if sys.platform != "darwin":
        return
    script = ('display notification %s with title "Claude sign-in needed"'
              % json.dumps(message))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--claude", default=os.environ.get("CLAUDE_AUTH_CHECK_CLAUDE"),
                    help="full path to the Claude CLI (default: auto-discovered)")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would run; call nothing")
    ap.add_argument("--skip-network-check", action="store_true",
                    help="probe even if the API host looks unreachable")
    args = ap.parse_args()

    claude = find_claude(args.claude)
    if not claude:
        log("the Claude CLI was not found; nothing to check")
        return EXIT_UNKNOWN

    if args.dry_run:
        log("dry run - would probe with: %s -p %r --allowedTools ''"
            % (claude, PROBE_PROMPT))
        log("marker: %s" % MARKER)
        return EXIT_OK

    if not args.skip_network_check and not network_ready():
        # Not a finding. Reported as not knowing, and deliberately not written
        # to the marker: overwriting a real "needs re-auth" with "unknown"
        # would erase the warning this exists to raise.
        log("api.anthropic.com is not reachable, so nothing about the session "
            "can be established. Leaving the previous state alone.")
        return EXIT_UNKNOWN

    try:
        completed = subprocess.run(
            [claude, "-p", PROBE_PROMPT, "--allowedTools", ""],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("the probe did not return within %ds; nothing established"
            % PROBE_TIMEOUT)
        return EXIT_UNKNOWN

    transcript = (completed.stdout or "") + (completed.stderr or "")

    if completed.returncode == 0 and not AUTH_FAILURE_RE.search(transcript):
        was = read_marker().get("state")
        write_marker("ok", "probe succeeded")
        if was == "auth-failed":
            log("session is working again (it was failing at the last check).")
        else:
            log("session is valid; token refreshed by this request.")
        return EXIT_OK

    if AUTH_FAILURE_RE.search(transcript):
        detail = next((ln.strip() for ln in transcript.splitlines()
                       if AUTH_FAILURE_RE.search(ln)), "authentication failed")
        write_marker("auth-failed", detail)
        notify("Sign in before tomorrow morning: run `claude` then /login. "
               "Otherwise the 05:00 meeting pull will fail.")
        log("ACTION NEEDED: the Claude CLI cannot authenticate (%s). Tomorrow's "
            "05:00 meeting pull will fail unless you run `claude` in a terminal "
            "and sign in with /login." % detail)
        return EXIT_AUTH

    # Failed for some other reason. Say so; do not dress it up as auth.
    last = next((ln.strip() for ln in reversed(transcript.splitlines())
                 if ln.strip()), "no output")
    log("the probe failed (exit %d) for a reason that is not authentication: "
        "%s" % (completed.returncode, last[:200]))
    return EXIT_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
