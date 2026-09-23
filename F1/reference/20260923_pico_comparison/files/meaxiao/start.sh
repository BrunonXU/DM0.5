#!/usr/bin/env bash
set -Eeuo pipefail

# v1.4.2场景与传输配置统一在 config/collection.yaml。
# 本地: mocap_data/<scene>/mocap_data；中央: dataset_base/<scene>/mocap_data。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"
RUN_DIR="$ROOT/run"
mode="${1:---check}"
[[ "$mode" == "--check" || "$mode" == "--allow-motion" ]] || {
    printf 'Usage: ./start.sh --check | --allow-motion\n' >&2
    exit 2
}
# shellcheck disable=SC1091
source "$ROOT/config/robot.env"
# shellcheck disable=SC1091
source "$ROOT/scripts/env.sh"
if [[ "$mode" == "--check" ]]; then
    exec "$ROOT/scripts/preflight.sh"
fi

mkdir -p "$RUN_DIR" "$LOG_DIR"
exec 8>"$RUN_DIR/launch.lock"
flock -n 8 || { printf 'PICO_ALREADY_STARTING_OR_STOPPING: wait for the existing operation; no duplicate launch\n'; exit 0; }
guard_rc=0
"$ROOT/.venv/bin/python" "$ROOT/scripts/session_processes.py" guard "$ROOT" || guard_rc=$?
[[ "$guard_rc" != 10 ]] || exit 0
[[ "$guard_rc" == 0 ]] || exit "$guard_rc"
"$ROOT/.venv/bin/python" "$ROOT/scripts/session_processes.py" remember "$RUN_DIR/startup.json" "$$"
gate_pid=""
motion_opened=false
cleanup_startup() {
    local rc=$?
    trap - ERR EXIT INT TERM
    if [[ -n "$gate_pid" ]]; then
        kill -TERM "$gate_pid" 2>/dev/null || true
        wait "$gate_pid" 2>/dev/null || true
    fi
    if (( rc != 0 )); then
        printf 'STARTUP_FAILED rc=%s line=%s command=%q\n' "$rc" \
            "${startup_failure_line:-unknown}" "${startup_failure_command:-unknown}" >&2
        if $motion_opened; then
            "$ROOT/stop.sh" --managed-only --keep-service --startup-cleanup || true
        else
            "$ROOT/.venv/bin/python" "$ROOT/scripts/collection_session.py" stop || true
        fi
    fi
    rm -f -- "$RUN_DIR/startup.json" "$RUN_DIR/startup_child.json"
    exit "$rc"
}
trap cleanup_startup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
run_gate() {
    "$@" 8>&- &
    gate_pid=$!
    "$ROOT/.venv/bin/python" "$ROOT/scripts/session_processes.py" remember "$RUN_DIR/startup_child.json" "$gate_pid"
    local rc=0
    wait "$gate_pid" || rc=$?
    gate_pid=""
    rm -f -- "$RUN_DIR/startup_child.json"
    return "$rc"
}

# Open the input-only service before the robot checks, so connection order is
# irrelevant. No bridge, recorder or robot command publisher exists yet.
run_gate "$ROOT/.venv/bin/python" "$ROOT/scripts/collection_session.py" start --robot "$EXPECTED_ROBOT_ID"
# Freeze source/task for every child including preflight and the recorder.
source "$ROOT/scripts/env.sh"
run_gate bash "$ROOT/scripts/ensure_xr_service.sh"
run_gate "$ROOT/scripts/preflight.sh"

mkdir -p "$LOG_DIR" "$RUN_DIR" "$PICO_F1_MOCAP_DIR"
# v1.3.7 single-command start: identity, ROS graph, feedback, cameras and disk
# were checked by preflight above. Verify that PICO is actively streaming valid
# poses automatically; Grip remains the runtime arm deadman and A/A+B remain
# the torso/chassis deadmen. No separate Shadow or authorization receipt.
xr_wait_started=$SECONDS
run_gate "$ROOT/.venv/bin/python" "$ROOT/scripts/wait_for_xr.py"
# A late connection can arrive minutes later; never reuse stale robot checks.
if (( SECONDS - xr_wait_started >= 5 )); then
    printf 'PICO_CONNECTED: rechecking robot after connection wait\n'
    run_gate "$ROOT/scripts/preflight.sh"
fi
# The operator-owned workspace reset is authoritative. Capture it only after
# XR and one-time authorization gates pass, before opening any command process.
rm -f -- "$PICO_F1_INITIAL_POSE"
run_gate "$ROOT/.venv/bin/python" "$ROOT/scripts/capture_initial_pose.py" \
    --output "$PICO_F1_INITIAL_POSE" --robot-id "$EXPECTED_ROBOT_ID"
run_gate "$ROOT/.venv/bin/python" "$ROOT/scripts/check_start_pose.py" \
    --sample "$PICO_F1_INITIAL_POSE" --check-path
rm -f -- "$RUN_DIR/xr_input_gate.json" "$RUN_DIR/operator_authorization.json"
printf 'AUTOMATIC_LIVE_GATES_OK robot=%s separate_authorization=false\n' "$EXPECTED_ROBOT_ID"

start_python() {
    local name="$1" cwd="$2" script="$3"
    shift 3
    local log="$LOG_DIR/$name.log" pidfile="$RUN_DIR/$name.pid"
    [[ ! -f "$pidfile" ]] || { printf 'ERROR: stale/running pidfile %s\n' "$pidfile" >&2; exit 1; }
    (
        cd "$cwd"
        nohup "$ROOT/.venv/bin/python" -u "$script" "$@" >"$log" 2>&1 8>&- &
        printf '%s\n' "$!" >"$pidfile"
    )
    # Readiness is checked by the caller (UDP port or teleop log marker). A
    # short process-liveness gate avoids adding a fixed three-second delay for
    # every component.
    sleep 0.25
    local pid; pid="$(<"$pidfile")"
    kill -0 "$pid" 2>/dev/null || {
        printf 'ERROR: %s exited during startup\n' "$name" >&2
        tail -100 "$log" >&2 || true
        exit 1
    }
    printf 'STARTED %s pid=%s log=%s\n' "$name" "$pid" "$log"
}

record_startup_error() {
    local rc=$?
    startup_failure_line="${BASH_LINENO[0]:-unknown}"
    startup_failure_command="$BASH_COMMAND"
    return "$rc"
}
startup_failure_line=unknown
startup_failure_command=unknown
trap record_startup_error ERR

# The physical gamepad is another publisher on the chassis command topic.
# Quiesce it for exclusive PICO control, and restore it from stop.sh.
if [[ -f "$RUN_DIR/chassis_mode.changed" ]]; then
    run_gate "$ROOT/.venv/bin/python" "$ROOT/scripts/set_chassis_mode.py" --restore \
        --marker "$RUN_DIR/chassis_mode.changed"
fi
motion_opened=true
bash "$ROOT/scripts/manage_gamepad.sh" quiesce
"$ROOT/.venv/bin/python" "$ROOT/scripts/set_chassis_mode.py" --manual \
    --marker "$RUN_DIR/chassis_mode.changed"

# Resolve status before any real-motion process is opened.  Keeping this in a
# normal Python file avoids nested shell/f-string quoting regressions.
control_status="$("$ROOT/.venv/bin/python" "$ROOT/scripts/print_control_status.py")"

# Startup is deliberately motionless: bridge and IK both consume the captured
# per-launch pose. X+Y later returns only both arms and body_j3 to that snapshot.
start_python recorder "$ROOT/vendor/f1_bridge" "$ROOT/vendor/f1_bridge/lerobot_recorder.py"
for _ in {1..30}; do ss -lun | grep -q ':8899 ' && break; sleep 0.5; done
ss -lun | grep -q ':8899 ' || { printf 'ERROR: recorder UDP 8899 did not open\n' >&2; exit 1; }
start_python bridge "$ROOT/vendor/f1_bridge" "$ROOT/vendor/f1_bridge/bridge_main.py"
for port in 8888; do
    for _ in {1..30}; do ss -lun | grep -q ":$port " && break; sleep 0.5; done
    ss -lun | grep -q ":$port " || { printf 'ERROR: UDP %s did not open\n' "$port" >&2; exit 1; }
done
start_python teleop "$ROOT/vendor/xrobotoolkit" "$ROOT/vendor/xrobotoolkit/teleop_main.py"
for _ in {1..40}; do
    grep -Fq 'approved-command publisher started' "$LOG_DIR/teleop.log" && break
    pid="$(<"$RUN_DIR/teleop.pid")"
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
done
grep -Fq 'approved-command publisher started' "$LOG_DIR/teleop.log" || {
    printf 'ERROR: teleop safety/control worker did not become ready\n' >&2
    tail -100 "$LOG_DIR/teleop.log" >&2 || true
    exit 1
}

printf '\nPICO_F1_TELEOP_STARTED %s\n' "$control_status"
printf 'Recording log console: %s/recording_console.sh\n' "$ROOT"
printf 'Stop immediately: %s/stop.sh\n' "$ROOT"
trap - ERR EXIT
rm -f -- "$RUN_DIR/startup.json" "$RUN_DIR/startup_child.json"

