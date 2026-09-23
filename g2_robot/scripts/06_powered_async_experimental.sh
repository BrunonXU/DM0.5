#!/usr/bin/env bash
# 用途：运行75K Split异步实验，不作为当前默认真机路径。
# 输入：可选--duration-seconds正整数，默认60秒模型动作。
# 输出：中文运行状态及logs/telemetry/powered中的异步JSONL。
# 方法：调用统一runner；旧chunk剩20步时预取并跳过新chunk陈旧前缀；双臂显式C2 spline。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/powered_split_runner.sh
source "$SCRIPT_DIR/lib/powered_split_runner.sh"

run_powered_split async \
  --arm-interpolation-profile cubic_spline \
  "$@"
