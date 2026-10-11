@echo off
chcp 65001 >nul
rem Double-click = interactive menu. Drop game files (.twv .tws .wd1), normal videos or
rem audio, or a folder onto this file - the conversion direction is chosen automatically.
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add Python to PATH" during setup.
  pause
  exit /b 1
)
if "%~1"=="" (
  python "%~dp0twv2mp4.py"
  if errorlevel 1 pause
) else (
  python "%~dp0twv2mp4.py" %*
  echo.
  pause
)
