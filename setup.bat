@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe goto install
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m venv .venv) else (python -m venv .venv)
if errorlevel 1 goto fail
:install
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo Setup complete. Double-click run.bat to start.
pause
exit /b 0
:fail
echo.
echo Setup failed - see the error above. Python 3.10+ ^(python.org, "Add python.exe to PATH"^) and internet access are required. Keep this folder path short ^(Windows path length limit^).
pause
exit /b 1
