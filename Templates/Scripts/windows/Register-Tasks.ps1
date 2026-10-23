<#
.SYNOPSIS  Register (or refresh) Windows Task Scheduler jobs from schedules.psd1.
.DESCRIPTION
  Reads the job manifest and creates one scheduled task per job, named under the
  '\Obsidian' task-path. Idempotent: re-running replaces existing tasks (-Force).
  Each job's Enabled flag in the manifest is its starting state, with one
  exception on a refresh: a task that is currently enabled stays enabled, so
  re-running this never switches off a job someone turned on by hand. Jobs with
  Enabled=$true go live immediately and fire on their triggers (rag-sync only
  where the local RAG stack is set up). Jobs with Enabled=$false are
  registered but left DISABLED, since
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

  Every task starts in %LOCALAPPDATA%\obsidian-logs (the security tasks in
  their installed copy's folder), never in the vault's Templates\Scripts: a
  program started by bare name, or found through shutil.which, is looked up
  in the working directory first on Windows, and the vault is a synced
  folder (findings 5278618, 5278616, 5277585). The scripts find their own
  folder from __file__, so nothing depends on the working directory.

  The two security tasks (Isolated=$true in the manifest) are the exception:
  they run an installed copy of run_logged.py and the control from
  %LOCALAPPDATA%\obsidian-security\bin under the base pythonw.exe with -I -S,
  so no code from the vault they watch runs inside them. This script installs
  that copy before registering them.
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
# The working directory of every non-isolated task: run_logged.py's own log
# folder, outside the vault and outside any sync client. Created here because
# Task Scheduler refuses to start a task whose WorkingDirectory is missing,
# and run_logged would otherwise create it only on its first run.
$jobWorkDir = Join-Path $env:LOCALAPPDATA 'obsidian-logs'

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

# How long one run may take before Task Scheduler ends it. A task left at the
# default (72 hours) with the default IgnoreNew instance policy let one hung
# tagger run block every 30-minute start for over a day (2026-10-11). A job
# may set TimeLimitMinutes in schedules.psd1; otherwise an interval job gets
# twice its interval (at least 15 minutes) and a daily or weekly job 2 hours.
function Get-TimeLimitMinutes($job) {
    if ($job.TimeLimitMinutes) { return [int]$job.TimeLimitMinutes }
    if ($job.Trigger.Type -eq 'MinuteInterval') {
        return [Math]::Max(2 * [int]$job.Trigger.Minutes, 15)
    }
    return 120
}

# Every task that did not end up as intended. Reported at the end and turned
# into a non-zero exit: this script is how an existing install receives a fix,
# so a silent failure here leaves the old behaviour running behind a screen of
# "registered" lines.
$failed = @()

# Jobs marked RequiresRag run only where the optional local-LLM RAG layer is
# set up. Asked once, of rag_status.py, through the console python.exe: the
# windowless pythonw.exe is a GUI program, which PowerShell does not wait for,
# so its exit code would not be this run's.
$ragSetUp = $false
if ($manifest.Jobs | Where-Object { $_.RequiresRag }) {
    $ragCheck = Join-Path $scriptsDir 'rag_status.py'
    $console  = Get-VenvPython
    if ((Test-Path -LiteralPath $ragCheck) -and (Test-Path -LiteralPath $console)) {
        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & $console $ragCheck *> $null; $ragSetUp = ($LASTEXITCODE -eq 0) }
        catch { $ragSetUp = $false }
        finally { $ErrorActionPreference = $prevEAP }
    }
}

# Jobs marked Isolated (the two security controls, finding 5278112) run an
# installed copy outside the vault -- run_logged.py and the control both --
# under the base interpreter with -I -S. The copy is refreshed here, from the
# vault's scripts, before any of those tasks is registered against it: every
# install and update re-runs this script.
$isolatedJobs = @($manifest.Jobs | Where-Object { $_.Isolated -and (-not $Only -or $_.Name -eq $Only) })
$controlsDir = Get-ControlsDir
$controlsReady = $false
if ($isolatedJobs.Count -gt 0) {
    try {
        $controlPython = Get-ControlPython -Windowless
        if ($PSCmdlet.ShouldProcess($controlsDir, 'Install the security controls')) {
            Install-SecurityControls
        }
        $controlsReady = $true
    } catch {
        Write-Warning "  could not install the security controls: $_"
    }
}

foreach ($job in $manifest.Jobs) {
    if ($Only -and $job.Name -ne $Only) { continue }

    if ($job.Isolated) {
        if (-not $controlsReady) {
            # Left as it was rather than pointed at a copy that is not there.
            $failed += "$($job.Name) (security controls not installed in $controlsDir)"
            continue
        }
        $jobRunner  = Join-Path $controlsDir 'run_logged.py'
        $scriptPath = Join-Path $controlsDir $job.Script
        $jobPython  = $controlPython
        $jobFlags   = '-I -S -B '
        $workDir    = $controlsDir
    } else {
        $jobRunner  = $runner
        $scriptPath = Join-Path $scriptsDir $job.Script
        $jobPython  = $python
        $jobFlags   = ''
        $workDir    = $jobWorkDir
    }
    if (-not (Test-Path $scriptPath)) {
        Write-Warning "Skipping $($job.Name): script not found at $scriptPath"
        continue
    }
    if (-not (Test-Path -LiteralPath $workDir)) {
        try { New-Item -ItemType Directory -Path $workDir -Force -ErrorAction Stop | Out-Null }
        catch {
            $failed += "$($job.Name) (could not create its working directory $workDir)"
            continue
        }
    }

    # run_logged.py <task-name> <script> [args]: the task name doubles as the
    # log file name. Quote both paths (handles spaces in the profile/vault
    # path) and append the job's own args.
    # --timeout: run_logged's own limit, two minutes under Task Scheduler's.
    # When it fires, run_logged stops the job's whole process tree and the job
    # has already dumped its stacks into its log (job_boot.py); Task
    # Scheduler's limit alone ends only run_logged and orphans the job.
    $limitMin  = Get-TimeLimitMinutes $job
    $argString = '{0}"{1}" --timeout {2} {3} "{4}"' -f $jobFlags, $jobRunner, ([Math]::Max(60, $limitMin * 60 - 120)), $job.Name, $scriptPath
    if ($job.Args -and $job.Args.Count -gt 0) { $argString += ' ' + ($job.Args -join ' ') }

    $action   = New-ScheduledTaskAction -Execute $jobPython -Argument $argString -WorkingDirectory $workDir
    $trigger  = New-TriggerFromSpec $job.Trigger
    $limit    = New-TimeSpan -Minutes $limitMin
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
                  -ExecutionTimeLimit $limit
    $taskName = $job.Name

    # Read before -Force replaces it. Re-running this is how an install picks
    # up a change to the task action (such as the run_logged.py wrapper), and
    # it used to re-disable meeting-pull and the other opt-in jobs every time.
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath "$folder\" -ErrorAction SilentlyContinue
    $enable   = $job.Enabled -or ($existing -and $existing.State -ne 'Disabled')
    if ($job.RequiresRag) { $enable = $ragSetUp }

    if ($PSCmdlet.ShouldProcess("$folder\$taskName", 'Register scheduled task')) {
        # -ErrorAction Stop on each call, not the preference set at the top:
        # the ScheduledTasks cmdlets are CDXML functions that run in their own
        # module scope and do not see this script's $ErrorActionPreference. A
        # denied registration was a non-terminating error, so the loop went on
        # to print "registered" for a task it had not touched.
        try {
            Register-ScheduledTask -TaskName $taskName -TaskPath $folder `
                -Action $action -Trigger $trigger -Settings $settings -Force `
                -ErrorAction Stop | Out-Null
            if (-not $enable) {
                Disable-ScheduledTask -TaskName $taskName -TaskPath $folder -ErrorAction Stop | Out-Null
            }
        } catch {
            Write-Warning ("NOT registered: {0} -- {1}" -f $taskName, $_.Exception.Message)
            $failed += $taskName
            continue
        }
        # Read it back. "No error" is not the claim being printed; "the task
        # now runs this" is, so that is what gets checked.
        $live = Get-ScheduledTask -TaskName $taskName -TaskPath "$folder\" -ErrorAction SilentlyContinue
        $liveLimit = $null
        try { $liveLimit = [System.Xml.XmlConvert]::ToTimeSpan([string]$live.Settings.ExecutionTimeLimit) } catch { }
        $ok = $live -and $live.Actions[0].Execute -eq $jobPython -and
              $live.Actions[0].Arguments -eq $argString -and
              $live.Actions[0].WorkingDirectory -eq $workDir -and
              $liveLimit -eq $limit -and
              (($live.State -eq 'Disabled') -eq (-not $enable))
        if (-not $ok) {
            Write-Warning ("NOT registered: {0} -- the task does not show the new definition" -f $taskName)
            $failed += $taskName
            continue
        }
        if (-not $enable -and $job.RequiresRag) {
            Write-Host ("  registered (DISABLED): {0}  (RAG is not set up here; it is enabled once OBSIDIAN_COLLECTION_ID is set)" -f $taskName)
        } elseif (-not $enable -and $taskName -eq 'meeting-pull' -and
                  (Test-Path (Join-Path $scriptsDir '.config\meeting_pull.json'))) {
            # Configured but off: say so loudly. It is not enabled for you --
            # the opt-in is a hand run that proves the connector first -- but
            # left quiet, it read as "0 meetings" for a week (2026-10-09).
            Write-Warning ("registered (DISABLED): {0} -- it is CONFIGURED (meeting_pull.json exists) but will not run." -f $taskName)
            Write-Warning "  Confirm it with: & `"$(Get-VenvPython)`" `"$(Join-Path $scriptsDir 'meeting_pull.py')`" --dry-run"
            Write-Warning "  then: Enable-ScheduledTask -TaskPath '\Obsidian\' -TaskName 'meeting-pull'"
        } elseif (-not $enable) {
            Write-Host ("  registered (DISABLED): {0}" -f $taskName)
        } elseif (-not $job.Enabled) {
            Write-Host ("  registered (ENABLED):  {0}  (kept: was enabled before this refresh)" -f $taskName)
        } else {
            Write-Host ("  registered (ENABLED):  {0}" -f $taskName)
        }
    }
}
# Retired jobs: unregister any still present. Skipped under -Only (a refresh
# of one job). A failure is a warning, not a FAILED entry: a leftover retired
# task is disabled or failing, not running old code in place of new.
if (-not $Only) {
    foreach ($retired in @($manifest.RetiredJobs)) {
        if (-not $retired) { continue }
        $old = Get-ScheduledTask -TaskName $retired -TaskPath "$folder\" -ErrorAction SilentlyContinue
        if (-not $old) { continue }
        if ($PSCmdlet.ShouldProcess("$folder\$retired", 'Unregister retired scheduled task')) {
            try {
                Unregister-ScheduledTask -TaskName $retired -TaskPath "$folder\" -Confirm:$false -ErrorAction Stop
                Write-Host ("  removed (retired upstream): {0}" -f $retired)
            } catch {
                Write-Warning ("could not remove retired task {0} -- {1}. If it was registered from an elevated prompt, run this from PowerShell opened with 'Run as administrator'." -f $retired, $_.Exception.Message)
            }
        }
    }
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host ("FAILED: {0} task(s) were NOT registered and still run their old definition:" -f $failed.Count) -ForegroundColor Red
    foreach ($n in $failed) { Write-Host ("  {0}" -f $n) -ForegroundColor Red }
    Write-Host "If the error was 'Access is denied', the existing tasks were registered from an"
    Write-Host "elevated prompt. Re-run this script from PowerShell opened with 'Run as administrator'."
    exit 1
}
Write-Host "Done. Review:  Get-ScheduledTask -TaskPath '\Obsidian\' | Select State,TaskName"
Write-Host "Enable one:    Enable-ScheduledTask -TaskName tag-clippings -TaskPath '\Obsidian'"
# Explicit, so a caller's $LASTEXITCODE reflects this run and not whatever
# native command ran before it.
exit 0
