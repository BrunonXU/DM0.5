@echo off
setlocal
title G2 HIL ZERO OUTPUT candidate sender
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PYTHON_EXE=%USERPROFILE%\.venv\Scripts\python.exe"
set "PYTHONPATH=%PROJECT_ROOT%\src"
cd /d "%PROJECT_ROOT%"
echo HIL candidate on port 7906. START toggles ownership; RB does not.
"%PYTHON_EXE%" "%PROJECT_ROOT%\scripts\03_windows_shadow_sender.py" --port 7906
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
