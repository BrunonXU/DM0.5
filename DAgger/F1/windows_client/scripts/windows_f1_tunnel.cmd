@echo off
setlocal
title F1-09 F710 SSH tunnel
if not defined F1_SSH_HOST (
  echo Set F1_SSH_HOST to your verified F1-09 SSH alias first.
  exit /b 2
)
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:7907:127.0.0.1:7906 %F1_SSH_HOST%
