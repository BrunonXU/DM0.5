#!/usr/bin/env bash
set -euo pipefail
DAGGER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
echo "MOVED: use $DAGGER_ROOT/../docs/G2/START_HERE.md"
exec bash "$DAGGER_ROOT/scripts/37_g2_hil_network_shadow.sh" "$@"
