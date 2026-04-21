<#
.SYNOPSIS
  Build a single-file ScreenViewPlayer.exe via PyInstaller.

.DESCRIPTION
  Creates / reuses a venv, installs deps and runs PyInstaller. Place
  ``libmpv-2.dll`` (or ``mpv-2.dll``) next to ``ScreenViewPlayer.spec``
  before invoking this script so video playback works out of the box.

.EXAMPLE
  .\scripts\build.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$VenvDir = ".venv"
)

$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)

try {
    if (-not (Test-Path $VenvDir)) {
        & $PythonExe -m venv $VenvDir
    }

    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip | Out-Null
    & $venvPython -m pip install -r requirements.txt
    & $venvPython -m pip install pyinstaller

    & $venvPython -m PyInstaller ScreenViewPlayer.spec --clean --noconfirm
    Write-Host "Build complete. Output in .\dist\ScreenViewPlayer.exe"
}
finally {
    Pop-Location
}
