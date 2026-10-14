@echo off
setlocal
title DAgger F710 operator sender
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PYTHON_EXE=%USERPROFILE%\.venv\Scripts\python.exe"
set "PYTHONPATH=%PROJECT_ROOT%\src;%PROJECT_ROOT%\..\shared\src"
cd /d "%PROJECT_ROOT%"
echo F710 sender: local 7907 to G2 7906. G2 entry 12 = manual teleop; entry 40 = HIL.
"%PYTHON_EXE%" "%PROJECT_ROOT%\scripts\03_windows_shadow_sender.py" --host 127.0.0.1 --port 7907
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
