#!/usr/bin/env bash
# 用途：隔离验证NewLora RTC双臂连续性；固定约10秒且绝不发送灵巧手命令。
# 输入：无；不支持continuous或自定义时长。
# 输出：RTC切换、安全门和14D双臂实际下发JSONL。
set -Eeuo pipefail

if (( $# > 0 )); then
  echo "STOP: 本入口没有参数，只允许固定10秒arms-only验证" >&2
  echo "usage: $0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

POLICY_TRANSPORT="${NEWLORA_RTC_TRANSPORT:-websocket-binary}"
if [[ "$POLICY_TRANSPORT" == "websocket-binary" ]]; then
  POLICY_ENDPOINT="${NEWLORA_RTC_ENDPOINT:-ws://192.0.2.1:7893/process_frame}"
elif [[ "$POLICY_TRANSPORT" == "http-multipart-json" ]]; then
  POLICY_ENDPOINT="${NEWLORA_RTC_ENDPOINT:-http://192.0.2.1:7893/process_frame}"
else
  echo "STOP: NEWLORA_RTC_TRANSPORT必须是http-multipart-json或websocket-binary" >&2
  exit 2
fi
REQUESTS=11

assert_no_controller_process
check_policy_endpoint
load_g2_environment

section "NewLora RTC双臂限定10秒验证"
echo "服务=$POLICY_ENDPOINT H=50 d=16 s=20 guidance=0.5"
echo "执行范围：仅14D双臂；硬件层不创建手线程、不调用move_ee_pos"
echo "时序：提前响应缓存到d=16；C0/C1投影；仅arm suffix做jerk(weight=10)+C2 spline"
echo "安全门：首跳0.05rad、clip0.02rad及arm动力学全部hard reject"
echo "双臂通道：LOW-LATENCY（无GDK碰撞保护）"
echo "时长：固定约10秒，最多$REQUESTS次请求；不支持continuous"
echo "要求：清空双臂工作空间，专人全程握住物理急停。"
read -r -p "确认双手保持当前姿态、现场安全，输入 arms-only 继续：" answer
if [[ "${answer,,}" != "arms-only" ]]; then
  echo "STOP: 未输入arms-only，取消真实动作" >&2
  exit 2
fi

log="$G2_POWERED_LOG_DIR/g2_powered_rtc_arms_only_10s_$(timestamp).jsonl"
section "5秒倒计时；专人握住物理急停"
for remaining in 5 4 3 2 1; do
  echo "$remaining..."
  sleep 1
done

python3.12 -m g2_sort.deployment.g2_sdk_client \
  --mode powered-rtc \
  --servo-channel low-latency \
  --servo-output-mode arms-only \
  --policy-endpoint "$POLICY_ENDPOINT" \
  --policy-transport "$POLICY_TRANSPORT" \
  --rtc-process-isolation \
  --request-timeout-seconds 10 \
  --rtc-predicted-delay-steps 16 \
  --rtc-execution-horizon 20 \
  --max-requests "$REQUESTS" \
  --boundary-blend-seconds 0.12 \
  --boundary-blend-profile cubic_smoothstep \
  --arm-internal-jerk-weight 10 \
  --arm-interpolation-profile cubic_spline \
  --max-start-jump-radians 0.05 \
  --max-clip-delta-radians 0.02 \
  --max-servo-send-seconds 0.20 \
  --max-servo-tick-lateness-seconds 0.20 \
  --interactive-keys \
  --telemetry-jsonl "$log"

section "RTC双臂限定验证结束"
echo "【日志位置】$log"
echo "本入口从未发送双手命令；结束不自动执行Home复位。"
echo "STOP: 先分析日志，禁止继续运行09或任何continuous入口。"
