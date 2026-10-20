<#
.SYNOPSIS  Bring an existing Windows install up to date in one command.
.DESCRIPTION
  Three main steps, always in this order. Between 2 and 3 it also reinstalls
  plugins that are off their pins, re-applies the QuickAdd patch and the
  timestamp ignores, and removes any plugin the template has retired; after 3
  it refreshes the dashboard buttons' handler if one is registered, keeps
  Ollama and Open WebUI on loopback, and reports the integrity monitor's
  status without adopting a baseline:

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
    # Not while Obsidian is open: it keeps the old plugins loaded (a retired one
    # included) until it restarts, and writes its in-memory copies of tracked
    # settings back over the pulled ones, so the next update would refuse.
    if (Get-Process -Name Obsidian -ErrorAction SilentlyContinue) {
        Write-Host 'Obsidian is running; not updating.' -ForegroundColor Red
        Write-Host 'Quit Obsidian (close every window, or right-click its taskbar icon > Close all windows),'
        Write-Host 'then run this update again. While it is open it keeps the old plugins loaded and'
        Write-Host 'writes its own copy of the settings back over the updated ones.'
        exit 1
    }
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

# Plugins: reinstalled from their pins when the pins moved or an installed
# plugin is not its pinned copy, as macOS update.sh does. Without this a re-pin
# -- a plugin's security fix included -- never reached an existing Windows
# install. A failure here does not stop the remaining steps, but the update
# ends INCOMPLETE: Install-Plugins throws when a download fails or does not
# match its pinned hash, and that refusal needs a person to look at it.
$pluginFailure = $null
$pluginReason = Get-PluginReinstallReason -Vault $vault -From $From -VenvPython $venvPy
if ($pluginReason) {
    Write-Host '== plugins =='
    Write-Host "  $pluginReason; reinstalling from the pins"
    try {
        & (Join-Path $PSScriptRoot 'Install-Plugins.ps1')
    } catch {
        $pluginFailure = "$_"
        Write-Host "  Plugins NOT updated: $pluginFailure" -ForegroundColor Red
    }
    # After a failure there is nothing to vet yet; the update ends INCOMPLETE.
    if (-not $pluginFailure -and $pluginReason -eq 'the plugin pins changed') {
        Write-Host '  The plugin integrity check will now report the new plugin files. Vet what' -ForegroundColor Yellow
        Write-Host "  changed (git -C `"$vault`" diff $From HEAD -- installers/plugin-pins.json) before adopting it." -ForegroundColor Yellow
    } elseif (-not $pluginFailure) {
        Write-Host '  The plugins were brought back to their pins. The plugin integrity check will' -ForegroundColor Yellow
        Write-Host '  report any whose vetted version differs; vet those before adopting.' -ForegroundColor Yellow
    }
}

# The QuickAdd patch, on every update: it is idempotent, a reinstall (even a
# failed one: Install-Plugins replaces every plugin it can before it throws)
# brings back the unpatched bundle, and QuickAdd's patched main.js is exempt
# from the drift check -- so a patch that failed once is retried here.
Invoke-QuickAddPatch -Vault $vault -Python @($venvPy)
# And the timestamp plugin's ignore list, so this pull's docs are not stamped:
# a clone made before the step existed has none.
Invoke-TimestampIgnores -Vault $vault -Python @($venvPy)

# Plugins the template has retired: disabled by the pull, removed here --
# before the task step, whose failure ends this script.
Remove-RetiredPlugins -Vault $vault
Repair-SettingsLineEndings -Vault $vault

Write-Host '== 3/3 scheduled tasks =='
& (Join-Path $PSScriptRoot 'Register-Tasks.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'Update INCOMPLETE: the code and requirements are updated, but some tasks still' -ForegroundColor Red
    Write-Host 'run their old definition (see FAILED above). Re-run this update from an elevated' -ForegroundColor Red
    Write-Host 'PowerShell; it is safe to repeat.' -ForegroundColor Red
    if ($pluginFailure) {
        Write-Host 'The plugins were not updated either (see "Plugins NOT updated" above).' -ForegroundColor Red
    }
    exit 1
}

# The dashboard buttons' handler is opt-in (Install-DashboardActions.ps1), so
# an update only refreshes one that is already registered -- picking up a
# moved venv -- and never adds it.
if (Test-Path -LiteralPath $DashboardSchemeKey) {
    Write-Host '== dashboard buttons =='
    $null = Register-DashboardActions -ScriptsDir $scriptsDir
}

# An Open WebUI container created before 2026-10-11 by setup-rag.ps1 is
# published on every interface. Only touched when one exists and Docker is up.
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $owui = (docker ps -a --filter 'name=^open-webui$' --format '{{.Names}}' 2>$null) }
    finally { $ErrorActionPreference = $prevEAP }
    if ($owui -eq 'open-webui') {
        Write-Host '== open-webui binding =='
        if (Repair-OpenWebUIBinding) { Write-Host '  Recreated, bound to 127.0.0.1:3000.' }
        else { Write-Host '  Bound to 127.0.0.1 only.' }
        if (Test-OpenWebUIImagePin) { Write-Host '  On the pinned image.' }
        if (Test-OpenWebUIHardening) { Write-Host '  Login required; CORS limited to the loopback origins.' }
    }
}

# Ollama's API is unauthenticated; an older setup-rag.ps1 suggested widening it.
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Write-Host '== Ollama binding =='
    if (-not (Test-OllamaExposure)) { Write-Host '  Not listening beyond this machine.' }
}

$to = (Invoke-Native -ErrorMessage 'git rev-parse failed' { git -C $vault rev-parse --short HEAD }).Trim()

# Report only. Adopting a new baseline is a decision for the person reading
# this, after they have checked the drift is the update and nothing else.
Write-Host ''
Write-Host '== integrity monitor =='
# --json only reads: no notification, no alert record. It also reports a
# missing baseline itself, so this does not have to know where state lives.
# The installed copy, as the task runs it (finding 5278112); Register-Tasks
# refreshed it in step 3.
$monitor = Join-Path (Get-ControlsDir) 'integrity_monitor.py'
$controlPy = $null
try { $controlPy = Get-ControlPython } catch { }
$json = $null
$prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
try { if ($controlPy) { $json = & $controlPy -I -S $monitor --json 2>$null } }
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
    Write-Host "    findings:  & `"$controlPy`" -I -S `"$monitor`" --json"
    Write-Host "    the diff:  git -C `"$vault`" diff --name-status $From $to"
    # Every file scope, not just scripts: a pull also changes templates and the
    # vault's CLAUDE.md (agent_config), and a rule naming only scripts and
    # tasks left those to guesswork (a Windows update, 2026-10-12).
    Write-Host '  Every file finding (scripts, templates, agent_config, script_config) must be a'
    Write-Host '  file in that diff. venv findings fit only if requirements.lock is in the diff,'
    Write-Host '  or if you rebuilt the venv with install.ps1 since the last baseline and'
    Write-Host '  `.venv\Scripts\python.exe -m pip check` is clean.'
    Write-Host '  Task findings must be \Obsidian\ tasks. Any state_dir, secrets, PERMISSIONS or'
    Write-Host '  BULK_DELETE finding is NOT the update, nor is INSTALLED_COPY_MISMATCH (the update'
    Write-Host '  refreshed the installed controls) -- except state_dir plugin_allowlist.json'
    Write-Host '  if you re-vetted plugins (plugin_integrity_check.py --update) since the last'
    Write-Host '  baseline. Only then adopt it with:'
    Write-Host "    & `"$controlPy`" -I -S `"$monitor`" --update"
}

Write-Host ''
if ($pluginFailure) {
    Write-Host 'Update INCOMPLETE: everything else is updated, but the plugins are not (see' -ForegroundColor Red
    Write-Host '"Plugins NOT updated" above). A hash mismatch means upstream changed a file under' -ForegroundColor Red
    Write-Host 'its pin: do not work around it. A download failure is safe to retry: run the update again.' -ForegroundColor Red
    Write-Host "Don't adopt a new plugin baseline until an update completes." -ForegroundColor Red
    exit 1
}
if ($From -eq $to) {
    Write-Host "Already at $to; requirements and scheduled tasks refreshed." -ForegroundColor Green
} else {
    Write-Host "Updated $From -> $to." -ForegroundColor Green
}
exit 0
