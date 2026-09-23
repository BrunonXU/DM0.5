#!/usr/bin/env bash
# 用途：运行NewLora RTC端到端Shadow；绝不创建Servo或发送动作。
# 输入：可选请求数与d/s；默认使用原正式7893的native-image binary WebSocket。
# 输出：逐请求RTC时序、可行性和前缀误差JSONL。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

REQUESTS=30
RTC_D=16
RTC_S=20
if (( $# > 0 )) && [[ "$1" =~ ^[0-9]+$ ]]; then
  REQUESTS="$1"
  shift
fi
while (( $# > 0 )); do
  case "$1" in
    --requests)
      REQUESTS="${2:-}"
      shift 2
      ;;
    --d)
      RTC_D="${2:-}"
      shift 2
      ;;
    --s)
      RTC_S="${2:-}"
      shift 2
      ;;
    -h|--help)
      echo "usage: $0 [request-count>=2] [--requests N] [--d 0..25] [--s D..50-D]"
      exit 0
      ;;
    *)
      echo "STOP: 未知参数：$1" >&2
      exit 2
      ;;
  esac
done
if ! [[ "$REQUESTS" =~ ^[0-9]+$ ]] || (( REQUESTS < 2 )); then
  echo "STOP: requests必须是至少2的整数" >&2
  exit 2
fi
if ! [[ "$RTC_D" =~ ^[0-9]+$ && "$RTC_S" =~ ^[0-9]+$ ]] \
  || (( RTC_D > RTC_S || RTC_S > 50 - RTC_D )); then
  echo "STOP: RTC要求0 <= d <= s <= 50-d" >&2
  exit 2
fi

POLICY_TRANSPORT="${NEWLORA_RTC_TRANSPORT:-websocket-binary}"
if [[ "$POLICY_TRANSPORT" == "websocket-binary" ]]; then
  POLICY_ENDPOINT="${NEWLORA_RTC_ENDPOINT:-ws://10.130.153.114:7893/process_frame}"
elif [[ "$POLICY_TRANSPORT" == "http-multipart-json" ]]; then
  POLICY_ENDPOINT="${NEWLORA_RTC_ENDPOINT:-http://10.130.153.114:7893/process_frame}"
else
  echo "STOP: NEWLORA_RTC_TRANSPORT必须是http-multipart-json或websocket-binary" >&2
  exit 2
fi
assert_no_controller_process
check_policy_endpoint
load_g2_environment

LOG="$G2_SHADOW_LOG_DIR/g2_rtc_shadow_d${RTC_D}_s${RTC_S}_$(timestamp).jsonl"
section "运行RTC Shadow（绝不发送Servo）"
echo "服务=$POLICY_ENDPOINT transport=$POLICY_TRANSPORT 请求数=$REQUESTS H=50 d=$RTC_D s=$RTC_S"
echo "本入口只验证RTC模型合同和真实延迟，不创建ServoStreamer。"
python3.12 -m g2_sort.deployment.g2_sdk_client \
  --mode rtc-shadow \
  --policy-endpoint "$POLICY_ENDPOINT" \
  --policy-transport "$POLICY_TRANSPORT" \
  --request-timeout-seconds 10 \
  --rtc-process-isolation \
  --max-requests "$REQUESTS" \
  --rtc-predicted-delay-steps "$RTC_D" \
  --rtc-execution-horizon "$RTC_S" \
  --arm-internal-jerk-weight 10 \
  --arm-interpolation-profile cubic_spline \
  --telemetry-jsonl "$LOG"

section "RTC Shadow完成"
echo "【日志位置】$LOG"
