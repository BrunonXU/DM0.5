#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/run"
managed_only=false
# Keep the XR PC Service by default so stop -> start does not tear down the
# headset stream and force the operator to press Send again. Maintenance can
# still request a complete shutdown explicitly.
keep_service=true
startup_cleanup=false
for option in "$@"; do
    case "$option" in
        --managed-only) managed_only=true ;;
        --keep-service) keep_service=true ;;
        --stop-service) keep_service=false ;;
        --startup-cleanup) startup_cleanup=true ;;
        *) printf 'ERROR: unknown option %s\n' "$option" >&2; exit 2 ;;
    esac
done

# Data-service cleanup must still run if a ROS/mode recovery step fails.
finish_collection() {
    local stop_rc=$?
    trap - EXIT
    if ! "$ROOT/.venv/bin/python" "$ROOT/scripts/collection_session.py" stop; then
        printf '[自动传输] 关闭未完全确认：保留会话记录，恢复SSH后重试 ./stop.sh；不要删除数据或回执\n' >&2
        stop_rc=1
    fi
    exit "$stop_rc"
}
trap finish_collection EXIT

mkdir -p "$RUN_DIR"
if ! $startup_cleanup; then
    "$ROOT/.venv/bin/python" "$ROOT/scripts/session_processes.py" cancel "$ROOT"
    exec 8>"$RUN_DIR/launch.lock"
    flock -w 25 8 || { printf 'STOP_FAILED: startup did not exit; do not restart\n' >&2; exit 1; }
fi
recovery_failed=0

stop_pidfile() {
    local name="$1" expected="$2" pidfile="$RUN_DIR/$1.pid"
    [[ -f "$pidfile" ]] || return 0
    local pid command attempts=10 i
    # Video finalization and recovery journal flush can take several seconds.
    # Keep the short actuator shutdown timeout, but do not SIGKILL a recorder
    # after only two seconds and truncate the long episode being preserved.
    [[ "$name" != "recorder" ]] || attempts=300
    pid="$(<"$pidfile")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
        [[ "$command" == *"$expected"* ]] || {
            printf 'STALE_PIDFILE %s pid=%s\n' "$name" "$pid" >&2
            rm -f -- "$pidfile"
            return 0
        }
        kill -INT "$pid" 2>/dev/null || true
        for (( i=0; i<attempts; i++ )); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        if kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid" 2>/dev/null || true; fi
        for _ in {1..10}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid" 2>/dev/null || true; fi
        printf 'STOPPED %s pid=%s\n' "$name" "$pid"
    fi
    rm -f -- "$pidfile"
}

# Remove the ROS-command bridge first so a delayed teleop exit cannot move the robot.
stop_pidfile bridge bridge_main.py
stop_pidfile teleop teleop_main.py
# A SIGKILL or interpreter failure may bypass the teleop destructor. Reassert
# zero after the PICO publisher is gone, then restore the platform mode.
if [[ -x "$ROOT/.venv/bin/python" ]]; then
    # shellcheck disable=SC1091
    source "$ROOT/config/robot.env"
    # shellcheck disable=SC1091
    source "$ROOT/scripts/env.sh"
    "$ROOT/.venv/bin/python" "$ROOT/scripts/publish_chassis_zero.py" --seconds 0.5 || true
    if [[ -f "$RUN_DIR/chassis_mode.changed" ]]; then
        "$ROOT/.venv/bin/python" "$ROOT/scripts/set_chassis_mode.py" --restore \
            --marker "$RUN_DIR/chassis_mode.changed" || recovery_failed=1
    fi
fi
if (( recovery_failed == 0 )); then
    bash "$ROOT/scripts/manage_gamepad.sh" restore 8>&- || recovery_failed=1
fi
# Motion publishers are gone and the chassis is zeroed before waiting for media.
stop_pidfile recorder lerobot_recorder.py
if ! $keep_service; then
    stop_pidfile roboticsservice RoboticsServiceProcess
    stop_pidfile roboticsservice-launcher runService.sh
fi
if ! $managed_only && pgrep -af "$ROOT/.+(bridge_main|teleop_main)\.py" >/dev/null; then
    printf 'ERROR: unmanaged project process remains\n' >&2
    exit 1
fi
if (( recovery_failed != 0 )); then
    printf 'PICO_PROCESSES_STOPPED_RECOVERY_PENDING: chassis mode/gamepad not restored; markers retained; physical stop is NOT verified\n' >&2
else
    printf 'PICO_F1_MANAGED_PROCESSES_STOPPED\n'
fi
if $keep_service; then
    printf 'XR_SERVICE_KEPT_RUNNING restart="./start.sh --allow-motion"\n'
fi
exit "$recovery_failed"
