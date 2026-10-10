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
".venv\Scripts\python.exe" voice_claude.py
pause
