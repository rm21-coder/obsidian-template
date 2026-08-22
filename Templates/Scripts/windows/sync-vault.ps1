<#
.SYNOPSIS  Port of sync-vault.sh — wrapper for obsidian-rag-sync.py.
Loads OPEN_WEBUI_API_KEY / OBSIDIAN_COLLECTION_ID from the .env, sets sensible
defaults, then runs the indexer with the venv python.
#>
[CmdletBinding()] param([Parameter(ValueFromRemainingArguments)] $Rest)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

Import-DotEnv
if (-not $env:OBSIDIAN_VAULT)  { $env:OBSIDIAN_VAULT  = (Get-VaultRoot) }
if (-not $env:OPEN_WEBUI_URL)  { $env:OPEN_WEBUI_URL  = 'http://localhost:3000' }

$py     = Get-VenvPython
$script = Join-Path (Get-ScriptsDir) 'obsidian-rag-sync.py'
& $py $script @Rest
exit $LASTEXITCODE
