#!/usr/bin/env bash
# 用途：预览或执行G2双手+双臂训练初始Home复位。
# 输入：默认只读；--execute要求VLA已退出、最后成功手指命令记录有效并按ENTER确认。
# 输出：双手20D和双臂14D目标预览，或完整Split复位进度。
# 方法：先让双臂100Hz用5秒回H5第0帧中位数，再让双手5Hz最多8步回批准Home。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

section "G2双手+双臂Home"
execute_requested=false
for argument in "$@"; do
  if [[ "$argument" == "--execute" ]]; then
    execute_requested=true
  fi
done
if [[ "$execute_requested" == true ]]; then
  assert_no_controller_process
  load_g2_environment
  section "Split Home真实复位"
  echo "顺序：双臂100Hz用5秒回H5初始姿态 → O10双手5Hz最多8步回Home。"
  echo "LOW-LATENCY没有GDK碰撞保护；清空运动空间并由专人握住物理急停。"
  echo "程序先等待DDS/GDK稳定并显示双臂最大行程；按ENTER后倒数5秒开始。"
  echo "最后成功手指命令不再按文件年龄拒绝；仍校验20D、finite、批准Home和分步限幅。"
else
  echo "默认只读：显示双手20D和双臂14D固定Home，不初始化GDK、不发送动作。"
fi
python3.12 -m g2_sort.operations.recovery.hand_home "$@"
