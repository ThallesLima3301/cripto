@echo off
REM crypto_monitor — maintenance task wrapper.
REM Invoked by Windows Task Scheduler once a day (default 03:00 local).
REM Runs evaluation of matured signals/buys, prunes old candles, and
REM optionally VACUUMs the database (controlled by config.toml).
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

"%PYTHON%" -m crypto_monitor.cli evaluate >> "logs\maintenance.cmd.log" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%
