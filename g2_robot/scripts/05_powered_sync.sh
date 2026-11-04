#!/usr/bin/env bash
# 用途：运行当前正式75K Split同步真机推理。
# 输入：可选--duration-seconds正整数，或--continuous持续到人工/故障结束。
# 输出：中文运行状态及logs/telemetry/powered中的同步JSONL。
# 方法：native JPEG/PNG + binary WebSocket；固定双臂120ms/weight10/C2 spline，
# 并启用3.5mrad最终100Hz近静止minimum-jerk投影；
# 双手直接保留模型原始20D轨迹，以5Hz采样并只提交变化目标；不再使用preview/QP重塑手部轨迹。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/powered_split_runner.sh
source "$SCRIPT_DIR/lib/powered_split_runner.sh"

NEWLORA_SYNC_TRANSPORT=websocket-binary \
NEWLORA_SYNC_ENDPOINT="${NEWLORA_SYNC_ENDPOINT:-ws://192.0.2.1:7892/process_frame}" \
run_powered_split sync \
  --boundary-blend-seconds 0.12 \
  --arm-internal-jerk-weight 10 \
  --arm-interpolation-profile cubic_spline \
  --arm-micro-motion-profile stationary_minjerk_v1 \
  --arm-micro-motion-threshold-rad 0.0035 \
  --hand-smoothing-profile off \
  "$@"
