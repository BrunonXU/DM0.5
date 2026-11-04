#!/usr/bin/env bash
# Configure this installation; never start a robot or a service.
set -euo pipefail
F1_SETUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$F1_SETUP_ROOT/deploy/initialize.py" "$@"
