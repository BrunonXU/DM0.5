#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/agi/xza/G2Teleop
DM=/home/agi/xza/DM0.5
APP=/home/agi/app
DEPLOYMENT="$DM/g2_sort/deployment"
ARTIFACT="$DM/logs/diagnostics/g2_bc_eef_causal_commands_20260902_173215.npz"
EXPECTED_SHA=31537fe91ef96f1a9931bd4d33cedf51f344a757b2a21810ee67978965c54984

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

python3 scripts/28_g2_o10_event_replay.py \
  --artifact-npz "$ARTIFACT" \
  --arm-mode q-direct \
  --hand-execution raw-5hz-changes

echo
echo "TIMING-FAITHFUL REPLAY: frozen Q-direct arms + raw changed O10 samples at 5 Hz."
echo "First 2 s: arms hold the Q-direct start while O10 receives the dataset initial target."
echo "Then arms replay at 100 Hz normal Servo; hands retain the dataset's gradual 1-3 s motion."
echo "Unchanged 5 Hz hand samples are not resent. No v1_1 hand tracker is used."
echo "This preserves data timing but may reveal renewed O10 retargeting stutter."
echo "No online Pink/Ruckig, network/model, waist, or chassis output. Total duration 93.22 s."
echo "Keep the physical E-stop continuously available."
read -r -p "Press Enter to start the countdown (Ctrl-C to abort): " _
for LEFT in 5 4 3 2 1; do
  echo "Starting Q-direct + raw 5 Hz O10 in $LEFT... Ctrl-C to abort."
  sleep 1
done

STAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY="$DM/logs/telemetry/powered/g2_qdirect_o10_raw5_${STAMP}.jsonl"
CONSOLE="$ROOT/logs/g2_qdirect_o10_raw5_${STAMP}.console.log"
mkdir -p "$ROOT/logs"
set +e
python3 scripts/28_g2_o10_event_replay.py \
  --execute \
  --artifact-npz "$ARTIFACT" \
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
