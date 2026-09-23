#!/usr/bin/env bash
# 同一canonical目标对比单34D与双17D；必须由现场操作者执行。
set -Eeuo pipefail
layout=unified-34
motion=static
execute_args=()
while (( $# )); do
  case "$1" in
    --execute) execute_args=(--execute); shift ;;
    --layout|--motion)
      if (( $# < 2 )); then echo "missing value for $1" >&2; exit 2; fi
      if [[ "$1" == --layout ]]; then layout="$2"; else motion="$2"; fi
      shift 2 ;;
    *) echo "usage: $0 [--layout unified-34|dual-side-17d] [--motion static|left-index|right-index] [--execute]" >&2; exit 2 ;;
  esac
 done
case "$layout" in unified-34|dual-side-17d) ;; *) echo "invalid layout" >&2; exit 2 ;; esac
case "$motion" in static|left-index|right-index) ;; *) echo "invalid motion" >&2; exit 2 ;; esac
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
assert_no_controller_process
load_g2_environment
report="$G2_DIAGNOSTIC_LOG_DIR/layout_${layout}_${motion}_$(timestamp).json"
echo "normal/100Hz，双臂双手空载且有活动空间；专人握住物理急停。"
echo "布局=$layout 动作=$motion；static保持当前位置1秒，index为指定食指+0.20rad往返4秒。"
echo "其余轴保持当前姿态；异常立即急停，不自动返回/复位；每次单独确认。"
python3.12 -m g2_sort.operations.hardware_tests.servo_layout_probe \
  --layout "$layout" --motion "$motion" --report "$report" "${execute_args[@]}"
