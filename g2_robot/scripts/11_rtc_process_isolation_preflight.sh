#!/usr/bin/env bash
# 用途：只读验证Control唯一Robot、Policy唯一Camera和共享34D state ring。
# 输出：一轮三相机+Control state+所选策略端点请求；绝不创建Servo或发送动作。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

POLICY_TRANSPORT="${G2_PREFLIGHT_POLICY_TRANSPORT:-${NEWLORA_RTC_TRANSPORT:-websocket-binary}}"
if [[ -n "${G2_PREFLIGHT_POLICY_ENDPOINT:-}" ]]; then
  POLICY_ENDPOINT="$G2_PREFLIGHT_POLICY_ENDPOINT"
elif [[ "$POLICY_TRANSPORT" == "websocket-binary" ]]; then
  POLICY_ENDPOINT="${NEWLORA_RTC_ENDPOINT:-ws://10.130.153.114:7893/process_frame}"
else
  if [[ "$POLICY_TRANSPORT" != "http-multipart-json" ]]; then
    echo "STOP: POLICY_TRANSPORT必须是http-multipart-json或websocket-binary" >&2
    exit 2
  fi
  POLICY_ENDPOINT="${NEWLORA_RTC_ENDPOINT:-http://10.130.153.114:7893/process_frame}"
fi

assert_no_controller_process
check_policy_endpoint
load_g2_environment

section "RTC单Robot-owner双进程只读Preflight"
echo "策略进程：三相机、state ring只读、协议=${POLICY_TRANSPORT}、端点=${POLICY_ENDPOINT}；不创建Robot"
echo "Control进程：唯一Robot owner，按请求采样34D state并写共享ring"
echo "硬件输出：0；不创建ServoStreamer，不调用joint_servo_control/move_ee_pos"

result="$G2_RUNTIME_LOG_DIR/rtc_process_isolation_preflight.json"
python3.12 -m g2_sort.operations.diagnostics.rtc_process_isolation_preflight \
  --policy-endpoint "$POLICY_ENDPOINT" \
  --policy-transport "$POLICY_TRANSPORT" \
  --request-timeout-seconds 10 \
  --camera-timeout-ms 200 \
  --gdk-stabilization-seconds 3 \
  --output-json "$result"

section "RTC单Robot-owner Preflight通过"
echo "【结果】$result"
