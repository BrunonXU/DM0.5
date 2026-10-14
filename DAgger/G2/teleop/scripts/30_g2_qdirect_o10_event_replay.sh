#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
ROOT="$G2TELEOP_ROOT"
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
  --hand-execution event-endpoint

echo
echo "COMBINED REPLAY: frozen Q-direct arms + event-driven O10 hands."
echo "First 2 s: arms hold the Q-direct start while O10 receives one open target."
echo "Then arms replay at 100 Hz normal Servo; each of 10 hand action windows submits one endpoint."
echo "No 10 Hz O10 refresh, online Pink/Ruckig, network/model, waist, or chassis output."
echo "Total powered duration is 93.22 s. Keep the physical E-stop continuously available."
read -r -p "Press Enter to start the countdown (Ctrl-C to abort): " _
for LEFT in 5 4 3 2 1; do
  echo "Starting Q-direct + event O10 in $LEFT... Ctrl-C to abort."
  sleep 1
done

STAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY="$DM/logs/telemetry/powered/g2_qdirect_o10_event_${STAMP}.jsonl"
CONSOLE="$G2TELEOP_LOG_DIR/g2_qdirect_o10_event_${STAMP}.console.log"
mkdir -p "$G2TELEOP_LOG_DIR"
set +e
python3 scripts/28_g2_o10_event_replay.py \
  --execute \
  --artifact-npz "$ARTIFACT" \
  --arm-mode q-direct \
  --hand-execution event-endpoint \
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
