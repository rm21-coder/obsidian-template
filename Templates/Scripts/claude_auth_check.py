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

The lifetime turned out to be absolute. Warming every evening did not stop it
expiring on 2026-10-02 and again on 2026-11-08, about 28 and 35 days after a
sign-in, and the CLI reports no expiry date to read. So the check also keeps
the sign-in date and reminds ahead of the observed lifetime:

  age    The date of the last /login, recorded when a failing session starts
         working again (the usual way a sign-in happens) or by hand with
         --signed-in after signing in early, while the session still worked.
  remind From day REMIND_DAYS, and on a Friday evening when that day falls
         over the weekend, a notification and a log line say a sign-in is due.
         The session still works, so the exit code stays 0; the dashboard
         reads the same reminder and shows the row as "due".

The thresholds come from two observations, not a documented limit. If an
early sign-in turns out not to reset the server's clock, the reminder is
still a reminder; the evening auth failure remains the backstop.

Scheduled by com.obsidian.claude-auth-check.plist (21:00, Sunday to Friday).
Safe to run by hand:

    python3 claude_auth_check.py                         # warm + check
    python3 claude_auth_check.py --dry-run               # print what it would do
    python3 claude_auth_check.py --signed-in             # after an early /login
    python3 claude_auth_check.py --signed-in 2026-11-09  # record a past sign-in
"""

import argparse
import datetime as _dt
import json
import os
import re
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

# Observed lifetimes: ~28 days (Sep 4 -> Oct 2) and ~35 (Oct 3/4 -> Nov 7/8).
# Day 24 leaves the shorter one four evenings of warning.
REMIND_DAYS = 24


def log(message):
    print("%s claude_auth_check: %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def find_claude(override):
    """Locate the CLI. Scheduled runs inherit a minimal PATH that excludes the
    user-level install location where this CLI actually lives."""
    if override:
        return override if Path(override).is_file() else None
    # PATH entries only, never the working directory or the vault (5277585;
    # see exe_lookup).
    import exe_lookup
    found = exe_lookup.find_executable("claude")
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


def write_marker(state, detail, signed_in=None):
    """Record the probe's verdict. `signed_in` (a date) replaces the recorded
    sign-in date; otherwise the one already in the marker is carried over, so
    an ordinary evening check never forgets it."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "state": state,
            "at": _dt.datetime.now().isoformat(timespec="seconds"),
            "detail": detail[:300],
        }
        kept = signed_in.isoformat() if signed_in else read_marker().get("signed_in")
        if kept:
            data["signed_in"] = kept
        MARKER.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        log("could not write %s: %s" % (MARKER, e))


def signed_in_date(marker):
    """The recorded sign-in date, or None when there is none or it is not a
    plain YYYY-MM-DD date."""
    raw = marker.get("signed_in") if isinstance(marker, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return _dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _days_until_attended(today):
    """Days until the next evening someone is likely to see a reminder.
    Friday -> Monday is 3, Saturday -> Monday is 2; any other day is 0."""
    return {4: 3, 5: 2}.get(today.weekday(), 0)


def signin_reminder(marker=None, today=None):
    """A one-line reminder when a sign-in is due, else None.

    Due once the sign-in is REMIND_DAYS old, or on a Friday or Saturday when it
    will be by Monday. Only for a session that last probed "ok": a failed one
    already has its own, louder warning. Shared with morning_dashboard.py."""
    marker = read_marker() if marker is None else marker
    if marker.get("state") != "ok":
        return None
    since = signed_in_date(marker)
    if since is None:
        return None
    today = today or _dt.date.today()
    age = (today - since).days
    if age < 0 or age + _days_until_attended(today) < REMIND_DAYS:
        return None
    return ("Claude sign-in is %d days old (signed in %s); it has expired "
            "after 28-35 days. Run `claude`, sign in with /login, then "
            "`python3 claude_auth_check.py --signed-in`."
            % (age, since.isoformat()))


def notify(message, title="Claude sign-in needed"):
    """Best-effort desktop notification. Never let it change the exit path."""
    if sys.platform != "darwin":
        return
    script = ('display notification %s with title %s'
              % (json.dumps(message), json.dumps(title)))
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
    ap.add_argument("--signed-in", nargs="?", const="today", metavar="YYYY-MM-DD",
                    help="record a sign-in (today, or the date given); recorded "
                         "only if the probe then succeeds")
    args = ap.parse_args()

    signed_in = None
    if args.signed_in:
        try:
            signed_in = (_dt.date.today() if args.signed_in == "today"
                         else _dt.date.fromisoformat(args.signed_in))
        except ValueError:
            log("--signed-in wants a date as YYYY-MM-DD, not %r" % args.signed_in)
            return EXIT_UNKNOWN
        if signed_in > _dt.date.today():
            log("--signed-in %s is in the future; nothing recorded" % signed_in)
            return EXIT_UNKNOWN

    claude = find_claude(args.claude)
    if not claude:
        log("the Claude CLI was not found; nothing to check")
        return EXIT_UNKNOWN

    if args.dry_run:
        log("dry run - would probe with: %s -p %r --allowedTools ''"
            % (claude, PROBE_PROMPT))
        log("marker: %s" % MARKER)
        since = signed_in_date(read_marker())
        log("recorded sign-in: %s" % (since.isoformat() if since else "none"))
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
        # A failing session that works again means someone signed in since
        # the last check; within a day is as close as this can know.
        recorded = signed_in or (_dt.date.today() if was == "auth-failed" else None)
        write_marker("ok", "probe succeeded", signed_in=recorded)
        if was == "auth-failed":
            log("session is working again (it was failing at the last check).")
        else:
            log("session is valid; token refreshed by this request.")
        if recorded:
            log("sign-in recorded as %s." % recorded.isoformat())
        elif signed_in_date(read_marker()) is None:
            log("no sign-in date is recorded, so no reminder can be given; after "
                "your next /login run this with --signed-in.")
        reminder = signin_reminder()
        if reminder:
            notify(reminder, title="Claude sign-in due soon")
            log("REMINDER: " + reminder)
        return EXIT_OK

    if AUTH_FAILURE_RE.search(transcript):
        detail = next((ln.strip() for ln in transcript.splitlines()
                       if AUTH_FAILURE_RE.search(ln)), "authentication failed")
        write_marker("auth-failed", detail)
        if signed_in:
            log("--signed-in ignored: the sign-in has not taken.")
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
