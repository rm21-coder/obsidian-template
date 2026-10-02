#!/usr/bin/env python3
"""Run one scheduled job with its stdout and stderr appended to a per-job log.

    python run_logged.py <job-name> <script.py> [script args...]

This is what every Windows scheduled task actually executes (see
windows/Register-Tasks.ps1). It exists because Task Scheduler has nothing
like launchd's StandardOutPath: a task that runs `python.exe script.py`
directly has its output thrown away, so on Windows the tagger, classifier,
dashboard and the rest used to leave no log anywhere, and a failure was
visible only as a bare LastTaskResult code.

The log is %LOCALAPPDATA%\\obsidian-logs\\<job-name>.log, one file per task,
stdout and stderr interleaved in the order they were written. That directory
sits beside obsidian-usage / obsidian-security but deliberately NOT inside
obsidian-security: the integrity monitor watches that directory, and a log
growing there would raise a drift alert every day.

Done here rather than in PowerShell because this is the one layer every job
already shares, it can be tested in the suite, and it sidesteps two Windows
PowerShell 5.1 traps: `>>` writes UTF-16, and a native command's stderr lines
come back wrapped as error records.

The job's exit code is passed through unchanged, so LastTaskResult (and the
dashboard's pipeline-health section, which reads it) means what it did
before. The log is observability: if it cannot be opened, the job still runs.

No console windows. Tasks launch this file with pythonw.exe, which has no
console, and this starts the job with the console python.exe beside it under
CREATE_NO_WINDOW: a console that exists but is never shown. The job must not
simply run under pythonw.exe too. Every console program a job starts (the
claude CLI under meeting-pull, PowerShell for a toast) would then find no
console to inherit and open a visible window of its own; under a hidden
console they inherit it. Toasts are not console windows and stay visible.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOG_DIRNAME = "obsidian-logs"
# How the directory is written for a person to paste into Explorer or cmd --
# the Windows counterpart of writing ~/Library/Logs on macOS.
LOG_DIR_DISPLAY = "%LOCALAPPDATA%\\" + LOG_DIRNAME

# One rotated generation per job. The macOS logs have no rotation at all, and
# on a real install the tagger's reached 7.9 MB in five months (~50 KB/day);
# the alert log that got a size cap in security_common reached 342 MB. At
# 5 MB a quiet job keeps months of history, and the worst case across every
# job is bounded instead of open-ended.
LOG_MAX_BYTES = 5 * 1024 * 1024

# Task names come from schedules.psd1, but the name is also a filename here,
# so refuse anything that could step outside the log directory.
_JOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / LOG_DIRNAME


def log_path(job: str) -> Path:
    return log_dir() / f"{job}.log"


def display_path(job: str) -> str:
    return f"{LOG_DIR_DISPLAY}\\{job}.log"


def rotate(path: Path, max_bytes: int | None = None) -> None:
    """Move a full log to <name>.1, replacing the previous generation.

    Checked once per run, before the job starts, so a single run is never
    split across two files. Best effort: on Windows a rename fails while
    anything holds the file open (someone tailing it, say), and in that case
    the log simply grows until a later run finds it free.
    """
    if max_bytes is None:
        max_bytes = LOG_MAX_BYTES
    try:
        if path.stat().st_size < max_bytes:
            return
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def child_env() -> dict:
    env = dict(os.environ)
    # With stdout on a file rather than a console, Windows Python encodes
    # output in the ANSI code page, and one print() of a character outside it
    # (a note title, a model's reply) raises UnicodeEncodeError and kills the
    # job. The redirect is what introduces that failure, so it also fixes it.
    # setdefault: a deliberate setting in the user's environment wins.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Unbuffered, so stdout and stderr land in the file in the order they were
    # written. Otherwise block-buffered stdout arrives after a traceback that
    # it actually preceded.
    env["PYTHONUNBUFFERED"] = "1"
    return env


# Only defined on Windows. The literal is the documented value, for the tests
# that exercise the Windows branch from another platform.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def child_command(script: str, args: list[str]) -> list[str]:
    """The job's command line: the console interpreter, even under pythonw.

    See the module docstring for why the child must be python.exe. If the
    console interpreter is missing, pythonw.exe runs the job instead: it still
    works, and only a grandchild console program would show a window.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.is_file():
            exe = console
    return [str(exe), script, *args]


def spawn_options() -> dict:
    """Extra subprocess.run keywords: on Windows, a console that is never shown."""
    if sys.platform == "win32":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def diag(message: str) -> None:
    """Report a problem with the runner itself, somewhere that survives.

    Under pythonw.exe sys.stderr is None and print() to it silently does
    nothing, so without the fallback these would be the one class of failure
    that leaves no trace at all. The fallback file sits beside the job logs.
    """
    if sys.stderr is not None:
        print(message, file=sys.stderr)
        return
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        with open(log_dir() / "run_logged.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def exit_status(rc: int) -> int:
    """The child's return code in a form sys.exit() passes on intact.

    Windows reports exit codes as unsigned 32-bit, so a native crash comes back
    as e.g. 3221225477 (0xC0000005). sys.exit() cannot represent that in a C
    long there; the same bits as a signed value exit with the same code.
    """
    if sys.platform == "win32" and rc > 0x7FFFFFFF:
        return rc - (1 << 32)
    return rc


# A time limit, in seconds, for this one run. Set only by the dashboard's
# handler (windows/dashboard_action.py); scheduled runs have none here, Task
# Scheduler's own limit applies to them. It is enforced in this process,
# which knows the job's process: on expiry the whole tree is stopped, not
# just the job's interpreter -- a meeting pull's claude CLI included.
TIMEOUT_ENV = "RUN_LOGGED_TIMEOUT"
TIMED_OUT = 124
# By absolute path, as security_common calls system tools: a bare name would
# be looked up in the interpreter's own folder and the working directory
# (both user-writable) before System32.
TASKKILL_EXE = str(Path(os.environ.get("SystemRoot") or r"C:\Windows")
                   / "System32" / "taskkill.exe")


def _timeout() -> float | None:
    try:
        t = float(os.environ.get(TIMEOUT_ENV, ""))
    except ValueError:
        return None
    return t if t > 0 else None


def _stop_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run([TASKKILL_EXE, "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=60, **spawn_options())
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        pass


def _run_child(cmd: list[str], job: str, **kw) -> int:
    limit = _timeout()
    if limit is None:
        return subprocess.run(cmd, env=child_env(), **spawn_options(), **kw).returncode
    extra = {} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, env=child_env(), **spawn_options(), **extra, **kw)
    try:
        return proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        _stop_tree(proc)
        # In the job's own log too: that is the file the toast points to.
        _note(job, f"run_logged: {job} ran past {limit:.0f}s; stopped it and its children")
        return TIMED_OUT


def run(job: str, script: str, args: list[str]) -> int:
    cmd = child_command(script, args)
    path = log_path(job)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate(path)
        fh = open(path, "ab")
    except OSError as exc:
        diag(f"run_logged: cannot open {path} ({exc}); running {job} without a log")
        return _run_child(cmd, job)

    with fh:
        rc = _run_child(cmd, job, stdout=fh, stderr=subprocess.STDOUT)
        if rc != 0:
            # Most jobs don't timestamp their own output, so without this a
            # traceback in the file could not be tied to a run.
            fh.write(("%s run_logged: %s exited with code %d\n"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), job, rc)).encode("utf-8"))
    return rc


# One run of a job at a time. Task Scheduler already refuses a second
# instance of the same task, but the dashboard's buttons
# (windows/dashboard_action.py) start the same jobs outside Task Scheduler, so
# without this a scheduled rag-sync could start while a clicked one was still
# running. The click takes this same lock itself and names the job here, so
# the run it starts does not wait on its own lock.
LOCK_HELD_ENV = "RUN_LOGGED_LOCK_HELD"


def job_lock_name(job: str) -> str:
    return f"run-{job}"


def _note(job: str, message: str) -> None:
    """A line about the runner itself, in the job's own log and in diag()."""
    diag(message)
    try:
        log_path(job).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path(job), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _busy(job: str) -> int:
    _note(job, f"run_logged: {job} is already running; not starting a second copy")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        diag("usage: run_logged.py <job-name> <script.py> [args...]")
        return 2
    job, script, rest = argv[0], argv[1], argv[2:]
    if not _JOB_NAME.match(job):
        diag(f"run_logged: refusing job name {job!r}: it becomes a filename")
        return 2
    lock = None
    if os.environ.get(LOCK_HELD_ENV) != job:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import script_lock
        lock, error = script_lock.try_acquire(job_lock_name(job))
        if error:
            # Fail open: the lock only keeps a dashboard click and a
            # scheduled run of the same job apart. A broken lock directory
            # must not stop every scheduled job -- least of all silently,
            # with a success code that shows green on the dashboard.
            _note(job, f"run_logged: {error}; running {job} without its single-run guard")
        elif lock is None:
            return _busy(job)
    try:
        return exit_status(run(job, script, rest))
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
