@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [error] venv python not found: %PY%
  echo run uv sync first.
  exit /b 1
)
start "" "http://127.0.0.1:7860/"
"%PY%" "%ROOT%jarvis_ui_server.py"
endlocal
