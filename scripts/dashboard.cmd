@echo off
REM crypto_monitor — dashboard API launcher (read-only).
REM
REM Foreground process: serves the FastAPI adapter at
REM http://127.0.0.1:8787 with auto-reload on code changes. Intended
REM for local development, not for unattended scheduling.
REM
REM Usage:
REM     scripts\dashboard.cmd               (default port 8787)
REM     scripts\dashboard.cmd 8000          (override port)
REM
REM This wrapper:
REM   1. cds into the project root (one level up from this script).
REM   2. Activates the in-tree .venv if one exists, else falls back to
REM      the system `python` on PATH.
REM   3. Runs uvicorn with --reload bound to 127.0.0.1.
REM
REM The `[dashboard]` extra must already be installed:
REM     pip install -e ".[dashboard]"
REM or, if you're using requirements files:
REM     pip install fastapi "uvicorn[standard]" pydantic

setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || exit /b 1

if "%~1"=="" (
    set "PORT=8787"
) else (
    set "PORT=%~1"
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" -m uvicorn crypto_monitor.dashboard.api:app --reload --host 127.0.0.1 --port %PORT%
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%
