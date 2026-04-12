@echo off
REM crypto_monitor — register Windows scheduled tasks.
REM
REM Creates three tasks under your user account:
REM
REM   "crypto_monitor scan"         every 5 minutes
REM   "crypto_monitor maintenance"  daily at 03:00 local
REM   "crypto_monitor weekly"       Sundays at 09:00 local
REM
REM Run this from a regular (non-elevated) command prompt — /RL LIMITED
REM means the tasks run as the current user with no admin rights.
REM Re-running this script overwrites the existing tasks (/F).
REM
REM To remove the tasks, run unregister_tasks.cmd from the same folder.

setlocal
set "SCRIPT_DIR=%~dp0"

echo Registering "crypto_monitor scan" (every 5 minutes)...
schtasks /Create /TN "crypto_monitor scan" ^
    /TR "\"%SCRIPT_DIR%scan.cmd\"" ^
    /SC MINUTE /MO 5 ^
    /RL LIMITED /F
if errorlevel 1 goto :err

echo Registering "crypto_monitor maintenance" (daily 03:00)...
schtasks /Create /TN "crypto_monitor maintenance" ^
    /TR "\"%SCRIPT_DIR%maintenance.cmd\"" ^
    /SC DAILY /ST 03:00 ^
    /RL LIMITED /F
if errorlevel 1 goto :err

echo Registering "crypto_monitor weekly" (Sundays 09:00)...
schtasks /Create /TN "crypto_monitor weekly" ^
    /TR "\"%SCRIPT_DIR%weekly.cmd\"" ^
    /SC WEEKLY /D SUN /ST 09:00 ^
    /RL LIMITED /F
if errorlevel 1 goto :err

echo.
echo Done. Verify with:
echo     schtasks /Query /TN "crypto_monitor scan"
echo     schtasks /Query /TN "crypto_monitor maintenance"
echo     schtasks /Query /TN "crypto_monitor weekly"
endlocal
exit /b 0

:err
echo.
echo ERROR: schtasks failed. See output above.
endlocal
exit /b 1
