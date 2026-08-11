#!/usr/bin/env bash
# 用途：运行端到端DM0.5 Shadow验证，保证机器人不接收Servo动作。
# 输入：可选正整数请求次数及双臂内部jerk权重；服务地址来自POLICY_ENDPOINT。
# 输出：logs/telemetry/shadow/下唯一带时间戳的JSONL。
# 方法：通过端口和旧进程检查后运行正式客户端的shadow状态机。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

REQUESTS=10
ARM_INTERNAL_JERK_WEIGHT=0
if (( $# > 0 )) && [[ "$1" != -* ]]; then
  REQUESTS="$1"
  shift
fi
while (( $# > 0 )); do
  case "$1" in
    --arm-internal-jerk-weight)
      if (( $# < 2 )); then
        echo "STOP: --arm-internal-jerk-weight缺少数值" >&2
        exit 2
      fi
      ARM_INTERNAL_JERK_WEIGHT="$2"
      shift 2
      ;;
    --arm-internal-jerk-weight=*)
      ARM_INTERNAL_JERK_WEIGHT="${1#*=}"
      shift
      ;;
    -h|--help)
      echo "usage: $0 [positive-request-count] [--arm-internal-jerk-weight 0..100]"
      exit 0
      ;;
    *)
      echo "STOP: 未知参数：$1" >&2
      echo "usage: $0 [positive-request-count] [--arm-internal-jerk-weight 0..100]" >&2
      exit 2
      ;;
  esac
done
if ! [[ "$REQUESTS" =~ ^[1-9][0-9]*$ ]]; then
  echo "usage: $0 [positive-request-count] [--arm-internal-jerk-weight 0..100]" >&2
  exit 2
fi
if [[ ! "$ARM_INTERNAL_JERK_WEIGHT" =~ ^(0|[0-9]+)(\.[0-9]+)?$ ]] \
  || ! awk "BEGIN {exit !($ARM_INTERNAL_JERK_WEIGHT >= 0 && $ARM_INTERNAL_JERK_WEIGHT <= 100)}"; then
  echo "STOP: --arm-internal-jerk-weight必须在0到100之间" >&2
  exit 2
fi

assert_no_controller_process
check_endpoint_405
load_g2_environment

LOG="$G2_SHADOW_LOG_DIR/g2_shadow_$(timestamp).jsonl"
section "运行Shadow（绝不发送Servo）"
echo "即将连续推理${REQUESTS}次；每次成功都会显示延迟、首跳和限位裁剪。"
echo "双臂chunk内部jerk正则权重：${ARM_INTERNAL_JERK_WEIGHT}（Shadow零硬件发送）"
echo "看到【推理成功】表示服务器已返回严格50×34动作。"
python3.12 -m g2_sort.deployment.g2_sdk_client \
  --mode shadow \
  --policy-endpoint "$POLICY_ENDPOINT" \
  --request-timeout-seconds 10 \
  --max-requests "$REQUESTS" \
  --arm-internal-jerk-weight "$ARM_INTERNAL_JERK_WEIGHT" \
  --telemetry-jsonl "$LOG"

section "Shadow完成"
echo "【Shadow完成】请重点检查：请求数、P95延迟、首跳关节、最大裁剪。"
echo "【日志位置】$LOG"
