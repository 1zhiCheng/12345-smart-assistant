@echo off
setlocal
set "LAUNCHER_ROOT=%~dp0"
start "Wuhu12345Assistant" /min "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -STA -File "%LAUNCHER_ROOT%scripts\desktop_launcher.ps1"
endlocal
