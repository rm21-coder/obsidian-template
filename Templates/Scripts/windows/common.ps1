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
