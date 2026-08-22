<# .SYNOPSIS  Remove all Obsidian scheduled tasks registered by Register-Tasks.ps1. #>
[CmdletBinding(SupportsShouldProcess)] param()
Get-ScheduledTask -TaskPath '\Obsidian\*' -ErrorAction SilentlyContinue | ForEach-Object {
    if ($PSCmdlet.ShouldProcess($_.TaskName, 'Unregister')) {
        Unregister-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath -Confirm:$false
        Write-Host "  removed: $($_.TaskName)"
    }
}
