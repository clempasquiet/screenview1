<#
.SYNOPSIS
  Removes the ScreenView player scheduled task and (optionally) local state.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "ScreenView Player",
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'."
} else {
    Write-Host "No scheduled task named '$TaskName' found."
}

if ($PurgeData) {
    $appData = Join-Path $env:LOCALAPPDATA "ScreenView"
    if (Test-Path -LiteralPath $appData) {
        Remove-Item -LiteralPath $appData -Recurse -Force
        Write-Host "Removed $appData"
    }
}
