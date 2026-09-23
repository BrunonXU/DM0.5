#!/usr/bin/env bash
# Stopping this session needs only Python stdlib, even if a ROS environment broke.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PICO_UPPER_RUNTIME="${PICO_UPPER_RUNTIME:-/home/user/Bruno/F1/pico_upper/runtime}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m pico_upper.supervisor stop
