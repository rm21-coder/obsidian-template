<#
.SYNOPSIS  Install the fully-local RAG stack on Windows: Ollama + a small model
           + Open WebUI (Docker). Windows counterpart of the Mac's
           installers/components/50-llm-rag.sh — same Docker container image, so
           Open WebUI stays self-contained and doesn't pollute the host with its
           own Python/runtime. Nothing here calls any other machine.
.DESCRIPTION
  Steps (each idempotent):
    1. Install Ollama (winget Ollama.Ollama). Ollama serves http://localhost:11434
       as a background service after install.
    2. Pull the chat model (default llama3.1:8b, ~4.9 GB).
    3. Ensure Docker is available (install Docker Desktop via winget if missing).
       Docker Desktop runs its engine in WSL2. If WSL2 isn't installed, run
       `wsl --install` in an admin PowerShell and reboot first. After Docker
       Desktop is installed, launch it once (accept the license, let the engine
       start). If the engine isn't running yet, this script installs Docker and
       stops with instructions; re-run it once Docker Desktop is up.
    4. Run the official Open WebUI container on http://localhost:3000, pointed at
       the host's Ollama via host.docker.internal:11434.

  One-time MANUAL setup in Open WebUI afterward (interactive admin signup — can't
  be scripted):
    a. Open http://localhost:3000, create the local admin account.
    b. Create a Knowledge collection named "Obsidian".
    c. Enable API keys under Admin Panel -> Settings -> Authentication ("Enable
       API Key", Open WebUI 0.11 — hidden until enabled), then
       Settings -> Account -> API Keys: create one.
    d. Put OPEN_WEBUI_URL=http://localhost:3000, OPEN_WEBUI_API_KEY,
       OBSIDIAN_COLLECTION_ID in %USERPROFILE%\dev\secrets\.env
    e. Enable-ScheduledTask -TaskName rag-sync -TaskPath '\Obsidian\'
.PARAMETER Model      Ollama model tag to pull (default llama3.1:8b).
.PARAMETER SkipModel  Install the stack but don't pull the model.
.PARAMETER ResetWebUI Wipe any existing open-webui container + Docker volume
                       first, forcing a fresh admin signup. Use this if a
                       previous run (or an earlier validation pass on this
                       box) already created an account you don't have the
                       credentials for. Destroys anything stored in that
                       volume (accounts, any already-synced knowledge docs).
#>
[CmdletBinding()] param(
    [string]$Model = 'llama3.1:8b',
    [switch]$SkipModel,
    [switch]$ResetWebUI
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

# This script installs things via winget (Ollama, Docker Desktop) and then
# immediately checks for them with Get-Command in the same process -- refresh
# PATH from the registry first so a stale inherited environment (e.g. this
# script invoked via `powershell -File ...` from a console that's been open
# since before those installs happened) doesn't cause a false "not installed"
# read on something a PRIOR run already installed.
Sync-Path

# ---- 1. Ollama -------------------------------------------------------------
Write-Host '== Ollama =='
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if ((Get-Command ollama -ErrorAction SilentlyContinue) -or (Test-Path $ollamaExe)) {
    Write-Host '  Ollama already installed'
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host '  installing Ollama via winget...'
    Invoke-Native -Warn -ErrorMessage 'winget install failed' {
        winget install --id Ollama.Ollama --source winget `
            --accept-package-agreements --accept-source-agreements
    }
} else {
    Write-Warning '  winget not found; install Ollama from https://ollama.com/download'
}

$ollama = $null
if (Get-Command ollama -ErrorAction SilentlyContinue) { $ollama = 'ollama' }
elseif (Test-Path $ollamaExe) { $ollama = $ollamaExe }

# ---- 2. Model --------------------------------------------------------------
Write-Host '== model =='
if ($SkipModel) {
    Write-Host '  --SkipModel: not pulling a model'
} elseif ($ollama) {
    Write-Host "  pulling $Model (large download; can take several minutes) ..."
    Invoke-Native -Warn -ErrorMessage "'ollama pull $Model' failed" { & $ollama pull $Model }
} else {
    Write-Warning "  ollama not on PATH yet. Open a NEW terminal and run: ollama pull $Model"
}

# ---- 3. Docker -------------------------------------------------------------
Write-Host '== Docker =='
# Same belt-and-suspenders as Ollama above: fall back to the known install
# location if Get-Command still can't see it even after Sync-Path (e.g. a
# registry write that genuinely hasn't landed yet). If only the fallback
# resolves, add its directory to THIS process's PATH so the bare `docker`
# calls later in this script (Open WebUI container section) keep working.
$dockerExe = Join-Path ${env:ProgramFiles} 'Docker\Docker\resources\bin\docker.exe'
if (-not (Get-Command docker -ErrorAction SilentlyContinue) -and (Test-Path $dockerExe)) {
    $env:Path = "$env:Path;" + (Split-Path $dockerExe)
}
$dockerInstalled = [bool](Get-Command docker -ErrorAction SilentlyContinue)

$dockerReady = $false
if ($dockerInstalled) {
    # docker info's own stderr (e.g. "engine not running") must not become a
    # terminating NativeCommandError under this script's ErrorActionPreference
    # ='Stop' -- see Invoke-Native's doc comment in common.ps1 for the class
    # of bug this is. This is a probe (decide via exit code), not a command
    # whose failure should throw.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { docker info *> $null } finally { $ErrorActionPreference = $prevEAP }
    if ($LASTEXITCODE -eq 0) { $dockerReady = $true }
}

if (-not $dockerReady) {
    if (-not $dockerInstalled) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host '  installing Docker Desktop via winget...'
            Invoke-Native -Warn -ErrorMessage 'winget install failed' {
                winget install --id Docker.DockerDesktop --source winget `
                    --accept-package-agreements --accept-source-agreements
            }
        } else {
            Write-Warning '  winget not found; install Docker Desktop from https://docker.com'
        }
    }
    Write-Warning '  Docker engine is not running yet.'
    if (Test-WSL2Present) {
        Write-Warning '  WSL2 is already installed on this box, so no reboot is needed --'
        Write-Warning '  launch Docker Desktop once, accept the license, and wait for'
        Write-Warning '  "Engine running" in the whale icon.'
    } else {
        Write-Warning '  Docker Desktop runs its engine inside WSL2, which is not installed'
        Write-Warning '  yet. Run this in an ADMINISTRATIVE PowerShell and then REBOOT:'
        Write-Warning '      wsl --install'
        Write-Warning '  After the reboot, launch Docker Desktop once (accept the license,'
        Write-Warning '  wait for "Engine running").'
    }
    Write-Warning '  Then re-run this script:'
    Write-Warning "      .\Templates\Scripts\windows\setup-rag.ps1 -SkipModel"
    return
}
Write-Host '  Docker engine is running'

# ---- 4. Open WebUI container ----------------------------------------------
Write-Host '== Open WebUI (Docker) =='

# The 'open-webui' Docker volume outlives uninstall.ps1 (only -RemoveRAG drops
# it), so a rebuild can silently reattach to an old admin account. -ResetWebUI
# wipes both container and volume first for a guaranteed-fresh signup.
if ($ResetWebUI) {
    Write-Host '  -ResetWebUI: removing any existing open-webui container + volume...'
    Invoke-Native -Warn -ErrorMessage 'docker rm failed' { docker rm -f open-webui 1>$null }
    Invoke-Native -Warn -ErrorMessage 'docker volume rm failed' { docker volume rm open-webui 1>$null }
}

$existing = (docker ps -a --filter 'name=^open-webui$' --format '{{.Names}}' 2>$null)
if ($existing -eq 'open-webui') {
    Invoke-Native -Warn -ErrorMessage 'docker start failed' { docker start open-webui *> $null }
    Write-Host '  open-webui container present; started (if it was stopped)'
} else {
    Write-Host '  pulling and starting the official open-webui image...'
    Invoke-Native -ErrorMessage 'docker run failed' {
        docker run -d --restart=always `
            -p 3000:8080 `
            -v open-webui:/app/backend/data `
            --add-host=host.docker.internal:host-gateway `
            -e 'OLLAMA_BASE_URL=http://host.docker.internal:11434' `
            --name open-webui `
            ghcr.io/open-webui/open-webui:main
    }
    Write-Host '  open-webui started; web UI at http://localhost:3000'
}

# Check whether this landed on a fresh volume or an existing admin account --
# don't make the user discover this the hard way via a plain "Sign in" screen.
Write-Host '  waiting for Open WebUI to respond...'
$cfg = $null
for ($i = 0; $i -lt 20 -and -not $cfg; $i++) {
    Start-Sleep -Milliseconds 750
    try { $cfg = Invoke-RestMethod -Uri 'http://localhost:3000/api/config' -TimeoutSec 2 -ErrorAction Stop }
    catch { }
}

# Already fully configured? Check .env rather than guessing from Open WebUI's
# signup state alone -- enable_signup=false is also the normal, expected state
# right after YOUR OWN completed setup, not just a leftover one.
$secretsFile = Get-SecretsFile
$alreadyConfigured = $false
if (Test-Path $secretsFile) {
    Import-DotEnv -Path $secretsFile
    $alreadyConfigured = [bool]$env:OPEN_WEBUI_API_KEY -and [bool]$env:OBSIDIAN_COLLECTION_ID
}

Write-Host ''
if ($alreadyConfigured) {
    Write-Host 'RAG stack ready. .env already has OPEN_WEBUI_API_KEY and OBSIDIAN_COLLECTION_ID --'
    Write-Host 'this box looks fully configured already. Nothing else to do. If rag-sync is'
    Write-Host 'still disabled:'
    Write-Host "  Enable-ScheduledTask -TaskName rag-sync -TaskPath '\Obsidian\'"
} else {
    Write-Host 'RAG stack ready. One-time MANUAL setup (Open WebUI needs an interactive'
    Write-Host 'admin signup, which can''t be scripted):'
    if (-not $cfg) {
        Write-Warning '  could not reach http://localhost:3000 yet -- give it a few more seconds and check manually.'
    } elseif ($cfg.features.enable_signup) {
        Write-Host '  Fresh volume detected -- go to http://localhost:3000 and create the admin account.'
    } else {
        Write-Warning '  An admin account already exists in the "open-webui" Docker volume, but .env'
        Write-Warning '  is missing OPEN_WEBUI_API_KEY / OBSIDIAN_COLLECTION_ID. If that account is'
        Write-Warning '  yours (e.g. you completed steps 1-3 below in a separate browser), log in and'
        Write-Warning '  grab those two values now. If you don''t recognize it (e.g. left over from an'
        Write-Warning '  earlier validation run on this box -- uninstall.ps1 only clears it with'
        Write-Warning '  -RemoveRAG), force a clean slate instead:'
        Write-Warning '      .\Templates\Scripts\windows\setup-rag.ps1 -SkipModel -ResetWebUI'
    }
    Write-Host '  1. Open http://localhost:3000; create the local admin account (first user = admin).'
    Write-Host '  2. Workspace -> Knowledge -> "+"/Create Knowledge Base -> name it "Obsidian"'
    Write-Host '     -> hit Save. Open it; the UUID in the URL'
    Write-Host '     (.../workspace/knowledge/<uuid>) is your OBSIDIAN_COLLECTION_ID.'
    Write-Host '  3. Enable API keys first: Admin Panel -> Settings -> Authentication'
    Write-Host '     -> "Enable API Key" (Open WebUI 0.11; the section is hidden until'
    Write-Host '     this is on) -> hit Save at the bottom of the page (the toggle alone'
    Write-Host '     does not persist it). Then Settings -> Account -> API Keys: create a'
    Write-Host '     key -- it is only shown once, copy it before navigating away.'
    Write-Host '  4. Set in %USERPROFILE%\dev\secrets\.env:'
    Write-Host '       OPEN_WEBUI_URL=http://localhost:3000'
    Write-Host '       OPEN_WEBUI_API_KEY=<key>   OBSIDIAN_COLLECTION_ID=<id>'
    Write-Host "  5. Enable-ScheduledTask -TaskName rag-sync -TaskPath '\Obsidian\'"
}
Write-Host ''
Write-Host 'If Open WebUI cannot reach Ollama, set a user env var OLLAMA_HOST=0.0.0.0'
Write-Host 'and restart Ollama so the container can reach it via host.docker.internal.'
