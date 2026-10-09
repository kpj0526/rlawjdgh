@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
.venv\Scripts\python -m mock_site.server %*
