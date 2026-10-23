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
# N12: not @(... | ConvertFrom-Json): Windows PowerShell 5.1 emits a JSON
# array as ONE object, so @() held the whole list as a single element and
# every id was refused as one space-joined name. Assign, then enumerate with
# foreach (which unrolls on 5.1 and 7) so $ids is a flat array either way.
$parsedIds = Get-Content $listFile -Raw | ConvertFrom-Json
$ids = @(foreach ($i in $parsedIds) { $i })

# Pin values are input, not code (findings 5278122, 5278131; the twin of
# plugins.sh). A plugin id and a file name each become ONE path component under
# .obsidian\plugins: a letter or digit, then letters, digits, '.', '_' or '-',
# never '..' and never a trailing '.' (Windows drops it). A download URL must
# be https on a host pin_plugins.py writes: github.com (release assets) or
# raw.githubusercontent.com (release-less plugins pinned at a commit). All of
# it is checked before the first download, and every destination must resolve
# inside plugins\<id>.
$PluginNameRe = '^[A-Za-z0-9][A-Za-z0-9._-]*$'
$PluginUrlRe  = '^https://(github\.com|raw\.githubusercontent\.com)/\S+$'

function Test-PluginName([string]$Name) {
    return ($Name -cmatch $PluginNameRe) -and ($Name -notlike '*..*') -and (-not $Name.EndsWith('.'))
}

function Test-PluginUrl([string]$Url) {
    return $Url -cmatch $PluginUrlRe
}

function Install-OnePlugin {
    param([string]$Id, [string]$Vault)

    if (-not (Test-PluginName $Id)) {
        Write-Warning "  REFUSED plugin id '$Id': not a plain name (letters, digits, . _ -; no '..')"
        return $false
    }
    $pin = $pinById[$Id]
    if (-not $pin) {
        Write-Warning "  plugin '$Id' has no pin -- re-run pin_plugins.py"
        return $false
    }
    Write-Host "  $Id @ $($pin.ref) (pinned)"

    $dir = Join-Path $Vault ".obsidian\plugins\$Id"
    $dirFull = [System.IO.Path]::GetFullPath($dir).TrimEnd('\') + '\'
    $files = @($pin.files.PSObject.Properties)
    if ($files.Count -eq 0) {
        Write-Warning "    no files pinned for $Id (pin parse problem?)"
        return $false
    }
    foreach ($prop in $files) {
        $name = [string]$prop.Name
        $url  = [string]$prop.Value.url
        $sha  = [string]$prop.Value.sha256
        if (-not (Test-PluginName $name)) {
            Write-Warning "    REFUSED file name '$name' in the $Id pin: not a plain name (letters, digits, . _ -; no '..')"
            return $false
        }
        if (-not (Test-PluginUrl $url)) {
            Write-Warning "    REFUSED URL for $Id/${name}: '$url' is not https://github.com/ or https://raw.githubusercontent.com/"
            return $false
        }
        if ($sha -notmatch '^[0-9a-fA-F]{64}$') {
            Write-Warning "    REFUSED sha256 for $Id/${name}: not 64 hex digits"
            return $false
        }
        $target = [System.IO.Path]::GetFullPath((Join-Path $dir $name))
        if (-not $target.StartsWith($dirFull, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Warning "    REFUSED $Id/${name}: resolves to $target, outside $dirFull"
            return $false
        }
    }
    if ((Test-Path -LiteralPath $dir) -and ((Get-Item -LiteralPath $dir -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Write-Warning "    REFUSED: $dir is a link or junction"
        return $false
    }

    $staging = Join-Path $env:TEMP ("obsidian_plugin_" + [System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    try {
        foreach ($prop in $files) {
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
