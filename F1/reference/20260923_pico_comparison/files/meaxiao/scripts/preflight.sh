#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/config/robot.env"
# shellcheck disable=SC1091
source "$ROOT/scripts/env.sh"

failures=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; failures=$((failures + 1)); }

actual_robot_id="$(cat /home/user/.f1_robot_id 2>/dev/null || true)"
[[ "$actual_robot_id" == "$EXPECTED_ROBOT_ID" ]] \
    && pass "robot identity $actual_robot_id" \
    || fail "robot identity is ${actual_robot_id:-missing}, expected $EXPECTED_ROBOT_ID"

required_files=(
    "$ROOT/.venv/bin/python"
    "$ROOT/config/ip_config.py"
    "$ROOT/config/control.json"
    "$ROOT/assets/f1_wheel/urdf/f1_wheel_0602_new.urdf"
    "$ROOT/vendor/f1_safety/urdf/f1_wheel/urdf/f1_wheel_0602_new.urdf"
    "$ROOT/vendor/f1_bridge/bridge_main.py"
    "$ROOT/vendor/f1_bridge/lerobot_recorder.py"
    "$ROOT/vendor/xrobotoolkit/teleop_main.py"
    "$ROOT/vendor/roboticsservice/RoboticsServiceProcess"
    "$ROOT/recording_console.sh"
    "$ROOT/scripts/check_lerobot_v3.py"
    "$ROOT/scripts/wait_for_xr.py"
    "$ROOT/scripts/session_processes.py"
    "$ROOT/src/pico_f1/xr_readiness.py"
)
for file in "${required_files[@]}"; do
    [[ -e "$file" ]] && pass "file $file" || fail "missing $file"
done

if "$ROOT/.venv/bin/python" - <<'PY'
import importlib
from pico_f1.config import load_control_config
for name in (
    "placo", "pyarrow", "rclpy", "xrobotoolkit_sdk", "xrobotoolkit_teleop",
    "pico_f1.lerobot_v3",
    "geometry_msgs.msg", "move.msg", "move.srv",
):
    importlib.import_module(name)
print("CONTROL_CONFIG", load_control_config())
PY
then pass "Python/ROS/XR imports"; else fail "Python/ROS/XR imports"; fi

for command in ffmpeg ffprobe; do
    if command -v "$command" >/dev/null; then
        pass "command $command"
    else
        fail "missing command $command"
    fi
done

mkdir -p "$PICO_F1_MOCAP_DIR"
if [[ -w "$PICO_F1_MOCAP_DIR" ]]; then
    pass "writable LeRobot output $PICO_F1_MOCAP_DIR"
else
    fail "LeRobot output is not writable: $PICO_F1_MOCAP_DIR"
fi
printf 'CHECK LeRobot startup structure (metadata/footer only; no historical video decoding; timeout=20s)\n'
if timeout --kill-after=2 20 "$ROOT/.venv/bin/python" "$ROOT/scripts/check_lerobot_v3.py" \
    "$PICO_F1_MOCAP_DIR" --allow-empty --startup-only; then
    pass "LeRobot startup compatibility (full media audit is separate)"
else
    audit_rc=$?
    if [[ "$audit_rc" == 124 || "$audit_rc" == 137 ]]; then
        fail "LeRobot startup structure check timed out; inspect storage/metadata (no motion started)"
    else
        fail "existing LeRobot v3 dataset structure is incomplete or incompatible"
    fi
fi
available_kb="$(df -Pk "$PICO_F1_MOCAP_DIR" | awk 'NR==2 {print $4}')"
if [[ "$available_kb" =~ ^[0-9]+$ ]] && (( available_kb >= 5242880 )); then
    pass "LeRobot output free space >= 5 GiB"
else
    fail "LeRobot output free space below 5 GiB or unavailable"
fi

local_ip="$($ROOT/.venv/bin/python -c 'import ip_config; print(ip_config.local_ip)')"
if [[ "$local_ip" == "127.0.0.1" ]] && ip -o -4 addr show lo | grep -Fq " $local_ip/"; then
    pass "project-internal UDP is loopback-only $local_ip"
else
    fail "project-internal UDP must be bound to loopback 127.0.0.1"
fi

probe_dir="$(mktemp -d /tmp/f1-pico-preflight.XXXXXX)"
trap 'rm -rf -- "$probe_dir"' EXIT

# All live ROS checks are independent. Start them together so the two direct
# DDS discovery windows cost seven seconds in total instead of fourteen, while
# retaining the same timeouts and acceptance criteria.
timeout 10 ros2 topic echo --qos-reliability best_effort \
    /hal/joint_states sensor_msgs/msg/JointState --once >"$probe_dir/hal" 2>&1 &
hal_probe_pid=$!
timeout 15 ros2 topic echo /move/State move/msg/State --once --field speed \
    >"$probe_dir/move_state" 2>&1 &
move_probe_pid=$!
timeout 12 ros2 service info /move/SetControlMode \
    >"$probe_dir/mode_service" 2>&1 &
mode_probe_pid=$!
timeout 12 ros2 topic info --no-daemon --spin-time 7 \
    /move/ManualMoveCmd >"$probe_dir/chassis_topic" 2>&1 &
chassis_probe_pid=$!
timeout 15 "$ROOT/.venv/bin/python" "$ROOT/scripts/check_chassis_braking.py" \
    >"$probe_dir/braking" 2>&1 &
braking_probe_pid=$!
timeout 12 ros2 topic info --no-daemon --spin-time 7 \
    /motion_ctl/joint_ctl >"$probe_dir/joint_topic" 2>&1 &
joint_probe_pid=$!

camera_specs=(
    '/camera/left_wrist/color/image_raw/compressed|[r]ealsense2_camera_node .*__node:=left_wrist'
    '/camera/right_wrist/color/image_raw/compressed|[r]ealsense2_camera_node .*__node:=right_wrist'
    '/camera/head/color/image_raw/compressed|[c]omponent_container .*__ns:=/camera/head'
)
camera_probe_pids=()
for index in "${!camera_specs[@]}"; do
    topic="${camera_specs[$index]%%|*}"
    timeout 10 ros2 topic echo --qos-reliability best_effort \
        "$topic" sensor_msgs/msg/CompressedImage --once --field header >"$probe_dir/camera-$index" 2>&1 &
    camera_probe_pids+=("$!")
done

if wait "$hal_probe_pid"; then
    pass "live /hal/joint_states"
else
    fail "no /hal/joint_states sample within 10 seconds"
fi

# The supplier chassis participant is remote on the robot subnet and uses
# legacy DDS type metadata. Endpoint counts are unreliable, so require a real
# deserialized feedback sample instead.
chassis_feedback_ok=false
if wait "$move_probe_pid"; then
    chassis_feedback_ok=true
    pass "live /move/State speed.linear.x/y and speed.angular.z"
else
    fail "no deserializable /move/State sample within 15 seconds"
fi

if wait "$braking_probe_pid"; then
    grep '^CHASSIS_BRAKING_' "$probe_dir/braking" || true
    pass "supplier yaw braking configuration (physical stop angle not certified)"
else
    tail -8 "$probe_dir/braking" >&2
    fail "supplier yaw braking check failed; inspect parameters, do not bypass"
fi

# Receiving State alone is insufficient on a multi-NIC F1: partial DDS
# discovery can still hide the mode service and the ManualMoveCmd consumer.
# Query DDS directly so a long-lived ros2 daemon cannot report stale endpoints.
wait "$mode_probe_pid" || true
mode_service_servers="$(sed -n 's/^Services count: //p' "$probe_dir/mode_service")"
[[ "$mode_service_servers" == "1" ]] \
    && pass "/move/SetControlMode service count=1" \
    || fail "/move/SetControlMode service count=${mode_service_servers:-unknown}, expected 1"

wait "$chassis_probe_pid" || true
chassis_subscriptions="$(sed -n 's/^Subscription count: //p' "$probe_dir/chassis_topic")"
[[ "$chassis_subscriptions" == "1" ]] \
    && pass "/move/ManualMoveCmd subscription count=1" \
    || fail "/move/ManualMoveCmd subscription count=${chassis_subscriptions:-unknown}, expected 1"
if ! $chassis_feedback_ok || [[ "$mode_service_servers" != 1 || "$chassis_subscriptions" != 1 ]]; then
    printf 'CHASSIS_PATH_UNAVAILABLE: check platform services, chassis NIC and DDS; this check does not depend on PICO connection order. No motion started.\n' >&2
fi

wait "$joint_probe_pid" || true
subscriptions="$(sed -n 's/^Subscription count: //p' "$probe_dir/joint_topic")"
[[ "$subscriptions" == "1" ]] \
    && pass "/motion_ctl/joint_ctl subscription count=1" \
    || fail "/motion_ctl/joint_ctl subscription count=${subscriptions:-unknown}, expected 1"

motor_processes="$(pgrep -fc '[m]otor_ctl_node --ros-args' || true)"
[[ "$motor_processes" == "1" ]] \
    && pass "local motor_ctl_node process count=1" \
    || fail "local motor_ctl_node process count=${motor_processes:-unknown}, expected 1"

for index in "${!camera_specs[@]}"; do
    spec="${camera_specs[$index]}"
    topic="${spec%%|*}"
    process_pattern="${spec#*|}"
    camera_processes="$(pgrep -fc "$process_pattern" || true)"
    [[ "$camera_processes" == "1" ]] \
        && pass "$topic local camera process count=1" \
        || fail "$topic local camera process count=${camera_processes:-unknown}, expected 1"
    if wait "${camera_probe_pids[$index]}"; then
        pass "$topic live sample"
    else
        fail "$topic no live sample within 10 seconds"
    fi
done

rm -rf -- "$probe_dir"
trap - EXIT

if pgrep -af '/home/user/lxl/.+(teleop|all_remote)' >/dev/null; then
    fail "legacy /home/user/lxl teleop process is running"
else
    pass "no legacy /home/user/lxl teleop process"
fi

if (( failures )); then
    printf '\nPRECHECK_FAILED count=%d\n' "$failures" >&2
    exit 1
fi
printf '\nPRECHECK_OK\n'
