<#
.SYNOPSIS  Register (or refresh) Windows Task Scheduler jobs from schedules.psd1.
.DESCRIPTION
  Reads the job manifest and creates one scheduled task per job, named under the
  '\Obsidian' task-path. Idempotent: re-running replaces existing tasks (-Force).
  Each job's Enabled flag in the manifest is honoured as-is. Jobs with
  Enabled=$true (12 of 15) go live immediately and fire on their triggers.
  Jobs with Enabled=$false (3 of 15) are registered but left DISABLED, since
  each needs a per-user resource this template can't assume exists; validate
  the script by hand, then enable it deliberately:
      Enable-ScheduledTask -TaskName source-mail-pull -TaskPath '\Obsidian'
.PARAMETER Only    Register just one job by name.
.PARAMETER WhatIf  Show what would be registered without changing anything.
#>
[CmdletBinding(SupportsShouldProcess)] param([string]$Only)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

$manifest   = Import-PowerShellDataFile (Join-Path $PSScriptRoot 'schedules.psd1')
$scriptsDir = Get-ScriptsDir
$python     = Get-VenvPython
$folder     = '\Obsidian'

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

    # Quote the script path (handles spaces in the profile/vault path) and append args.
    $argString = '"{0}"' -f $scriptPath
    if ($job.Args -and $job.Args.Count -gt 0) { $argString += ' ' + ($job.Args -join ' ') }

    $action   = New-ScheduledTaskAction -Execute $python -Argument $argString -WorkingDirectory $scriptsDir
    $trigger  = New-TriggerFromSpec $job.Trigger
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    $taskName = $job.Name

    if ($PSCmdlet.ShouldProcess("$folder\$taskName", 'Register scheduled task')) {
        Register-ScheduledTask -TaskName $taskName -TaskPath $folder `
            -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        if (-not $job.Enabled) {
            Disable-ScheduledTask -TaskName $taskName -TaskPath $folder | Out-Null
            Write-Host ("  registered (DISABLED): {0}" -f $taskName)
        } else {
            Write-Host ("  registered (ENABLED):  {0}" -f $taskName)
        }
    }
}
Write-Host ""
Write-Host "Done. Review:  Get-ScheduledTask -TaskPath '\Obsidian\*' | Select State,TaskName"
Write-Host "Enable one:    Enable-ScheduledTask -TaskName tag-clippings -TaskPath '\Obsidian'"
