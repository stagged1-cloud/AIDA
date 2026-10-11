@echo off
rem start_console.bat = the ORIGINAL start.bat, unchanged: always runs the app in this console (Ctrl+C quits).
rem Use it for debugging, or as the fallback if the windowless start in start.bat ever fails.
title AIDA voice
cd /d "%~dp0"
rem Packages are installed (or topped up) whenever requirements.txt differs from the copy saved after the last
rem complete install, so a cancelled first run or a newer requirements.txt is handled automatically.
if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the Python environment.
  python -m venv .venv || goto :err
)
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
  echo Installing packages. The first time this downloads about 1.5 GB and takes a few minutes.
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
  copy /y requirements.txt ".venv\requirements.installed" >nul
)
".venv\Scripts\python.exe" voice_claude.py
pause
exit /b

:err
echo Setup failed. Scroll up for the error.
pause
