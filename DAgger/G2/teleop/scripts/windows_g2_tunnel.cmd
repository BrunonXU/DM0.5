@echo off
setlocal
title G2Teleop - SSH tunnel (keep this window open)

for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\logs"
if exist "%PROJECT_ROOT%\..\..\docs\G2" for %%I in ("%PROJECT_ROOT%\..\..\artifacts\logs\g2_teleop") do set "LOG_DIR=%%~fI"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "SSH_LOG=%LOG_DIR%\windows_tunnel_ssh.log"

set "G2_HOST=10.130.86.13"
set "G2_USER=agi"
set "G2_PORT=7904"

echo [G2Teleop] Windows 127.0.0.1:%G2_PORT% ^> G2 127.0.0.1:%G2_PORT%
echo [G2Teleop] Keep this window open. Close it to stop automatic reconnect.
echo [G2Teleop] Persistent SSH diagnostic log: %SSH_LOG%

:TUNNEL_RECONNECT
ssh.exe -v -E "%SSH_LOG%" -N -T -o ExitOnForwardFailure=yes -o ConnectTimeout=5 -o TCPKeepAlive=yes -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -L 127.0.0.1:%G2_PORT%:127.0.0.1:%G2_PORT% %G2_USER%@%G2_HOST%

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [G2Teleop] Tunnel exited with code %EXIT_CODE%.
echo [G2Teleop] Receiver is HOLD. Reconnecting in 2 seconds; password may be requested again.
timeout /t 2 /nobreak
goto TUNNEL_RECONNECT
