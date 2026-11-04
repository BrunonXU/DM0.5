#!/usr/bin/env bash
# Frozen source actions, single canonical 34D normal Servo; no model or sender.
set -Eeuo pipefail
DM="${G2_PROJECT_DIR:-/home/user/xza/DM0.5}"
EXECUTE=false
for argument in "$@"; do
  case "$argument" in
    --execute) EXECUTE=true ;;
    *) echo "Usage: $0 [--execute]" >&2; exit 2 ;;
  esac
done
export PYTHONPATH="$DM${PYTHONPATH:+:$PYTHONPATH}"
python3 -m g2_sort.operations.hardware_tests.training_data_replay --unified-34 --prepare-hands --files-only
if [[ "$EXECUTE" != true ]]; then
  echo "No robot output. On G2, add --execute for ENTER + countdown."
  exit 0
fi
source "$DM/scripts/lib/common.sh"
assert_no_controller_process
load_g2_environment
LOG="$G2_DIAGNOSTIC_LOG_DIR/training_replay_unified34_$(timestamp).json"
CONSOLE="${LOG%.json}.console.log"
echo "DATASET REPLAY: sorting_20260409161901, actions[0..947], 30Hz / 31.567s."
echo "Single 34D joint_servo_control normal, 100Hz; hands included, no move_ee_pos."
echo "No model / camera inference / Windows sender. Keep physical E-stop available."
echo "Arm start must match frame 0. After ENTER: if needed, hold arms and prepare hands over 5s; verify before replay."
echo "Report: $LOG"
echo "Console: $CONSOLE"
python3.12 -m g2_sort.operations.hardware_tests.training_data_replay \
  --unified-34 --prepare-hands --execute --report-json "$LOG" 2>&1 | tee "$CONSOLE"
