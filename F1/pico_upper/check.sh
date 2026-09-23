#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/env.sh"
exec "$PICO_UPPER_PYTHON" -m pico_upper.preflight "$@"
