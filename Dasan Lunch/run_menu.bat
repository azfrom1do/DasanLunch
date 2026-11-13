@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "menu_collector_free.py" (
  echo menu_collector_free.py not found next to this file.
  pause
  exit /b 1
)
for %%I in ("%~dp0..") do set "REPO=%%~fI"
set "LOG=%~dp0run_log.txt"

echo Working... this can take a few minutes.
echo This window closes automatically when done.
echo.
py -3.12 -u menu_collector_free.py
if errorlevel 1 (
  echo.
  echo ERROR - see run_log.txt for details
  pause
  exit /b 1
)

cd /d "%REPO%"
git add -A >> "%LOG%" 2>&1
git commit -m "menu %date%" >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1

start "" "%REPO%\index.html"
exit
