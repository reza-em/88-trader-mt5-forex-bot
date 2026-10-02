@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set PYX=".venv\Scripts\python.exe") else (set PYX=python)
%PYX% check_setup.py
pause
