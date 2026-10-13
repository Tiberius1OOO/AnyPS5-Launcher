#Requires -Version 5.1
<#
.SYNOPSIS
  Build AnyPS5Launcher.exe (PyInstaller onedir) and zip under dist/release/
#>
[CmdletBinding()]
param(
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

Write-Host "=== AnyPS5 Launcher release build ===" -ForegroundColor Cyan
Write-Host "Root: $Root"

$py = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = "py" }
if (-not $py) { throw "Python not found on PATH" }

function Invoke-Py {
    param([Parameter(Mandatory = $true)][string[]]$PyArgs)
    if ($py -eq "py") {
        & py -3 @PyArgs
    } else {
        & python @PyArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $($PyArgs -join ' ')" }
}

Write-Host "==> pip install requirements..." -ForegroundColor Cyan
Invoke-Py -PyArgs @("-m", "pip", "install", "-r", "requirements.txt")

$distDir = Join-Path $Root "dist"
$workDir = Join-Path $Root "build"
$releaseDir = Join-Path $distDir "release"
$outName = "AnyPS5-Launcher"
$stage = Join-Path $releaseDir $outName

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$ico = Join-Path $Root "app\static\img\app-icon.ico"
$addData = @(
    "$(Join-Path $Root 'app\templates');templates",
    "$(Join-Path $Root 'app\static');static",
    "$(Join-Path $Root 'app\bootstrap.ps1');.",
    "$(Join-Path $Root 'app\setup_engine.py');.",
    "$(Join-Path $Root 'app\requirements.txt');."
)

$piArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--console",
    "--name", "AnyPS5Launcher",
    "--distpath", (Join-Path $distDir "pyi"),
    "--workpath", $workDir,
    "--specpath", $workDir,
    "--paths", (Join-Path $Root "app")
)
if (Test-Path $ico) {
    $piArgs += @("--icon", $ico)
}
foreach ($d in $addData) {
    $piArgs += @("--add-data", $d)
}
$piArgs += @((Join-Path $Root "app\app.py"))

Write-Host "==> PyInstaller onedir..." -ForegroundColor Cyan
Invoke-Py -PyArgs $piArgs

$built = Join-Path $distDir "pyi\AnyPS5Launcher"
if (-not (Test-Path (Join-Path $built "AnyPS5Launcher.exe"))) {
    throw "PyInstaller output missing: $built\AnyPS5Launcher.exe"
}

Write-Host "==> Stage release folder..." -ForegroundColor Cyan
Copy-Item -Recurse -Force (Join-Path $built "*") $stage
# Release Start script: exe is beside the cmd
@"
@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0AnyPS5Launcher.exe" (
  start "" "%~dp0AnyPS5Launcher.exe"
  exit /b 0
)
echo [ERROR] AnyPS5Launcher.exe missing.
pause
exit /b 1
"@ | Set-Content -Path (Join-Path $stage "Start-AnyPS5.cmd") -Encoding ASCII

Copy-Item -Force (Join-Path $PSScriptRoot "LIESMICH.txt") (Join-Path $stage "LIESMICH.txt")
Copy-Item -Force (Join-Path $Root "README.md") (Join-Path $stage "README.md") -ErrorAction SilentlyContinue

$zip = Join-Path $releaseDir "AnyPS5-Launcher-windows.zip"
if (-not $SkipZip) {
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Write-Host "==> Zip $zip" -ForegroundColor Cyan
    Compress-Archive -Path $stage -DestinationPath $zip -Force
}

Write-Host ""
Write-Host "[OK] Folder: $stage" -ForegroundColor Green
if (-not $SkipZip) { Write-Host "[OK] Zip   : $zip" -ForegroundColor Green }
