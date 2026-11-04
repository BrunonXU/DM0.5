#!/usr/bin/env bash
# 6000D only. This exposes the model to F1 loopback; it starts no robot process.
set -euo pipefail
projects=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
if tmux has-session -t f1-toys-policy-tunnel 2>/dev/null; then
    echo 'Policy tunnel session already exists; check F1 /health before use.'
    exit 0
fi
printf -v command 'exec ssh -F %q -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:8096:127.0.0.1:8096 f1-09-deploy' \
    "$projects/F1-4242/results/pose_task_20260928/ssh_config"
tmux new-session -d -s f1-toys-policy-tunnel "$command"
echo 'Policy tunnel started; verify F1 /health. No robot commands sent.'
