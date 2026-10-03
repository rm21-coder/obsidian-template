#!/usr/bin/env python3
"""dashboard_action.py -- the Windows handler for the Morning Dashboard's buttons.

The dashboard is a static file:// page, and a browser never lets a page start
a local program. So its buttons are links to a custom URL scheme,
obsidian-dashboard://run/<action>. On macOS DashboardActions.app answers that
scheme (build_dashboard_actions_app.sh); on Windows a per-user registry key
written by install.ps1 / update.ps1 (Register-DashboardActions in
common.ps1) points it here, run with the venv's pythonw.exe:

    pythonw.exe dashboard_action.py "obsidian-dashboard://run/<action>"

ANY web page can fire a registered scheme, not just the dashboard -- the
browser's "open this app?" prompt is the only thing in the way, and "always
allow" removes it. So this accepts exactly three actions, takes nothing else
from the URL, and each one only runs a job that already runs on a schedule:

    pull-meetings      meeting_pull.py        (the meeting-pull task's job)
    refresh-dashboard  morning_dashboard.py   (morning-dashboard)
    refresh-rag        obsidian-rag-sync.py   (rag-sync)

There is deliberately no action that adopts a security baseline, and there
must never be one: a page that could fire it would erase a tamper the
integrity controls had just detected (see dashboard_actions.sh).

Each job runs the way its scheduled task runs it -- through run_logged.py,
appending to the same %LOCALAPPDATA%\\obsidian-logs\\<task>.log, with no
console window -- except that pull-meetings drops the task's --skip-if-fresh:
a click means "pull now". Dropping it also drops meeting_pull's own guard
against retrying a sign-in refusal recorded today, so that guard is applied
here instead. An action whose task is disabled or missing is reported as not
set up rather than run: the meeting pull ships disabled until a profile turns
it on.

Since any page can fire the scheme, repetition is bounded too:
  - one run at a time per job: the click takes run_logged's per-job lock, the
    one a scheduled run of the same job takes, so a click and a scheduled run
    never overlap either way;
  - a cooldown between clicks of one action (COOLDOWN_SEC);
  - a time limit on each run (TIMEOUT_SEC), so a hung job cannot hold its
    lock -- and every later click -- indefinitely.
A toast says when an action starts and how it ended. Every click, refused or
not, and any failure of this handler itself, goes to dashboard-actions.log.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

# action -> (scheduled task, script, args, label)
ACTIONS: dict[str, tuple[str, str, list[str], str]] = {
    "pull-meetings":     ("meeting-pull",      "meeting_pull.py",      [], "Pull meetings"),
    "refresh-dashboard": ("morning-dashboard", "morning_dashboard.py", [], "Refresh dashboard"),
    "refresh-rag":       ("rag-sync",          "obsidian-rag-sync.py", [], "Refresh RAG index"),
}

# The whole URL, or nothing: scheme, the fixed "run" segment, one action name,
# at most a trailing slash (some browsers add one). No query, no fragment, no
# further path -- nothing past the name is ever read.
# ASCII only: without re.ASCII, IGNORECASE lets [a-z] match the long s and
# the dotless i, so "obſidian-dashboard://" would match.
_URL = re.compile(r"\Aobsidian-dashboard://run/([a-z-]{1,40})/?\Z", re.IGNORECASE | re.ASCII)

# Seconds between two runs of one action started from the dashboard.
COOLDOWN_SEC = {"pull-meetings": 600, "refresh-dashboard": 120, "refresh-rag": 120}
# Longest a run started from the dashboard may take. meeting_pull's own
# producer timeout is 600 s per attempt, up to three attempts.
TIMEOUT_SEC = {"pull-meetings": 45 * 60, "refresh-dashboard": 15 * 60, "refresh-rag": 4 * 3600}

TASK_PATH = "\\Obsidian\\"
CREATE_NO_WINDOW = 0x08000000
POWERSHELL_EXE = str(Path(os.environ.get("SystemRoot") or r"C:\Windows")
                     / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def action_from_url(url: str) -> str | None:
    """The allowlisted action a URL names, or None."""
    m = _URL.match(url or "")
    if not m:
        return None
    name = m.group(1).lower()
    return name if name in ACTIONS else None


def _log_dir() -> Path:
    import run_logged
    return run_logged.log_dir()


def log(message: str) -> None:
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(d / "dashboard-actions.log", "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:            # never let logging be what ends a click
        pass


def notify(title: str, message: str) -> None:
    try:
        import security_common
        # No double quotes: Windows PowerShell 5.1 splits a -File argument
        # that contains one.
        security_common.notify(title, message.replace('"', "'"))
    except Exception:
        pass


def task_state(task: str) -> str | None:
    """'Ready', 'Running', 'Disabled', ... for \\Obsidian\\<task>, or None if
    the task does not exist (or cannot be read). Read through
    Get-ScheduledTask, whose State is an enum name -- schtasks.exe prints a
    localized status, which would misread on a non-English Windows."""
    script = (f"$t = Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{task}' "
              "-ErrorAction SilentlyContinue; if ($t) { [string]$t.State }")
    try:
        p = subprocess.run([POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    state = (p.stdout or "").strip()
    return state or None


def run_job(task: str, script: str, args: list[str], timeout: int) -> int:
    """Run the job as its scheduled task does: run_logged.py under this
    interpreter (the venv's pythonw), which starts the console python beside
    it with no window and appends to the task's own log. The caller holds
    run_logged's lock for this job, and says so in the environment."""
    # The limit is enforced by run_logged, which stops the job's whole
    # process tree; this process waits a little longer only as a backstop
    # in case run_logged itself hangs.
    import run_logged
    cmd = [sys.executable, str(SCRIPTS / "run_logged.py"), task, script, *args]
    env = {**os.environ, run_logged.LOCK_HELD_ENV: task, run_logged.TIMEOUT_ENV: str(timeout)}
    return subprocess.run(cmd, cwd=str(SCRIPTS), env=env, timeout=timeout + 300,
                          creationflags=CREATE_NO_WINDOW).returncode


def _stamp_path(action: str) -> Path:
    import script_lock
    return script_lock.LOCK_DIR / f"dashboard-{action}.last"


def cooling_down(action: str, now: float) -> int:
    """Seconds left before `action` may run again from the dashboard (0 if it
    may run now)."""
    try:
        last = float(_stamp_path(action).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0
    left = int(last + COOLDOWN_SEC[action] - now)
    return left if 0 < left <= COOLDOWN_SEC[action] else 0


def mark_started(action: str, now: float) -> None:
    try:
        p = _stamp_path(action)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{now:.0f}\n", encoding="ascii")
    except OSError as exc:
        log(f"{action}: could not record the start ({exc}); its cooldown will not apply")


def rag_set_up() -> bool:
    """Whether the optional local-LLM RAG layer is configured here (see
    rag_status.py). A failed check counts as not set up."""
    try:
        import rag_status
        return rag_status.configured()
    except Exception:
        return False


def pull_refused_today() -> str | None:
    """Today's recorded sign-in refusal for the meeting pull, if any -- the
    guard the scheduled run applies through --skip-if-fresh."""
    try:
        import meeting_pull
        return meeting_pull.auth_block_active()
    except Exception:
        return None


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except Exception as exc:                     # pythonw: stderr goes nowhere
        import traceback
        log("handler failed: " + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip())
        notify("Obsidian dashboard", "Failed (internal error). See %LOCALAPPDATA%\\obsidian-logs\\dashboard-actions.log")
        return 1


def _main(argv: list[str]) -> int:
    url = argv[0] if len(argv) == 1 else ""
    action = action_from_url(url)
    log(f"click: {[a[:200] for a in argv]!r} -> {action or 'REFUSED'}")
    if action is None:
        # More than one argument means the link carried quotes that split it
        # on the way here: refused, not read past its first part.
        notify("Obsidian dashboard", "Refused a dashboard link that is not one of its three buttons.")
        return 2
    task, script, args, label = ACTIONS[action]
    title = f"Dashboard: {label}"

    if action == "refresh-rag" and not rag_set_up():
        # The dashboard draws no RAG button here; this is a typed or forged link.
        notify(title, "Not set up on this machine: the local RAG layer is not configured.")
        log(f"{action}: RAG is not set up here")
        return 3

    state = task_state(task)
    if state is None:
        notify(title, f"Not installed here: there is no {task} scheduled task. Re-run install.ps1.")
        log(f"{action}: no {task} task")
        return 3
    if state == "Disabled":
        notify(title, f"Not set up on this machine: the {task} job is disabled. "
                      "See Scheduled jobs in docs\\Windows Setup.md.")
        log(f"{action}: {task} is disabled")
        return 3
    if state == "Running":
        notify(title, "Already running as a scheduled job.")
        log(f"{action}: {task} already running")
        return 0
    if action == "pull-meetings":
        refused = pull_refused_today()
        if refused:
            notify(title, "Not retrying: the Claude sign-in was refused earlier today. "
                          f"See %LOCALAPPDATA%\\obsidian-logs\\{task}.log")
            log(f"{action}: today's {refused} sign-in block is set")
            return 3

    import run_logged
    import script_lock
    lock, error = script_lock.try_acquire(run_logged.job_lock_name(task))
    if error:
        notify(title, "Failed: could not take its run lock. "
                      "See %LOCALAPPDATA%\\obsidian-logs\\dashboard-actions.log")
        log(f"{action}: {error}")
        return 1
    if lock is None:
        notify(title, "Already running.")
        log(f"{action}: {task} already running")
        return 0
    try:
        now = time.time()
        wait = cooling_down(action, now)
        if wait:
            notify(title, f"Ran moments ago; try again in {max(1, wait // 60)} min.")
            log(f"{action}: cooling down, {wait}s left")
            return 0
        mark_started(action, now)
        notify(title, "Started.")
        try:
            rc = run_job(task, script, args, TIMEOUT_SEC[action])
        except subprocess.TimeoutExpired:
            rc = run_logged.TIMED_OUT
        if rc == run_logged.TIMED_OUT:
            log(f"{action}: stopped after {TIMEOUT_SEC[action]}s")
            notify(title, f"Stopped: it ran past {TIMEOUT_SEC[action] // 60} min. "
                          f"See %LOCALAPPDATA%\\obsidian-logs\\{task}.log")
            return rc
    finally:
        lock.close()
    log(f"{action}: exit {rc}")
    if rc == 0:
        notify(title, "Finished.")
    else:
        notify(title, f"Failed (exit {rc}). See %LOCALAPPDATA%\\obsidian-logs\\{task}.log")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
