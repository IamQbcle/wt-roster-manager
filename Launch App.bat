@echo off
cd /d "%~dp0"
set "PYTHON_CMD="
where py >nul 2>nul
if %errorlevel%==0 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if %errorlevel%==0 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  echo Python 3 is required to run the local server.
  echo Download and install the latest Python from https://www.python.org/downloads/
  echo During installation, enable "Add python.exe to PATH".
  pause
  exit /b 1
)
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
  echo Python 3.9 or newer is required.
  echo Download and install the latest Python from https://www.python.org/downloads/
  pause
  exit /b 1
)
start "WT Roster Local Server" /min %PYTHON_CMD% scripts\local_server.py
timeout /t 2 >nul
start "" "http://127.0.0.1:8765/index.html?v=3.74"
