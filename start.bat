@echo off
rem Starts Offer Ledger in the background (no console window) and opens the dashboard.
cd /d "%~dp0"
if not exist .venv (
  echo Setting up for the first time...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  .venv\Scripts\python.exe -m playwright install chromium
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-desktop-icon.ps1"
)
if not exist .env copy .env.example .env >nul
rem Same as the desktop icon: run the latest code (restarting if it changed) and open the dashboard.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\server.ps1" open
