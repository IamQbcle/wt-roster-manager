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
  echo Python 3 is required for data updates.
  echo Download and install the latest Python from https://www.python.org/downloads/
  echo During installation, enable "Add python.exe to PATH".
  pause
  exit /b 1
)
%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
  echo Python 3.9 or newer is required for data updates.
  echo Download and install the latest Python from https://www.python.org/downloads/
  pause
  exit /b 1
)
%PYTHON_CMD% -m pip install requests
if errorlevel 1 goto fail
if not exist data mkdir data
%PYTHON_CMD% scripts\update_from_api.py --workers 24 --fetch-wiki-names
if errorlevel 1 goto fail
echo SUCCESS
pause
exit /b 0
:fail
echo FAILED
pause
exit /b 1
