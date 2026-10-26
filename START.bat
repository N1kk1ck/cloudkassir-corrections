@echo off
setlocal
cd /d "%~dp0"
title CloudKassir correction receipts
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Open http://127.0.0.1:8765
".venv\Scripts\python.exe" app.py
exit /b
:fail
echo Setup failed. Install Python 3.11+ and check internet access.
pause
exit /b 1
