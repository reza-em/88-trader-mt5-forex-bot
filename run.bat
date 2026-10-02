@echo off
setlocal
cd /d "%~dp0"
title 88 Trader (do not close this window)
if exist ".venv\Scripts\python.exe" (set PYX=".venv\Scripts\python.exe") else (set PYX=python)
if exist stop.flag del stop.flag
:loop
echo [%date% %time%] starting 88 Trader...
%PYX% main.py
set CODE=%errorlevel%
if exist stop.flag (
  del stop.flag
  echo Stopped by stop.bat.
  goto :end
)
if %CODE%==2 goto :config_error
if %CODE%==3 goto :config_error
if %CODE%==4 goto :config_error
echo [%date% %time%] 88 Trader exited with code %CODE%. Restarting in 10 seconds... (close this window to stop)
timeout /t 10 /nobreak >nul
goto :loop
:config_error
echo.
echo Fix the problem described above (token / password / config.yaml), then run run.bat again.
:end
pause
