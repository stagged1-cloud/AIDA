@echo off
title AIDA voice
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo AIDA is not installed yet. Run install.ps1 first ^(see README.md^).
  pause
  exit /b 1
)
if not exist "config.yaml" (
  echo No config.yaml yet. Run install.ps1 ^(or: .venv\Scripts\python.exe setup_wizard.py^).
  pause
  exit /b 1
)
rem Top-up: after an update brings a newer requirements.txt, install the new packages once before starting.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
  echo Installing new or updated packages. This can take a few minutes.
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
  copy /y requirements.txt ".venv\requirements.installed" >nul
)
rem Windowless start: when config.yaml has chat_window: true (and overlay is not off), run with pythonw.exe so no
rem console stays open. Any error or doubt falls through to the normal console run (same as start_console.bat).
if exist ".venv\Scripts\pythonw.exe" (
  ".venv\Scripts\python.exe" windowless.py check >nul 2>&1
  if not errorlevel 1 (
    start "" ".venv\Scripts\pythonw.exe" voice_claude.py
    exit /b
  )
)
".venv\Scripts\python.exe" voice_claude.py
pause
exit /b

:err
echo Package install failed. Scroll up for the error, or run install.ps1 again.
pause
