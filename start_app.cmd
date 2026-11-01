@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "scripts\start_app.ps1" %*
exit /b %errorlevel%
