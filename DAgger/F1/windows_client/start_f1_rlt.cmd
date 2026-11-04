@echo off
setlocal
title F1 RLT - single window
set "DAGGER_CLIENT_ROOT=%~dp0"
if not defined F1_SENDER_PYTHON set "F1_SENDER_PYTHON=%USERPROFILE%\.venv\Scripts\python.exe"
"%F1_SENDER_PYTHON%" "%DAGGER_CLIENT_ROOT%scripts\windows_f1_bootstrap.py" %*
set "F1_SESSION_EXIT=%ERRORLEVEL%"
rem Never trap a reusable PowerShell inside cmd.exe's PAUSE after Ctrl+C.
exit /b %F1_SESSION_EXIT%
