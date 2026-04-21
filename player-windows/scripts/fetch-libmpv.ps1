<#
.SYNOPSIS
  Download libmpv-2.dll and place it next to the player.

.DESCRIPTION
  Fetches a pre-built libmpv release from zhongfly/mpv-winbuild (the
  community GitHub Actions build of mpv for Windows), extracts
  libmpv-2.dll, and copies it to the target directory — by default
  the parent of this script (so it ends up next to main.py and the
  .spec file).

  The script uses `tar.exe` (bsdtar, ships with Windows 10 1803+ and
  handles .7z via libarchive) as the primary extractor and falls back
  to 7z.exe or 7zr.exe if they are on PATH.

  Re-run it periodically to pick up newer mpv builds.

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

Write-Host "ScreenView :: libmpv fetcher"
Write-Host "  Target dir : $TargetDir"
Write-Host "  Variant    : $Variant"

# ---------------------------------------------------------------------------
# 1. Resolve the latest release asset URL from the GitHub API.
# ---------------------------------------------------------------------------
$apiUrl = "https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest"
Write-Host "Querying $apiUrl …"
$headers = @{ "User-Agent" = "screenview-player-installer" }
try {
    $release = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 30
} catch {
    throw "Failed to query GitHub: $($_.Exception.Message)"
}

$pattern = "mpv-dev-$Variant-*.7z"
$asset = $release.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
if (-not $asset) {
    throw "No asset matching '$pattern' in release $($release.tag_name)."
}
Write-Host "Selected asset: $($asset.name) ($([math]::Round($asset.size/1MB, 1)) MB)"

# ---------------------------------------------------------------------------
# 2. Download to a temp location.
# ---------------------------------------------------------------------------
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("screenview-libmpv-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
try {
    $archive = Join-Path $tempRoot $asset.name
    Write-Host "Downloading …"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -Headers $headers -TimeoutSec 300
    Write-Host "Downloaded $archive"

    # -----------------------------------------------------------------------
    # 3. Extract the archive.
    # -----------------------------------------------------------------------
    $extractDir = Join-Path $tempRoot "extracted"
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null

    $extractors = @(
        @{ Exe = (Get-Command "7z.exe" -ErrorAction SilentlyContinue);  Args = @("x", "-y", "-o$extractDir", $archive) },
        @{ Exe = (Get-Command "7zr.exe" -ErrorAction SilentlyContinue); Args = @("x", "-y", "-o$extractDir", $archive) },
        @{ Exe = (Get-Command "tar.exe" -ErrorAction SilentlyContinue); Args = @("-xf", $archive, "-C", $extractDir) }
    )

    $extracted = $false
    foreach ($ex in $extractors) {
        if (-not $ex.Exe) { continue }
        Write-Host "Extracting with $($ex.Exe.Source) …"
        try {
            & $ex.Exe.Source @($ex.Args) | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $extracted = $true
                break
            } else {
                Write-Warning "$($ex.Exe.Name) exited with code $LASTEXITCODE; trying next extractor."
            }
        } catch {
            Write-Warning "Extraction with $($ex.Exe.Name) failed: $($_.Exception.Message)"
        }
    }

    if (-not $extracted) {
        throw @"
Could not extract the .7z archive with any external tool.
Install 7-Zip from https://www.7-zip.org/ (recommended) and re-run this
script. Alternatively, the Python player itself ships a pure-Python
extractor (py7zr) that avoids this problem entirely — run
`python main.py` once with an internet connection to have the player
self-provision libmpv-2.dll under %LOCALAPPDATA%\ScreenView\libmpv\.
"@
    }

    # -----------------------------------------------------------------------
    # 4. Copy libmpv-2.dll into place.
    # -----------------------------------------------------------------------
    $dll = Get-ChildItem -Path $extractDir -Filter "libmpv-2.dll" -Recurse -File | Select-Object -First 1
    if (-not $dll) {
        # Some archives ship it as mpv-2.dll; python-mpv accepts either.
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
