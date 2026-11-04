#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
ROOT="$G2TELEOP_ROOT"
DM=/home/user/xza/DM0.5
APP=/home/user/app
DEPLOYMENT="$DM/g2_sort/deployment"
ARTIFACT="$DM/logs/diagnostics/g2_bc_eef_causal_commands_20260902_173215.npz"
EXPECTED_SHA=31537fe91ef96f1a9931bd4d33cedf51f344a757b2a21810ee67978965c54984

cd "$ROOT"
export PYTHONPATH="$APP/gdk/lib:$ROOT/vendor_ik:$ROOT/src:$DM:$DEPLOYMENT${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$APP/lib:$APP/gdk/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export APP_CONF_PATH="$APP/gdk/config/app_conf.yml"
export AORTA_DISCOVERY_URI=http://192.0.2.9:2379
export AORTA_DISPATCHER_THREAD_NUM=6

ACTUAL_SHA=$(sha256sum "$ARTIFACT" | awk '{print $1}')
if [[ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]]; then
  echo "HOLD: causal artifact SHA mismatch: $ACTUAL_SHA" >&2
  exit 2
fi
G2_LOCATOR_IP=$(ip -o -4 addr list | awk '$4 ~ /^10\.42\.1\./ {sub(/\/.*/, "", $4); print $4; exit}')
if [[ -z "$G2_LOCATOR_IP" ]]; then
  echo "HOLD: no existing 10.42.1.x G2 locator address" >&2
  exit 2
fi
export LOCATOR_IP="$G2_LOCATOR_IP"

python3 scripts/26_g2_o10_point_target_ab.py --artifact-npz "$ARTIFACT"

echo
echo "O10 POINT-TARGET A/B; arms hold the measured current pose at 100 Hz normal Servo."
echo "A at 6 s: submit the frozen close endpoint exactly once."
echo "Reset at 12 s: submit the identical frozen open start exactly once."
echo "B at 18 s: submit v1_1 intermediate targets at 10 Hz to the same endpoint."
echo "No background hand worker refreshes A, reset, or B after its final command."
echo "No Pink/Ruckig, network/model input, waist, or chassis output."
echo "Observe whether A and B close continuously or in several 0.x s chunks."
read -r -p "Press Enter to start the countdown (Ctrl-C to abort): " _
for LEFT in 5 4 3 2 1; do
  echo "Starting O10 point-target A/B in $LEFT... Ctrl-C to abort."
  sleep 1
done

STAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY="$DM/logs/telemetry/powered/g2_o10_point_target_ab_${STAMP}.jsonl"
CONSOLE="$G2TELEOP_LOG_DIR/g2_o10_point_target_ab_${STAMP}.console.log"
mkdir -p "$G2TELEOP_LOG_DIR"
set +e
python3 scripts/26_g2_o10_point_target_ab.py \
  --execute \
  --artifact-npz "$ARTIFACT" \
  --telemetry-jsonl "$TELEMETRY" 2>&1 | (trap '' INT; tee "$CONSOLE")
STATUS=${PIPESTATUS[0]}
set -e
echo "Exit status: $STATUS"
echo "Main telemetry: $TELEMETRY"
echo "Console log: $CONSOLE"
FEEDBACK="$DM/logs/telemetry/feedback/$(basename "$TELEMETRY")"
if [[ -s "$FEEDBACK" ]]; then
  echo "Feedback telemetry: $FEEDBACK"
else
  echo "Feedback telemetry: unavailable or remained partial"
fi
exit "$STATUS"
