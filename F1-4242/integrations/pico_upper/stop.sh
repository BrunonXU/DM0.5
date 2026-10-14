#!/usr/bin/env bash
# Stopping this session needs only Python stdlib, even if a ROS environment broke.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERE="$(cd "$ROOT/../.." && pwd -P)"
if [[ "${F1_ENV_LOADED:-}" != 1 && -f "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}" ]]; then
    source "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}"
fi
export PICO_UPPER_RUNTIME="${PICO_UPPER_RUNTIME:-${F1_CONSOLE_RUNTIME:-${F1_INFRA_HOME:-$HOME/Bruno/F1-infra}/runtime}/pico_upper}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m pico_upper.supervisor stop
