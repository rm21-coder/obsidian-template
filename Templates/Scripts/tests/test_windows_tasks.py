"""
test_windows_tasks.py -- static checks on how Register-Tasks.ps1 starts each
Windows task (pwsh is not available to the suite, so the strings are pinned).

Two findings from the 2026-10-20 review:

  5278618 / 5278616 / 5277585  Every task started in the vault's
      Templates\\Scripts. On Windows a program run by bare name (CreateProcess)
      or found through shutil.which is looked up in the working directory
      first, so a planted ffprobe.cmd, brctl.exe or claude.cmd there ran
      inside the job. Tasks now start in %LOCALAPPDATA%\\obsidian-logs.
  5278625  podcast-watch's per-episode limit (14,400 s) was longer than the
      run_logged kill (TimeLimitMinutes*60-120 = 10,680 s), so a long episode
      was killed from outside and retried forever. The manifest now passes a
      --timeout that fires first.
"""
from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
WIN = SCRIPTS / "windows"
REG_BYTES = (WIN / "Register-Tasks.ps1").read_bytes()
PSD_BYTES = (WIN / "schedules.psd1").read_bytes()
REG = REG_BYTES.decode("ascii")
PSD = PSD_BYTES.decode("ascii")

# The margin podcast_watch needs after its own limit fires: stop
# podcast_transcribe, write the error log, move the drop to failed\.
STOP_MARGIN_SEC = 300


def _job_line(name: str) -> str:
    [line] = [l for l in PSD.splitlines()
              if f"Name='{name}'" in l and not l.lstrip().startswith("#")]
    return line


# ---- encoding ----------------------------------------------------------------

def test_both_files_are_ascii_with_crlf_line_ends():
    for name, data in (("Register-Tasks.ps1", REG_BYTES), ("schedules.psd1", PSD_BYTES)):
        data.decode("ascii")                                  # raises on non-ASCII
        assert data.count(b"\n") == data.count(b"\r\n"), f"{name} has a bare LF"


# ---- working directory (5278618, 5278616, 5277585) -------------------------

def test_non_isolated_tasks_start_in_the_log_folder_not_the_vault():
    assert "$jobWorkDir = Join-Path $env:LOCALAPPDATA 'obsidian-logs'" in REG
    assert "$workDir    = $jobWorkDir" in REG
    assert "$workDir    = $controlsDir" in REG                # the isolated pair
    assert "$workDir    = $scriptsDir" not in REG
    assert re.search(r"-WorkingDirectory \$scriptsDir\b", REG) is None
    assert ("New-ScheduledTaskAction -Execute $jobPython -Argument $argString "
            "-WorkingDirectory $workDir") in REG


def test_the_working_directory_is_created_and_read_back():
    """Task Scheduler will not start a task whose WorkingDirectory is
    missing, and a registration that 'succeeded' with the old folder still
    in place would leave the hole open behind a green line."""
    create = REG.index("New-Item -ItemType Directory -Path $workDir")
    register = REG.index("Register-ScheduledTask -TaskName $taskName")
    assert create < register
    assert "$live.Actions[0].WorkingDirectory -eq $workDir" in REG


def test_the_log_folder_is_run_loggeds():
    import run_logged
    assert run_logged.LOG_DIRNAME == "obsidian-logs"


def test_every_task_script_goes_by_full_path():
    """With the working directory moved, a relative script path would no
    longer resolve; the action must name the script in full."""
    assert "$scriptPath = Join-Path $scriptsDir $job.Script" in REG
    assert "$scriptPath = Join-Path $controlsDir $job.Script" in REG
    assert '--timeout {2} {3} "{4}"\' -f $jobFlags, $jobRunner' in REG


# ---- podcast-watch's own limit fits inside the task's (5278625) -------------

def test_podcast_watch_timeout_fires_before_run_logged_stops_the_run():
    line = _job_line("podcast-watch")
    args = re.search(r"Args=@\(([^)]*)\)", line).group(1)
    items = [a.strip().strip("'") for a in args.split(",")]
    assert items[0] == "--once"
    timeout = int(items[items.index("--timeout") + 1])
    limit_min = int(re.search(r"TimeLimitMinutes=(\d+)", line).group(1))
    # Register-Tasks: run_logged --timeout = max(60, limit*60 - 120)
    assert "([Math]::Max(60, $limitMin * 60 - 120))" in REG
    kill = max(60, limit_min * 60 - 120)
    assert timeout + STOP_MARGIN_SEC <= kill, (timeout, kill)
    assert timeout >= 2 * 3600          # still room for a long episode on CPU


def test_podcast_watch_default_is_documented_as_longer_than_the_task():
    """The default stays for macOS, where launchd puts no limit on the run;
    that is why Windows must pass its own."""
    import podcast_watch
    line = _job_line("podcast-watch")
    kill = int(re.search(r"TimeLimitMinutes=(\d+)", line).group(1)) * 60 - 120
    assert podcast_watch.DEFAULT_TIMEOUT > kill
    assert "--timeout" in line
