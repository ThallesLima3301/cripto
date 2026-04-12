@echo off
REM crypto_monitor — remove the scheduled tasks created by register_tasks.cmd.
REM
REM Safe to run even if some of the tasks don't exist; schtasks will just
REM print a "task does not exist" line and we keep going.

setlocal

echo Removing "crypto_monitor scan"...
schtasks /Delete /TN "crypto_monitor scan" /F

echo Removing "crypto_monitor maintenance"...
schtasks /Delete /TN "crypto_monitor maintenance" /F

echo Removing "crypto_monitor weekly"...
schtasks /Delete /TN "crypto_monitor weekly" /F

echo.
echo Done.
endlocal
exit /b 0
