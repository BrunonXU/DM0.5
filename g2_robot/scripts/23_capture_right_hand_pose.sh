#!/usr/bin/env bash
# 只读记录当前右手（含拇指）构型；原始双手报告与右手参考同时留存。
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
assert_no_controller_process
load_g2_environment
OUT_DIR="$(mktemp -d "$G2_DIAGNOSTIC_LOG_DIR/right_hand_boot_$(timestamp)_XXXXXX")"
echo "保持当前认可的手型；只读10次，不开手、不闭手、不复位。"
python3.12 -m g2_sort.operations.diagnostics.hand_state_check \
  --output-dir "$OUT_DIR" --samples 10 --interval-seconds 0.2
python3.12 -m g2_sort.operations.diagnostics.save_right_hand_pose "$OUT_DIR/hand_state.json"
