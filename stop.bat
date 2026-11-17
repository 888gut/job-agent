@echo off
rem Stops the Job Agent background server.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\server.ps1" stop
