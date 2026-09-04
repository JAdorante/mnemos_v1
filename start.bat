@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Sparrow is not installed yet - run install.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe run_all.py
pause
