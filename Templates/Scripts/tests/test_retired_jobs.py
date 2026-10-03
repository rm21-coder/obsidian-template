"""
test_retired_jobs.py -- a job the template retires is removed by the next update.

The Azure Blob relay was removed on 2026-09-30, but its Windows task
(\\Obsidian\\handoff-blob-pull) stayed registered on every machine that had it
(found on the ARM and x64 test laptops, 2026-10-02): Register-Tasks
only registered manifest jobs, and update.sh on macOS likewise never removed a
retired LaunchAgent. Both now remove the jobs on an explicit retired list --
never "anything not in the manifest", which would delete a user's own task.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "Templates" / "Scripts"
PSD = (SCRIPTS / "windows" / "schedules.psd1").read_text(encoding="utf-8")
REG = (SCRIPTS / "windows" / "Register-Tasks.ps1").read_text(encoding="utf-8")
LIB = (REPO / "installers" / "lib" / "update.sh").read_text(encoding="utf-8")
UNINSTALL = (REPO / "uninstall.sh").read_text(encoding="utf-8")


def _win_retired() -> list[str]:
    return re.findall(r"'([\w-]+)'", re.search(r"RetiredJobs = @\(([^)]*)\)", PSD).group(1))


def _mac_retired() -> list[str]:
    return re.search(r"RETIRED_AGENTS=\(([^)]*)\)", LIB).group(1).split()


def test_both_platforms_retire_the_same_jobs() -> None:
    win, mac = _win_retired(), _mac_retired()
    assert "handoff-blob-pull" in win
    assert sorted(f"com.obsidian.{n}" for n in win) == sorted(mac)


def test_no_retired_job_still_ships() -> None:
    live = re.findall(r"Name='([\w-]+)'", PSD)
    assert not set(_win_retired()) & set(live)
    for label in _mac_retired():
        assert not (SCRIPTS / f"{label}.plist").exists(), label
        assert label in UNINSTALL                      # uninstall removes it too


def test_windows_unregisters_retired_tasks_only_on_a_full_refresh() -> None:
    block = REG[REG.index("if (-not $Only) {\n    foreach ($retired in @($manifest.RetiredJobs))"):]
    block = block[:block.index('\nWrite-Host ""')]
    assert "Unregister-ScheduledTask -TaskName $retired -TaskPath \"$folder\\\" -Confirm:$false -ErrorAction Stop" in block
    assert "$PSCmdlet.ShouldProcess" in block          # -WhatIf shows it, changes nothing
    assert "Write-Warning" in block and "$failed +=" not in block
    REG.encode("ascii")
    PSD.encode("ascii")


def _retire(tmp_path: Path, dry: int) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.obsidian.handoff-blob-pull.plist").write_text("<plist/>", encoding="utf-8")
    (la / "com.obsidian.tag-clippings.plist").write_text("<plist/>", encoding="utf-8")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls"
    (stubs / "launchctl").write_text(f'#!/bin/sh\necho "launchctl $*" >> "{calls}"\n', encoding="utf-8")
    (stubs / "launchctl").chmod(0o755)
    backup = tmp_path / "backup"
    p = subprocess.run(
        ["bash", "-c", 'source "$1/installers/lib/common.sh"; source "$1/installers/lib/update.sh";'
                       ' retire_agents "$2" "$3" "$4"', "_",
         str(REPO), str(la), str(backup), str(dry)],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "NO_COLOR": "1"})
    return p, la, backup, calls


def test_macos_update_unloads_and_moves_aside_a_retired_agent(tmp_path, allow_subprocess) -> None:
    p, la, backup, calls = _retire(tmp_path, dry=0)
    assert p.returncode == 0, p.stdout + p.stderr
    assert not (la / "com.obsidian.handoff-blob-pull.plist").exists()
    assert (backup / "com.obsidian.handoff-blob-pull.plist").exists()        # kept, not deleted
    assert (la / "com.obsidian.tag-clippings.plist").exists()                # a live job untouched
    assert "launchctl bootout gui/" in calls.read_text() and "com.obsidian.handoff-blob-pull" in calls.read_text()
    assert "com.obsidian.handoff-blob-pull: retired upstream; unloaded and removed" in p.stdout + p.stderr


def test_macos_dry_run_only_says_so(tmp_path, allow_subprocess) -> None:
    p, la, backup, calls = _retire(tmp_path, dry=1)
    assert (la / "com.obsidian.handoff-blob-pull.plist").exists()
    assert not calls.exists() and not backup.exists()
    assert "dry run: would unload and remove it" in p.stdout + p.stderr


def test_update_sh_calls_it_in_the_jobs_step() -> None:
    upd = (REPO / "update.sh").read_text(encoding="utf-8")
    step = upd[upd.index("== 3/6 scheduled jobs =="):upd.index("# ---- 4. plugins")]
    assert 'retire_agents "$LA"' in step and '"$DRY_RUN"' in step
