@echo off
setlocal
title F1-09 F710 operator
for %%I in ("%~dp0..") do set "DAGGER_CLIENT_ROOT=%%~fI"
if not defined F1_SENDER_PYTHON set "F1_SENDER_PYTHON=%USERPROFILE%\.venv\Scripts\python.exe"
set "PYTHONPATH=%DAGGER_CLIENT_ROOT%\src"
cd /d "%DAGGER_CLIENT_ROOT%"
"%F1_SENDER_PYTHON%" "%DAGGER_CLIENT_ROOT%\scripts\03_windows_f1_sender.py" --host 127.0.0.1 --port 7907
