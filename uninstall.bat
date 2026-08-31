@echo off
rem Delete everything Mnemos captured on this machine, and print a receipt.
rem Close Mnemos first - this refuses to run while the server is up.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Mnemos is not installed here - there is nothing to delete.
    echo If you want the folder gone, just delete it.
    pause
    exit /b 1
)
.venv\Scripts\python.exe scripts\uninstall.py %*
set RC=%ERRORLEVEL%
pause
exit /b %RC%
