# tree_gate.ps1 - refuse to run installer code that the git checkout does not vouch for.
#
# Windows counterpart of installers/lib/tree_gate.sh; findings 5278120 /
# 5278133 (prevention half). In the documented install the vault IS the git
# checkout and is synced by Obsidian Sync, so a compromised paired device can
# write into installers\ or Templates\Scripts\, and the next install.ps1 or
# update.ps1 would dot-source or run what it wrote. The integrity monitor's
# installers scope detects that afterwards; this refuses it beforehand.
#
# Same checks as tree_gate.sh: git status over installers/, Templates/Scripts/,
# the root entry scripts, .gitignore and .gitattributes (modified, untracked
# or deleted refuses; gitignored paths are not listed), ignored files that
# could hide code (a nested .gitignore, an ignored script), and in update mode
# any tracked change anywhere. A tree that is not a git checkout gets a warning
# and continues, unless -StrictTree.
#
# THE LIMIT. Dot-sourced before common.ps1, so self-contained. install.ps1 and
# update.ps1 check this file's blob against HEAD before dot-sourcing it.
# Nothing checks the entry script itself: an edited install.ps1 runs whatever
# it was edited to run. The Obsidian Sync setting printed at the end of an
# install (Show-TreeGateSyncNotice) is what keeps a paired device from editing
# it. Git's own .git\ configuration is trusted; Obsidian Sync does not sync it.
#
# Definitions only. Windows PowerShell 5.1; ASCII only.

$TreeGatePaths = @('installers', 'Templates/Scripts', 'install.sh', 'update.sh',
                   'uninstall.sh', '.gitignore', '.gitattributes')

# Run git with nothing from local config that starts a program during status.
# Returns $null on failure, else an array of output lines (possibly empty).
function Invoke-TreeGateGit {
    param([string]$Root, [string[]]$GitArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git -C $Root -c core.fsmonitor=false -c core.untrackedCache=false `
            -c core.quotepath=off @GitArgs 2>$null
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    if ($code -ne 0) { return $null }
    return ,@($out | Where-Object { $_ -ne $null -and "$_" -ne '' } | ForEach-Object { "$_" })
}

function Test-TreeGateCheckout {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath (Join-Path $Root '.git'))) { return $false }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $false }
    $top = Invoke-TreeGateGit -Root $Root -GitArgs @('rev-parse', '--show-toplevel')
    if ($null -eq $top -or $top.Count -lt 1) { return $false }
    try {
        $a = [IO.Path]::GetFullPath(($top[0] -replace '/', '\')).TrimEnd('\')
        $b = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    } catch { return $false }
    return [string]::Equals($a, $b, [StringComparison]::OrdinalIgnoreCase)
}

# An ignored path that may sit in a gated directory without refusing.
function Test-TreeGateIgnoredOk {
    param([string]$Path)
    $p = $Path
    if ($p -eq '.gitignore' -or $p -like '*/.gitignore') { return $false }
    if ($p -like '*/.DS_Store') { return $true }
    if ($p -like 'installers/profiles/*/*') { return $false }
    if ($p -like 'installers/profiles/*.env') { return $true }
    if ($p -eq 'installers/lib/__pycache__/') { return $true }
    if ($p -eq 'installers/lib/identity-denylist.local') { return $true }
    if ($p -eq 'installers/lib/identity-allowed-identities.local') { return $true }
    if ($p -like 'installers/*') { return $false }
    foreach ($ext in @('*.sh', '*.py', '*.pyc', '*.plist', '*.ps1', '*.psm1', '*.psd1', '*.js',
                       '*.applescript', '*.cmd', '*.bat', '*.exe', '*.command')) {
        if ($p -like $ext) { return $false }
    }
    return $true
}

function Write-TreeGateLog {
    param([string]$Line)
    try {
        $dir = Join-Path $env:LOCALAPPDATA 'obsidian-template'
        if (-not (Test-Path -LiteralPath $dir)) { $null = New-Item -ItemType Directory -Path $dir -Force }
        $stamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
        Add-Content -LiteralPath (Join-Path $dir 'tree-gate.log') -Value ("{0} {1}" -f $stamp, $Line) -Encoding ASCII
    } catch { }
}

# Test-TreeGate -Root <repo> -Mode install|update|uninstall [-AllowLocalChanges] [-Strict]
# uninstall judges the installer code as install does (review R3, 2026-10-26:
# uninstall.ps1 deletes paths its libraries compute, as uninstall.sh does).
# Returns $true to proceed, $false to refuse (the reason is already printed).
function Test-TreeGate {
    param([Parameter(Mandatory)][string]$Root,
          [Parameter(Mandatory)][ValidateSet('install', 'update', 'uninstall')][string]$Mode,
          [switch]$AllowLocalChanges,
          [switch]$Strict)
    $flag = '-AllowLocalChanges'
    # N5: an ignored __pycache__ folder is one entry to git, so the gate cannot
    # see a planted .pyc inside it. From here on no Python this run starts
    # reads or writes bytecode in the tree.
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONPYCACHEPREFIX = Join-Path $env:LOCALAPPDATA 'obsidian-template\pycache'
    if (-not (Test-TreeGateCheckout -Root $Root)) {
        if ($Strict) {
            Write-Host "TREE GATE: REFUSED. $Root is not a git checkout (or git is not available)," -ForegroundColor Red
            Write-Host 'so its installer code cannot be checked. This run requires a checkout (-StrictTree, or an update, which pulls).' -ForegroundColor Red
            return $false
        }
        Write-Warning "$Root is not a git checkout (or git is not available), so this installer"
        Write-Warning 'cannot check that its code is the code that was published. Continuing.'
        Write-Warning 'Install from a git clone to get that check, or pass -StrictTree to refuse instead.'
        return $true
    }
    $head = Invoke-TreeGateGit -Root $Root -GitArgs @('rev-parse', '--short', 'HEAD')
    $changes = Invoke-TreeGateGit -Root $Root -GitArgs (@('status', '--porcelain', '--untracked-files=all', '--') + $TreeGatePaths)
    $ignored = Invoke-TreeGateGit -Root $Root -GitArgs (@('ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '--') + $TreeGatePaths)
    if ($null -eq $head -or $null -eq $changes -or $null -eq $ignored) {
        Write-Host "TREE GATE: REFUSED. git could not read the tree in $Root." -ForegroundColor Red
        return $false
    }
    $head = $head[0]
    $changes = @($changes)
    foreach ($p in $ignored) {
        if (-not (Test-TreeGateIgnoredOk -Path $p)) { $changes += "!! $p" }
    }
    $other = @()
    if ($Mode -eq 'update') {
        # Tracked changes outside the installer code: a pull over a changed
        # tracked file is refused, as update.ps1 always did. Notes are untracked.
        $tracked = Invoke-TreeGateGit -Root $Root -GitArgs @('status', '--porcelain', '--untracked-files=no')
        if ($null -eq $tracked) {
            Write-Host "TREE GATE: REFUSED. git status failed in $Root." -ForegroundColor Red
            return $false
        }
        $other = @($tracked | Where-Object { $changes -notcontains $_ })
        # N9: outside the installer code, a file whose only change is its line
        # endings (an editor or autocrlf) is not a reason to refuse an update.
        # Installer paths never get here: they are in $changes.
        $eolOnly = @()
        $kept = @()
        foreach ($l in $other) {
            if ($l.StartsWith(' M ')) {
                $path = $l.Substring(3)
                $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                try {
                    & git -C $Root -c core.fsmonitor=false diff --ignore-cr-at-eol --quiet -- $path 2>$null
                    $same = ($LASTEXITCODE -eq 0)
                } finally { $ErrorActionPreference = $prev }
                if ($same) { $eolOnly += $path; continue }
            }
            $kept += $l
        }
        $other = $kept
        if ($eolOnly.Count -gt 0) {
            Write-Host "tree gate: line endings only, not refused (reset with git -C `"$Root`" checkout -- <path>):"
            foreach ($p in $eolOnly) { Write-Host "  $p" }
        }
    }
    if ($changes.Count -eq 0 -and $other.Count -eq 0) {
        Write-Host "tree gate: installer code matches the git checkout (HEAD $head)."
        return $true
    }
    if ($AllowLocalChanges) {
        $bar = '!' * 76
        Write-Host $bar -ForegroundColor Yellow
        Write-Host "TREE GATE OVERRIDE: $flag given. Running $Mode with local changes" -ForegroundColor Yellow
        Write-Host "that git does not vouch for (HEAD $head):" -ForegroundColor Yellow
        foreach ($l in ($changes + $other)) { Write-Host "  $l" -ForegroundColor Yellow }
        Write-Host 'Recorded in %LOCALAPPDATA%\obsidian-template\tree-gate.log.' -ForegroundColor Yellow
        Write-Host $bar -ForegroundColor Yellow
        Write-TreeGateLog ("OVERRIDE {0} root={1} head={2} {3}" -f $Mode, $Root, $head, (($changes + $other) -join ';'))
        return $true
    }
    if ($changes.Count -gt 0) {
        Write-Host 'TREE GATE: REFUSED. The installer code in this tree differs from the git checkout.' -ForegroundColor Red
        Write-Host 'These paths under installers/, Templates/Scripts/ or the root scripts are modified,' -ForegroundColor Red
        Write-Host "untracked, or hidden from git (HEAD $head):" -ForegroundColor Red
        foreach ($l in $changes) { Write-Host "  $l" }
        Write-Host ''
        Write-Host 'This vault is synced, so another device could have written them. Nothing has run.'
        Write-Host 'Look at what changed before you run anything:'
        Write-Host "  git -C `"$Root`" status --untracked-files=all"
        Write-Host "  git -C `"$Root`" diff"
        Write-Host "To go back to the published code: git -C `"$Root`" checkout -- <path> for a"
        Write-Host 'changed file, and delete an untracked one.'
        Write-Host 'If these are your own edits (a maintainer working on the installer), re-run with'
        Write-Host "$flag. That is logged."
    }
    if ($other.Count -gt 0) {
        Write-Host 'Local changes to tracked files; not updating:' -ForegroundColor Red
        foreach ($l in $other) { Write-Host "  $l" }
        if ($changes.Count -eq 0 -and $other.Count -eq 1 -and $other[0] -match '\.obsidian/types\.json$') {
            Write-Host ''
            Write-Host 'Obsidian rewrites .obsidian/types.json when it sees new note properties.'
            Write-Host 'It only records property types, so taking the upstream copy is safe:'
            Write-Host "  git -C `"$Root`" checkout -- .obsidian/types.json"
            Write-Host 'Then run this update again.'
        }
    }
    return $false
}

# A profile is parsed as data on Windows (never executed), but it still names
# where Claude calls go. Allowed in installers\profiles\ or outside the vault;
# refused elsewhere inside the vault, where it is synced and invisible to git.
# Prints the file and its SHA-256. Returns $false to refuse.
function Test-TreeGateProfile {
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $true }   # not found: the caller reports it
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Write-Host "TREE GATE: REFUSED. The profile $Path is a link; pass the file itself." -ForegroundColor Red
        return $false
    }
    $full = [IO.Path]::GetFullPath($item.FullName)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $profilesDir = (Join-Path $rootFull 'installers\profiles').TrimEnd('\')
    $parent = [IO.Path]::GetDirectoryName($full).TrimEnd('\')
    if ($full.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase) -and
        -not [string]::Equals($parent, $profilesDir, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "TREE GATE: REFUSED. The profile $full is inside the vault but not in" -ForegroundColor Red
        Write-Host 'installers\profiles\. Anything in the vault is synced. Move it to' -ForegroundColor Red
        Write-Host 'installers\profiles\, or keep it outside the vault (for example Downloads),' -ForegroundColor Red
        Write-Host 'and pass that path.' -ForegroundColor Red
        return $false
    }
    $sum = 'unavailable'
    try { $sum = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant() } catch { }
    Write-Host "  profile file:   $full"
    Write-Host "  profile sha256: $sum"
    Write-Host '  Compare that hash with the one its sender gave you.'
    return $true
}

# Printed at the end of a full install.
function Show-TreeGateSyncNotice {
    Write-Host ''
    Write-Host 'REQUIRED STEP (findings 5278120, 5278133): keep other devices from changing the code.' -ForegroundColor Yellow
    Write-Host "This vault is the installer's git checkout. If it syncs with Obsidian Sync, then"
    Write-Host 'in Obsidian on EVERY device that syncs this vault:'
    Write-Host '  Settings > Sync > turn OFF "Sync all other types" (at least, do not sync code'
    Write-Host '  file types)'
    Write-Host 'and on THIS machine, the one that runs the jobs:'
    Write-Host '  Settings > Sync > Excluded folders > add Templates/Scripts and installers'
    Write-Host 'Otherwise a paired device can change the scripts these jobs run and the'
    Write-Host 'installer code the next update runs.'
}
