#!/usr/bin/env bash
# 用途：只读验证Sync原生三图+binary WebSocket候选，不发送任何动作。
# 输入：可选正整数请求数，默认400；使用与正式Sync相同的7892，只读且不发送动作。
# 输出：logs/telemetry/shadow中的分段延迟与严格50x34动作合同。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

REQUESTS="${1:-400}"
if ! [[ "$REQUESTS" =~ ^[1-9][0-9]*$ ]]; then
  echo "usage: $0 [positive-request-count]" >&2
  exit 2
fi

POLICY_TRANSPORT=websocket-binary
POLICY_ENDPOINT="${NEWLORA_SYNC_ENDPOINT:-ws://10.130.153.114:7892/process_frame}"

assert_no_controller_process
check_policy_endpoint
load_g2_environment

LOG="$G2_SHADOW_LOG_DIR/g2_shadow_sync_binary_${REQUESTS}_$(timestamp).jsonl"
section "Sync binary WebSocket Shadow（严格零硬件输出）"
echo "服务：$POLICY_ENDPOINT"
echo "请求：$REQUESTS；原生GDK JPEG/PNG；持久WebSocket；50x34 float32响应"
python3.12 -m g2_sort.deployment.g2_sdk_client \
  --mode shadow \
  --policy-endpoint "$POLICY_ENDPOINT" \
  --policy-transport "$POLICY_TRANSPORT" \
  --rtc-process-isolation \
  --request-timeout-seconds 10 \
  --max-requests "$REQUESTS" \
  --telemetry-jsonl "$LOG"

section "Sync binary Shadow完成"
echo "【日志位置】$LOG"
echo "【验收】Servo发送必须为0；可与历史7892 HTTP Shadow比较E2E P50/P95/P99。"
