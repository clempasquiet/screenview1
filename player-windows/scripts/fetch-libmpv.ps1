<#
.SYNOPSIS
  Download libmpv-2.dll and place it next to the player.

.DESCRIPTION
  Fetches a pre-built libmpv release from zhongfly/mpv-winbuild (the
  community GitHub Actions build of mpv for Windows), extracts
  libmpv-2.dll, and copies it to the target directory — by default
  the parent of this script (so it ends up next to main.py and the
  .spec file).

  The mpv release archives use the BCJ2 filter (LZMA optimised for
  binaries) which plain ``tar.exe`` / ``py7zr`` cannot decompress,
  so the script bootstraps the official standalone ``7zr.exe`` from
  https://www.7-zip.org/a/7zr.exe (~600 KB, signed by Igor Pavlov)
  on first use and caches it alongside the DLL for future runs.

  Re-run the script periodically to pick up newer mpv builds.

.PARAMETER TargetDir
  Directory to copy libmpv-2.dll into. Defaults to the project root.

.PARAMETER Variant
  "x86_64" (default) or "x86_64-v3" (requires Haswell/Excavator or newer).

.EXAMPLE
  .\scripts\fetch-libmpv.ps1

.EXAMPLE
  .\scripts\fetch-libmpv.ps1 -TargetDir "C:\Program Files\ScreenView"
#>
[CmdletBinding()]
param(
    [string]$TargetDir,
    [ValidateSet("x86_64", "x86_64-v3")]
    [string]$Variant = "x86_64"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is ~10x faster without the progress bar.

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $TargetDir) { $TargetDir = $projectRoot }
$toolsDir = Join-Path $env:LOCALAPPDATA "ScreenView\libmpv"
if (-not (Test-Path -LiteralPath $toolsDir)) {
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
}

Write-Host "ScreenView :: libmpv fetcher"
Write-Host "  Target dir : $TargetDir"
Write-Host "  Variant    : $Variant"
Write-Host "  Tools dir  : $toolsDir"

$headers = @{ "User-Agent" = "screenview-player-installer" }

# ---------------------------------------------------------------------------
# 1. Make sure we have a working 7zr.exe.
# ---------------------------------------------------------------------------
function Get-SevenZr {
    $cached = Join-Path $toolsDir "7zr.exe"
    if ((Test-Path -LiteralPath $cached) -and ((Get-Item $cached).Length -gt 100KB)) {
        return $cached
    }
    foreach ($exe in @("7zr.exe", "7z.exe")) {
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    Write-Host "Bootstrapping 7zr.exe from https://www.7-zip.org/a/7zr.exe …"
    Invoke-WebRequest -Uri "https://www.7-zip.org/a/7zr.exe" -OutFile $cached -Headers $headers -TimeoutSec 60
    $size = (Get-Item $cached).Length
    if ($size -lt 100KB -or $size -gt 10MB) {
        Remove-Item -LiteralPath $cached -ErrorAction SilentlyContinue
        throw "Downloaded 7zr.exe has unexpected size ($size bytes); aborting."
    }
    $magic = [System.IO.File]::ReadAllBytes($cached)[0..1]
    if ($magic[0] -ne 0x4D -or $magic[1] -ne 0x5A) {
        Remove-Item -LiteralPath $cached -ErrorAction SilentlyContinue
        throw "Downloaded 7zr.exe is not a PE executable (magic=$($magic -join ',')); aborting."
    }
    return $cached
}

$sevenZr = Get-SevenZr
Write-Host "Using extractor: $sevenZr"

# ---------------------------------------------------------------------------
# 2. Resolve the latest mpv-dev release asset URL.
# ---------------------------------------------------------------------------
$apiUrl = "https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest"
Write-Host "Querying $apiUrl …"
$release = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 30

$pattern = "mpv-dev-$Variant-*.7z"
$asset = $release.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
if (-not $asset) {
    throw "No asset matching '$pattern' in release $($release.tag_name)."
}
Write-Host "Selected asset: $($asset.name) ($([math]::Round($asset.size/1MB, 1)) MB)"

# ---------------------------------------------------------------------------
# 3. Download + extract + copy.
# ---------------------------------------------------------------------------
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("screenview-libmpv-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
try {
    $archive = Join-Path $tempRoot $asset.name
    Write-Host "Downloading …"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -Headers $headers -TimeoutSec 300

    $extractDir = Join-Path $tempRoot "extracted"
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null

    Write-Host "Extracting with $sevenZr …"
    & $sevenZr x "-o$extractDir" "-y" $archive | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "7zr.exe exited with code $LASTEXITCODE"
    }

    $dll = Get-ChildItem -Path $extractDir -Filter "libmpv-2.dll" -Recurse -File | Select-Object -First 1
    if (-not $dll) {
        $dll = Get-ChildItem -Path $extractDir -Filter "mpv-2.dll" -Recurse -File | Select-Object -First 1
    }
    if (-not $dll) {
        throw "Archive extracted but contains no libmpv-2.dll / mpv-2.dll."
    }

    if (-not (Test-Path -LiteralPath $TargetDir)) {
        New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    }
    $destination = Join-Path $TargetDir $dll.Name
    Copy-Item -LiteralPath $dll.FullName -Destination $destination -Force

    $sizeKB = [math]::Round((Get-Item $destination).Length / 1KB, 0)
    Write-Host ""
    Write-Host "Done!" -ForegroundColor Green
    Write-Host "  $destination  ($sizeKB KB)"
    Write-Host "  mpv build: $($release.tag_name)"
    Write-Host ""
    Write-Host "You can now run 'python main.py' (or re-launch the kiosk task)."
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
