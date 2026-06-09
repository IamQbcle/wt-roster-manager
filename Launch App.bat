@echo off
title WT Roster Manager - Launcher
cd /d "%~dp0"
set "PYTHON_CMD="
where py >nul 2>nul
if %errorlevel%==0 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if %errorlevel%==0 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  echo.
  echo Python 3 is required to run War Thunder Roster Manager.
  echo Download page: https://www.python.org/downloads/
  echo During installation, enable "Add python.exe to PATH".
  echo.
  choice /C YN /M "Open the Python download page now"
  if errorlevel 2 exit /b 1
  start "" "https://www.python.org/downloads/"
  exit /b 1
)
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
  echo.
  echo Python 3.9 or newer is required.
  echo Download page: https://www.python.org/downloads/
  echo.
  choice /C YN /M "Open the Python download page now"
  if errorlevel 2 exit /b 1
  start "" "https://www.python.org/downloads/"
  exit /b 1
)
start "WT Roster Local Server" /min cmd /c "%PYTHON_CMD% scripts\local_server.py"
timeout /t 2 >nul
start "" "http://127.0.0.1:8765/index.html?v=3.79"
