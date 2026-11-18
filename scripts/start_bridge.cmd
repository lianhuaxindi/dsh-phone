@echo off
rem Double-click launcher for DSH Phone Bridge (LAN mode).
rem ASCII-only on purpose: cmd.exe reads .cmd in the OEM codepage.
chcp 65001 >nul
title DSH Phone Bridge
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_bridge.ps1" %*
set RC=%ERRORLEVEL%
echo.
echo Bridge process exited with code %RC%.
echo Press any key to close this window...
pause >nul
exit /b %RC%
