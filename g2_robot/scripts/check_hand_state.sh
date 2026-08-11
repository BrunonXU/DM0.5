#!/usr/bin/env bash
# 用途：只读检查O10双手原始位置、控制状态和每关节健康字段。
# 输入：可选采样次数和采样间隔，默认10次、0.2秒。
# 输出：logs/diagnostics/hand_state_<时间>/hand_state.json及中文摘要。
# 方法：只调用GDK状态接口；不检查HTTP、不构造请求、绝不发送Servo。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SAMPLES="${1:-10}"
INTERVAL_SECONDS="${2:-0.2}"

assert_no_controller_process
load_g2_environment

OUT_DIR="$G2_DIAGNOSTIC_LOG_DIR/hand_state_$(timestamp)"
mkdir -p "$OUT_DIR"

section "O10双手原始状态只读检查"
echo "连续采样${SAMPLES}次，间隔${INTERVAL_SECONDS}秒；绝不发送Servo。"
python3.12 -m g2_sort.operations.diagnostics.hand_state_check \
  --output-dir "$OUT_DIR" \
  --samples "$SAMPLES" \
  --interval-seconds "$INTERVAL_SECONDS"

section "只读检查结束"
echo "【报告位置】$OUT_DIR/hand_state.json"
echo "【注意】通过只代表状态字段完整，不代表Servo或Powered可运行。"
