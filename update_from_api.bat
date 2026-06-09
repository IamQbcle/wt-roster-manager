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
  echo.
  echo Python 3 is required for data updates.
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
  echo Python 3.9 or newer is required for data updates.
  echo Download page: https://www.python.org/downloads/
  echo.
  choice /C YN /M "Open the Python download page now"
  if errorlevel 2 exit /b 1
  start "" "https://www.python.org/downloads/"
  exit /b 1
)
%PYTHON_CMD% -m pip install requests
if errorlevel 1 goto fail
if not exist data mkdir data
%PYTHON_CMD% scripts\update_from_api.py --workers 24 --fetch-wiki-names
if errorlevel 1 goto fail
echo SUCCESS
echo.
echo After a major War Thunder patch, open the Changes tab and review the patch-status warning.
echo If the data is still OK, use "Считать данные актуальными" in the app.
pause
exit /b 0
:fail
echo FAILED
pause
exit /b 1
