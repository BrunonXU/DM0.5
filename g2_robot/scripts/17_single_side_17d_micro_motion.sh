#!/usr/bin/env bash
# 用途：在静态17D通过后验证左食指PIP的100Hz连续微动。
# 输入：默认只读预览；唯一动作参数--execute。
# 输出：左食指+0.05rad minimum-jerk后返回，其余16轴保持；不连接模型。
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

report="$G2_DIAGNOSTIC_LOG_DIR/single_side_17d_micro_left_$(timestamp).json"
echo "侧别：仅左侧；通道：normal（保留GDK碰撞保护）"
echo "轨迹：左食指PIP从新鲜反馈+0.05rad，再以minimum-jerk返回；总2秒/100Hz"
echo "左臂7轴及其他9个手指轴保持当前位置；不控制右侧"
if (( $# == 0 )); then
  echo "模式：只读预览；不会调用joint_servo_control"
else
  echo "模式：Powered微动探针；专人握住物理急停"
fi

python3.12 -m g2_sort.operations.hardware_tests.single_side_17d_servo_probe \
  --side left \
  --motion-profile left-index-pip-minjerk \
  --report "$report" \
  "$@"
