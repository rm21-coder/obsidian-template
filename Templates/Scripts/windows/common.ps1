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
    Set-Content -LiteralPath $Path -Value $out -Encoding utf8
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
    ConvertTo-Json -InputObject $config -Depth 8 |
        Set-Content -LiteralPath $Path -Encoding utf8
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
