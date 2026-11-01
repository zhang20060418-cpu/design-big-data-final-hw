@echo off
setlocal
cd /d "%~dp0"

if not exist "backend\.venv\Scripts\python.exe" goto setup
if not exist "frontend\dist\index.html" goto setup

start "" powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "scripts\launch.ps1"
exit /b 0

:setup
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "scripts\start_app.ps1"
exit /b %errorlevel%
