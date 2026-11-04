#!/usr/bin/env bash
# Run on 6000D. Credentials stay on H20; TLS is passed through unchanged.
set -euo pipefail
scripts=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if ! tmux has-session -t h20-wandb-proxy 2>/dev/null; then
    # Explicit local egress route; no W&B key and no TLS termination on 6000D.
    printf -v relay 'exec env WANDB_UPSTREAM_PROXY=%q python3 %q' "${WANDB_UPSTREAM_PROXY:-${HTTPS_PROXY:-}}" "$scripts/wandb_connect_proxy.py"
    tmux new-session -d -s h20-wandb-proxy "$relay"
fi
if ! tmux has-session -t h20-wandb-tunnel 2>/dev/null; then
    tmux new-session -d -s h20-wandb-tunnel \
      'exec ssh -o BatchMode=yes -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:18096:127.0.0.1:18096 h20-train'
fi
echo 'W&B relay sessions started. Verify online authentication before training.'
