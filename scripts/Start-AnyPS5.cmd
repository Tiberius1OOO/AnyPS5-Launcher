@echo off
setlocal EnableDelayedExpansion
title AnyPS5 Launcher
cd /d "%~dp0.."

REM Prefer frozen release next to scripts (dev) or package root (release zip)
set "EXE="
if exist "%~dp0..\AnyPS5Launcher.exe" set "EXE=%~dp0..\AnyPS5Launcher.exe"
if exist "%~dp0AnyPS5Launcher.exe" set "EXE=%~dp0AnyPS5Launcher.exe"
if exist "%~dp0..\dist\release\AnyPS5-Launcher\AnyPS5Launcher.exe" set "EXE=%~dp0..\dist\release\AnyPS5-Launcher\AnyPS5Launcher.exe"

if defined EXE (
  echo Starting AnyPS5Launcher.exe ...
  start "" "!EXE!"
  exit /b 0
)

where python >nul 2>&1 && (
  echo Starting from source (python)...
  python -m pip install -q -r "%~dp0..\requirements.txt"
  python "%~dp0..\app\app.py"
  exit /b !ERRORLEVEL!
)

where py >nul 2>&1 && (
  echo Starting from source (py -3)...
  py -3 -m pip install -q -r "%~dp0..\requirements.txt"
  py -3 "%~dp0..\app\app.py"
  exit /b !ERRORLEVEL!
)

echo [ERROR] No AnyPS5Launcher.exe and no Python found.
pause
exit /b 1
