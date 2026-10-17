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

# Preserve user data across rebuilds (AnyPS5 clone, configs, logs)
$preserveNames = @(
    "AnyPS5",
    "logs",
    "launcher-config.json",
    "library-cache.json",
    "setup-state.json",
    "input-profile.json",
    "anyps5-input.ini"
)
$preserveStaging = Join-Path $releaseDir ("_preserve-" + (Get-Date -Format yyyyMMddHHmmss))
if (Test-Path $stage) {
    New-Item -ItemType Directory -Force -Path $preserveStaging | Out-Null
    foreach ($name in $preserveNames) {
        $src = Join-Path $stage $name
        if (Test-Path $src) {
            Write-Host "Preserving $name" -ForegroundColor DarkCyan
            Move-Item -LiteralPath $src -Destination (Join-Path $preserveStaging $name) -Force
        }
    }
    try {
        Remove-Item -Recurse -Force $stage -ErrorAction Stop
    } catch {
        $bak = "$stage.old-$(Get-Date -Format yyyyMMddHHmmss)"
        Write-Host "Stage locked; moving aside to $bak" -ForegroundColor Yellow
        Move-Item -LiteralPath $stage -Destination $bak -Force
    }
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$ico = Join-Path $Root "app\static\img\app-icon.ico"
$versionPy = Join-Path $Root "app\version.py"
$launcherVersion = "0.0.0"
if (Test-Path $versionPy) {
    $m = Select-String -Path $versionPy -Pattern 'LAUNCHER_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $launcherVersion = $m.Matches[0].Groups[1].Value }
}
Write-Host "Launcher version: $launcherVersion" -ForegroundColor Cyan

$addData = @(
    "$(Join-Path $Root 'app\templates');templates",
    "$(Join-Path $Root 'app\static');static",
    "$(Join-Path $Root 'app\bootstrap.ps1');.",
    "$(Join-Path $Root 'app\setup_engine.py');.",
    "$(Join-Path $Root 'app\library.py');.",
    "$(Join-Path $Root 'app\game_session.py');.",
    "$(Join-Path $Root 'app\applog.py');.",
    "$(Join-Path $Root 'app\version.py');.",
    "$(Join-Path $Root 'app\self_update.py');.",
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
# Drop any accidental runtime state copied from the PyInstaller tree
@(
    "launcher-config.json",
    "setup-state.json",
    "input-profile.json",
    "anyps5-input.ini",
    "AnyPS5",
    "logs"
) | ForEach-Object {
    $p = Join-Path $stage $_
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
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
Set-Content -Path (Join-Path $stage "VERSION.txt") -Value $launcherVersion -Encoding ASCII

# Zip the clean launcher first (without restored AnyPS5 / logs — keeps zip small)
$zip = Join-Path $releaseDir "AnyPS5-Launcher-windows.zip"
if (-not $SkipZip) {
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Write-Host "==> Zip $zip" -ForegroundColor Cyan
    Compress-Archive -Path $stage -DestinationPath $zip -Force
}

# Restore preserved user data after zip (local rebuild convenience)
if (Test-Path $preserveStaging) {
    Write-Host "==> Restoring preserved user data..." -ForegroundColor Cyan
    Get-ChildItem -LiteralPath $preserveStaging | ForEach-Object {
        $dest = Join-Path $stage $_.Name
        if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
        Move-Item -LiteralPath $_.FullName -Destination $dest -Force
        Write-Host "Restored $($_.Name)" -ForegroundColor DarkCyan
    }
    Remove-Item -Recurse -Force $preserveStaging -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "[OK] Folder: $stage" -ForegroundColor Green
if (-not $SkipZip) { Write-Host "[OK] Zip   : $zip" -ForegroundColor Green }
