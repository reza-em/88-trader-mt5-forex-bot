@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set PYX=".venv\Scripts\python.exe") else (set PYX=python)
%PYX% -m pip install yfinance -q
%PYX% backtest.py %*
pause
