@echo off
python scripts\update_from_api.py --skip-images --skip-tree-links --skip-tree-order --clear-name-cache
if errorlevel 1 (
  echo ERROR
  pause
  exit /b 1
)
echo SUCCESS
pause
