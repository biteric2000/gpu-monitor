@echo off
cd /d %~dp0..
echo === Setting up venv for GPU monitor ===
if exist .venv\Scripts\python.exe goto venv_ok
python -m venv .venv
if errorlevel 1 goto fail
:venv_ok
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check -r server\requirements.txt
if errorlevel 1 goto fail
echo Setup complete. Run start_server.bat to launch.
pause
exit /b 0
:fail
echo ERROR: setup failed. Is Python installed and on PATH?
pause
exit /b 1
