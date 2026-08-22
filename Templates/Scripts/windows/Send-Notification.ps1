<#
.SYNOPSIS  Windows replacement for the scripts' macOS `osascript` notifications.
.EXAMPLE   .\Send-Notification.ps1 -Title "Tagger" -Message "Tagged 4 clippings"
Falls back to logging only if BurntToast is not installed.
#>
param(
    [Parameter(Mandatory)] [string]$Title,
    [Parameter(Mandatory)] [string]$Message
)
$logDir = Join-Path $env:LOCALAPPDATA 'obsidian-automation\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path (Join-Path $logDir 'notifications.log') -Value "[$stamp] $Title — $Message"

if (Get-Module -ListAvailable -Name BurntToast) {
    Import-Module BurntToast -ErrorAction SilentlyContinue
    New-BurntToastNotification -Text $Title, $Message | Out-Null
}
# else: log-only (install BurntToast for real toasts).
