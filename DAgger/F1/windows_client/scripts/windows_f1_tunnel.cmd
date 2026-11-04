@echo off
setlocal
title F1-09 F710 SSH tunnel
rem Set F1_SSH_HOST=user@192.0.2.3 before launch for the Wi-Fi fallback.
if not defined F1_SSH_HOST set "F1_SSH_HOST=user@192.0.2.7"
echo F1 SSH target: %F1_SSH_HOST% (fixed for this session)
ssh -F NUL -N -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:7907:127.0.0.1:7906 %F1_SSH_HOST%
