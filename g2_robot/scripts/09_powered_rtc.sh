#!/usr/bin/env bash
# TODO(RTC_UNIFIED_34D_MIGRATION): migrate RTC hardware output to canonical single-34D normal Servo at 100Hz; preserve one Control Robot owner. Pending implementation/validation; current RTC behavior unchanged.
# 用途：连续运行NewLora 75K的Split Powered RTC真机控制。
# 输入：可选--duration-seconds正整数，或--continuous持续到人工/故障结束。
# 输出：RTC请求、真实延迟、切换接缝和实际双臂/双手下发JSONL。
# 方法：native JPEG/PNG + binary WebSocket；H=50、d=16、s=20，旧chunk在
# 推理时继续执行；双臂固定weight10/C2 spline，双手暂保留RTC cap V2等待共享轨迹器。
set -Eeuo pipefail

RTC_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$RTC_SCRIPT_DIR/lib/common.sh"

duration_seconds=60
duration_explicit=false
continuous=false
arm_interpolation_profile=cubic_spline
while (( $# > 0 )); do
  case "$1" in
    --duration-seconds)
      if (( $# < 2 )); then
        echo "STOP: --duration-seconds缺少数值" >&2
        exit 2
      fi
      duration_seconds="$2"
      duration_explicit=true
      shift 2
      ;;
    --duration-seconds=*)
      duration_seconds="${1#*=}"
      duration_explicit=true
      shift
      ;;
    --continuous)
      continuous=true
      shift
      ;;
    --arm-interpolation-profile)
      if (( $# < 2 )); then
        echo "STOP: --arm-interpolation-profile缺少数值" >&2
        exit 2
      fi
      arm_interpolation_profile="$2"
      shift 2
      ;;
    --arm-interpolation-profile=*)
      arm_interpolation_profile="${1#*=}"
      shift
      ;;
    -h|--help)
      echo "usage: $0 [--duration-seconds POSITIVE_INTEGER | --continuous] [--arm-interpolation-profile cubic_hermite|cubic_spline]"
      exit 0
      ;;
    *)
      echo "STOP: 未知参数：$1" >&2
      exit 2
      ;;
  esac
done

if [[ "$continuous" == true && "$duration_explicit" == true ]]; then
  echo "STOP: --continuous不能与--duration-seconds同时使用" >&2
  exit 2
fi
if [[ "$continuous" != true && ! "$duration_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "STOP: --duration-seconds必须是正整数" >&2
  exit 2
fi
if [[ "$arm_interpolation_profile" != "cubic_hermite" \
  && "$arm_interpolation_profile" != "cubic_spline" ]]; then
  echo "STOP: --arm-interpolation-profile必须是cubic_hermite或cubic_spline" >&2
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
if [[ "$continuous" == true ]]; then
  requests=0
  run_label=continuous
else
  # 首个请求用于bootstrap；稳态每个新chunk推进约s=20个30Hz步。
  requests=$(( 1 + (duration_seconds * 30 + 19) / 20 ))
  run_label="${duration_seconds}s"
fi
if [[ "$arm_interpolation_profile" == "cubic_spline" ]]; then
  interpolation_tag=c2_spline
else
  interpolation_tag=c1_hermite
fi
run_label="${run_label}_${interpolation_tag}_hand_rtc_cap_v2"

assert_no_controller_process
check_policy_endpoint
load_g2_environment

section "Powered前自动执行双进程只读Preflight"
"$RTC_SCRIPT_DIR/11_rtc_process_isolation_preflight.sh"

section "NewLora 75K Split Powered RTC授权"
echo "推理服务：$POLICY_ENDPOINT"
echo "传输协议：$POLICY_TRANSPORT"
echo "RTC参数：H=50，d=16，s=20，服务端guidance=approx_identity/0.5"
echo "时序：旧chunk执行到第20步发请求；早到响应缓存至逻辑d=16，晚到按真实d切换"
echo "后处理：RTC生成 → C0/C1+suffix jerk(weight=10) → 双臂${arm_interpolation_profile}三次插值（日志tag=${interpolation_tag}）；稳态RTC不重复边界融合"
echo "安全门：双臂/双手动作动力学仅告警；物理限位clip、HTTP/Servo/GDK故障仍会停止；LOW-LATENCY无空间碰撞保护"
echo "首跳仅告警：双臂参考0.05rad；双手参考0.35rad，均不因超限结束RTC"
echo "灵巧手10Hz RTC cap V2（日志tag=hand_rtc_cap_v2）：边界首条最多0.15rad，内部最多0.30rad；这是共享轨迹器上线前的显式临时基线"
echo "双臂通道：LOW-LATENCY（无GDK碰撞保护）"
if [[ "$continuous" == true ]]; then
  echo "运行方式：连续；由q/Ctrl+C、RTC超时、HTTP/Servo/GDK故障结束"
else
  echo "运行方式：约${duration_seconds}秒；最多${requests}次模型请求"
fi
echo "注意：RTC改善chunk连续性，不是防碰撞器，也不保证修正模型的错误意图。"
read -r -p "确认现场安全并有专人握住物理急停，输入 ok 继续：" answer
if [[ "${answer,,}" != "ok" ]]; then
  echo "STOP: 未输入ok，取消真实动作" >&2
  exit 2
fi

log="$G2_POWERED_LOG_DIR/g2_powered_rtc_${run_label}_$(timestamp).jsonl"
section "5秒倒计时；专人握住物理急停"
for remaining in 5 4 3 2 1; do
  echo "$remaining..."
  sleep 1
done

python3.12 -m g2_sort.deployment.g2_sdk_client \
  --mode powered-rtc \
  --split-control-profile \
  --policy-endpoint "$POLICY_ENDPOINT" \
  --policy-transport "$POLICY_TRANSPORT" \
  --request-timeout-seconds 10 \
  --rtc-predicted-delay-steps 16 \
  --rtc-execution-horizon 20 \
  --rtc-process-isolation \
  --max-requests "$requests" \
  --boundary-blend-seconds 0.12 \
  --boundary-blend-profile cubic_smoothstep \
  --arm-internal-jerk-weight 10 \
  --arm-interpolation-profile "$arm_interpolation_profile" \
  --hand-stream-hz 10 \
  --hand-smoothing-profile rtc_cap_v2 \
  --hand-boundary-max-delta-radians 0.15 \
  --hand-max-delta-radians 0.30 \
  --max-start-jump-radians 0.05 \
  --max-hand-start-jump-radians 0.35 \
  --max-clip-delta-radians 0.02 \
  --max-servo-send-seconds 0.20 \
  --max-servo-tick-lateness-seconds 0.20 \
  --interactive-keys \
  --telemetry-jsonl "$log"

section "Powered RTC结束"
echo "【日志位置】$log"
echo "结束只停止动作下发，不自动执行双手或双臂Home复位。"
