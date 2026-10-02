<#
.SYNOPSIS  Make the Morning Dashboard's buttons work (or remove them again).
.DESCRIPTION
  The dashboard's three buttons (Pull meetings, Refresh dashboard, Refresh RAG
  index) are obsidian-dashboard:// links. This registers that URL scheme for
  the current user -- a registry key under HKCU, no elevation -- pointing at
  windows\dashboard_action.py run by the venv's pythonw.exe, which accepts
  only those three actions. The buttons appear on the next dashboard render.
  This is the Windows analog of macOS installer component 57-dashboard-actions.

  Opt-in, as on macOS: a registered scheme can be fired by any web page (the
  browser asks first), so install.ps1 asks, or follows the profile's
  PROFILE_DASHBOARD_ACTIONS. Run this directly to opt in later.
  Idempotent: re-running rewrites the key.
.PARAMETER Remove  Remove the handler instead of installing it.
#>
[CmdletBinding()] param([switch]$Remove)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"

if ($Remove) { Unregister-DashboardActions; return }

Register-DashboardActions -ScriptsDir (Get-ScriptsDir)
Write-Host '  The buttons appear on the next dashboard render (Refresh, or tomorrow 07:00).'
Write-Host '  Each click is logged to %LOCALAPPDATA%\obsidian-logs\dashboard-actions.log.'
