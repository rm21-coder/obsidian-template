<#
.SYNOPSIS  Register (or refresh) Windows Task Scheduler jobs from schedules.psd1.
.DESCRIPTION
  Reads the job manifest and creates one scheduled task per job, named under the
  '\Obsidian' task-path. Idempotent: re-running replaces existing tasks (-Force).
  Each job's Enabled flag in the manifest is its starting state, with one
  exception on a refresh: a task that is currently enabled stays enabled, so
  re-running this never switches off a job someone turned on by hand. Jobs with
  Enabled=$true (12 of 15) go live immediately and fire on their triggers.
  Jobs with Enabled=$false (3 of 15) are registered but left DISABLED, since
  each needs a per-user resource this template can't assume exists; validate
  the script by hand, then enable it deliberately:
      Enable-ScheduledTask -TaskName source-mail-pull -TaskPath '\Obsidian'

  Every task runs its script through run_logged.py, which appends the job's
  stdout and stderr to %LOCALAPPDATA%\obsidian-logs\<task-name>.log. Task
  Scheduler has no equivalent of launchd's StandardOutPath and discards the
  output of a bare `python.exe script.py`, so without the runner no job leaves
  a log. The runner passes the job's exit code through, so LastTaskResult is
  unchanged. Re-run this script after upgrading from a version that registered
  tasks without it.

  Tasks launch pythonw.exe, not python.exe, so a job firing does not open a
  console window. (-Hidden on the settings set would not do this: it hides the
  task in the Task Scheduler UI, not the window.) Re-run this script to move an
  existing install's tasks to pythonw.exe.
.PARAMETER Only    Register just one job by name.
.PARAMETER WhatIf  Show what would be registered without changing anything.
#>
[CmdletBinding(SupportsShouldProcess)] param([string]$Only)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

$manifest   = Import-PowerShellDataFile (Join-Path $PSScriptRoot 'schedules.psd1')
$scriptsDir = Get-ScriptsDir
$python     = Get-VenvPythonW   # windowless: see common.ps1
$runner     = Join-Path $scriptsDir 'run_logged.py'
$folder     = '\Obsidian'

# Registering a task that points at a missing runner would give a task that
# fails on every trigger with nothing in any log to say why.
if (-not (Test-Path $runner)) { throw "run_logged.py not found at $runner" }

function New-TriggerFromSpec($t) {
    switch ($t.Type) {
        'MinuteInterval' {
            # NOTE: a long finite duration is used instead of [TimeSpan]::MaxValue,
            # which some Windows builds reject. ~10 years is effectively forever.
            return New-ScheduledTaskTrigger -Once -At (Get-Date) `
                     -RepetitionInterval (New-TimeSpan -Minutes $t.Minutes) `
                     -RepetitionDuration (New-TimeSpan -Days 3650)
        }
        'Daily'   { return New-ScheduledTaskTrigger -Daily -At $t.At }
        'Weekly'  { return New-ScheduledTaskTrigger -Weekly -DaysOfWeek $t.DaysOfWeek -At $t.At }
        'AtLogon' { return New-ScheduledTaskTrigger -AtLogOn }
        default   { throw "Unknown trigger type: $($t.Type)" }
    }
}

foreach ($job in $manifest.Jobs) {
    if ($Only -and $job.Name -ne $Only) { continue }

    $scriptPath = Join-Path $scriptsDir $job.Script
    if (-not (Test-Path $scriptPath)) {
        Write-Warning "Skipping $($job.Name): script not found at $scriptPath"
        continue
    }

    # run_logged.py <task-name> <script> [args]: the task name doubles as the
    # log file name. Quote both paths (handles spaces in the profile/vault
    # path) and append the job's own args.
    $argString = '"{0}" {1} "{2}"' -f $runner, $job.Name, $scriptPath
    if ($job.Args -and $job.Args.Count -gt 0) { $argString += ' ' + ($job.Args -join ' ') }

    $action   = New-ScheduledTaskAction -Execute $python -Argument $argString -WorkingDirectory $scriptsDir
    $trigger  = New-TriggerFromSpec $job.Trigger
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    $taskName = $job.Name

    # Read before -Force replaces it. Re-running this is how an install picks
    # up a change to the task action (such as the run_logged.py wrapper), and
    # it used to re-disable meeting-pull and the other opt-in jobs every time.
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath "$folder\" -ErrorAction SilentlyContinue
    $enable   = $job.Enabled -or ($existing -and $existing.State -ne 'Disabled')

    if ($PSCmdlet.ShouldProcess("$folder\$taskName", 'Register scheduled task')) {
        Register-ScheduledTask -TaskName $taskName -TaskPath $folder `
            -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        if (-not $enable) {
            Disable-ScheduledTask -TaskName $taskName -TaskPath $folder | Out-Null
            Write-Host ("  registered (DISABLED): {0}" -f $taskName)
        } elseif (-not $job.Enabled) {
            Write-Host ("  registered (ENABLED):  {0}  (kept: was enabled before this refresh)" -f $taskName)
        } else {
            Write-Host ("  registered (ENABLED):  {0}" -f $taskName)
        }
    }
}
Write-Host ""
Write-Host "Done. Review:  Get-ScheduledTask -TaskPath '\Obsidian\*' | Select State,TaskName"
Write-Host "Enable one:    Enable-ScheduledTask -TaskName tag-clippings -TaskPath '\Obsidian'"
