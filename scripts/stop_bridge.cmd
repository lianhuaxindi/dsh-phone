@echo off
rem Double-click launcher: stop any running DSH Phone Bridge process.
rem ASCII-only on purpose: cmd.exe reads .cmd in the OEM codepage.
chcp 65001 >nul
title DSH Phone Bridge - stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_bridge.ps1" -Stop %*
echo.
echo Press any key to close this window...
pause >nul
