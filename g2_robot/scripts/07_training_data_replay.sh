#!/usr/bin/env bash
# 用途：预览或完整回放一条冻结训练episode的双臂轨迹，隔离模型与本体问题。
# 输入：默认只读双臂；--with-hands选择无V1/无平滑Split；--execute才真实下发。
# 输出：终端动作合同/跟踪误差；执行时写logs/diagnostics下JSON报告。
# 方法：不调用DM0.5；固定normal碰撞保护；双手完全不下发，因此V1不参与。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

EXECUTE=false
WITH_HANDS=false
for argument in "$@"; do
  case "$argument" in
    --execute) EXECUTE=true ;;
    --with-hands) WITH_HANDS=true ;;
    *)
      echo "usage: $0 [--with-hands] [--execute]" >&2
      exit 2
      ;;
  esac
done

assert_no_controller_process
load_g2_environment

section "完整冻结训练数据回放"
echo "episode=sorting_20260409161901 split=train frames=0..947"
echo "时长约31.567秒；推理服务=不使用；模型=不使用"
if [[ "$WITH_HANDS" == true ]]; then
  echo "输出=split；Servo=normal碰撞保护；双手=原始5Hz；V1=off；平滑=off"
  echo "WARNING: 本episode原始5Hz手指目标最大单步约0.498rad。"
  echo "O10全零/近零反馈不作为物理起点；此时直接以数采frame-0命令锚定原样回放。"
else
  echo "输出=arms-only；Servo=normal碰撞保护；双手命令=完全禁用；V1=不参与"
fi
if [[ "$EXECUTE" == false ]]; then
  echo "当前为PREVIEW：只读取G2状态并检查门禁，不发送任何动作。"
fi

COMMAND=(
  python3.12 -m g2_sort.operations.hardware_tests.training_data_replay
)
if [[ "$WITH_HANDS" == true ]]; then
  COMMAND+=(--with-hands)
fi
if [[ "$EXECUTE" == true ]]; then
  if [[ "$WITH_HANDS" == true ]]; then
    LOG="$G2_DIAGNOSTIC_LOG_DIR/training_replay_split_raw_full_$(timestamp).json"
  else
    LOG="$G2_DIAGNOSTIC_LOG_DIR/training_replay_arms_full_$(timestamp).json"
  fi
  COMMAND+=(--execute --report-json "$LOG")
fi
"${COMMAND[@]}"

if [[ "$EXECUTE" == true ]]; then
  section "训练数据回放结束"
  echo "日志位置=$LOG"
  echo "请记录：是否到达瓶子、是否仍向下压纸盒、交接时双手是否接近碰撞。"
fi
