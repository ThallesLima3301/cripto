@echo off
REM crypto_monitor — weekly summary task wrapper.
REM Invoked by Windows Task Scheduler once a week (default Sunday 09:00 local).
REM See scan.cmd for the wrapper convention.

setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || exit /b 1

if not exist "logs" mkdir "logs"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" -m crypto_monitor.cli weekly >> "logs\weekly.cmd.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%
