#!/usr/bin/env bash
# 用途：只读预览官方O10 move_ee_pos当前位置请求；真实执行当前锁定。
# 输入：left/right/dual侧别。
# 输出：中文验收结果与logs/diagnostics/o10_move_ee_<时间>/report.json。
# 方法：构造但不发送请求；--execute会被Python安全锁直接拒绝。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SIDE="${1:-left}"
if [[ "$SIDE" != "left" && "$SIDE" != "right" && "$SIDE" != "dual" ]]; then
  echo "usage: $0 {left|right|dual}" >&2
  exit 2
fi
shift || true

assert_no_controller_process
load_g2_environment

OUT_DIR="$G2_DIAGNOSTIC_LOG_DIR/o10_move_ee_$(timestamp)"
mkdir -p "$OUT_DIR"

section "O10官方move_ee_pos单次当前位置验证"
echo "侧别=$SIDE；当前只允许预览，--execute已软件锁定。"
python3.12 -m g2_sort.operations.hardware_tests.o10_move_ee_smoke \
  --side "$SIDE" \
  --report-path "$OUT_DIR/report.json" \
  "$@"

section "验证结束"
echo "【报告位置】$OUT_DIR/report.json"
