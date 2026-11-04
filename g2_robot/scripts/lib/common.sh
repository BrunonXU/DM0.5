#!/usr/bin/env bash
# 用途：为所有G2现场脚本提供统一环境、日志目录、端口检查和进程互斥。
# 输入：可选G2_APP_DIR、G2_PROJECT_DIR、POLICY_ENDPOINT等环境变量。
# 输出：分类日志目录及可复用Shell函数；本文件被source时不会启动机器人。
# 方法：统一加载env.sh，并把GDK原生日志固定到logs/gdk_native。
set -Eeuo pipefail

G2_APP_DIR="${G2_APP_DIR:-/home/user/app}"
G2_PROJECT_DIR="${G2_PROJECT_DIR:-/home/user/xza/DM0.5}"
POLICY_ENDPOINT="${POLICY_ENDPOINT:-http://192.0.2.1:7892/process_frame}"
G2_RUNTIME_PID_FILE="${G2_RUNTIME_PID_FILE:-/tmp/g2_dm05_runtime.pid}"
G2_LOG_ROOT="${G2_LOG_ROOT:-$G2_PROJECT_DIR/logs}"
G2_SHADOW_LOG_DIR="$G2_LOG_ROOT/telemetry/shadow"
G2_POWERED_LOG_DIR="$G2_LOG_ROOT/telemetry/powered"
G2_DIAGNOSTIC_LOG_DIR="$G2_LOG_ROOT/diagnostics"
G2_NATIVE_LOG_DIR="$G2_LOG_ROOT/gdk_native"
G2_BUNDLE_LOG_DIR="$G2_LOG_ROOT/bundles"
G2_RUNTIME_LOG_DIR="$G2_LOG_ROOT/runtime"

section() {
  echo
  echo "===== $* ====="
}

load_g2_environment() {
  section "加载GDK环境"
  mkdir -p \
    "$G2_SHADOW_LOG_DIR" \
    "$G2_POWERED_LOG_DIR" \
    "$G2_DIAGNOSTIC_LOG_DIR" \
    "$G2_NATIVE_LOG_DIR" \
    "$G2_BUNDLE_LOG_DIR" \
    "$G2_RUNTIME_LOG_DIR"
  export GLOG_log_dir="$G2_NATIVE_LOG_DIR"
  cd "$G2_APP_DIR"
  # shellcheck disable=SC1091
  # G2官方env.sh直接读取$1；临时关闭nounset以兼容无参数source。
  set +u
  source env.sh
  set -u
  cd "$G2_PROJECT_DIR"
}

timestamp() {
  date +%Y%m%d_%H%M%S
}

check_endpoint_405() {
  section "检查DM0.5中继端口"
  local status
  if ! status="$(
      env -u LD_LIBRARY_PATH /usr/bin/curl \
        --connect-timeout 3 \
        --max-time 5 \
        -sS \
        -o /dev/null \
        -w '%{http_code}' \
        "$POLICY_ENDPOINT"
    )"; then
    echo "STOP: 无法访问DM0.5中继端口" >&2
    return 1
  fi
  echo "endpoint=$POLICY_ENDPOINT status=$status"
  if [[ "$status" != "405" ]]; then
    echo "STOP: 期望GET返回405，实际为$status" >&2
    return 1
  fi
}

check_policy_endpoint() {
  if [[ "${POLICY_TRANSPORT:-http-multipart-json}" == "websocket-binary" ]]; then
    section "检查DM0.5 WebSocket端口"
    env -u LD_LIBRARY_PATH python3.12 -c \
      'import sys; from websockets.sync.client import connect; c = connect(sys.argv[1], compression=None, open_timeout=3, close_timeout=1); c.close(); print("websocket handshake=ok")' \
      "$POLICY_ENDPOINT"
  else
    check_endpoint_405
  fi
}

assert_no_controller_process() {
  section "检查旧控制进程"
  local matches
  matches="$(
    pgrep -af \
      'g2_sort\.deployment\.g2_sdk_client|operations\.recovery\.(reset_pose|hand_home)|operations\.diagnostics\.(servo_hold|rtc_process_isolation_preflight)|operations\.hardware_tests\.(thumb_servo_smoke|o10_move_ee_smoke|training_data_replay|single_side_17d_servo_probe|servo_layout_probe)' \
      || true
  )"
  if [[ -n "$matches" ]]; then
    echo "$matches" >&2
    echo "STOP: 检测到可能占用G2控制器的进程" >&2
    return 1
  fi
  echo "PASS: 未发现旧控制进程"
}
