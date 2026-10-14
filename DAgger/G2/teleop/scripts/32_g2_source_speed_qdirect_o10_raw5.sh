#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
ROOT="$G2TELEOP_ROOT"
DM=/home/agi/xza/DM0.5
APP=/home/agi/app
DEPLOYMENT="$DM/g2_sort/deployment"
REPLAY="$DM/g2_sort/config/training_replay_v1.json"
EXPECTED_SHA=28233ac288e6fdbeb552dc44bc491817eea31c2f6b99949c8080b2068f799f3d

cd "$ROOT"
export PYTHONPATH="$APP/gdk/lib:$ROOT/vendor_ik:$ROOT/src:$DM:$DEPLOYMENT${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$APP/lib:$APP/gdk/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export APP_CONF_PATH="$APP/gdk/config/app_conf.yml"
export AORTA_DISCOVERY_URI=http://10.42.1.101:2379
export AORTA_DISPATCHER_THREAD_NUM=6

ACTUAL_SHA=$(sha256sum "$REPLAY" | awk '{print $1}')
if [[ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]]; then
  echo "HOLD: source replay SHA mismatch: $ACTUAL_SHA" >&2
  exit 2
fi
G2_LOCATOR_IP=$(ip -o -4 addr list | awk '$4 ~ /^10\.42\.1\./ {sub(/\/.*/, "", $4); print $4; exit}')
if [[ -z "$G2_LOCATOR_IP" ]]; then
  echo "HOLD: no existing 10.42.1.x G2 locator address" >&2
  exit 2
fi
export LOCATOR_IP="$G2_LOCATOR_IP"

python3 scripts/28_g2_o10_event_replay.py \
  --source-replay-json "$REPLAY" \
  --arm-mode q-direct \
  --hand-execution raw-5hz-changes

echo
echo "SOURCE-SPEED REPLAY: measured-state arms at the original 30 Hz data timing."
echo "Arms are interpolated to 100 Hz normal Servo without the prior 3x timeline stretch."
echo "Hands use raw action targets at 5 Hz and skip unchanged samples; no v1_1 tracker."
echo "First 2 s prepares the source start; replay plus 1 s terminal hold is 32.57 s."
echo "Total powered duration is 34.57 s, not the prior 93.22 s stretched timeline."
echo "No online Pink/Ruckig, network/model, waist, or chassis output."
echo "Keep the physical E-stop continuously available."
read -r -p "Press Enter to start the countdown (Ctrl-C to abort): " _
for LEFT in 5 4 3 2 1; do
  echo "Starting source-speed arms + raw 5 Hz O10 in $LEFT... Ctrl-C to abort."
  sleep 1
done

STAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY="$DM/logs/telemetry/powered/g2_source_speed_o10_raw5_${STAMP}.jsonl"
CONSOLE="$G2TELEOP_LOG_DIR/g2_source_speed_o10_raw5_${STAMP}.console.log"
mkdir -p "$G2TELEOP_LOG_DIR"
set +e
python3 scripts/28_g2_o10_event_replay.py \
  --execute \
  --source-replay-json "$REPLAY" \
  --arm-mode q-direct \
  --hand-execution raw-5hz-changes \
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
