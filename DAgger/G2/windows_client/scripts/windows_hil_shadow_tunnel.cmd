@echo off
setlocal
title DAgger tunnel: local 7907 to G2 7906
:reconnect
ssh.exe -N -T -o ExitOnForwardFailure=yes -o ConnectTimeout=5 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -L 127.0.0.1:7907:127.0.0.1:7906 agi@192.0.2.8
echo Tunnel ended; reconnecting in 2 seconds. Receiver must remain HOLD.
timeout /t 2 /nobreak
goto reconnect
