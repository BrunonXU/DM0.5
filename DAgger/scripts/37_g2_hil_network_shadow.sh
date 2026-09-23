#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
echo 'ZERO OUTPUT HIL: no GDK / Camera / Robot / Servo; port 7906.'
echo 'Real operator network; synthetic observation, policy and hand completion.'
read -r -p 'Press ENTER to start; Ctrl-C cancels. ' _confirm
LOG_PATH="$PROJECT_ROOT/logs/hil_shadow/hil_network_$(date +%Y%m%d_%H%M%S)_$$.jsonl"
exec python3 scripts/37_g2_hil_network_shadow.py --log "$LOG_PATH"
