<#
.SYNOPSIS  Windows replacement for the scripts' macOS `osascript` notifications.
.EXAMPLE   .\Send-Notification.ps1 -Title "Tagger" -Message "Tagged 4 clippings"
Every notification is appended to notifications.log first, then shown as a
toast: through BurntToast if that module is installed, otherwise through the
Windows notification API directly.

The direct path matters because these toasts carry the security alerts
(integrity drift, plugin tampering) and the Claude sign-in expiry. install.ps1
never installed BurntToast, so on a standard install every one of them used to
go to the log file and nowhere else. The direct path needs no module: Windows
PowerShell 5.1 (which security_common.py launches by absolute path) can load
the WinRT notification types itself. A failure to show the toast is logged,
never thrown -- the caller treats notification as best effort.
#>
param(
    [Parameter(Mandatory)] [string]$Title,
    [Parameter(Mandatory)] [string]$Message
)
$logDir = Join-Path $env:LOCALAPPDATA 'obsidian-automation\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'notifications.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$stamp] $Title -- $Message"

# The AppUserModelID of Windows PowerShell itself. It is registered on every
# Windows install, which is what lets an unpackaged script raise a toast
# without creating a Start-menu shortcut of its own (BurntToast uses the same).
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Show-NativeToast([string]$Title, [string]$Message) {
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
    # The text is data, and it can come from a note or a log line: escaped,
    # it cannot add elements (an image fetched from a URL, an action button)
    # to the toast it is shown in.
    $t = [System.Security.SecurityElement]::Escape($Title)
    $m = [System.Security.SecurityElement]::Escape($Message)
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml("<toast><visual><binding template=`"ToastGeneric`"><text>$t</text><text>$m</text></binding></visual></toast>")
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
}

try {
    if (Get-Module -ListAvailable -Name BurntToast) {
        Import-Module BurntToast -ErrorAction Stop
        New-BurntToastNotification -Text $Title, $Message -ErrorAction Stop | Out-Null
    } else {
        Show-NativeToast $Title $Message
    }
} catch {
    # Recorded beside the message it failed to show, so a missing toast is
    # distinguishable from one that was never sent (e.g. notifications turned
    # off by policy).
    Add-Content -Path $log -Value ("[$stamp]   toast not shown: " + $_.Exception.Message)
}
