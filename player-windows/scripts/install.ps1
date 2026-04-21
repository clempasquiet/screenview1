<#
.SYNOPSIS
  Installer / updater for the ScreenView Windows player.

.DESCRIPTION
  Registers the Task Scheduler entry that starts the player at user logon
  and restarts it on failure. Equivalent to enabling the systemd unit on
  Linux. Run from an elevated PowerShell prompt:

      Set-ExecutionPolicy -Scope Process Bypass
      .\install.ps1 -InstallDir "C:\Program Files\ScreenView"

.PARAMETER InstallDir
  Directory that contains ScreenViewPlayer.exe.

.PARAMETER ServerUrl
  Optional. When provided, seeds config.json with this base URL before the
  player launches for the first time.

.PARAMETER TaskName
  Task Scheduler task name. Defaults to "ScreenView Player".

.PARAMETER User
  Optional. Run the task as this user. Defaults to the installing user.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [string]$ServerUrl,
    [string]$TaskName = "ScreenView Player",
    [string]$User
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $InstallDir)) {
    throw "InstallDir '$InstallDir' does not exist."
}

$exe = Join-Path $InstallDir "ScreenViewPlayer.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "ScreenViewPlayer.exe not found at '$exe'."
}

if ($ServerUrl) {
    $appData = Join-Path $env:LOCALAPPDATA "ScreenView"
    if (-not (Test-Path -LiteralPath $appData)) {
        New-Item -ItemType Directory -Path $appData -Force | Out-Null
    }
    $configPath = Join-Path $appData "config.json"
    $config = if (Test-Path -LiteralPath $configPath) {
        Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    } else {
        [pscustomobject]@{
            server_url               = $ServerUrl
            device_id                = $null
            device_name              = $null
            reconnect_delay_seconds  = 5
            sync_poll_interval_seconds = 60
            cache_dir                = $null
            fullscreen               = $true
            show_cursor              = $false
            prevent_display_sleep    = $true
            libmpv_dir               = $null
        }
    }
    $config.server_url = $ServerUrl
    $config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $configPath -Encoding UTF8
    Write-Host "Seeded $configPath with server_url = $ServerUrl"
}

$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $InstallDir
$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn
    New-ScheduledTaskTrigger -AtStartup
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 9999

$principalParams = @{
    LogonType = "Interactive"
    RunLevel  = "Highest"
}
if ($User) { $principalParams.UserId = $User } else { $principalParams.UserId = "$env:USERDOMAIN\$env:USERNAME" }
$principal = New-ScheduledTaskPrincipal @principalParams

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'. It will start at next logon (or now via Start-ScheduledTask)."
