#!/usr/bin/env bash
set -euo pipefail
DAGGER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../DAgger" && pwd)"
echo "MOVED: use $DAGGER_ROOT/START_HERE.md"
exec bash "$DAGGER_ROOT/scripts/40_g2_powered_hil.sh" "$@"
