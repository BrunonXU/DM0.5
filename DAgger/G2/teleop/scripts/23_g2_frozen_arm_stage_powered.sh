#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
ROOT="$G2TELEOP_ROOT"
DM=/home/agi/xza/DM0.5
APP=/home/agi/app
DEPLOYMENT="$DM/g2_sort/deployment"
ARTIFACT="$DM/logs/diagnostics/g2_bc_eef_causal_commands_20260902_173215.npz"
EXPECTED_SHA=31537fe91ef96f1a9931bd4d33cedf51f344a757b2a21810ee67978965c54984
STAGE=${1:-}

if [[ "$STAGE" != "direct" && "$STAGE" != "otg" ]]; then
  echo "Usage: bash scripts/23_g2_frozen_arm_stage_powered.sh direct|otg" >&2
  exit 2
fi

cd "$ROOT"
export PYTHONPATH="$APP/gdk/lib:$ROOT/vendor_ik:$ROOT/src:$DM:$DEPLOYMENT${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$APP/lib:$APP/gdk/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export APP_CONF_PATH="$APP/gdk/config/app_conf.yml"
export AORTA_DISCOVERY_URI=http://10.42.1.101:2379
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

python3 scripts/21_g2_frozen_arm_stage_replay.py \
  --artifact-npz "$ARTIFACT" \
  --stage "$STAGE" \
  --servo-channel normal \
  --hand-mode recorded-actions

echo
echo "POWERED TEST: frozen Q-$STAGE arms + recorded O10 actions."
echo "Arms: 100 Hz normal Servo. O10: split move_ee_pos, v1_1, 10 Hz."
echo "No chassis, waist, online Pink/Ruckig calculation, network input, or model inference."
echo "Duration is 91.22 s including a 1 s stationary tail."
echo "Keep a second person continuously on the physical E-stop."
echo "Abort immediately for sustained vibration, abnormal sound, contact, or unexpected motion."
read -r -p "Press Enter to start the countdown (Ctrl-C to abort): " _

for LEFT in 5 4 3 2 1; do
  echo "Starting Q-$STAGE arms-only normal Servo in $LEFT... Ctrl-C to abort."
  sleep 1
done

STAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY="$DM/logs/telemetry/powered/g2_frozen_arm_o10_${STAGE}_normal_${STAMP}.jsonl"
CONSOLE="$G2TELEOP_LOG_DIR/g2_frozen_arm_o10_${STAGE}_normal_${STAMP}.console.log"
mkdir -p "$G2TELEOP_LOG_DIR"

set +e
python3 scripts/21_g2_frozen_arm_stage_replay.py \
  --execute \
  --artifact-npz "$ARTIFACT" \
  --stage "$STAGE" \
  --servo-channel normal \
  --hand-mode recorded-actions \
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
