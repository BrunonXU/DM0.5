#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
ROOT="$G2TELEOP_ROOT"
DM=/home/agi/xza/DM0.5
STAMP=$(date +%Y%m%d_%H%M%S)
TELEMETRY="$DM/logs/telemetry/shadow/g2_bc_eef_causal_commands_${STAMP}.jsonl"
ARTIFACT="$DM/logs/diagnostics/g2_bc_eef_causal_commands_${STAMP}.npz"

cd "$ROOT"
export PYTHONPATH="$ROOT/vendor_ik:$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

python3 scripts/20_g2_bc_eef_chain_replay.py \
  --preflight-files-only \
  --timeline velocity-limited \
  --path-source states

echo
echo "Generating the common-clock Q-direct/Q-pink/Q-otg artifact OFFLINE."
echo "There is no GDK import or hardware output in this script."

mkdir -p "$G2TELEOP_LOG_DIR"
CONSOLE_LOG="$G2TELEOP_LOG_DIR/bc_eef_causal_commands_console.log"
set +e
python3 scripts/20_g2_bc_eef_chain_replay.py \
  --timeline velocity-limited \
  --path-source states \
  --offline-artifact-npz "$ARTIFACT" \
  --telemetry-jsonl "$TELEMETRY" 2>&1 | (trap '' INT; tee -a "$CONSOLE_LOG")
STATUS=${PIPESTATUS[0]}
set -e

echo "Stored telemetry: $TELEMETRY"
echo "Stored causal artifact: $ARTIFACT"
exit "$STATUS"
