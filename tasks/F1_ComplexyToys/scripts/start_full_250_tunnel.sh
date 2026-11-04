#!/usr/bin/env bash
# Existing SSH authorization, session-scoped reverse forwarding; no key changes.
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
projects=$(cd -- "$task_root/../.." && pwd -P)
if [[ "${1:-}" != --supervise ]]; then
    if ! tmux has-session -t f1-full-250-tunnel 2>/dev/null; then
        tmux new-session -d -s f1-full-250-tunnel \
            "bash '$task_root/scripts/start_full_250_tunnel.sh' --supervise"
    fi
    exit 0
fi
log="$task_root/artifacts/20261020-v1-full-training/tunnel-250.log"
while true; do
    ssh -F "$projects/F1-4242/results/pose_task_20260928/ssh_config" -NT \
        -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=5 -o ServerAliveCountMax=3 \
        -R 127.0.0.1:8151:127.0.0.1:8150 f1-09-deploy >> "$log" 2>&1 || true
    sleep 2
done
