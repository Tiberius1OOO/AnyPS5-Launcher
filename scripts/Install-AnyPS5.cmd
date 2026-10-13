@echo off
setlocal EnableDelayedExpansion
title AnyPS5 Launcher — Install
cd /d "%~dp0.."

echo.
echo ========================================
echo   AnyPS5 Launcher — first run
echo ========================================
echo.
echo This starts the launcher UI. Inside Setup you can
echo install Git, Python, CMake, Ninja, WinLibs, and
echo clone AnyPS5 from GitHub.
echo.
echo Why administrator access may be needed later:
echo   - Git / Python / CMake often install into Program Files
echo   - WinLibs GCC is extracted under C:\winlibs
echo   - winget package installs usually need UAC
echo.
echo The launcher itself does NOT need admin to open.
echo You will get a clear dialog before any UAC prompt.
echo.

set "DEFAULT=%USERPROFILE%\AnyPS5"
echo AnyPS5 will be cloned to:
echo   %DEFAULT%
echo (changeable later via launcher-config.json anyps5Root)
echo.
pause

call "%~dp0Start-AnyPS5.cmd"
endlocal
