#!/usr/bin/env bash
# 6000D: run through sg docker. Does not modify the default 8096 service.
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
projects=$(cd -- "$task_root/../.." && pwd -P)
for spec in 2000:8122 5000:8125 14000:8134; do
    step=${spec%:*}; port=${spec#*:}
    F1_TOYS_GPU=1 F1_TOYS_PORT=$port bash "$task_root/scripts/start_6000d.sh" \
        "$task_root/artifacts/20261016-v1-formal-training/bundle-$step" "f1-toys-compare-$step"
done
if ! tmux has-session -t f1-toys-comparison-tunnel 2>/dev/null; then
    printf -v command 'exec ssh -F %q -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:8122:127.0.0.1:8122 -R 127.0.0.1:8125:127.0.0.1:8125 -R 127.0.0.1:8134:127.0.0.1:8134 f1-09-deploy' \
        "$projects/F1-4242/results/pose_task_20260928/ssh_config"
    tmux new-session -d -s f1-toys-comparison-tunnel "$command"
fi
echo 'Services/tunnel requested. Verify all three F1 /health endpoints before onsite use.'
