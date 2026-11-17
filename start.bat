@echo off
rem Starts the Job Agent in the background (no console window) and opens the dashboard.
cd /d "%~dp0"
if not exist .venv (
  echo Setting up for the first time...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  .venv\Scripts\python.exe -m playwright install chromium
)
if not exist .env copy .env.example .env >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\server.ps1" start
start "" http://127.0.0.1:8765
