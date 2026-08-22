<#
.SYNOPSIS  Windows installer for the Obsidian second-brain automation.
.DESCRIPTION
  Windows counterpart to install.sh. Sets up the vault, venv, plugins, and
  registers the scheduled tasks, honouring the per-job Enabled flag in
  schedules.psd1: 12 of the 15 jobs ship ENABLED and start running on their
  triggers as soon as this finishes; the other 3 are registered DISABLED
  because each needs a per-user resource this template can't assume exists
  (a dedicated mailbox, an Azure Blob relay, an MCP calendar connector).
  Idempotent — safe to re-run.

  Run it (one command, from the repo root):
      powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\install.ps1
  or, in an already-open PowerShell:
      Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
      .\Templates\Scripts\windows\install.ps1

  Steps:  00 preflight (python 3.10+) · 02 classification audit · 05 obsidian
          app (winget) · 06 docker desktop (winget, early — see note below) ·
          10 vault check · 20 secrets stub · 30 community
          plugins · 31 quickadd patch · 35 ribbon order · 40 venv + deps ·
          50 llm-rag (optional) · 80 register tasks (12 enabled, 3 disabled) ·
          90 status

  Docker Desktop's installer requests admin elevation — a UAC prompt with its
  own timeout, run on the secure desktop. If RAG is requested, step 06 installs
  it immediately after Obsidian, before the long unattended stretch (plugins,
  pip installs, the Ollama model pull) so that prompt lands while you're still
  at the keyboard from just having run this command, not minutes later after
  you've stepped away. Docker Desktop's first *launch* (accepting its license,
  waiting for "Engine running") stays a separate manual step regardless — see
  step 50's output.

  Not applicable on Windows (no counterpart needed): the markitdown .app
  dropper (a headless CLI equivalent + Send To shortcut cover this instead)
  and the macOS TCC scoped-access wrapper. Security controls, podcast
  transcription, and the LLM/RAG stack are all ported and included — see
  docs/Windows Setup.md.
.PARAMETER SkipTasks   Do everything except registering scheduled tasks.
.PARAMETER WithRAG     Also install the fully-local RAG stack (Ollama + a small
                       model + Open WebUI in Docker) via setup-rag.ps1. Off by
                       default: multi-GB download, and Docker Desktop needs a
                       one-time launch/reboot before the container can start.
.PARAMETER SkipAudit   Skip the classification audit (step 02).
.PARAMETER NonInteractive  Never prompt. RAG is installed only if -WithRAG is
                       passed. Set this for unattended installs; a normal
                       console run still offers RAG interactively (default
                       answer: no).
#>
[CmdletBinding()] param(
    [switch]$SkipTasks,
    [switch]$WithRAG,
    [switch]$SkipAudit,
    [switch]$NonInteractive
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

# Idempotent re-runs (and step 06 below) check for winget-installed tools via
# Get-Command in this same process -- refresh PATH first so a prior run's
# install (Docker, Ollama, ...) isn't invisible to this one because of a
# stale environment inherited from whatever console launched this script.
Sync-Path

# Prefix args after the exe (e.g. the '-3' in 'py -3'), empty for bare exes.
# Guarded against the 1..0 descending-range trap for single-element arrays.
function Get-CandPrefix($cand) {
    if ($cand.Count -gt 1) { return @($cand[1..($cand.Count - 1)]) }
    return @()
}

# Offer an optional add-on. Returns $false (no prompt) for unattended runs
# (-NonInteractive, or stdin redirected — e.g. CI), so defaults stay off; a
# human at a console gets a y/N prompt defaulting to No.
function Confirm-Optional([string]$Question) {
    if ($NonInteractive -or [Console]::IsInputRedirected) { return $false }
    return ((Read-Host "$Question [y/N]").Trim() -match '^(y|yes)$')
}

# Resolve a base Python 3.10+ interpreter, preferring the 'py' launcher.
# Uses `--version` (parsed by regex) rather than a `-c` probe: PowerShell 5.1
# mangles embedded double-quotes when passing args to native exes, which broke
# the old `python -c 'print("%d.%d"...)'` check.
function Resolve-BasePython {
    foreach ($cand in @(@('py','-3'), @('python'), @('python3'))) {
        $exe = $cand[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $prefix = Get-CandPrefix $cand
            $out = (& $exe @prefix '--version' 2>&1) | Out-String
            if ($out -match '(\d+)\.(\d+)(?:\.\d+)?') {
                $maj = [int]$Matches[1]; $min = [int]$Matches[2]
                if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 10)) {
                    return ,$cand   # array: exe + any prefix args
                }
            }
        } catch { }
    }
    return $null
}

Write-Host '== 00 preflight =='
$base = Resolve-BasePython
if (-not $base) {
    throw "No Python 3.10+ found. Install it (winget install Python.Python.3.12) and re-run. The scripts use PEP 604 unions, so 3.10 is the floor."
}
Write-Host ("  base python: {0}" -f ($base -join ' '))

Write-Host '== 02 classification audit =='
# Recipient-side belt-and-suspenders (mirrors macOS 02-classification-audit.sh):
# refuse to install if the repo carries non-public .md content in user folders.
if ($SkipAudit) {
    Write-Host '  -SkipAudit: skipping'
} else {
    $repoRoot = Get-RepoRoot
    $audit = Join-Path $repoRoot 'installers\lib\check_classification.py'
    if (Test-Path $audit) {
        Invoke-Native -ErrorMessage ("Classification audit FAILED: the repo has non-public .md " +
                "content in user folders. Investigate: python `"$audit`" --repo-root `"$repoRoot`" " +
                "(override with -SkipAudit).") `
            { & $base[0] @(Get-CandPrefix $base) $audit --repo-root $repoRoot --quiet }
        Write-Host '  audit passed - repo carries only public content'
    } else {
        Write-Warning "  audit script not found at $audit; skipping"
    }
}

Write-Host '== 05 obsidian app =='
# Install the Obsidian desktop client via winget (analog of the macOS Homebrew
# cask in 05-obsidian-app.sh). Idempotent: detects an existing install first.
# winget uses Obsidian's nullsoft user-scope installer, which lands in
# %LOCALAPPDATA%\Programs\Obsidian\. Check the machine-scope path too.
$obsidianExes = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Obsidian\Obsidian.exe'),
    (Join-Path ${env:ProgramFiles} 'Obsidian\Obsidian.exe')
)
if ($obsidianExes | Where-Object { Test-Path $_ }) {
    Write-Host '  Obsidian already installed'
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host '  installing Obsidian via winget...'
    Invoke-Native -Warn -ErrorMessage "winget install failed; install Obsidian from https://obsidian.md if it's not present" {
        winget install --id Obsidian.Obsidian --source winget `
            --accept-package-agreements --accept-source-agreements --silent
    }
} else {
    Write-Warning '  winget not found; install Obsidian from https://obsidian.md and re-run'
}

Write-Host '== 06 docker desktop (early install, if RAG requested) =='
# Decided here (not just before step 50) specifically so we know whether to
# install Docker now. This is the ONLY elevation-gated step in the whole
# install: winget's install of Docker Desktop pops a UAC prompt, and if that
# prompt shows up after several minutes of unattended downloads (Obsidian,
# plugins, pip, the ~5GB Ollama model), whoever ran this has likely walked
# away, the prompt times out unanswered, and the install step silently fails.
# Installing it right here, immediately after Obsidian's own quick winget
# call, means the prompt appears within the first few seconds of the run
# instead.
$doRAG = [bool]$WithRAG
if (-not $doRAG) {
    $doRAG = Confirm-Optional 'Install the local RAG stack now? (Ollama + llama3.1:8b + Open WebUI in Docker; multi-GB, needs Docker Desktop)'
}
if ($doRAG) {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        Write-Host '  Docker already installed'
    } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host '  installing Docker Desktop via winget...'
        Write-Host '  >>> A Windows admin approval (UAC) prompt should appear now -- please accept it. <<<'
        Invoke-Native -Warn -ErrorMessage 'Docker Desktop winget install failed' {
            winget install --id Docker.DockerDesktop --source winget `
                --accept-package-agreements --accept-source-agreements
        }
        # winget just updated the registry PATH, but this already-running
        # process's in-memory copy doesn't see it -- refresh now so later
        # steps (and setup-rag.ps1, called later in this same run) can find
        # `docker` without needing a brand new window.
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
    } else {
        Write-Warning '  winget not found; install Docker Desktop from https://docker.com'
    }
} else {
    Write-Host '  skipped (RAG not requested)'
}

Write-Host '== 10 vault check =='
# Mirror macOS 10-vault-bootstrap.sh: the vault must live at ~\Obsidian. If it
# doesn't exist, link it to this repo with a directory junction (no admin /
# Developer Mode needed, unlike a symlink). Idempotent: skips if already there.
$vault    = Get-VaultRoot
$repoRoot = Get-RepoRoot
if (Test-Path $vault) {
    $tgt = (Get-Item $vault).Target
    if ($tgt) { Write-Host "  vault at $vault -> $tgt" }
    else      { Write-Host "  vault at $vault" }
} else {
    Write-Host "  vault not found at $vault; linking it to this repo"
    New-Item -ItemType Junction -Path $vault -Target $repoRoot | Out-Null
    Write-Host "  created junction: $vault -> $repoRoot"
}

Write-Host '== 20 secrets =='
$secrets = Get-SecretsFile
if (-not (Test-Path $secrets)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $secrets) | Out-Null
    @('# Obsidian automation secrets - DO NOT COMMIT',
      '# Anthropic (tagger, voice-cleanup): https://console.anthropic.com/settings/keys',
      'ANTHROPIC_API_KEY=',
      '# Institutional AI gateway (optional). Uncomment both to route every',
      '# Claude call through your organization''s gateway instead of',
      '# api.anthropic.com, and put the gateway key under the name you give',
      '# here rather than in ANTHROPIC_API_KEY. The gateway must speak the',
      '# Anthropic Messages API. See Templates/Scripts/llm_endpoint.py.',
      '#LLM_BASE_URL=https://api.ai.example.edu',
      '#LLM_API_KEY_NAME=EXAMPLE_AI_API_KEY',
      '# RAG sync (obsidian-rag-sync): the LOCAL Open WebUI (Docker container on',
      '# port 3000, installed by setup-rag.ps1 / install.ps1 -WithRAG).',
      'OPEN_WEBUI_URL=http://localhost:3000',
      'OPEN_WEBUI_API_KEY=','OBSIDIAN_COLLECTION_ID=') |
        Set-Content -Path $secrets -Encoding utf8
    Write-Host "  created stub $secrets - fill in your keys."
} else { Write-Host "  secrets present: $secrets" }

Write-Host '== 30 plugins =='
# Fetch the community plugins listed in .obsidian\community-plugins.json.
# Non-fatal: a plugin or two can fail without blocking the rest of the install.
try {
    & (Join-Path $PSScriptRoot 'Install-Plugins.ps1')
} catch {
    Write-Warning "  plugin install had errors: $_"
}

Write-Host '== 31 quickadd patch =='
# Drop the topItems suggestion from QuickAdd's deterministic getFolderPath
# fall-through so a single-folder Template choice doesn't pop an unexpected
# folder picker. Idempotent, and a safe no-op on plugin versions that don't
# match.
#
# Delegates to installers\lib\quickadd_patch.py — the same helper macOS's
# 31-quickadd-patch.sh calls — rather than carrying a parallel PowerShell
# regex. The matching rule is subtle enough (QuickAdd's minifier renames the
# locals every release, so the fall-through has to be identified structurally,
# as the one getOrCreateFolder call whose first argument IS its own
# allowedRoots) that two implementations would inevitably drift.
$qa     = Join-Path $vault '.obsidian\plugins\quickadd\main.js'
$qaHelp = Join-Path $vault 'installers\lib\quickadd_patch.py'
if (-not (Test-Path $qa)) {
    Write-Host '  quickadd main.js not found; skipping'
} elseif (-not (Test-Path $qaHelp)) {
    Write-Warning "  patch helper not found at $qaHelp; skipping"
} else {
    $qaRes = (& $base[0] @(Get-CandPrefix $base) $qaHelp $qa 2>&1 | Out-String).Trim()
    switch -Regex ($qaRes) {
        '^PATCHED$'         { Write-Host    '  quickadd patched (dropped topItems in default fall-through)' }
        '^ALREADY_PATCHED$' { Write-Host    '  quickadd already patched' }
        '^NOT_FOUND$'       { Write-Warning '  quickadd pattern not found (plugin version differs); skipping' }
        '^AMBIGUOUS:(\d+)$' { Write-Warning "  expected 1 occurrence of the patch target, found $($Matches[1]); skipping" }
        default             { Write-Warning "  quickadd patch helper failed: $qaRes" }
    }
}

Write-Host '== 40 venv + deps =='
# CTranslate2 (faster-whisper, for podcast transcription) links against the MSVC
# runtime; without it the .dll fails to load. Ensure it's present. Idempotent —
# winget detects an existing install.
#
# Skipped for a native ARM64 interpreter: ctranslate2 publishes no win_arm64
# wheel (and no sdist), so faster-whisper is marker-guarded out of
# requirements.txt there and nothing left in the venv links against this. The
# arm64 redist would only cost a UAC elevation prompt in the middle of the run.
#
# Gated on the INTERPRETER's build target rather than the machine's: an x64
# Python under emulation on an ARM64 box loads x64 extension DLLs and does
# still need the x64 redist. `sysconfig.get_platform()` is the same tag pip
# resolves wheels against, and takes no embedded double quotes (see the note on
# Resolve-BasePython about PS 5.1 mangling those for native commands).
$pyPlat = ''
try {
    $pyPlat = (& $base[0] @(Get-CandPrefix $base) -c 'import sysconfig;print(sysconfig.get_platform())' 2>&1 | Out-String).Trim()
} catch { }
if ($pyPlat -eq 'win-arm64') {
    Write-Host '  skipping MSVC runtime (ARM64 Python: no ctranslate2 wheel to link against)'
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host '  ensuring MSVC runtime (for faster-whisper / ctranslate2)...'
    Invoke-Native -Warn -ErrorMessage 'MSVC runtime install failed' {
        winget install --id Microsoft.VCRedist.2015+.x64 --source winget `
            --accept-package-agreements --accept-source-agreements 1>$null
    }
}
$scriptsDir = Get-ScriptsDir
if (-not (Test-Path $scriptsDir)) { throw "Scripts dir not found: $scriptsDir (is the repo cloned into the vault?)" }
$venv   = Join-Path $scriptsDir '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Invoke-Native -ErrorMessage 'venv creation failed' { & $base[0] @(Get-CandPrefix $base) -m venv $venv }
    Write-Host "  created venv at $venv"
}
Invoke-Native -ErrorMessage 'pip upgrade failed' { & $venvPy -m pip install --upgrade pip 1>$null }
$req = Join-Path $scriptsDir 'requirements.txt'
if (Test-Path $req) {
    Write-Host '  installing requirements.txt (mlx-whisper is arm64-guarded and skips on Intel) ...'
    Invoke-Native -ErrorMessage 'dependency install failed' { & $venvPy -m pip install -r $req }
}
Write-Host '  deps installed'

Write-Host '== 35 ribbon order =='
# Merge the tracked ribbon icon order (.obsidian\ribbon-config.json) into
# workspace.json, mirroring macOS 35-ribbon-order.sh. Obsidian must be CLOSED,
# or it overwrites workspace.json from memory on quit and the order is lost.
$ribbonCfg = Join-Path $vault '.obsidian\ribbon-config.json'
$ribbonPy  = Join-Path $scriptsDir 'sync-ribbon-order.py'
if (-not (Test-Path $ribbonCfg)) {
    Write-Host '  no ribbon-config.json; skipping'
} elseif (Get-Process Obsidian -ErrorAction SilentlyContinue) {
    Write-Warning '  Obsidian is running; quit it, then re-run just this step:'
    Write-Warning ("    {0} `"{1}`" --apply --vault `"{2}`"" -f $venvPy, $ribbonPy, $vault)
} else {
    Invoke-Native -Warn -ErrorMessage 'ribbon order sync failed' { & $venvPy $ribbonPy --apply --vault $vault }
}

# $doRAG was already decided (and Docker Desktop's winget install already
# kicked off, if requested) back in step 06 -- see the note there.
if ($doRAG) {
    Write-Host '== 50 llm-rag (local Ollama + model + Open WebUI) =='
    & (Join-Path $PSScriptRoot 'setup-rag.ps1')
} else {
    Write-Host '== 50 llm-rag (skipped) =='
    Write-Host '  Optional local RAG. Add later:  install.ps1 -WithRAG   (or run setup-rag.ps1)'
}

if (-not $SkipTasks) {
    Write-Host '== 80 scheduled tasks (12 enabled, 3 disabled) =='
    & (Join-Path $PSScriptRoot 'Register-Tasks.ps1')
}

Write-Host '== 90 status =='
Get-ScheduledTask -TaskPath '\Obsidian\*' -ErrorAction SilentlyContinue |
    Select-Object State, TaskName | Format-Table -AutoSize

Write-Host ''
Write-Host 'install.ps1 complete.'
Write-Host ''
Write-Host 'Optional add-on (off by default; re-run with the switch, or answer the prompt):'
Write-Host '  -WithRAG      Local RAG: Ollama + llama3.1:8b + Open WebUI (Docker)'
Write-Host ''
Write-Host 'Extras (optional, run when you want them):'
Write-Host '  windows\Install-SendTo.ps1   right-click "Send to -> Markitdown to Obsidian" (file -> .md)'
Write-Host '  Fill in %USERPROFILE%\dev\secrets\.env: ANTHROPIC_API_KEY, RAG keys'
Write-Host ''
Write-Host 'The 12 enabled jobs are LIVE now and will fire on their triggers -- fill in'
Write-Host '.env first if you have not, or they will log errors until you do.'
Write-Host 'Sanity-check one by hand:'
Write-Host ("  {0} `"{1}`"" -f $venvPy, (Join-Path $scriptsDir 'tag_clippings.py'))
Write-Host 'The 3 disabled jobs (source-mail-pull, meeting-pull, handoff-blob-pull) each'
Write-Host 'need a per-user resource first; validate the script, then enable deliberately:'
Write-Host "  Enable-ScheduledTask -TaskName source-mail-pull -TaskPath '\Obsidian\'"
Write-Host 'See docs/Windows Setup.md for the full guide.'
