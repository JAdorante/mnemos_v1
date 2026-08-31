@echo off
rem Mnemos one-click tester install. Safe to re-run if it fails partway.
rem The exit code is the installer's, not pause's: `pause` succeeds even when
rem the install failed, so without capturing ERRORLEVEL first this always
rem exits 0 and any automated check downstream passes on a broken install.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
set RC=%ERRORLEVEL%
pause
exit /b %RC%
