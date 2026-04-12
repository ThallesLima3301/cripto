@echo off
REM crypto_monitor — scan task wrapper.
REM Invoked by Windows Task Scheduler every 5 minutes.
REM
REM This wrapper:
REM   1. cds into the project root (one level up from this script).
REM   2. Activates the in-tree .venv if one exists, else falls back to
REM      the system `python` on PATH.
REM   3. Runs `python -m crypto_monitor.cli scan`, appending stdout
REM      and stderr to logs\scan.cmd.log.
REM
REM Edit only if you move the project or rename the venv.

setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || exit /b 1

if not exist "logs" mkdir "logs"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" -m crypto_monitor.cli scan >> "logs\scan.cmd.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%
