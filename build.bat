@echo off
rem Windows 배포 파일 빌드: PyInstaller(onedir) -> dist\ChromeJumper-<버전>-win64.zip
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
if not exist .venv\Scripts\python.exe call setup.bat
.venv\Scripts\python -m pip install -q -r requirements-build.txt
if errorlevel 1 goto fail
if exist build\pyi-dist rmdir /s /q build\pyi-dist
.venv\Scripts\python -m PyInstaller --noconfirm --clean --log-level WARN --distpath build\pyi-dist --workpath build\pyi-work packaging\ChromeJumper.spec
if errorlevel 1 goto fail
.venv\Scripts\python -m PyInstaller --noconfirm --clean --log-level WARN --distpath build\pyi-dist --workpath build\pyi-work packaging\MockSite.spec
if errorlevel 1 goto fail
.venv\Scripts\python packaging\make_zip.py
if errorlevel 1 goto fail
echo.
echo Build complete: dist\
exit /b 0
:fail
echo Build failed.
exit /b 1
