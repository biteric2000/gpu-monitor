@echo off
cd /d %~dp0
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" goto nopy
echo Starting GPU monitor server (host and port from config.yaml)...
echo Press Ctrl+C in this window to stop.
"%PY%" main.py
pause
exit /b 0
:nopy
echo ERROR: venv not found at
echo   %PY%
echo Run setup.bat first, or install deps manually.
pause
exit /b 1
