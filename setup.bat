@echo off
setlocal
cd /d "%~dp0"
title 88 Trader - setup
echo ================================================
echo   88 Trader - setup
echo ================================================
where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3.11) else (set PY=python)
%PY% --version
if errorlevel 1 (
  echo.
  echo [ERROR] Python 3.11 was not found. Install Python 3.11 64-bit from python.org
  echo         and tick "Add python.exe to PATH". Then run setup.bat again.
  pause
  exit /b 1
)
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [ERROR] could not create venv & pause & exit /b 1 )
)
echo Installing packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed. Check your internet connection / Python must be 64-bit.
  pause
  exit /b 1
)
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo A file named .env was created. Open it with Notepad and fill in:
  echo    TRADER_TELEGRAM_BOT_TOKEN=...   and   MT5_PASSWORD=...
)
echo.
echo Setup finished. Next: edit config.yaml (login, server), then run check_setup.bat
pause
