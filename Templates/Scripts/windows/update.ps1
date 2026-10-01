<#
.SYNOPSIS  Bring an existing Windows install up to date in one command.
.DESCRIPTION
  Three steps, always in this order:

    1. git pull --ff-only     the new code
    2. requirements           the same pip step install.ps1 runs
    3. Register-Tasks.ps1     re-register every scheduled task

  All three are needed, and the order matters. A pull alone changes the
  scripts but installs nothing into the venv: an install from before
  2026-09-25 then still lacks tzdata, and its whole meeting pipeline cannot
  resolve a time zone. Re-registering before pulling re-applies the old task
  definitions. Fixes to how the tasks run (the run_logged.py wrapper, the
  windowless interpreter) reach an existing install only through step 3.

  The pull happens first, then the rest of this script runs again from the
  version just pulled (the -AfterPull pass). Without that, steps 2 and 3 would
  run as written in the OLD copy of this file.

  The update stops before pulling if a tracked file has local changes, and it
  never re-baselines a security control. If this machine has an integrity
  baseline, the update is expected to show up as drift. The final step says
  how to confirm that the drift is only the update before adopting it.

  First time on an install that predates this script: run `git pull` once by
  hand, then run this.

.PARAMETER AfterPull  Internal: run steps 2 and 3 (set by the first pass).
.PARAMETER From       Internal: the commit the update started from.
.EXAMPLE   powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\update.ps1
#>
[CmdletBinding()] param([switch]$AfterPull, [string]$From)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

$vault      = Get-VaultRoot
$scriptsDir = Get-ScriptsDir

if (-not $AfterPull) {
    Write-Host '== 1/3 pull =='
    # Tracked changes only: notes in the content folders are untracked and fine.
    $dirty = Invoke-Native -ErrorMessage 'git status failed' {
        git -C $vault status --porcelain --untracked-files=no
    }
    if ($dirty) {
        Write-Host 'Local changes to tracked files; not updating:' -ForegroundColor Red
        $dirty | ForEach-Object { Write-Host "  $_" }
        if (@($dirty).Count -eq 1 -and "$dirty" -match '\.obsidian/types\.json$') {
            Write-Host ''
            Write-Host 'Obsidian rewrites .obsidian/types.json when it sees new note properties.'
            Write-Host 'It only records property types, so taking the upstream copy is safe:'
            Write-Host "  git -C `"$vault`" checkout -- .obsidian/types.json"
            Write-Host 'Then run this update again.'
        }
        exit 1
    }
    $from = (Invoke-Native -ErrorMessage 'git rev-parse failed' {
        git -C $vault rev-parse --short HEAD
    }).Trim()
    Invoke-Native -ErrorMessage 'git pull failed (is the branch behind a fast-forward?)' {
        git -C $vault pull --ff-only
    }
    # Run the rest from the copy just pulled. Windows PowerShell by absolute
    # path, as security_common.py does, so a `powershell` earlier on PATH
    # cannot stand in for it.
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $ps -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -AfterPull -From $from
    exit $LASTEXITCODE
}

Write-Host '== 2/3 requirements =='
$venvPy = Join-Path $scriptsDir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { throw "No venv at $venvPy. This is not an existing install: run install.ps1." }
Install-Requirements -VenvPython $venvPy -ScriptsDir $scriptsDir

Write-Host '== 3/3 scheduled tasks =='
& (Join-Path $PSScriptRoot 'Register-Tasks.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'Update INCOMPLETE: the code and requirements are updated, but some tasks still' -ForegroundColor Red
    Write-Host 'run their old definition (see FAILED above). Re-run this update from an elevated' -ForegroundColor Red
    Write-Host 'PowerShell; it is safe to repeat.' -ForegroundColor Red
    exit 1
}

$to = (Invoke-Native -ErrorMessage 'git rev-parse failed' { git -C $vault rev-parse --short HEAD }).Trim()

# Report only. Adopting a new baseline is a decision for the person reading
# this, after they have checked the drift is the update and nothing else.
Write-Host ''
Write-Host '== integrity monitor =='
# --json only reads: no notification, no alert record. It also reports a
# missing baseline itself, so this does not have to know where state lives.
$monitor = Join-Path $scriptsDir 'integrity_monitor.py'
$prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
try { $json = & $venvPy $monitor --json 2>$null }
finally { $ErrorActionPreference = $prevEAP }
$status = 'unreadable'
try { $status = ($json | Out-String | ConvertFrom-Json).status } catch { }
if ($status -eq 'ok') {
    Write-Host '  Clean: nothing the monitor watches changed.'
} elseif ($status -eq 'no_baseline') {
    Write-Host '  No integrity baseline on this machine, so the integrity control is not active.'
    Write-Host '  See "Scheduled jobs" in docs\Windows Setup.md before setting one up.'
} else {
    Write-Host "  Status: $status. The update changed watched files, so this is expected." -ForegroundColor Yellow
    Write-Host '  Before adopting it, confirm every finding is part of this update:'
    Write-Host "    findings:  & `"$venvPy`" `"$monitor`" --json"
    Write-Host "    the diff:  git -C `"$vault`" diff --name-status $From $to -- Templates/Scripts"
    Write-Host '  Script findings must be files in that diff; task findings must be \Obsidian\ tasks;'
    Write-Host '  any state_dir or BULK_DELETE finding is NOT the update. Only then adopt it with:'
    Write-Host "    & `"$venvPy`" `"$monitor`" --update"
}

Write-Host ''
Write-Host "Updated $From -> $to." -ForegroundColor Green
exit 0
