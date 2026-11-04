#!/usr/bin/env bash
# 用途：为正式同步和实验异步入口提供统一的75K Split真机运行逻辑。
# 输入：调度模式、有限时长或sync专用--continuous，以及平滑参数。
# 输出：有界请求次数、中文安全提示及logs/telemetry/powered中的JSONL。
# 方法：14D双臂100Hz Servo；双手5Hz changed-only move_ee_pos；不合并为34D Servo请求。

G2_POWERED_RUNNER_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

run_powered_split() {
  local scheduler_mode="${1:?scheduler mode is required}"
  shift
  local POLICY_TRANSPORT="${NEWLORA_SYNC_TRANSPORT:-http-multipart-json}"
  local POLICY_ENDPOINT
  if [[ "$POLICY_TRANSPORT" == "websocket-binary" ]]; then
    POLICY_ENDPOINT="${NEWLORA_SYNC_ENDPOINT:-ws://192.0.2.1:7892/process_frame}"
  elif [[ "$POLICY_TRANSPORT" == "http-multipart-json" ]]; then
    POLICY_ENDPOINT="${NEWLORA_SYNC_ENDPOINT:-${POLICY_ENDPOINT:-http://192.0.2.1:7892/process_frame}}"
  else
    echo "STOP: NEWLORA_SYNC_TRANSPORT必须是http-multipart-json或websocket-binary" >&2
    return 2
  fi
  local duration_seconds=60
  local duration_explicit=false
  local continuous=false
  local boundary_blend_seconds=0.06
  local boundary_blend_profile=cubic_smoothstep
  local arm_internal_jerk_weight=0
  local arm_interpolation_profile=linear
  local arm_micro_motion_profile=off
  local arm_micro_motion_threshold_rad=0.0035
  local hand_smoothing_profile=off
  local arms_only=false
  local dual_side_17d=false
  local unified_34=false
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
      --arm-interpolation-profile)
        if (( $# < 2 )); then
          echo "STOP: --arm-interpolation-profile缺少数值" >&2
          return 2
        fi
        arm_interpolation_profile="$2"
        shift 2
        ;;
      --arm-interpolation-profile=*)
        arm_interpolation_profile="${1#*=}"
        shift
        ;;
      --arm-micro-motion-profile)
        if (( $# < 2 )); then
          echo "STOP: --arm-micro-motion-profile缺少数值" >&2
          return 2
        fi
        arm_micro_motion_profile="$2"
        shift 2
        ;;
      --arm-micro-motion-profile=*)
        arm_micro_motion_profile="${1#*=}"
        shift
        ;;
      --arm-micro-motion-threshold-rad)
        if (( $# < 2 )); then
          echo "STOP: --arm-micro-motion-threshold-rad缺少数值" >&2
          return 2
        fi
        arm_micro_motion_threshold_rad="$2"
        shift 2
        ;;
      --arm-micro-motion-threshold-rad=*)
        arm_micro_motion_threshold_rad="${1#*=}"
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
      --arms-only)
        arms_only=true
        shift
        ;;
      --unified-34)
        unified_34=true
        shift
        ;;
      --dual-side-17d)
        dual_side_17d=true
        shift
        ;;
      -h|--help)
        echo "usage: $0 [--duration-seconds POSITIVE_INTEGER | --continuous] [--arms-only|--dual-side-17d|--unified-34] [--boundary-blend-seconds 0..0.5] [--boundary-blend-profile cubic_smoothstep|quintic_smootherstep] [--arm-internal-jerk-weight 0..100] [--arm-interpolation-profile linear|cubic_hermite|cubic_spline] [--arm-micro-motion-profile off|stationary_minjerk_v1] [--arm-micro-motion-threshold-rad 0.0001..0.02] [--hand-smoothing-profile off|v1|v1_1|shared_preview_v1]"
        return 0
        ;;
      *)
        echo "STOP: 未知参数：$1" >&2
        echo "usage: $0 [--duration-seconds POSITIVE_INTEGER | --continuous] [--boundary-blend-seconds 0..0.5] [--boundary-blend-profile cubic_smoothstep|quintic_smootherstep] [--arm-internal-jerk-weight 0..100] [--arm-interpolation-profile linear|cubic_hermite|cubic_spline] [--arm-micro-motion-profile off|stationary_minjerk_v1] [--arm-micro-motion-threshold-rad 0.0001..0.02] [--hand-smoothing-profile off|v1|v1_1|shared_preview_v1]" >&2
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
  if [[ "$arm_interpolation_profile" != "linear" \
    && "$arm_interpolation_profile" != "cubic_hermite" \
    && "$arm_interpolation_profile" != "cubic_spline" ]]; then
    echo "STOP: --arm-interpolation-profile必须是linear、cubic_hermite或cubic_spline" >&2
    return 2
  fi
  if [[ "$arm_micro_motion_profile" != "off" \
    && "$arm_micro_motion_profile" != "stationary_minjerk_v1" ]]; then
    echo "STOP: --arm-micro-motion-profile必须是off或stationary_minjerk_v1" >&2
    return 2
  fi
  if [[ ! "$arm_micro_motion_threshold_rad" =~ ^(0|[0-9]+)(\.[0-9]+)?$ ]] \
    || ! awk "BEGIN {exit !($arm_micro_motion_threshold_rad >= 0.0001 && $arm_micro_motion_threshold_rad <= 0.02)}"; then
    echo "STOP: --arm-micro-motion-threshold-rad必须在0.0001到0.02rad之间" >&2
    return 2
  fi
  if [[ "$hand_smoothing_profile" != "off" \
    && "$hand_smoothing_profile" != "v1" \
    && "$hand_smoothing_profile" != "v1_1" \
    && "$hand_smoothing_profile" != "shared_preview_v1" ]]; then
    echo "STOP: --hand-smoothing-profile必须是off、v1、v1_1或shared_preview_v1" >&2
    return 2
  fi
  if [[ "$hand_smoothing_profile" != "off" && "$scheduler_mode" != "sync" ]]; then
    echo "STOP: 手部V1/V1.1当前只允许正式sync入口使用" >&2
    return 2
  fi
  if [[ "$arms_only" == true && "$hand_smoothing_profile" != "off" ]]; then
    echo "STOP: --arms-only要求--hand-smoothing-profile off" >&2
    return 2
  fi
  if [[ "$unified_34" == true && ( "$dual_side_17d" == true || "$arms_only" == true ) ]]; then
    echo "STOP: --unified-34不能与其他布局选项并用" >&2
    return 2
  fi
  if [[ "$unified_34" == true && "$hand_smoothing_profile" != off ]]; then
    echo "STOP: --unified-34要求hand smoothing off" >&2
    return 2
  fi
  if [[ "$dual_side_17d" == true && "$arms_only" == true ]]; then
    echo "STOP: --dual-side-17d不能与--arms-only同时使用" >&2
    return 2
  fi
  if [[ "$dual_side_17d" == true && "$hand_smoothing_profile" != "off" ]]; then
    echo "STOP: --dual-side-17d直接保留模型轨迹，要求hand smoothing off" >&2
    return 2
  fi
  if [[ "$scheduler_mode" != "sync" && "$scheduler_mode" != "async" ]]; then
    echo "STOP: scheduler必须是sync或async" >&2
    return 2
  fi

  local execute_prefix_steps=20
  local hand_stream_hz
  local runtime_mode requests estimated_intervals actual_seconds log_run_label
  if [[ "$continuous" == true ]]; then
    hand_stream_hz=5
    runtime_mode="powered-sync"
    requests=0
    estimated_intervals=0
    actual_seconds="无限；由操作者结束"
    log_run_label="continuous"
  elif [[ "$scheduler_mode" == "sync" ]]; then
    hand_stream_hz=5
    local target_intervals=$(( duration_seconds * 30 ))
    runtime_mode="powered-sync"
    requests=$(( (target_intervals + execute_prefix_steps - 1) / execute_prefix_steps ))
    estimated_intervals=$(( requests * execute_prefix_steps ))
    actual_seconds="$(awk "BEGIN {printf \"%.1f\", $estimated_intervals / 30}")"
    log_run_label="${duration_seconds}s"
  else
    hand_stream_hz=5
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
  local interpolation_tag
  case "$arm_interpolation_profile" in
    cubic_spline) interpolation_tag=c2_spline ;;
    cubic_hermite) interpolation_tag=c1_hermite ;;
    linear) interpolation_tag=linear ;;
  esac
  log_run_label="${log_run_label}_${interpolation_tag}"
  if [[ "$arm_micro_motion_profile" == "stationary_minjerk_v1" ]]; then
    log_run_label="${log_run_label}_arm_micro_minjerk_v1"
  fi
  if [[ "$hand_smoothing_profile" == "v1_1" ]]; then
    log_run_label="${log_run_label}_hand_v11"
  elif [[ "$hand_smoothing_profile" == "shared_preview_v1" ]]; then
    log_run_label="${log_run_label}_hand_shared_preview_soft"
  elif [[ "$hand_smoothing_profile" == "off" ]]; then
    log_run_label="${log_run_label}_hand_raw5_changed_only"
  fi
  if [[ "$arms_only" == true ]]; then
    log_run_label="${log_run_label}_arms_only"
  elif [[ "$dual_side_17d" == true ]]; then
    log_run_label="${log_run_label}_dual_side_17d_servo100"
  elif [[ "$unified_34" == true ]]; then
    log_run_label="${log_run_label}_unified_34_servo100"
  fi

  assert_no_controller_process
  check_policy_endpoint
  local -a process_isolation_args=()
  if [[ "$scheduler_mode" == "sync" ]]; then
    section "Powered前自动执行双进程只读Preflight"
    G2_PREFLIGHT_POLICY_ENDPOINT="$POLICY_ENDPOINT" \
      G2_PREFLIGHT_POLICY_TRANSPORT="$POLICY_TRANSPORT" \
      "$G2_POWERED_RUNNER_SCRIPT_DIR/11_rtc_process_isolation_preflight.sh"
    process_isolation_args+=(--rtc-process-isolation)
  fi
  load_g2_environment

  section "75K Split ${scheduler_mode} Powered授权"
  echo "推理服务：$POLICY_ENDPOINT"
  echo "传输协议：$POLICY_TRANSPORT"
  if [[ "$continuous" == true ]]; then
    echo "运行方式：连续；请求无上限"
    echo "结束条件：q/Ctrl+C、Servo/GDK故障或进程异常；模型不会自动判断任务完成"
  else
    echo "期望模型动作时间：${duration_seconds}秒"
    echo "请求上限：${requests}；预计有效模型动作约${actual_seconds}秒"
  fi
  if [[ "$arms_only" == true ]]; then
    echo "硬件下发：仅双臂14D/100Hz joint_servo_control；不创建手线程，不调用move_ee_pos"
  elif [[ "$unified_34" == true ]]; then
    echo "硬件下发：每个100Hz tick单次34D canonical joint_servo_control；不调用move_ee_pos"
  elif [[ "$dual_side_17d" == true ]]; then
    echo "硬件下发：每个100Hz tick依次发送左17D和右17D joint_servo_control；不调用move_ee_pos"
  else
    echo "硬件下发：双臂14D/100Hz joint_servo_control；双手20D/${hand_stream_hz}Hz move_ee_pos"
  fi
  echo "双臂chunk边界融合：${boundary_blend_seconds}秒，${boundary_blend_profile}"
  echo "双臂chunk内部jerk正则权重：${arm_internal_jerk_weight}（0表示关闭）"
  echo "双臂30→100Hz插值：${arm_interpolation_profile}（日志tag=${interpolation_tag}）"
  echo "双臂近静止微颤：${arm_micro_motion_profile}（阈值=${arm_micro_motion_threshold_rad}rad；最终100Hz同端点minimum-jerk）"
  if [[ "$arms_only" == true ]]; then
    echo "灵巧手：完全禁用；模型仍返回34D，但硬件只执行前14D"
  elif [[ "$dual_side_17d" == true || "$unified_34" == true ]]; then
    echo "灵巧手100Hz：模型原始20D线性插值；canonical名称；无阈值、无changed-only"
  elif [[ "$hand_smoothing_profile" == "v1_1" ]]; then
    echo "灵巧手${hand_stream_hz}Hz平滑：V1.1（0.175rad；1.75rad/s；7.5rad/s²；反转保护仅400ms边界；日志tag=hand_v11）"
  elif [[ "$hand_smoothing_profile" == "shared_preview_v1" ]]; then
    echo "灵巧手${hand_stream_hz}Hz平滑：共享Preview（跨chunk q/v/a；6步预览；物理位置硬约束；速度/加速度/jerk仅告警；jerk penalty=5；日志tag=hand_shared_preview_soft）"
  else
    echo "灵巧手${hand_stream_hz}Hz：直接采样模型原始20D；目标与上次成功提交值相同则跳过move_ee_pos（日志tag=hand_raw5_changed_only）"
  fi
  if [[ "$dual_side_17d" == true || "$unified_34" == true ]]; then
    echo "Servo通道：NORMAL（保留GDK碰撞保护）"
  else
    echo "双臂通道：LOW-LATENCY（无GDK碰撞保护）"
  fi
  if [[ "$scheduler_mode" == "sync" ]]; then
    echo "调度：同步；每次执行${execute_prefix_steps}/50步（约0.667秒）后重新观测并等待策略响应"
    echo "进程隔离：Control唯一Robot；Policy worker唯一Camera并访问${POLICY_ENDPOINT}"
  else
    echo "调度：异步实验；剩20步时预取，按观测年龄跳过陈旧前缀"
    echo "注意：异步已隐藏多数等待，但真机任务效果未优于同步，且跨chunk跳变更大"
  fi
  if [[ "$unified_34" == true ]]; then
    echo "34D计时：发送/调度超过200ms仅记录告警并继续；SDK错误及人工停止仍终止。"
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

  local -a output_mode_args=()
  if [[ "$arms_only" == true ]]; then
    output_mode_args+=(--servo-output-mode arms-only)
  elif [[ "$dual_side_17d" == true ]]; then
    output_mode_args+=(--servo-output-mode dual-side-17d --servo-channel normal)
  elif [[ "$unified_34" == true ]]; then
    output_mode_args+=(--servo-output-mode unified-34 --servo-channel normal --servo-timing-warning-only)
  fi

  python3.12 -m g2_sort.deployment.g2_sdk_client \
    --mode "$runtime_mode" \
    --split-control-profile \
    "${output_mode_args[@]}" \
    --policy-endpoint "$POLICY_ENDPOINT" \
    --policy-transport "$POLICY_TRANSPORT" \
    "${process_isolation_args[@]}" \
    --request-timeout-seconds 10 \
    --execute-prefix-steps "$execute_prefix_steps" \
    --chunk-prefetch-lookahead 20 \
    --async-min-fresh-steps 12 \
    --max-requests "$requests" \
    --boundary-blend-seconds "$boundary_blend_seconds" \
    --boundary-blend-profile "$boundary_blend_profile" \
    --arm-internal-jerk-weight "$arm_internal_jerk_weight" \
    --arm-interpolation-profile "$arm_interpolation_profile" \
    --arm-micro-motion-profile "$arm_micro_motion_profile" \
    --arm-micro-motion-threshold-rad "$arm_micro_motion_threshold_rad" \
    --hand-stream-hz "$hand_stream_hz" \
    --hand-smoothing-profile "$hand_smoothing_profile" \
    --max-servo-send-seconds 0.20 \
    --max-servo-tick-lateness-seconds 0.20 \
    --interactive-keys \
    --telemetry-jsonl "$log"

  section "75K Split ${scheduler_mode} Powered结束"
  echo "【日志位置】$log"
  echo "【注意】结束只停止动作下发，不会自动执行双手+双臂Home复位。"
}
