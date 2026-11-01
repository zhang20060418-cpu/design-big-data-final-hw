@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=%ROOT%backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if defined PYTHONPATH (
  set "PYTHONPATH=%ROOT%tooling;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%ROOT%tooling"
)
"%PY%" -m ppt_audit_tool %*
exit /b %errorlevel%
