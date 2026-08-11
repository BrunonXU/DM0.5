#!/usr/bin/env bash
# 用途：执行一次带严格安全门的短前缀Powered真机诊断，不用于完整任务。
# 输入：1/3/6个30Hz区间、通道，以及可选0.05/0.10/0.20首跳门限。
# 输出：logs/telemetry/powered/下带时间戳JSONL及终端安全状态。
# 方法：端口/进程/授权检查后采集一次、推理一次、最多执行指定前缀。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

STEPS="${1:-}"
CHANNEL="${2:-normal}"
START_JUMP_LIMIT="${3:-0.05}"
case "$STEPS" in
  1|3|6) ;;
  *)
    echo "usage: $0 {1|3|6} [normal|low-latency] [0.05|0.10|0.20]" >&2
    exit 2
    ;;
esac
case "$CHANNEL" in
  normal|low-latency) ;;
  *)
    echo "servo channel must be normal or low-latency" >&2
    exit 2
    ;;
esac
case "$START_JUMP_LIMIT" in
  0.05|0.10|0.20) ;;
  *)
    echo "start jump limit must be 0.05, 0.10, or 0.20 rad" >&2
    exit 2
    ;;
esac
if [[ "$START_JUMP_LIMIT" != "0.05" && "$CHANNEL" != "normal" ]]; then
  echo "STOP: 放宽首跳门只允许normal碰撞保护通道" >&2
  exit 2
fi

assert_no_controller_process
check_endpoint_405
load_g2_environment

section "Powered-sync授权"
echo "执行区间=$STEPS（30Hz） Servo通道=$CHANNEL 首跳门限=${START_JUMP_LIMIT}rad"
echo "硬件下发=Split（双臂14D/100Hz；双手20D/5Hz）"
echo "推理服务=$POLICY_ENDPOINT"
echo "预计模型区间运动时间：约$(awk "BEGIN {printf \"%.3f\", $STEPS/30}")秒"
if [[ "$CHANNEL" == "low-latency" ]]; then
  echo "WARNING: LOW-LATENCY没有GDK碰撞保护。"
else
  if [[ "$START_JUMP_LIMIT" != "0.05" ]]; then
    echo "WARNING: 首跳门已从默认0.05rad放宽到${START_JUMP_LIMIT}rad。"
  fi
fi
read -r -p "确认现场安全并有专人握住物理急停，输入 ok 继续：" ANSWER
if [[ "${ANSWER,,}" != "ok" ]]; then
  echo "STOP: 未输入ok，取消真实动作" >&2
  exit 2
fi

LOG="$G2_POWERED_LOG_DIR/g2_powered_sync_${STEPS}_${CHANNEL}_$(timestamp).jsonl"
section "5秒倒计时；专人握住物理急停"
for remaining in 5 4 3 2 1; do
  echo "$remaining..."
  sleep 1
done

python3.12 -m g2_sort.deployment.g2_sdk_client \
  --mode powered-sync \
  --servo-channel "$CHANNEL" \
  --servo-output-mode split \
  --policy-endpoint "$POLICY_ENDPOINT" \
  --request-timeout-seconds 10 \
  --execute-prefix-steps "$STEPS" \
  --max-requests 1 \
  --max-start-jump-radians "$START_JUMP_LIMIT" \
  --interactive-keys \
  --telemetry-jsonl "$LOG"

section "Powered-sync结束"
echo "【真机推理结束】若动作未执行，请向上查找【首动作跳变超限】或其他红色警告。"
echo "【日志位置】$LOG"
