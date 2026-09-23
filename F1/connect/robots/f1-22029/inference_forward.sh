#!/usr/bin/env bash
# Run on 6000D. Forward existing model services; never starts robot control.
set -euo pipefail
task_connect="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
task_socket="$task_connect/inference-forward.sock"
if ssh -F "$task_connect/client_ssh_config" -S "$task_socket" -O check f1-22029 2>/dev/null; then
  # Add the newly selected checkpoint without interrupting existing 5K/20K forwards.
  ssh -F "$task_connect/client_ssh_config" -S "$task_socket" -O forward \
    -R 127.0.0.1:7896:127.0.0.1:7896 f1-22029 2>/dev/null || true
else
ssh -F "$task_connect/client_ssh_config" -f -NT \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -M -S "$task_socket" \
  -R 127.0.0.1:7892:127.0.0.1:7892 \
  -R 127.0.0.1:7894:127.0.0.1:7894 \
  -R 127.0.0.1:7896:127.0.0.1:7896 f1-22029
fi
ssh -F "$task_connect/client_ssh_config" f1-22029 'python3 -' <<'PY_CHECK'
import json, urllib.request
for port, step in ((7892,5000),(7894,20000),(7896,25000)):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=10) as response:
        health=json.load(response)
    checkpoint=health['model_binding']['checkpoint_identity']['checkpoint']
    assert health['ready'] and checkpoint.endswith(f'/checkpoint-{step}'), (port,health)
    print(port,checkpoint,'ready')
PY_CHECK
