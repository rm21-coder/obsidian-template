<#
.SYNOPSIS  Tear down what install.ps1 set up on Windows. Windows counterpart of
           uninstall.sh. Safe by default; DATA and shared TOOLS are opt-in.
.DESCRIPTION
  Removed by DEFAULT (moving parts + regenerable state):
    - all \Obsidian\ scheduled tasks (via Unregister-Tasks.ps1)
    - the ~\Obsidian -> repo directory junction (the REPO/target is NOT touched;
      only a real junction is removed, never a real folder)
    - the per-vault venv (Templates\Scripts\.venv) and tagger scratch
      (.tag_tracking.json, last-tag-diff.md, tag-promotion-candidates.md)
    - generated Z_dashboards\ output
    - runtime state dirs: %LOCALAPPDATA%\{obsidian-security,obsidian-rag-sync,
      obsidian-automation}
    - the "Markitdown to Obsidian" Send To shortcut (via Install-SendTo.ps1)
    - the OBSIDIAN_VAULT user env var (if set)

  Removed ONLY when asked (they hold data or are heavy shared tools):
    -RemoveRAG       stop + remove the open-webui Docker container (+ prompt for its volume)
    -RemovePlugins   delete the DOWNLOADED plugin files named in
                     installers\plugin-pins.json. Each plugin's data.json --
                     your ribbon, QuickAdd and Templater settings -- is kept,
                     and so is its folder. A reinstall does not restore
                     data.json, so it is never removed here.
    -RemoveApps      winget-uninstall Obsidian, Ollama, Docker Desktop, iCloud
    -PurgeModels     ollama rm the pulled model(s)
    -All             = -RemoveRAG -RemovePlugins  (NOT apps/models)

  NEVER touched: the repo/vault content itself, %USERPROFILE%\dev\secrets\.env
  (other tools may share it -- this uninstaller doesn't own it and never
  deletes it), WSL, the MSVC runtime.

.PARAMETER Yes     Non-interactive; take the default answer for every prompt.
.PARAMETER DryRun  Print what would happen; change nothing.
.EXAMPLE  .\uninstall.ps1 -DryRun
.EXAMPLE  .\uninstall.ps1 -Yes                       # safe teardown, unattended
.EXAMPLE  .\uninstall.ps1 -All -RemoveApps -PurgeModels -Yes   # bare-metal rebuild
#>
[CmdletBinding()] param(
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$RemoveRAG,
    [switch]$RemovePlugins,
    [switch]$RemoveApps,
    [switch]$PurgeModels,
    [switch]$All
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

# This process may be freshly launched (`powershell -File ...`) from a
# long-lived parent console whose PATH predates Docker/Ollama's install --
# refresh it so the RAG-cleanup step's `Get-Command docker` below doesn't
# report "not installed" for something that genuinely is. See Sync-Path's
# doc comment in common.ps1 for the full failure mode.
Sync-Path

if ($All) { $RemoveRAG = $true; $RemovePlugins = $true }

$repo       = Get-RepoRoot
$scriptsDir = Join-Path $repo 'Templates\Scripts'
$vault      = Get-VaultRoot     # ~\Obsidian (the junction), independent of $repo

function Confirm-Step([string]$Question, [string]$Default = 'Y', [switch]$Critical) {
    if ($Yes) { return $true }                          # -Yes: yes to all prompts
    if ([Console]::IsInputRedirected) {
        # A redirected stdin means nobody is at the keyboard to answer. Taking
        # the default is fine for the granular steps, but the top-level gate
        # gates a teardown -- assuming yes there means a harness, a piped
        # invocation or a logging wrapper can start deleting with no human
        # having agreed to it. Unattended teardown must be asked for by name.
        if ($Critical) {
            Write-Warning '  stdin is redirected, so this prompt cannot be answered.'
            Write-Warning '  Refusing to assume yes for a destructive step. Re-run'
            Write-Warning '  interactively, or pass -Yes to confirm unattended.'
            return $false
        }
        return ($Default -eq 'Y')
    }
    $hint = if ($Default -eq 'Y') { '[Y/n]' } else { '[y/N]' }
    $ans = (Read-Host "$Question $hint").Trim()
    if (-not $ans) { return ($Default -eq 'Y') }
    return ($ans -match '^(y|yes)$')
}

function Remove-PathSafe([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($DryRun) { Write-Host "  [dry-run] would remove: $Path"; return }
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        # Trust Test-Path, not the return. Remove-Item -Recurse can report
        # success having left files behind when a handle is briefly held
        # during the walk -- observed 2026-08-25, when a teardown logged
        # "removed:" for a state directory that still held sync.log. A
        # security-control state dir reported gone but still on disk means the
        # next install silently inherits the old baseline, which is the exact
        # condition a teardown exists to eliminate.
        if (-not (Test-Path -LiteralPath $Path)) {
            Write-Host "  removed: $Path"
            return
        }
    } catch {
        # Falls through for the security-harness state dirs: their files carry
        # an icacls ACL with inheritance dropped (see security_common.py's
        # restrict_file), which can resist a plain Remove-Item even for the
        # owning user. Reset to inherited defaults and retry once before
        # giving up for real.
        try {
            & icacls $Path /reset /T /C /Q 2>&1 | Out-Null
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $Path) {
                throw 'path still present after a reported-successful delete'
            }
            Write-Host "  removed: $Path (after ACL reset)"
        } catch { Write-Warning "  could not remove $Path : $_" }
    }
}

# ---- banner & plan ---------------------------------------------------------
Write-Host ''
Write-Host '=== Obsidian second-brain - Windows uninstall ==='
Write-Host "  repo/vault target : $repo"
Write-Host "  vault junction    : $vault"
if ($DryRun) { Write-Warning 'DRY RUN - nothing will be changed.' }
Write-Host ("  extras: RAG={0} Plugins={1} Apps={2} PurgeModels={3}" -f `
    $RemoveRAG, $RemovePlugins, $RemoveApps, $PurgeModels)
Write-Host '  NEVER removed: the repo/vault content, your .env secrets, WSL, MSVC runtime.'
Write-Host ''
if (-not (Confirm-Step 'Proceed with uninstall?' 'Y' -Critical)) { Write-Host 'aborted; nothing changed.'; return }

# ---- 1. scheduled tasks ----------------------------------------------------
Write-Host '== scheduled tasks =='
if ($DryRun) {
    Get-ScheduledTask -TaskPath '\Obsidian\*' -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "  [dry-run] would unregister: $($_.TaskName)" }
} else {
    & (Join-Path $PSScriptRoot 'Unregister-Tasks.ps1')
    # Remove the now-empty \Obsidian task folder.
    # Warn rather than swallow: this is the persistence surface, and a folder
    # that would not delete is worth one line in the teardown log. Not fatal --
    # Unregister-Tasks.ps1 has already removed the tasks themselves, so what
    # survives here is an empty folder, not a live scheduled job.
    try {
        $svc = New-Object -ComObject 'Schedule.Service'; $svc.Connect()
        $root = $svc.GetFolder('\')
        try { $root.DeleteFolder('Obsidian', 0); Write-Host '  removed task folder \Obsidian' }
        catch { Write-Warning "  task folder \Obsidian not removed (tasks themselves are gone): $_" }
    } catch { Write-Warning "  could not reach Task Scheduler to tidy the \Obsidian folder: $_" }
}

# ---- 2. Send To shortcut ---------------------------------------------------
Write-Host '== Send To shortcut =='
if ($DryRun) { Write-Host '  [dry-run] would remove the Markitdown Send To shortcut' }
else { & (Join-Path $PSScriptRoot 'Install-SendTo.ps1') -Remove }

# ---- 3. regenerable state (venv, scratch, LOCALAPPDATA) --------------------
Write-Host '== regenerable state =='
Remove-PathSafe (Join-Path $scriptsDir '.venv')
Remove-PathSafe (Join-Path $repo '.tag_tracking.json')
Remove-PathSafe (Join-Path $scriptsDir 'last-tag-diff.md')
Remove-PathSafe (Join-Path $scriptsDir 'tag-promotion-candidates.md')
Remove-PathSafe (Join-Path $repo 'Z_dashboards')
foreach ($d in 'obsidian-security','obsidian-rag-sync','obsidian-automation') {
    Remove-PathSafe (Join-Path $env:LOCALAPPDATA $d)
}

# ---- 4. RAG / Open WebUI container (opt-in) --------------------------------
if ($RemoveRAG) {
    Write-Host '== Open WebUI (Docker) =='
    # Same belt-and-suspenders as setup-rag.ps1: fall back to the known install
    # location if Get-Command still can't see it even after Sync-Path.
    $dockerExe = Join-Path ${env:ProgramFiles} 'Docker\Docker\resources\bin\docker.exe'
    if (-not (Get-Command docker -ErrorAction SilentlyContinue) -and (Test-Path $dockerExe)) {
        $env:Path = "$env:Path;" + (Split-Path $dockerExe)
    }
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        # Probe silently: docker info's own stderr (e.g. "engine not running")
        # must not become a terminating NativeCommandError under this script's
        # $ErrorActionPreference='Stop' -- same class of issue Invoke-Native
        # exists for elsewhere, but this is a probe (decide via exit code),
        # not a command whose failure should throw or warn on its own.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { docker info *> $null } finally { $ErrorActionPreference = $prevEAP }
        if ($LASTEXITCODE -eq 0) {
            if ((docker ps -a --filter 'name=^open-webui$' --format '{{.Names}}') -eq 'open-webui') {
                if ($DryRun) { Write-Host '  [dry-run] would stop+remove container open-webui' }
                else {
                    Invoke-Native -Warn -ErrorMessage 'docker stop failed' { docker stop open-webui *> $null }
                    Invoke-Native -Warn -ErrorMessage 'docker rm failed' { docker rm open-webui *> $null }
                    Write-Host '  removed container: open-webui'
                }
            } else { Write-Host '  no open-webui container' }
            if (@(docker volume ls --format '{{.Name}}') -contains 'open-webui') {
                if (Confirm-Step 'Also delete the open-webui Docker volume (its data)?' 'N') {
                    if ($DryRun) { Write-Host '  [dry-run] would remove volume open-webui' }
                    else {
                        Invoke-Native -Warn -ErrorMessage 'docker volume rm failed' { docker volume rm open-webui *> $null }
                        Write-Host '  removed volume: open-webui'
                    }
                }
            }
        } else { Write-Warning '  docker engine not running; start Docker Desktop to remove the container' }
    } else { Write-Host '  docker not installed; nothing to remove' }
}

# ---- 5. community plugins (opt-in) -----------------------------------------
# Removes the DOWNLOADED artifacts named in installers\plugin-pins.json, never
# the plugins tree. Each plugin folder also holds data.json -- the settings that
# drive ribbon icons, QuickAdd choices, Templater config -- which is user
# content, is tracked in this repo for five plugins, and is NOT restored by a
# reinstall: Install-Plugins.ps1 fetches only manifest.json / main.js /
# styles.css. Deleting the tree wholesale destroyed 464 lines of tracked
# configuration in a 2026-08-25 teardown, contradicting this script's own
# "NEVER touched: the repo/vault content" banner. In a clone that is
# recoverable with git checkout; in a deployed vault it is not recoverable at
# all. Removing only the pinned filenames preserves data.json by construction,
# with no allow-list to keep in sync.
if ($RemovePlugins) {
    Write-Host '== community plugins =='
    $pluginsDir = Join-Path $repo '.obsidian\plugins'
    $pinsFile   = Join-Path $repo 'installers\plugin-pins.json'
    if (-not (Test-Path -LiteralPath $pluginsDir)) {
        Write-Host "  no plugins directory at $pluginsDir"
    } elseif (-not (Test-Path -LiteralPath $pinsFile)) {
        Write-Warning "  $pinsFile not found - cannot tell downloaded artifacts"
        Write-Warning "  from your settings, so nothing was removed. Delete by hand"
        Write-Warning "  if you are sure, keeping each plugin's data.json."
    } else {
        $pins = Get-Content $pinsFile -Raw | ConvertFrom-Json
        foreach ($pin in $pins) {
            $dir = Join-Path $pluginsDir $pin.id
            if (-not (Test-Path -LiteralPath $dir)) { continue }
            foreach ($prop in $pin.files.PSObject.Properties) {
                Remove-PathSafe (Join-Path $dir $prop.Name)
            }
            if ($DryRun) { continue }
            $left = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)
            if ($left.Count -eq 0) {
                Remove-PathSafe $dir
            } else {
                Write-Host ("  kept {0}\ ({1} file(s) that are not downloaded artifacts, e.g. data.json)" -f $pin.id, $left.Count)
            }
        }
    }
}

# ---- 6. junction (default; only if it IS a junction) -----------------------
Write-Host '== vault junction =='
if (Test-Path -LiteralPath $vault) {
    $item = Get-Item -LiteralPath $vault -Force
    if ($item.LinkType) {
        if ($DryRun) { Write-Host "  [dry-run] would remove junction: $vault -> $($item.Target)" }
        else {
            try { $item.Delete(); Write-Host "  removed junction: $vault (repo/target untouched)" }
            catch { Write-Warning "  could not remove junction $vault : $_" }
        }
    } else {
        Write-Warning "  $vault is a real directory, not a junction - leaving it untouched"
    }
} else { Write-Host '  no junction present' }

# ---- 7. OBSIDIAN_VAULT env var (default) -----------------------------------
Write-Host '== env var =='
if ([Environment]::GetEnvironmentVariable('OBSIDIAN_VAULT','User')) {
    if ($DryRun) { Write-Host '  [dry-run] would clear OBSIDIAN_VAULT (User)' }
    else { [Environment]::SetEnvironmentVariable('OBSIDIAN_VAULT', $null, 'User'); Write-Host '  cleared OBSIDIAN_VAULT (User)' }
} else { Write-Host '  OBSIDIAN_VAULT not set' }

# ---- 8. models (opt-in) ----------------------------------------------------
if ($PurgeModels) {
    Write-Host '== ollama models =='
    $oll = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    $ollcmd = if (Get-Command ollama -ErrorAction SilentlyContinue) { 'ollama' } elseif (Test-Path $oll) { $oll } else { $null }
    if ($ollcmd) {
        if ($DryRun) { Write-Host '  [dry-run] would run: ollama rm llama3.1:8b' }
        else {
            # try/catch + merge stderr: `ollama rm` errors if the model is
            # already gone, which under ErrorActionPreference=Stop would halt
            # the whole teardown before the apps step. Best-effort here.
            try { & $ollcmd rm llama3.1:8b 2>&1 | Out-Null } catch {}
            Write-Host '  removed model llama3.1:8b if present (others: ollama list / ollama rm <m>)'
        }
    } else { Write-Host '  ollama not found' }
}

# ---- 9. apps (opt-in, heavy) ------------------------------------------------
if ($RemoveApps) {
    Write-Host '== apps (winget uninstall) =='
    Write-Warning '  This removes shared applications. WSL and the MSVC runtime are left in place.'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        foreach ($id in 'Obsidian.Obsidian','Ollama.Ollama','Docker.DockerDesktop','9PKTQ5699M62') {
            if ($DryRun) { Write-Host "  [dry-run] would: winget uninstall --id $id (if installed)"; continue }
            # Check first rather than let a plain "not installed" (e.g. iCloud,
            # which install.ps1 no longer installs, or an app you already
            # removed by hand) surface as a scary "may need admin" warning --
            # that message should mean something, not fire on every app this
            # list optimistically tries regardless of whether it's present.
            $prevEAP = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $listOut = winget list --id $id --exact 2>&1 | Out-String
            $ErrorActionPreference = $prevEAP
            if ($LASTEXITCODE -ne 0 -or $listOut -match 'No installed package found') {
                Write-Host "  $id not installed; nothing to remove"
                continue
            }
            Write-Host "  uninstalling $id ..."
            # Per-app try/catch so one failure (e.g. a machine-scope app needing
            # admin) doesn't halt the loop or the script.
            try {
                winget uninstall --id $id --silent --accept-source-agreements 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) { Write-Host '    done' }
                else { Write-Warning "    winget exit $LASTEXITCODE (may need admin - uninstall via Settings > Apps)" }
            } catch { Write-Warning "    could not uninstall $id : $_" }
        }
    } else { Write-Warning '  winget not found' }
}

Write-Host ''
Write-Host 'uninstall complete.'
if ($DryRun) { Write-Warning '(dry run - nothing was actually changed)' }
Write-Host "Left in place: the repo at $repo (your content), $(Get-SecretsFile), WSL, MSVC runtime."
Write-Host 'To rebuild: run install.ps1 again. To delete the repo itself: remove that folder by hand.'
