@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo stop> stop.flag
echo Stopping 88 Trader...
if not exist data\88trader.pid goto :nopid
set /p PID=<data\88trader.pid
taskkill /PID !PID! /F >nul 2>nul
del data\88trader.pid >nul 2>nul
echo Bot process stopped.
goto :done
:nopid
echo No running bot found (data\88trader.pid missing).
:done
echo Open positions stay open in MT5 (they have stop losses).
echo If the run.bat window is still open, just close it.
pause
