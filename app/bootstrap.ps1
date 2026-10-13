#Requires -Version 5.1
<#
.SYNOPSIS
  AnyPS5 Bootstrap â€” prepares the repo + launcher only.
  Full toolchain (CMake/Ninja/WinLibs/submodules) is installed from the Launcher Setup tab.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File launcher\bootstrap.ps1 -RepoRoot "D:\AnyPS5" -PackageRoot "D:\Installer-Pack" -LauncherOnly
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$PackageRoot = "",
    [switch]$Elevated,
    [switch]$NoLaunch,
    [switch]$SkipElevationPrompt,
    [switch]$LauncherOnly
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

# Default for Installer-Pack: launcher-only (deps come from the web UI Setup button)
if (-not $PSBoundParameters.ContainsKey("LauncherOnly")) {
    $LauncherOnly = $true
}

function Write-Banner([string]$Text) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host " $Text" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
}

function Test-IsAdmin {
    $p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-HasGit {
    return [bool](Get-Command git -ErrorAction SilentlyContinue)
}

function Test-HasPython {
    return [bool](Get-Command python -ErrorAction SilentlyContinue) -or
           [bool](Get-Command py -ErrorAction SilentlyContinue)
}

# Resolve repo root
if (-not $RepoRoot) {
    if ($PSScriptRoot) {
        $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    } else {
        $RepoRoot = Join-Path $env:USERPROFILE "AnyPS5"
    }
}

Write-Banner "AnyPS5 Launcher Setup"
Write-Host "Zielordner : $RepoRoot"
Write-Host "Admin      : $(Test-IsAdmin)"
Write-Host "Modus      : $(if ($LauncherOnly) { 'Nur Launcher (Deps spaeter im Browser)' } else { 'Voll-Install inkl. Toolchain' })"
Write-Host "GitHub     : https://github.com/boykopovar/AnyPS5"
Write-Host "Kein GitHub-Account noetig (oeffentlicher Clone)."
Write-Host ""

# Elevate only when Git/Python are missing (needed for clone + Flask launcher)
$needTools = -not (Test-HasGit) -or -not (Test-HasPython)
if ($needTools -and -not (Test-IsAdmin) -and -not $SkipElevationPrompt -and -not $Elevated) {
    Write-Host "Git und/oder Python fehlen. Installation braucht oft Admin (UAC)." -ForegroundColor Yellow
    $ans = Read-Host "Jetzt als Administrator neu starten? [J/n]"
    if ($ans -notmatch '^[Nn]') {
        $self = if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }
        $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -RepoRoot `"$RepoRoot`" -Elevated"
        if ($PackageRoot) { $arg += " -PackageRoot `"$PackageRoot`"" }
        if ($NoLaunch) { $arg += " -NoLaunch" }
        if ($LauncherOnly) { $arg += " -LauncherOnly" }
        try {
            Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arg -Wait
            if (-not $NoLaunch) {
                $startCmd = Join-Path $RepoRoot "Start-AnyPS5.cmd"
                if (Test-Path $startCmd) {
                    Start-Process -FilePath $startCmd
                }
            }
            exit 0
        } catch {
            Write-Host "[ABBRUCH] UAC abgelehnt oder fehlgeschlagen: $_" -ForegroundColor Red
            Write-Host "Versuche ohne Admin weiter (kann scheitern)..." -ForegroundColor Yellow
        }
    }
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "[!] winget nicht gefunden. Windows 10/11 App-Installer empfohlen." -ForegroundColor Yellow
}

function Install-PythonBootstrap {
    if (Test-HasPython) { return $true }
    Write-Host "==> Python fehlt â€” Install via winget..." -ForegroundColor Cyan
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements --disable-interactivity
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    }
    return (Test-HasPython)
}

function Install-GitBootstrap {
    if (Test-HasGit) { return $true }
    Write-Host "==> Git fehlt â€” Install via winget..." -ForegroundColor Cyan
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements --disable-interactivity
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    }
    return (Test-HasGit)
}

Write-Banner "Schritt 1/3 â€” Git & Python (fuer Launcher)"
if (-not (Install-GitBootstrap)) {
    Write-Host "[ABBRUCH] Git konnte nicht installiert werden." -ForegroundColor Red
    Write-Host "Bitte manuell: https://git-scm.com/download/win" -ForegroundColor Yellow
    if (-not $Elevated) { pause }
    exit 1
}
Write-Host "[OK] Git: $(git --version)" -ForegroundColor Green

if (-not (Install-PythonBootstrap)) {
    Write-Host "[ABBRUCH] Python konnte nicht installiert werden." -ForegroundColor Red
    Write-Host "Bitte manuell: https://www.python.org/downloads/" -ForegroundColor Yellow
    if (-not $Elevated) { pause }
    exit 1
}
Write-Host "[OK] Python vorhanden" -ForegroundColor Green

Write-Banner "Schritt 2/3 â€” Repository"
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    $parent = Split-Path $RepoRoot -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    if ((Test-Path $RepoRoot) -and (Get-ChildItem $RepoRoot -Force | Measure-Object).Count -gt 0) {
        if (Test-Path (Join-Path $RepoRoot "launcher\setup_engine.py")) {
            Write-Host "Ordner existiert mit Launcher â€” git init + fetch..." -ForegroundColor Yellow
            Push-Location $RepoRoot
            git init 2>$null
            git remote remove origin 2>$null
            git remote add origin https://github.com/boykopovar/AnyPS5.git
            git fetch origin
            git checkout -B main origin/main
            Pop-Location
        } else {
            Write-Host "[ABBRUCH] $RepoRoot ist nicht leer und kein AnyPS5-Ordner." -ForegroundColor Red
            if (-not $Elevated) { pause }
            exit 1
        }
    } else {
        Write-Host "Clone https://github.com/boykopovar/AnyPS5.git ..." -ForegroundColor Cyan
        git clone --recursive https://github.com/boykopovar/AnyPS5.git "$RepoRoot"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ABBRUCH] git clone fehlgeschlagen (Exit $LASTEXITCODE)" -ForegroundColor Red
            if (-not $Elevated) { pause }
            exit 1
        }
    }
} else {
    Write-Host "[OK] Repo bereits vorhanden" -ForegroundColor Green
}

function Copy-PackageOverlay {
    param([string]$Pkg, [string]$Dest)
    if (-not $Pkg -or -not (Test-Path $Pkg)) { return }
    Write-Banner "Launcher aus Setup-Paket uebernehmen"
    Write-Host "Paket: $Pkg"
    Write-Host "Ziel : $Dest"
    $srcLauncher = Join-Path $Pkg "launcher"
    if (Test-Path $srcLauncher) {
        $dstLauncher = Join-Path $Dest "launcher"
        if (Test-Path $dstLauncher) { Remove-Item -Recurse -Force $dstLauncher }
        Copy-Item -Recurse -Force $srcLauncher $dstLauncher
        Write-Host "[OK] launcher\ kopiert" -ForegroundColor Green
    } else {
        Write-Host "[ABBRUCH] Paket hat keinen launcher\-Ordner" -ForegroundColor Red
        exit 1
    }
    foreach ($f in @("setup-deps.ps1", "INSTALL.md")) {
        $from = Join-Path $Pkg $f
        if (Test-Path $from) {
            Copy-Item -Force $from (Join-Path $Dest $f)
            Write-Host "[OK] $f" -ForegroundColor Green
        }
    }
    $startDest = Join-Path $Dest "Start-AnyPS5.cmd"
    $startPkgFull = Join-Path $Pkg "launcher\_Start-AnyPS5.full.cmd"
    if (Test-Path $startPkgFull) {
        Copy-Item -Force $startPkgFull $startDest
        Write-Host "[OK] Start-AnyPS5.cmd (voll)" -ForegroundColor Green
    } elseif (-not (Test-Path $startDest)) {
        @"
@echo off
cd /d "%~dp0"
title AnyPS5 Launcher
where python >nul 2>&1 && (
  python -m pip install -q -r "%~dp0launcher\requirements.txt"
  python "%~dp0launcher\app.py"
  goto :eof
)
py -3 -m pip install -q -r "%~dp0launcher\requirements.txt"
py -3 "%~dp0launcher\app.py"
"@ | Set-Content -Path $startDest -Encoding ASCII
    }
    Write-Host "[OK] Overlay fertig" -ForegroundColor Green
}

if ($PackageRoot) {
    $PackageRoot = $PackageRoot.TrimEnd('\', '/')
    Copy-PackageOverlay -Pkg $PackageRoot -Dest $RepoRoot
} elseif ($PSScriptRoot) {
    $maybePkg = Split-Path $PSScriptRoot -Parent
    $leaf = Split-Path $maybePkg -Leaf
    if ($leaf -eq "Installer-Pack" -or $leaf -eq "Freund-Paket" -or
        (Test-Path (Join-Path $maybePkg "LIESMICH.txt"))) {
        Copy-PackageOverlay -Pkg $maybePkg -Dest $RepoRoot
    }
}

$engine = Join-Path $RepoRoot "launcher\setup_engine.py"
if (-not (Test-Path $engine)) {
    Write-Host "[ABBRUCH] setup_engine.py fehlt nach Clone: $engine" -ForegroundColor Red
    if (-not $Elevated) { pause }
    exit 1
}

Write-Banner "Schritt 3/3 â€” Flask fuer den Launcher"
$req = Join-Path $RepoRoot "launcher\requirements.txt"
$pyCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $pyCmd = "python" }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $pyCmd = "py" }

if (-not $pyCmd) {
    Write-Host "[ABBRUCH] Python nach Install nicht im PATH." -ForegroundColor Red
    if (-not $Elevated) { pause }
    exit 1
}

if (Test-Path $req) {
    Write-Host "pip install -r launcher\requirements.txt ..." -ForegroundColor Cyan
    if ($pyCmd -eq "py") {
        & py -3 -m pip install -q -r $req
    } else {
        & python -m pip install -q -r $req
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ABBRUCH] pip install fehlgeschlagen (Exit $LASTEXITCODE)." -ForegroundColor Red
        if (-not $Elevated) { pause }
        exit $LASTEXITCODE
    }
    Write-Host "[OK] Launcher-Python-Deps" -ForegroundColor Green
}

if (-not $LauncherOnly) {
    Write-Banner "Voll-Install â€” CMake, Ninja, WinLibs, Submodules"
    if ($pyCmd -eq "py") {
        & py -3 "$engine" "$RepoRoot"
    } else {
        & python "$engine" "$RepoRoot"
    }
    $engineExit = $LASTEXITCODE
    if ($engineExit -ne 0) {
        Write-Host ""
        Write-Host "[ABBRUCH] Setup-Engine meldete Fehler (Exit $engineExit)." -ForegroundColor Red
        Write-Host "Tipp: Im Launcher Tab Setup â†’ Install / repair everything (ggf. als Admin)." -ForegroundColor Yellow
        if (-not $Elevated) { pause }
        exit $engineExit
    }
}

Write-Banner "Launcher ist startklar"
Write-Host "Repo: $RepoRoot" -ForegroundColor Green
Write-Host ""
Write-Host "Als Naechstes im Browser-Launcher:" -ForegroundColor Cyan
Write-Host "  1) Tab Setup  â†’  Install / repair everything" -ForegroundColor Cyan
Write-Host "  2) Tab Build  â†’  Full Rebuild" -ForegroundColor Cyan
Write-Host "  3) Tab Convert â†’ Spiel umwandeln" -ForegroundColor Cyan

if (-not $NoLaunch) {
    $start = Join-Path $RepoRoot "Start-AnyPS5.cmd"
    if (Test-Path $start) {
        Write-Host ""
        Write-Host "Starte Launcher..." -ForegroundColor Cyan
        Start-Process -FilePath $start
    }
}

if ($Elevated) {
    Write-Host ""
    Write-Host "Admin-Fenster kann geschlossen werden." -ForegroundColor DarkGray
    Start-Sleep -Seconds 3
}
