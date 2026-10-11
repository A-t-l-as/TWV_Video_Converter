@echo off
chcp 65001 >nul
rem Przeciagnij na ten plik filmy .twv / .mp4 (lub foldery) albo kliknij dwukrotnie.
if "%~1"=="" (
  python "%~dp0twv2mp4-v4.py"
) else (
  python "%~dp0twv2mp4-v4.py" %*
  echo.
  pause
)
