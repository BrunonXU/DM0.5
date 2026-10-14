#!/usr/bin/env bash
set -euo pipefail
DAGGER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
echo "MOVED: use $DAGGER_ROOT/../docs/G2/START_HERE.md"
exec bash "$DAGGER_ROOT/scripts/39_g2_real_observation_hil_shadow.sh" "$@"
