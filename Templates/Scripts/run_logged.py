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


def exit_status(rc: int) -> int:
    """The child's return code in a form sys.exit() passes on intact.

    Windows reports exit codes as unsigned 32-bit, so a native crash comes back
    as e.g. 3221225477 (0xC0000005). sys.exit() cannot represent that in a C
    long there; the same bits as a signed value exit with the same code.
    """
    if sys.platform == "win32" and rc > 0x7FFFFFFF:
        return rc - (1 << 32)
    return rc


def run(job: str, script: str, args: list[str]) -> int:
    cmd = [sys.executable, script, *args]
    path = log_path(job)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate(path)
        fh = open(path, "ab")
    except OSError as exc:
        print(f"run_logged: cannot open {path} ({exc}); running {job} without a log",
              file=sys.stderr)
        return subprocess.run(cmd, env=child_env()).returncode

    with fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                            env=child_env()).returncode
        if rc != 0:
            # Most jobs don't timestamp their own output, so without this a
            # traceback in the file could not be tied to a run.
            fh.write(("%s run_logged: %s exited with code %d\n"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), job, rc)).encode("utf-8"))
    return rc


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: run_logged.py <job-name> <script.py> [args...]", file=sys.stderr)
        return 2
    job, script, rest = argv[0], argv[1], argv[2:]
    if not _JOB_NAME.match(job):
        print(f"run_logged: refusing job name {job!r}: it becomes a filename", file=sys.stderr)
        return 2
    return exit_status(run(job, script, rest))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
