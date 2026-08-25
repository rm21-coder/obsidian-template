<#
.SYNOPSIS  Install Obsidian community plugins from the PINNED manifest
           (Windows port of installers/lib/plugins.sh + 30-plugins.sh).
.DESCRIPTION
  Every plugin comes from installers\plugin-pins.json: exact release tag (or
  commit for release-less plugins), exact download URL, and a SHA256 per
  file, verified before anything lands in the vault. A download that does
  not hash-match is discarded and the plugin fails closed -- a compromised
  or merely surprising upstream release cannot reach the vault until a
  maintainer re-pins deliberately:

      python installers/lib/pin_plugins.py    # refresh pins (network)
      git diff installers/plugin-pins.json    # review, then commit

  No registry lookup, no GitHub API, no "latest": installs are reproducible
  from the pin file alone. You still open Obsidian once and click "Trust
  author and enable plugins" -- this does not bypass that consent gate.
.PARAMETER Only  Install just one plugin id (for iterating on a single plugin).
#>
[CmdletBinding()] param([string]$Only)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

# PS 5.1 on older boxes can default to TLS 1.0; GitHub requires 1.2+.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$UA = 'obsidian-template-installer'

$vault    = Get-VaultRoot
$pinsFile = Join-Path $vault 'installers\plugin-pins.json'
if (-not (Test-Path $pinsFile)) { throw "pin manifest not found: $pinsFile (run pin_plugins.py)" }
$pins     = Get-Content $pinsFile -Raw | ConvertFrom-Json
$pinById  = @{}
foreach ($p in $pins) { $pinById[$p.id] = $p }

$listFile = Join-Path $vault '.obsidian\community-plugins.json'
if (-not (Test-Path $listFile)) { throw "plugin list not found: $listFile" }
$ids = @(Get-Content $listFile -Raw | ConvertFrom-Json)

function Install-OnePlugin {
    param([string]$Id, [string]$Vault)

    $pin = $pinById[$Id]
    if (-not $pin) {
        Write-Warning "  plugin '$Id' has no pin -- re-run pin_plugins.py"
        return $false
    }
    Write-Host "  $Id @ $($pin.ref) (pinned)"

    $staging = Join-Path $env:TEMP ("obsidian_plugin_" + [System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    try {
        foreach ($prop in $pin.files.PSObject.Properties) {
            $name = $prop.Name
            $meta = $prop.Value
            $dst  = Join-Path $staging $name
            try {
                Invoke-WebRequest -Uri $meta.url -OutFile $dst -UseBasicParsing `
                    -Headers @{ 'User-Agent' = $UA } -ErrorAction Stop
            } catch {
                Write-Warning "    fetch failed: $($meta.url)"
                return $false
            }
            $got = (Get-FileHash -Algorithm SHA256 -Path $dst).Hash.ToLower()
            if ($got -ne $meta.sha256.ToLower()) {
                Write-Warning "    HASH MISMATCH for $Id/$name -- refusing to install."
                Write-Warning "    expected $($meta.sha256)"
                Write-Warning "    got      $got"
                Write-Warning "    Upstream changed under the pin. Re-run pin_plugins.py, review, commit."
                return $false
            }
        }
        # All files verified -- move into place.
        $dir = Join-Path $Vault ".obsidian\plugins\$Id"
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Get-ChildItem $staging | ForEach-Object {
            Copy-Item $_.FullName (Join-Path $dir $_.Name) -Force
        }
        Write-Host "    installed (verified): $dir"
        return $true
    } finally {
        Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$targets = if ($Only) { @($Only) } else { $ids }
$ok = 0; $fail = 0
foreach ($id in $targets) {
    if ([string]::IsNullOrWhiteSpace($id)) { continue }
    if (Install-OnePlugin -Id $id -Vault $vault) { $ok++ } else { $fail++ }
}
Write-Host ("  plugins: {0}/{1} installed, {2} failed" -f $ok, ($ok + $fail), $fail)

# Mirrors the bash component: any verification/fetch failure is a hard stop --
# unlike a transient network blip, a pin/upstream disagreement needs eyes.
if ($fail -gt 0) { throw "$fail plugin(s) failed verification or fetch" }
