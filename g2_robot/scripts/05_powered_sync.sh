#!/usr/bin/env bash
# 用途：运行当前正式75K Split同步真机推理。
# 输入：可选--duration-seconds正整数，或--continuous持续到人工/故障结束。
# 输出：中文运行状态及logs/telemetry/powered中的同步JSONL。
# 方法：固定双臂120ms/weight10，并启用双手5Hz因果V1。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/powered_split_runner.sh
source "$SCRIPT_DIR/lib/powered_split_runner.sh"

run_powered_split sync \
  --boundary-blend-seconds 0.12 \
  --arm-internal-jerk-weight 10 \
  --hand-smoothing-profile v1 \
  "$@"
