#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
dagger_artifacts_root="${DAGGER_ARTIFACTS_ROOT:-$PROJECT_ROOT}"
if [[ -z "${DAGGER_ARTIFACTS_ROOT:-}" && -d "$PROJECT_ROOT/../shared/src/dagger_core" ]]; then
    dagger_artifacts_root="$PROJECT_ROOT/../artifacts"
fi
CLIENT_ROOT="${G2_PROJECT_DIR:-/home/agi/xza/DM0.5}"
export PYTHONPATH="$PROJECT_ROOT/src:$PROJECT_ROOT/../shared/src:$CLIENT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$PROJECT_ROOT"
python3 scripts/39_g2_real_observation_hil_shadow.py --client-root "$CLIENT_ROOT" --preflight "$@"
echo 'REAL camera/state/policy SHADOW: ZERO robot action output. Stop other Robot owners first.'
echo 'Capture defaults to 10Hz; action waypoint contract remains 30Hz. Duration defaults to 120s.'
read -r -p 'Press ENTER to initialize read-only GDK; Ctrl-C cancels. ' _confirm
set +u
cd /home/agi/app
source /home/agi/app/env.sh /home/agi/app
set -u
export PYTHONPATH="$PROJECT_ROOT/src:$PROJECT_ROOT/../shared/src:$CLIENT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$PROJECT_ROOT"
OUTPUT="${dagger_artifacts_root}/data/raw/real_hil_shadow/run_$(date +%Y%m%d_%H%M%S)_$$"
exec python3 scripts/39_g2_real_observation_hil_shadow.py --client-root "$CLIENT_ROOT" --output "$OUTPUT" "$@"
