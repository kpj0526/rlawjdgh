@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
.venv\Scripts\python -m pip install -q -r requirements-dev.txt
.venv\Scripts\python -m pytest -v tests %*
set RC=%errorlevel%
pause
exit /b %RC%
