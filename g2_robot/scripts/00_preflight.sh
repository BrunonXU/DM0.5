#!/usr/bin/env bash
# 用途：在任何真机动作前检查端口、GDK、34D状态和三路相机。
# 输入：POLICY_ENDPOINT等公共环境变量，无位置参数。
# 输出：logs/diagnostics/preflight_<时间>/中的JSON报告与三张JPEG。
# 方法：先检查控制进程和HTTP 405，再调用只读Python诊断；绝不发送Servo。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

assert_no_controller_process
check_endpoint_405
load_g2_environment

OUT_DIR="$G2_DIAGNOSTIC_LOG_DIR/preflight_$(timestamp)"
mkdir -p "$OUT_DIR"

section "只读检查GDK、34D和三路相机"
python3.12 -m g2_sort.operations.diagnostics.preflight \
  --output-dir "$OUT_DIR"

section "验收结果"
echo "【预检通过】推理端口、GDK、34D状态和三路相机均已通过。"
echo "【报告位置】$OUT_DIR/preflight.json"
echo "【图片位置】$OUT_DIR/{head,left_wrist,right_wrist}.jpg"
