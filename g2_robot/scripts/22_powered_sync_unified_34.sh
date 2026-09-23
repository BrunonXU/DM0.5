#!/usr/bin/env bash
# 用途：完整DM0.5双臂双手连续Servo同步推理。
# 输入：可选--duration-seconds正整数，或--continuous持续运行。
# 输出：单次canonical名称34D normal Servo请求/100Hz tick；绝不调用move_ee_pos。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/powered_split_runner.sh
source "$SCRIPT_DIR/lib/powered_split_runner.sh"

NEWLORA_SYNC_TRANSPORT=websocket-binary \
NEWLORA_SYNC_ENDPOINT="${NEWLORA_SYNC_ENDPOINT:-ws://10.130.153.114:7892/process_frame}" \
run_powered_split sync \
  --unified-34 \
  --boundary-blend-seconds 0.12 \
  --arm-internal-jerk-weight 10 \
  --arm-interpolation-profile cubic_spline \
  --arm-micro-motion-profile stationary_minjerk_v1 \
  --arm-micro-motion-threshold-rad 0.0035 \
  --hand-smoothing-profile off \
  "$@"
