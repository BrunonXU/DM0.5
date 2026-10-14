@echo off
setlocal
title G2Teleop - F710 sender (keep this window open)

for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PYTHON_EXE=%USERPROFILE%\.venv\Scripts\python.exe"
set "PYTHONPATH=%PROJECT_ROOT%\src"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Missing the verified Python environment: %PYTHON_EXE%
  echo [ERROR] Do not create another environment in a different directory.
  pause
  exit /b 2
)
if not exist "%PROJECT_ROOT%\scripts\03_windows_shadow_sender.py" (
  echo [ERROR] Repository is incomplete: %PROJECT_ROOT%
  pause
  exit /b 3
)

cd /d "%PROJECT_ROOT%"
"%PYTHON_EXE%" -c "import pygame, g2teleop; print('ENV_OK', pygame.version.ver)"
if errorlevel 1 (
  echo [ERROR] Python environment check failed.
  pause
  exit /b 4
)

echo [G2Teleop] With RB released: tap D-pad left/right, then release D-pad.
echo [G2Teleop] Press/hold RB only after selection; tap A to toggle low/high speed.
echo [G2Teleop] RT toggles the selected hand independently; RB is not required.
echo [G2Teleop] With RB released, Y runs the approved global arms+hands Home.
"%PYTHON_EXE%" "%PROJECT_ROOT%\scripts\03_windows_shadow_sender.py" --host 127.0.0.1 --port 7904 --hz 50

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [G2Teleop] Sender exited with code %EXIT_CODE%.
echo [G2Teleop] This window is paused so an error cannot flash away.
pause
exit /b %EXIT_CODE%
