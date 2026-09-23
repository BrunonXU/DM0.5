#!/usr/bin/env bash
# 用途：确认左食指PIP在17D Servo下产生肉眼可见的受控运动。
# 输入：默认只读预览；唯一动作参数--execute。
# 输出：+0.20rad平滑运动、峰值保持1秒、平滑返回、基线保持1秒。
set -Eeuo pipefail

if (( $# > 1 )) || { (( $# == 1 )) && [[ "$1" != "--execute" ]]; }; then
  echo "usage: $0 [--execute]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

assert_no_controller_process
load_g2_environment

report="$G2_DIAGNOSTIC_LOG_DIR/single_side_17d_visible_left_$(timestamp).json"
echo "侧别：仅左侧；通道：normal（保留GDK碰撞保护）"
echo "轨迹：左食指PIP +0.20rad，峰值保持1秒，再平滑返回并保持1秒；总4秒/100Hz"
echo "左臂7轴及其他9个手指轴保持当前位置；不控制右侧"
if (( $# == 0 )); then
  echo "模式：只读预览；不会调用joint_servo_control"
else
  echo "模式：Powered可见运动探针；专人握住物理急停"
fi

python3.12 -m g2_sort.operations.hardware_tests.single_side_17d_servo_probe \
  --side left \
  --motion-profile left-index-pip-visible \
  --report "$report" \
  "$@"
