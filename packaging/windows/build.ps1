<#
.SYNOPSIS
  Build the Sentinel one-folder program for Windows (dist\Sentinel\Sentinel.exe).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -FfmpegDir C:\tools\ffmpeg\bin

.NOTES
  * Creates/updates the build virtualenv .venv-build with hash-checked, pinned packages from
    requirements\build.txt (PyInstaller and dependencies). Nothing is installed system-wide.
  * FFmpeg is NOT downloaded: -FfmpegDir must point at the folder with ffmpeg.exe and ffprobe.exe
    to distribute. It must be an LGPL build (owner decision); GPL builds are refused unless
    -AllowGplInternalUse is given, nonfree builds always (see docs\08_PACKAGING.md section 8).
  * -Python selects the base interpreter (default: the "py" launcher's Python 3.14).
#>
param(
    [Parameter(Mandatory = $true)][string]$FfmpegDir,
    [string]$Python = "",
    [switch]$AllowDirty,
    [switch]$AllowGplInternalUse,   # bundle a GPL FFmpeg for a build that never leaves the organisation
    [switch]$InspectOnly,           # only check the FFmpeg folder (licence policy, required components)
    [string]$SourceOfferContact = ""  # who honours the LGPL source offer (licences\SOURCE-OFFER.txt)
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venv = Join-Path $root ".venv-build"
$vpy = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $vpy)) {
    if ($Python) { & $Python -m venv $venv } else { & py -3.14 -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "could not create the build virtualenv" }
}
& $vpy -m pip install --disable-pip-version-check --require-hashes --no-deps -r (Join-Path $root "requirements\build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install of requirements\build.txt failed" }

$argsList = @((Join-Path $root "packaging\build.py"), "--ffmpeg-dir", $FfmpegDir)
if ($AllowDirty) { $argsList += "--allow-dirty" }
if ($AllowGplInternalUse) { $argsList += "--allow-gpl-internal-use" }
if ($InspectOnly) { $argsList += "--inspect-only" }
if ($SourceOfferContact) { $argsList += @("--source-offer-contact", $SourceOfferContact) }
& $vpy @argsList
exit $LASTEXITCODE
