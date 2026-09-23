#!/usr/bin/env bash
# 右手逐指发送名修正候选；默认食指，每次只测一指。
set -Eeuo pipefail
finger=index
execute_args=()
while (( $# )); do
  case "$1" in
    --execute) execute_args=(--execute); shift ;;
    --finger)
      if (( $# < 2 )); then echo "--finger requires index|middle|ring|pinky" >&2; exit 2; fi
      finger="$2"; shift 2 ;;
    *) echo "usage: $0 [--finger index|middle|ring|pinky] [--execute]" >&2; exit 2 ;;
  esac
done
case "$finger" in
  index) finger_label=食指 ;;
  middle) finger_label=中指 ;;
  ring) finger_label=无名指 ;;
  pinky) finger_label=小指 ;;
  *) echo "invalid finger: $finger" >&2; exit 2 ;;
esac
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
assert_no_controller_process
load_g2_environment
report="$G2_DIAGNOSTIC_LOG_DIR/right_${finger}_canonical_$(timestamp).json"
echo "右手空载、手指有活动空间；专人握住物理急停。"
echo "仅右17D/normal：右${finger_label}PIP +0.20rad，1秒去、1秒保持、1秒回、1秒保持。"
echo "右臂及其余手指保持当前反馈；记录右17轴反馈；不调用推理或相机。"
echo "异常时停止发送，不自动复位或返回；看到其他指/手臂异常运动立即急停。"
python3.12 -m g2_sort.operations.hardware_tests.single_side_17d_servo_probe \
  --side right --motion-profile right-finger-pip-canonical-visible --finger "$finger" \
  --report "$report" "${execute_args[@]}"
