#!/usr/bin/env bash
# 用途：以正式75K Sync的相同手臂配置做固定10秒arms-only因果隔离。
# 输入：无；固定约10秒，不支持continuous。
# 输出：仅14D双臂下发日志；硬件层不创建手线程且不调用move_ee_pos。
set -Eeuo pipefail

if (( $# > 0 )); then
  echo "STOP: 本入口没有参数，只允许固定10秒Sync arms-only验证" >&2
  echo "usage: $0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/powered_split_runner.sh
source "$SCRIPT_DIR/lib/powered_split_runner.sh"

NEWLORA_SYNC_TRANSPORT=websocket-binary \
NEWLORA_SYNC_ENDPOINT="${NEWLORA_SYNC_ENDPOINT:-ws://10.130.153.114:7892/process_frame}" \
run_powered_split sync \
  --duration-seconds 10 \
  --arms-only \
  --boundary-blend-seconds 0.12 \
  --arm-internal-jerk-weight 10 \
  --arm-interpolation-profile cubic_spline \
  --arm-micro-motion-profile stationary_minjerk_v1 \
  --arm-micro-motion-threshold-rad 0.0035 \
  --hand-smoothing-profile off
