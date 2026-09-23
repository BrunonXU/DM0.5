#!/usr/bin/env bash
# 用途：验证官方单侧7D手臂+动态10D O10的normal 100Hz Servo合同。
# 输入：默认只读预览；唯一动作参数--execute，固定左侧当前位置保持1秒。
# 输出：logs/diagnostics中的JSON报告；不连接模型、不控制右侧。
set -Eeuo pipefail

if (( $# > 1 )) || (( $# == 1 )) && [[ "$1" != "--execute" ]]; then
  echo "usage: $0 [--execute]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

assert_no_controller_process
load_g2_environment

report="$G2_DIAGNOSTIC_LOG_DIR/single_side_17d_servo_left_$(timestamp).json"
echo "侧别：仅左侧；通道：normal（保留GDK碰撞保护）"
echo "目标：5次新鲜反馈确认静止后，原样拼接左臂7D与get_end_state动态10D名称/位置"
if (( $# == 0 )); then
  echo "模式：只读预览；不会调用joint_servo_control"
else
  echo "模式：Powered静态探针；100Hz保持当前位置1秒，不发送模型轨迹"
fi

python3.12 -m g2_sort.operations.hardware_tests.single_side_17d_servo_probe \
  --side left \
  --motion-profile static \
  --report "$report" \
  "$@"
