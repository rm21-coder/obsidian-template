# common.ps1 -- shared helpers for the Windows layer. Dot-source this.
# . "$PSScriptRoot\common.ps1"

# The vault lives at %USERPROFILE%\Obsidian. When the repo is cloned elsewhere
# (e.g. %USERPROFILE%\obsidian-template), install.ps1 links ~\Obsidian to it with
# a directory junction -- the Windows analog of the Mac's ~/Obsidian symlink (see
# installers/components/10-vault-bootstrap.sh). $env:OBSIDIAN_VAULT overrides,
# but the junction is the canonical setup so ~\Obsidian\... paths just work.
function Get-VaultRoot {
    if ($env:OBSIDIAN_VAULT) { return $env:OBSIDIAN_VAULT }
    return (Join-Path $env:USERPROFILE 'Obsidian')
}

# Repo root = three levels up from this file (<repo>\Templates\Scripts\windows).
function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}

function Get-ScriptsDir {
    return (Join-Path (Get-VaultRoot) 'Templates\Scripts')
}

function Get-VenvPython {
    $py = Join-Path (Get-ScriptsDir) '.venv\Scripts\python.exe'
    if (Test-Path $py) { return $py }
    # Fall back to a launcher-resolved interpreter (must be 3.10+).
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "No venv python at $py and no 'python' on PATH. Run install.ps1 first."
}

# The windowless interpreter, for scheduled tasks. A task that starts the
# console python.exe opens a console window in the user's session every time
# it fires -- with three jobs on a 5-minute trigger, a burst of windows all
# day. pythonw.exe starts with no console. It runs only run_logged.py, which
# starts the job itself hidden (see run_logged.py, child_command).
#
# The fallback keeps a task working when pythonw.exe is missing, at the cost
# of the window, and says so -- a missing interpreter is not a silent choice.
function Get-VenvPythonW {
    $pyw = Join-Path (Get-ScriptsDir) '.venv\Scripts\pythonw.exe'
    if (Test-Path $pyw) { return $pyw }
    Write-Warning "No pythonw.exe at $pyw; tasks will use python.exe and show a console window."
    return (Get-VenvPython)
}

# The lowest Python 3 minor version that can install requirements.lock here.
# 3.10 is the floor everywhere (the scripts use PEP 604 unions); on Windows
# ARM64 it is 3.12, because pyyaml publishes no win_arm64 wheel for 3.11 and
# the lock installs wheels only.
function Test-ArmWindows {
    return ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64')
}
function Get-MinPythonMinor {
    if (Test-ArmWindows) { return 12 } else { return 10 }
}
# ... and the highest: the lock is resolved and wheel-checked up to 3.14
# (TARGETS in installers/lib/lock_requirements.py). A newer Python has no
# wheels for the compiled packages yet, so pip would refuse.
function Get-MaxPythonMinor { return 14 }

# The requirements step, shared by install.ps1 and update.ps1 so the two can
# never install differently.
#
# Installs requirements.lock, never requirements.txt: every package at the
# release a maintainer locked, each file checked against its SHA256
# (--require-hashes), nothing the lock does not name (--no-deps), and no
# source builds (--only-binary), whose build tools pip would fetch unhashed.
# pip itself is not upgraded: that would be an unpinned fetch ahead of the
# pinned ones, and the venv's bundled pip handles all of this.
function Remove-PipLeftovers {
    param([Parameter(Mandatory)][string]$VenvPython)
    $site = Join-Path (Split-Path (Split-Path $VenvPython)) 'Lib\site-packages'
    if (-not (Test-Path $site)) { return }
    $left = @(Get-ChildItem -LiteralPath $site -Directory -Filter '~*' -ErrorAction SilentlyContinue)
    $kept = @()
    foreach ($d in $left) {
        Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $d.FullName) { $kept += $d.Name }
    }
    if ($left.Count -gt $kept.Count) {
        Write-Host "  removed $($left.Count - $kept.Count) folder(s) of old files pip set aside while they were in use"
    }
    if ($kept.Count -gt 0) {
        Write-Host "  still in use, cleared on the next update: $($kept -join ' ')"
    }
}

function Install-Requirements {
    param(
        [Parameter(Mandatory)][string]$VenvPython,
        [Parameter(Mandatory)][string]$ScriptsDir
    )
    $lock = Join-Path $ScriptsDir 'requirements.lock'
    if (-not (Test-Path $lock)) {
        throw "$lock is missing; refusing to install unpinned requirements"
    }
    # An existing venv may predate the floor (3.12 on ARM64) or exceed the
    # ceiling; say so plainly rather than let pip report a missing wheel.
    $ver = ((& $VenvPython --version 2>&1) | Out-String)
    if ($ver -match '3\.(\d+)') {
        $minor = [int]$Matches[1]
        if ($minor -lt (Get-MinPythonMinor) -or $minor -gt (Get-MaxPythonMinor)) {
            throw "The venv runs Python $($ver.Trim()); the pinned dependencies need 3.$(Get-MinPythonMinor) to 3.$(Get-MaxPythonMinor) here. Install Python 3.12 (winget install Python.Python.3.12), delete $(Split-Path (Split-Path $VenvPython)), and re-run install.ps1."
        }
    }
    # A file in use cannot be deleted on Windows, so when a scheduled job has
    # a package loaded, pip moves its old files to site-packages\~<name> and
    # leaves them. Clear what earlier runs left (anything still held stays
    # until next time); nothing pip installs starts with '~'.
    Remove-PipLeftovers -VenvPython $VenvPython
    # --force-reinstall, but only when the lock is new to this venv: pip skips
    # a package already at the locked version without checking its files, so
    # the first install from a lock, and the first after it changes, reinstall
    # every package. The lock's SHA256 is then recorded in the venv
    # (<venv>\requirements.lock.sha256); later runs with the same lock install
    # only what is missing or at the wrong version, hash-checked too.
    # Reinstalling everything every time took 4-5 minutes per update on x64
    # (2026-10-02). Delete the .sha256 file to force a full reinstall.
    $stamp = Join-Path (Split-Path (Split-Path $VenvPython)) 'requirements.lock.sha256'
    $want = (Get-FileHash -Algorithm SHA256 -LiteralPath $lock).Hash.ToLower()
    $have = ''
    if (Test-Path -LiteralPath $stamp) { $have = ((Get-Content -LiteralPath $stamp -Raw) -replace '\s', '').ToLower() }
    $reinstall = @()
    if ($have -eq $want) {
        Write-Host '  requirements.lock unchanged since this venv''s last install: installing only what is missing (hash-checked) ...'
    } else {
        Write-Host '  requirements.lock is new to this venv: reinstalling every package from it (hash-checked) ...'
        $reinstall = @('--force-reinstall')
    }
    Invoke-Native -ErrorMessage 'dependency install failed (a hash mismatch or a missing wheel is a refusal, not a glitch)' {
        & $VenvPython -m pip install --disable-pip-version-check --require-hashes --no-deps --only-binary ':all:' @reinstall -r $lock
    }
    # Only after pip succeeded (Invoke-Native throws otherwise): a failed
    # install retries in full next time.
    Set-Content -LiteralPath $stamp -Value $want -Encoding ascii
    Remove-PipLeftovers -VenvPython $VenvPython
    # The lock does not remove what it no longer names; say so.
    $extras = Join-Path $ScriptsDir 'lock_extras.py'
    if (Test-Path $extras) {
        # Through Invoke-Native: under 'Stop', stray stderr from a native
        # command would otherwise end update.ps1 after a good install.
        $names = @(Invoke-Native -Warn -ErrorMessage 'listing packages outside the lock failed' { & $VenvPython $extras $lock } | Where-Object { $_ })
        if ($names.Count -gt 0) {
            Write-Warning ("installed but not in requirements.lock (never hash-checked, not audited): " + ($names -join ' '))
            Write-Warning ("delete $(Split-Path (Split-Path $VenvPython)) and re-run install.ps1 to clear them")
        }
    }
}

# The Morning Dashboard's buttons are obsidian-dashboard://run/<action> links:
# a file:// page cannot start a program, so the browser hands the link to
# whatever the scheme is registered to. That is a per-user registry key -- no
# elevation -- pointing at windows\dashboard_action.py under the venv's
# pythonw.exe, which accepts exactly three actions. Shared by install.ps1 and
# update.ps1 (so existing installs get the buttons on their next update);
# uninstall.ps1 removes it. morning_dashboard.py draws the buttons only when
# this key points at a handler that exists.
$DashboardSchemeKey = 'HKCU:\Software\Classes\obsidian-dashboard'

function Register-DashboardActions {
    param([Parameter(Mandatory)][string]$ScriptsDir)
    $pyw = Join-Path $ScriptsDir '.venv\Scripts\pythonw.exe'
    $handler = Join-Path $ScriptsDir 'windows\dashboard_action.py'
    if (-not (Test-Path -LiteralPath $pyw) -or -not (Test-Path -LiteralPath $handler)) {
        Write-Warning "  dashboard buttons not registered: need $pyw and $handler"
        return
    }
    $command = '"{0}" "{1}" "%1"' -f $pyw, $handler
    # Optional, so a failure warns rather than ending an install or update
    # that has otherwise succeeded; the dashboard then simply has no buttons.
    try {
        # Start from nothing: an existing key may carry other verbs under
        # shell, or another default verb, that this would otherwise keep.
        if (Test-Path -LiteralPath $DashboardSchemeKey) {
            Remove-Item -LiteralPath $DashboardSchemeKey -Recurse -Force -ErrorAction Stop
        }
        # -Force creates the missing keys on the way down to 'command'.
        New-Item -Path "$DashboardSchemeKey\shell\open\command" -Force -ErrorAction Stop | Out-Null
        Set-Item -Path $DashboardSchemeKey -Value 'URL:Obsidian dashboard action' -ErrorAction Stop
        New-ItemProperty -Path $DashboardSchemeKey -Name 'URL Protocol' -Value '' -PropertyType String -Force -ErrorAction Stop | Out-Null
        Set-Item -Path "$DashboardSchemeKey\shell\open\command" -Value $command -ErrorAction Stop
        Write-Host "  obsidian-dashboard:// -> $handler"
    } catch {
        Write-Warning "  dashboard buttons not registered: $_"
    }
}

function Unregister-DashboardActions {
    if (Test-Path -LiteralPath $DashboardSchemeKey) {
        # A warning, not a stop: an uninstall should carry on past this.
        try {
            Remove-Item -LiteralPath $DashboardSchemeKey -Recurse -Force -ErrorAction Stop
            Write-Host '  removed the obsidian-dashboard:// handler'
        } catch {
            Write-Warning "  could not remove $DashboardSchemeKey`: $_"
        }
    } else {
        Write-Host '  no obsidian-dashboard:// handler to remove'
    }
}

# Community plugins the template once shipped and has since retired; update.ps1
# removes them (Remove-RetiredPlugins). The enabled list arrives with the pull;
# the plugin's own files -- fetched by the installer, never tracked -- are moved
# out of the vault so no dormant plugin code stays behind. Names only ever get
# added here, and must match RETIRED_PLUGINS in installers/lib/update.sh.
#   templater-obsidian   Templater, retired 2026-10-03
$RetiredPlugins = @('templater-obsidian')

function Remove-RetiredPlugins {
    param([Parameter(Mandatory)][string]$Vault)
    $backup = Join-Path $env:LOCALAPPDATA ("obsidian-template-update\" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '\plugins')
    foreach ($id in $RetiredPlugins) {
        $dir = Join-Path $Vault ".obsidian\plugins\$id"
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        try {
            New-Item -ItemType Directory -Force -Path $backup -ErrorAction Stop | Out-Null
            Move-Item -LiteralPath $dir -Destination (Join-Path $backup $id) -ErrorAction Stop
            Write-Host "  $id`: retired upstream; removed (old copy in $backup)"
        } catch {
            Write-Warning "  $id`: retired upstream, but could not move $dir aside: $_"
        }
    }
}

# Obsidian's settings JSON checked out before .gitattributes made it LF
# (2bf2afb) is still CRLF on disk. Obsidian re-saves it as LF, and git then
# reports the file modified with no diff, so the next update refuses. Rewrite
# those files from git as LF now. Only a file whose content already matches
# git apart from line endings is touched; anything else is left alone.
# Deleting first is what makes git write it: a checkout skips a file whose
# cached state says it is up to date. (Seen on the ARM test laptop 2026-10-03.)
function Repair-SettingsLineEndings {
    param([Parameter(Mandatory)][string]$Vault)
    $tracked = Invoke-Native -ErrorMessage 'git ls-files failed' {
        git -C $Vault ls-files -- '.obsidian/*.json'
    }
    $redo = @()
    foreach ($rel in @($tracked)) {
        if (-not $rel) { continue }
        $path = Join-Path $Vault $rel
        if (-not (Test-Path -LiteralPath $path)) { continue }
        if (-not [IO.File]::ReadAllText($path).Contains("`r`n")) { continue }
        git -C $Vault diff --quiet --ignore-cr-at-eol -- $rel 2>$null
        if ($LASTEXITCODE -eq 0) { $redo += $rel }
    }
    if (-not $redo) { return }
    try {
        foreach ($rel in $redo) { Remove-Item -LiteralPath (Join-Path $Vault $rel) -ErrorAction Stop }
        Invoke-Native -ErrorMessage 'git checkout failed' { git -C $Vault checkout -- @redo }
        Write-Host "  settings: $($redo.Count) file(s) rewritten with LF line endings (content unchanged)"
    } catch {
        Write-Warning "  settings: could not rewrite line endings: $_. Run: git -C `"$Vault`" checkout -- .obsidian"
    }
}

function Get-SecretsFile {
    return (Join-Path $env:USERPROFILE 'dev\secrets\.env')
}

# Run a native .exe without letting incidental stderr chatter become a
# terminating error. PS 5.1 can wrap a native command's stderr lines into
# ErrorRecords, which $ErrorActionPreference='Stop' then treats as fatal --
# this bites even a totally benign message (e.g. venv's own "environment
# location may have moved" notice after we junction the vault) whenever the
# CALLER captures this script's output for logging (`.\install.ps1 *>&1 |
# Tee-Object ...`), since that redirection propagates down to nested native
# calls. Exit code is the only thing that should decide success here.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][scriptblock]$Command,
        [string]$ErrorMessage = 'command failed',
        [switch]$Warn   # warn instead of throw on a nonzero exit code
    )
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $prevEAP }
    if ($LASTEXITCODE -ne 0) {
        if ($Warn) { Write-Warning "$ErrorMessage (exit $LASTEXITCODE)" }
        else { throw "$ErrorMessage (exit $LASTEXITCODE)" }
    }
}

# True if the WSL2 platform is already present, even with zero distros
# installed -- Docker Desktop only needs its own lightweight utility VMs on
# top of that, no `wsl --install` / reboot required in that case.
#
# Exit code only, deliberately: wsl.exe emits its status text in an encoding
# that survives fine on a real console but gets mangled (interstitial nulls
# that read as extra spaces) once captured through a pipe, which breaks any
# text match against "Default Version: 2" even when WSL2 is genuinely
# present and `wsl --status` succeeded. The exit code doesn't have that
# problem: 0 means the WSL2 platform responded, which is all this needs.
function Test-WSL2Present {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) { return $false }
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & wsl --status 2>$null | Out-Null } finally { $ErrorActionPreference = $prevEAP }
    return ($LASTEXITCODE -eq 0)
}

# Refresh $env:Path from the registry (Machine + User scopes). A PowerShell
# process's PATH is a snapshot taken at ITS OWN startup; anything a winget
# install adds afterward (docker, ollama, ...) is invisible to Get-Command in
# that process until this runs. This bites harder than "open a new window"
# suggests: `powershell -File ...` launches a CHILD process, which inherits
# its PARENT's (possibly long-stale) environment block rather than
# re-reading the registry itself -- so even a freshly-invoked one-liner run
# from an old, long-lived console can still see a stale PATH. Call this at
# the top of any script that Get-Command's a winget-installed tool.
function Sync-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# Minimal KEY=VALUE .env loader (mirrors the bash `set -a; source` wrappers).
function Import-DotEnv {
    param([string]$Path = (Get-SecretsFile))
    if (-not (Test-Path $Path)) { throw "Secrets file not found: $Path" }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq '' -or $line.StartsWith('#')) { return }
        $kv = $line -split '=', 2
        if ($kv.Count -eq 2) {
            $name = $kv[0].Trim()
            $val  = $kv[1].Trim().Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($name, $val, 'Process')
        }
    }
}

# ---- install profiles -------------------------------------------------------
# A profile is a KEY=VALUE file (installers\profiles\<name>.env) that
# pre-answers the installer: where Claude calls go, which opt-in jobs to turn
# on, and the tenant-specific answers those jobs need. The point is
# distribution -- "clone this and run install.ps1 -Profile ours" reproduces a
# working setup instead of handing someone a list of choices they have no basis
# to make yet. Same files the macOS installer takes, so one profile serves both
# platforms. See installers\profiles\README.md.
#
# One deliberate difference from macOS, and it is a security property rather
# than an omission: install.sh SOURCES the profile, which makes a profile there
# shell code running at the installer's trust level. This reads it as DATA --
# no value is ever evaluated -- so a profile cannot execute anything on
# Windows. The cost is that shell constructs in a value have no meaning here:
# $HOME / ${HOME} are translated (they appear in the shipped examples), and a
# value carrying a command substitution is REFUSED rather than silently taken
# as a literal, since a literal '$(hostname)' in a config file is a wrong
# answer that would surface much later as a confusing runtime error.
function Get-ProfilesDir {
    return (Join-Path (Get-RepoRoot) 'installers\profiles')
}

# Mirror of install.sh's list_profiles: real profiles first, then the
# *.env.example templates, which are meant to be copied rather than run.
function Show-InstallProfiles {
    $dir = Get-ProfilesDir
    Write-Host "Profiles in $dir :"
    $found = $false
    Get-ChildItem -Path $dir -Filter '*.env' -File -ErrorAction SilentlyContinue |
        Sort-Object Name | ForEach-Object {
            $found = $true
            # Line 2 of a profile is its one-line description, by convention.
            $desc = (Get-Content $_.FullName -TotalCount 2 |
                     Select-Object -Last 1) -replace '^#\s*', ''
            Write-Host ("  {0,-16} {1}" -f $_.BaseName, $desc)
        }
    if (-not $found) { Write-Host '  (none)' }
    Get-ChildItem -Path $dir -Filter '*.env.example' -File -ErrorAction SilentlyContinue |
        Sort-Object Name | ForEach-Object {
            Write-Host ("  {0,-16} {1}" -f $_.Name, '(template - copy to <name>.env first)')
        }
    Write-Host 'Use: install.ps1 -Profile <name>   (or a path to a profile file)'
}

# Resolve a profile spec the way install.sh does: a bare name means
# installers\profiles\<name>.env; anything with a separator or an .env suffix
# is a path, so a profile handed over out-of-band (and never committed) works
# without being copied into the repo first. A bare filename falls back to the
# repo root so the command does not depend on the caller's cwd.
function Resolve-InstallProfilePath {
    param([Parameter(Mandatory)][string]$Spec)
    if ($Spec -match '[\\/]' -or $Spec -like '*.env') {
        $file = [Environment]::ExpandEnvironmentVariables($Spec)
        if ($file -like '~*') { $file = Join-Path $env:USERPROFILE $file.Substring(1) }
        if (Test-Path -LiteralPath $file) { return (Resolve-Path -LiteralPath $file).Path }
        $atRepo = Join-Path (Get-RepoRoot) $Spec
        if (Test-Path -LiteralPath $atRepo) { return (Resolve-Path -LiteralPath $atRepo).Path }
        return $null
    }
    $named = Join-Path (Get-ProfilesDir) ("{0}.env" -f $Spec)
    if (Test-Path -LiteralPath $named) { return (Resolve-Path -LiteralPath $named).Path }
    return $null
}

# Parse a profile into an ordered hashtable of KEY -> value. Data only.
function Import-InstallProfile {
    param([Parameter(Mandatory)][string]$Spec)

    $path = Resolve-InstallProfilePath -Spec $Spec
    if (-not $path) {
        Show-InstallProfiles
        throw "profile not found: $Spec"
    }

    $values = [ordered]@{}
    $lineNo = 0
    foreach ($raw in (Get-Content -LiteralPath $path)) {
        $lineNo++
        $line = $raw.Trim()
        if ($line -eq '' -or $line.StartsWith('#')) { continue }
        # `export KEY=value` is valid in a sourced profile; accept it here too.
        if ($line -match '^export\s+(.*)$') { $line = $Matches[1].Trim() }
        $kv = $line -split '=', 2
        if ($kv.Count -ne 2) {
            Write-Warning "  profile line $lineNo is not KEY=VALUE; ignored: $line"
            continue
        }
        $name = $kv[0].Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            Write-Warning "  profile line ${lineNo}: '$name' is not a valid key name; ignored"
            continue
        }
        $val = $kv[1].Trim()
        # Strip one layer of matching quotes, then drop a trailing comment only
        # on a value that was NOT quoted (a '#' inside quotes is data).
        $wasQuoted = $false
        if ($val.Length -ge 2 -and
            (($val.StartsWith('"') -and $val.EndsWith('"')) -or
             ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
            $wasQuoted = $true
        }
        if (-not $wasQuoted -and $val -match '^(.*?)\s+#.*$') { $val = $Matches[1].TrimEnd() }
        # Refuse what we cannot evaluate rather than passing it through as a
        # literal. Backtick is PowerShell's own escape character, hence the
        # doubled one in the character class.
        if ($val -match '\$\(' -or $val -match '``' -or $val -match '\$\{[^}]*[:\-+#%/]') {
            Write-Warning ((("  profile line {0}: {1} contains a shell expression this " +
                'installer will not evaluate; ignored (set a literal value instead)')) -f $lineNo, $name)
            continue
        }
        # The one expansion the shipped profiles actually rely on.
        $val = $val.Replace('${HOME}', $env:USERPROFILE).Replace('$HOME', $env:USERPROFILE)
        $values[$name] = $val
    }

    $desc = $values['PROFILE_DESCRIPTION']
    $obj = [pscustomobject]@{
        Name   = [IO.Path]::GetFileNameWithoutExtension($path)
        Path   = $path
        Values = $values
    }
    Write-Host ("  profile: {0} ({1})" -f $obj.Name, $obj.Path)
    if ($desc) { Write-Host "    $desc" }
    return $obj
}

# The profile's value for PROFILE_<Key>, else $Fallback. Pass it as a prompt
# default so a profile pre-fills the answer and a human can still overtype it
# (the analog of the macOS pdefault).
function Get-ProfileValue {
    param($InstallProfile, [Parameter(Mandatory)][string]$Key, [string]$Fallback = '')
    if (-not $InstallProfile) { return $Fallback }
    $v = $InstallProfile.Values[("PROFILE_{0}" -f $Key)]
    if ([string]::IsNullOrWhiteSpace($v)) { return $Fallback }
    return $v
}

# A profile's raw (unprefixed) value -- for the keys that are read by the
# scripts themselves rather than by the installer's prompts, e.g. LLM_BASE_URL.
function Get-ProfileRaw {
    param($InstallProfile, [Parameter(Mandatory)][string]$Key, [string]$Fallback = '')
    if (-not $InstallProfile) { return $Fallback }
    $v = $InstallProfile.Values[$Key]
    if ([string]::IsNullOrWhiteSpace($v)) { return $Fallback }
    return $v
}

# Tri-state opt-in answer for PROFILE_<Key>: $true / $false / $null (unset).
# $null means "the profile did not say", which is what lets a caller fall back
# to asking. A profile's explicit answer is the consent that allows an
# unattended run to install an opt-in component (the macOS pconfirm rule).
function Get-ProfileFlag {
    param($InstallProfile, [Parameter(Mandatory)][string]$Key)
    if (-not $InstallProfile) { return $null }
    $v = $InstallProfile.Values[("PROFILE_{0}" -f $Key)]
    if ([string]::IsNullOrWhiteSpace($v)) { return $null }
    switch -Regex ($v.Trim()) {
        '^(1|y|yes|true)$'  { return $true }
        '^(0|n|no|false)$'  { return $false }
        default {
            Write-Warning "  ignoring PROFILE_${Key}='$v' (expected 1 or 0)"
            return $null
        }
    }
}

# Free-text answer with a default, honouring an unattended run by taking the
# default instead of blocking on a read nobody is there to answer (the analog
# of the macOS prompt()).
function Read-Answer {
    param(
        [Parameter(Mandatory)][string]$Question,
        [string]$Default = '',
        [switch]$NoPrompt
    )
    if ($NoPrompt -or [Console]::IsInputRedirected) { return $Default }
    if ($Default) {
        $ans = Read-Host ("{0} [{1}]" -f $Question, $Default)
        if ([string]::IsNullOrWhiteSpace($ans)) { return $Default }
        return $ans.Trim()
    }
    $ans = Read-Host $Question
    if ($null -eq $ans) { return '' }
    return $ans.Trim()
}

# Idempotent single-key write into the .env the scheduled tasks read -- the
# analog of the macOS env_set, including its refusal to clobber a value that is
# already there without -Force. Also replaces a COMMENTED placeholder for the
# same key (the stub install.ps1 writes ships `#LLM_BASE_URL=...`), so a
# profile-driven run does not leave both a comment and a live line behind.
# Write text files as UTF-8 WITHOUT a byte-order mark. Windows PowerShell 5.1's
# `Set-Content -Encoding utf8` always prepends one (EF BB BF), and Python's
# json.loads rejects it: every meeting_pull.json and meeting_prepopulate.json
# this installer wrote carried one, so meeting_pull failed on every run and
# meeting_prepopulate silently fell back to defaults. Use this for every file a
# Python script reads.
function Set-Utf8Content {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Value
    )
    # .NET resolves a relative path against the process directory, not the
    # PowerShell location; resolve it the PowerShell way first.
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LiteralPath)
    $text = ($Value -join "`r`n") + "`r`n"
    [System.IO.File]::WriteAllText($full, $text, (New-Object System.Text.UTF8Encoding $false))
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value,
        [string]$Path = (Get-SecretsFile),
        [switch]$Force
    )
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @(Get-Content -LiteralPath $Path) }

    $live = $lines | Where-Object { $_ -match ("^\s*{0}\s*=" -f [regex]::Escape($Key)) }
    if ($live) {
        $existing = ($live | Select-Object -First 1) -replace ("^\s*{0}\s*=\s*" -f [regex]::Escape($Key)), ''
        if (-not [string]::IsNullOrWhiteSpace($existing) -and -not $Force) {
            Write-Host "  $Key already set in $Path (use -Force to overwrite)"
            return
        }
    }
    # Drop every live AND commented line for this key, then append one.
    $kept = $lines | Where-Object { $_ -notmatch ("^\s*#?\s*{0}\s*=" -f [regex]::Escape($Key)) }
    $out  = @($kept) + @("{0}={1}" -f $Key, $Value)
    Set-Utf8Content -LiteralPath $Path -Value $out
    Write-Host "  set: $Key in $Path"
}

# Merge values into a JSON config file, preserving whatever is already there.
# The analog of the `config.update(...)` / `config.setdefault(...)` python
# blocks in installers\components\5{2,4}-*.sh: -Set always wins, -Default is
# written only when the key is absent, so an explicit choice a user made by
# hand survives a re-run of the installer.
function Merge-JsonConfig {
    param(
        [Parameter(Mandatory)][string]$Path,
        [hashtable]$Set = @{},
        [hashtable]$Default = @{}
    )
    $config = [ordered]@{}
    if (Test-Path -LiteralPath $Path) {
        try {
            $raw = Get-Content -LiteralPath $Path -Raw
            if (-not [string]::IsNullOrWhiteSpace($raw)) {
                foreach ($p in (ConvertFrom-Json $raw).PSObject.Properties) {
                    $config[$p.Name] = $p.Value
                }
            }
        } catch {
            Write-Warning "  $Path is not valid JSON; rewriting it from scratch"
            $config = [ordered]@{}
        }
    }
    foreach ($k in $Set.Keys)     { $config[$k] = $Set[$k] }
    foreach ($k in $Default.Keys) { if (-not $config.Contains($k)) { $config[$k] = $Default[$k] } }

    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    # -InputObject, not the pipeline: piping a collection into ConvertTo-Json
    # unrolls it, which is how a one-element array turns into a bare scalar.
    Set-Utf8Content -LiteralPath $Path -Value (ConvertTo-Json -InputObject $config -Depth 8)
    return $config
}

# Best-effort IANA zone for this machine, since meeting_pull.json wants an IANA
# name and Windows carries its own ("Eastern Standard Time"). Deliberately the
# same five entries as mcp_meeting_transform.py's WINDOWS_TZ_MAP rather than a
# fuller table: an unmapped zone returns '' so the caller can ask rather than
# guess wrong, and a wrong timezone here silently shifts every meeting note's
# date.
function Get-IanaTimeZoneGuess {
    $map = @{
        'Eastern Standard Time'  = 'America/New_York'
        'Central Standard Time'  = 'America/Chicago'
        'Mountain Standard Time' = 'America/Denver'
        'Pacific Standard Time'  = 'America/Los_Angeles'
        'UTC'                    = 'UTC'
    }
    $winId = [System.TimeZoneInfo]::Local.Id
    if ($map.ContainsKey($winId)) { return $map[$winId] }
    return ''
}
