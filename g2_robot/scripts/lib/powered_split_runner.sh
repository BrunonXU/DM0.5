#!/usr/bin/env bash
# 用途：为正式同步和实验异步入口提供统一的75K Split真机运行逻辑。
# 输入：调度模式、有限时长或sync专用--continuous，以及平滑参数。
# 输出：有界请求次数、中文安全提示及logs/telemetry/powered中的JSONL。
# 方法：14D双臂100Hz Servo、20D O10双手5Hz move_ee_pos；不合并为34D Servo请求。

run_powered_split() {
  local scheduler_mode="${1:?scheduler mode is required}"
  shift
  local duration_seconds=10
  local duration_explicit=false
  local continuous=false
  local boundary_blend_seconds=0.06
  local boundary_blend_profile=cubic_smoothstep
  local arm_internal_jerk_weight=0
  local hand_smoothing_profile=off
  while (( $# > 0 )); do
    case "$1" in
      --duration-seconds)
        if (( $# < 2 )); then
          echo "STOP: --duration-seconds缺少数值" >&2
          return 2
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
      --boundary-blend-seconds)
        if (( $# < 2 )); then
          echo "STOP: --boundary-blend-seconds缺少数值" >&2
          return 2
        fi
        boundary_blend_seconds="$2"
        shift 2
        ;;
      --boundary-blend-seconds=*)
        boundary_blend_seconds="${1#*=}"
        shift
        ;;
      --boundary-blend-profile)
        if (( $# < 2 )); then
          echo "STOP: --boundary-blend-profile缺少数值" >&2
          return 2
        fi
        boundary_blend_profile="$2"
        shift 2
        ;;
      --boundary-blend-profile=*)
        boundary_blend_profile="${1#*=}"
        shift
        ;;
      --arm-internal-jerk-weight)
        if (( $# < 2 )); then
          echo "STOP: --arm-internal-jerk-weight缺少数值" >&2
          return 2
        fi
        arm_internal_jerk_weight="$2"
        shift 2
        ;;
      --arm-internal-jerk-weight=*)
        arm_internal_jerk_weight="${1#*=}"
        shift
        ;;
      --hand-smoothing-profile)
        if (( $# < 2 )); then
          echo "STOP: --hand-smoothing-profile缺少数值" >&2
          return 2
        fi
        hand_smoothing_profile="$2"
        shift 2
        ;;
      --hand-smoothing-profile=*)
        hand_smoothing_profile="${1#*=}"
        shift
        ;;
      -h|--help)
        echo "usage: $0 [--duration-seconds POSITIVE_INTEGER | --continuous] [--boundary-blend-seconds 0..0.5] [--boundary-blend-profile cubic_smoothstep|quintic_smootherstep] [--arm-internal-jerk-weight 0..100] [--hand-smoothing-profile off|v1]"
        return 0
        ;;
      *)
        echo "STOP: 未知参数：$1" >&2
        echo "usage: $0 [--duration-seconds POSITIVE_INTEGER | --continuous] [--boundary-blend-seconds 0..0.5] [--boundary-blend-profile cubic_smoothstep|quintic_smootherstep] [--arm-internal-jerk-weight 0..100] [--hand-smoothing-profile off|v1]" >&2
        return 2
        ;;
    esac
  done
  if [[ "$continuous" == true && "$duration_explicit" == true ]]; then
    echo "STOP: --continuous不能与--duration-seconds同时使用" >&2
    return 2
  fi
  if [[ "$continuous" == true && "$scheduler_mode" != "sync" ]]; then
    echo "STOP: --continuous当前只允许正式sync入口使用" >&2
    return 2
  fi
  if [[ "$continuous" != true && ! "$duration_seconds" =~ ^[1-9][0-9]*$ ]]; then
    echo "STOP: --duration-seconds必须是正整数" >&2
    return 2
  fi
  if [[ ! "$boundary_blend_seconds" =~ ^(0|[0-9]+)(\.[0-9]+)?$ ]] \
    || ! awk "BEGIN {exit !($boundary_blend_seconds >= 0 && $boundary_blend_seconds <= 0.5)}"; then
    echo "STOP: --boundary-blend-seconds必须在0到0.5秒之间" >&2
    return 2
  fi
  if [[ "$boundary_blend_profile" != "cubic_smoothstep" \
    && "$boundary_blend_profile" != "quintic_smootherstep" ]]; then
    echo "STOP: --boundary-blend-profile必须是cubic_smoothstep或quintic_smootherstep" >&2
    return 2
  fi
  if [[ ! "$arm_internal_jerk_weight" =~ ^(0|[0-9]+)(\.[0-9]+)?$ ]] \
    || ! awk "BEGIN {exit !($arm_internal_jerk_weight >= 0 && $arm_internal_jerk_weight <= 100)}"; then
    echo "STOP: --arm-internal-jerk-weight必须在0到100之间" >&2
    return 2
  fi
  if [[ "$hand_smoothing_profile" != "off" \
    && "$hand_smoothing_profile" != "v1" ]]; then
    echo "STOP: --hand-smoothing-profile必须是off或v1" >&2
    return 2
  fi
  if [[ "$hand_smoothing_profile" == "v1" && "$scheduler_mode" != "sync" ]]; then
    echo "STOP: 手部V1当前只允许正式sync入口使用" >&2
    return 2
  fi
  if [[ "$scheduler_mode" != "sync" && "$scheduler_mode" != "async" ]]; then
    echo "STOP: scheduler必须是sync或async" >&2
    return 2
  fi

  local runtime_mode requests estimated_intervals actual_seconds log_run_label
  if [[ "$continuous" == true ]]; then
    runtime_mode="powered-sync"
    requests=0
    estimated_intervals=0
    actual_seconds="无限；由操作者结束"
    log_run_label="continuous"
  elif [[ "$scheduler_mode" == "sync" ]]; then
    local target_intervals=$(( duration_seconds * 30 ))
    runtime_mode="powered-sync"
    requests=$(( (target_intervals + 24) / 49 ))
    estimated_intervals=$(( requests * 49 ))
    actual_seconds="$(awk "BEGIN {printf \"%.1f\", $estimated_intervals / 30}")"
    log_run_label="${duration_seconds}s"
  else
    local target_intervals=$(( duration_seconds * 30 ))
    runtime_mode="powered-async"
    local expected_fresh_steps=35
    if (( target_intervals <= 50 )); then
      requests=1
    else
      requests=$(( 1 + (target_intervals - 50 + expected_fresh_steps - 1) / expected_fresh_steps ))
    fi
    estimated_intervals=$(( 50 + (requests - 1) * expected_fresh_steps ))
    actual_seconds="$(awk "BEGIN {printf \"%.1f\", $estimated_intervals / 30}")"
    log_run_label="${duration_seconds}s"
  fi
  if [[ "$continuous" != true ]] && (( requests < 1 )); then
    requests=1
  fi

  assert_no_controller_process
  check_endpoint_405
  load_g2_environment

  section "75K Split ${scheduler_mode} Powered授权"
  echo "推理服务：$POLICY_ENDPOINT"
  if [[ "$continuous" == true ]]; then
    echo "运行方式：连续；请求无上限"
    echo "结束条件：q/Ctrl+C、Servo/GDK故障或进程异常；模型不会自动判断任务完成"
  else
    echo "期望模型动作时间：${duration_seconds}秒"
    echo "请求上限：${requests}；预计有效模型动作约${actual_seconds}秒"
  fi
  echo "硬件下发：双臂14D/100Hz joint_servo_control；双手20D/5Hz move_ee_pos"
  echo "双臂chunk边界融合：${boundary_blend_seconds}秒，${boundary_blend_profile}"
  echo "双臂chunk内部jerk正则权重：${arm_internal_jerk_weight}（0表示关闭）"
  echo "灵巧手5Hz平滑：${hand_smoothing_profile}（V1=400ms cubic+持续追赶+cap40）"
  echo "双臂通道：LOW-LATENCY（无GDK碰撞保护）"
  if [[ "$scheduler_mode" == "sync" ]]; then
    echo "调度：同步；每个1.633秒动作chunk结束后hold并等待约0.47～0.52秒HTTP RTT"
  else
    echo "调度：异步实验；剩20步时预取，按观测年龄跳过陈旧前缀"
    echo "注意：异步已隐藏多数等待，但真机任务效果未优于同步，且跨chunk跳变更大"
  fi
  echo "首跳、裁剪幅度和动力学超限：记录告警，不拒绝chunk"
  echo "仍保留：50×34/finite合同、物理限位、HTTP/GDK失败停止"
  read -r -p "确认现场安全并有专人握住物理急停，输入 ok 继续：" answer
  if [[ "${answer,,}" != "ok" ]]; then
    echo "STOP: 未输入ok，取消真实动作" >&2
    return 2
  fi

  local log="$G2_POWERED_LOG_DIR/g2_powered_split_${scheduler_mode}_${log_run_label}_$(timestamp).jsonl"
  section "5秒倒计时；专人握住物理急停"
  local remaining
  for remaining in 5 4 3 2 1; do
    echo "$remaining..."
    sleep 1
  done

  python3.12 -m g2_sort.deployment.g2_sdk_client \
    --mode "$runtime_mode" \
    --split-control-profile \
    --policy-endpoint "$POLICY_ENDPOINT" \
    --request-timeout-seconds 10 \
    --execute-prefix-steps 49 \
    --chunk-prefetch-lookahead 20 \
    --async-min-fresh-steps 12 \
    --max-requests "$requests" \
    --boundary-blend-seconds "$boundary_blend_seconds" \
    --boundary-blend-profile "$boundary_blend_profile" \
    --arm-internal-jerk-weight "$arm_internal_jerk_weight" \
    --hand-smoothing-profile "$hand_smoothing_profile" \
    --max-servo-send-seconds 0.20 \
    --max-servo-tick-lateness-seconds 0.20 \
    --interactive-keys \
    --telemetry-jsonl "$log"

  section "75K Split ${scheduler_mode} Powered结束"
  echo "【日志位置】$log"
  echo "【注意】结束只停止动作下发，不会自动执行双手+双臂Home复位。"
}
