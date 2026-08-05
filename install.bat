@echo off
rem Mnemos one-click tester install. Safe to re-run if it fails partway.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
pause
