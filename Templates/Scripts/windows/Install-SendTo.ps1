<#
.SYNOPSIS  Install (or remove) a "Markitdown to Obsidian" Send To shortcut.
.DESCRIPTION
  Adds a shortcut to the user's Send To menu so you can right-click any file ->
  Send to -> "Markitdown to Obsidian" to convert it to Markdown in the vault via
  markitdown_convert.py. This is the Windows analog of the macOS .app dropper.
  Idempotent: re-running overwrites the shortcut.
.PARAMETER Remove  Remove the shortcut instead of installing it.
#>
[CmdletBinding()] param([switch]$Remove)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

$sendTo = Join-Path $env:APPDATA 'Microsoft\Windows\SendTo'
$lnk    = Join-Path $sendTo 'Markitdown to Obsidian.lnk'

if ($Remove) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed: $lnk" }
    else { Write-Host 'no Send To shortcut to remove' }
    return
}

$py     = Get-VenvPython
$script = Join-Path (Get-ScriptsDir) 'markitdown_convert.py'
if (-not (Test-Path $script)) { throw "markitdown_convert.py not found at $script" }

New-Item -ItemType Directory -Force -Path $sendTo | Out-Null
$wsh = New-Object -ComObject WScript.Shell
$sc  = $wsh.CreateShortcut($lnk)
# python.exe (not pythonw) so the brief console shows the "->" result.
$sc.TargetPath       = $py
$sc.Arguments        = ('"{0}"' -f $script)   # Send To appends the file path(s)
$sc.WorkingDirectory = (Get-ScriptsDir)
$sc.IconLocation     = "$py,0"
$sc.Description       = 'Convert this file to Markdown in the Obsidian vault'
$sc.Save()

Write-Host "installed Send To shortcut: $lnk"
Write-Host ("  runs: {0} `"{1}`" <file>" -f $py, $script)
Write-Host '  Use it: right-click a file -> Send to -> "Markitdown to Obsidian".'
