@echo off
rem Stops the Offer Ledger background server.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\server.ps1" stop
