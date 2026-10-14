#!/usr/bin/env bash
# Run in the F1 operator's local terminal. No detached automatic motion start.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -ne 0 ]]; then echo 'Usage: ./start.sh (现场终端执行；只读检查用 ./check.sh)'; exit 2; fi
if [[ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ]]; then
    echo '请在 F1 现场本机终端执行 start.sh；SSH 只支持 check.sh。' >&2; exit 1
fi
source "$ROOT/scripts/env.sh"
exec "$PICO_UPPER_PYTHON" -m pico_upper.supervisor start
