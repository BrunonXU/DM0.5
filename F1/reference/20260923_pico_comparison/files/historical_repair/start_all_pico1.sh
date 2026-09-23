#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=/home/user/lxl/XRoboToolkit-Teleop-Sample-Python
REMOTE_DIST=/home/user/lxl/remote_sth/dist
TELEOP_DIST="$PROJECT_DIR/dist"
SAFETY_DIR=/home/user/cyb_safe_control_real/src/python
F1_URDF="$PROJECT_DIR/assets/arx/f1_wheel/urdf/f1_wheel_0602_new.urdf"
CONDA_BASE=/home/user/miniconda3
ENV_NAME=pico4ultra_py312
LOG_DIR=/home/user/lxl/pico_logs
RUN_DIR=/home/user/lxl/pico_run
HEALTHCHECK=/home/user/lxl/check_f1_pico.sh
XR_CHECK=/home/user/lxl/check_pico_xr_data.py

usage() {
    cat <<'EOF'
Usage:
  ./start_all_pico1.sh --check
  ./start_all_pico1.sh --allow-motion

--check         Validate the encrypted/PyArmor deployment and run the read-only
                F1 preflight. No robot command is published by this mode.
--allow-motion  Start encrypted teleoperation after all gates pass. This moves
                both arms to ip_config.py target_left/target_right during
                all_remote_lxl.py startup.
EOF
}

mode="${1:-}"
case "$mode" in
    --check|--allow-motion) ;;
    *) usage >&2; exit 2 ;;
esac

required=(
    "$REMOTE_DIST/all_remote_lxl.py"
    "$REMOTE_DIST/pyarmor_runtime_000000/pyarmor_runtime.so"
    "$TELEOP_DIST/scripts/simulation/teleop_lxl.py"
    "$TELEOP_DIST/pyarmor_runtime_000000/pyarmor_runtime.so"
    "$SAFETY_DIR/F1_safe_control_real_with_com.py"
    "$SAFETY_DIR/com_and_collision_protect_modules.py"
    "$SAFETY_DIR/pyarmor_runtime_000000/pyarmor_runtime.so"
    "$F1_URDF"
    "$HEALTHCHECK"
    "$XR_CHECK"
)
for path in "${required[@]}"; do
    [[ -e "$path" ]] || { printf 'ERROR: missing %s\n' "$path" >&2; exit 1; }
done

set +u
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
# shellcheck disable=SC1091
source /home/user/workspace/f1/install/setup.bash
set -u
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"
export RCUTILS_LOGGING_MAX_SEVERITY=2
export LD_LIBRARY_PATH="$PROJECT_DIR/dependencies/XRoboToolkit-PC-Service-Pybind/lib/aarch64:${LD_LIBRARY_PATH:-}"

printf 'Checking encrypted safety and teleoperation imports...\n'
PYTHONPATH="$SAFETY_DIR:$TELEOP_DIST:$PROJECT_DIR:${PYTHONPATH:-}" python - <<'PY'
import F1_safe_control_real_with_com
import com_and_collision_protect_modules
from xrobotoolkit_teleop.simulation.placo_teleop_controller import PlacoTeleopController
print("ENCRYPTED_IMPORTS_OK")
PY

(
    cd "$REMOTE_DIST"
    PYTHONPATH="$REMOTE_DIST:$SAFETY_DIR:$TELEOP_DIST:$PROJECT_DIR:${PYTHONPATH:-}" \
        python -c 'import all_remote_lxl; assert callable(all_remote_lxl.main)'
)

# The supplied encrypted teleop script still defaults to an older Tianji M6
# URDF. Always pass the current F1 URDF explicitly, including during this
# parser-only check.
PYTHONPATH="$TELEOP_DIST:$SAFETY_DIR:$PROJECT_DIR:${PYTHONPATH:-}" \
    python "$TELEOP_DIST/scripts/simulation/teleop_lxl.py" \
    --robot-urdf-path "$F1_URDF" --help >/dev/null
printf 'ENCRYPTED_STATIC_IMPORTS_OK\n'

"$HEALTHCHECK"

cat >&2 <<'EOF'
ERROR: the supplied encrypted teleop bundle is not compatible with the F1 URDF.
Its runtime controller requires the legacy frame Link7_R; F1 provides
arm_R_Link7. Import and --help checks alone cannot detect this incompatibility.
Use ./start_all_pico2.sh --check and ./start_all_pico2.sh --allow-motion, or
replace dist with an encrypted build generated from the F1 source controller.
EOF
exit 1

mkdir -p "$LOG_DIR" "$RUN_DIR"

start_service() {
    if pgrep -f '(^|/)RoboticsServiceProcess([[:space:]]|$)' >/dev/null; then
        printf 'REUSED RoboticsServiceProcess pid=%s\n' \
            "$(pgrep -f '(^|/)RoboticsServiceProcess([[:space:]]|$)' | head -1)"
        return
    fi

    : >"$LOG_DIR/roboticsservice.log"
    (
        cd /opt/apps/roboticsservice
        nohup ./runService.sh >>"$LOG_DIR/roboticsservice.log" 2>&1 &
        printf '%s\n' "$!" >"$RUN_DIR/roboticsservice-launcher.pid"
    )
    for _ in {1..20}; do
        if pgrep -f '(^|/)RoboticsServiceProcess([[:space:]]|$)' >/dev/null \
            && ss -lnt 2>/dev/null | grep -q ':60061 '; then
            break
        fi
        sleep 0.5
    done
    pgrep -f '(^|/)RoboticsServiceProcess([[:space:]]|$)' >/dev/null || {
        printf 'ERROR: RoboticsServiceProcess failed to start\n' >&2
        tail -80 "$LOG_DIR/roboticsservice.log" >&2 || true
        exit 1
    }
    pgrep -f '(^|/)RoboticsServiceProcess([[:space:]]|$)' | head -1 \
        >"$RUN_DIR/roboticsservice.pid"
    printf 'STARTED RoboticsServiceProcess pid=%s\n' \
        "$(<"$RUN_DIR/roboticsservice.pid")"
}

start_python() {
    local name="$1"
    local cwd="$2"
    local primary_pythonpath="$3"
    local script="$4"
    shift 4
    local logfile="$LOG_DIR/$name.log"
    local pidfile="$RUN_DIR/$name.pid"

    if pgrep -f "$script" >/dev/null; then
        printf 'ERROR: %s is already running\n' "$script" >&2
        exit 1
    fi

    : >"$logfile"
    (
        cd "$cwd"
        export PYTHONPATH="$primary_pythonpath:$SAFETY_DIR:$TELEOP_DIST:$PROJECT_DIR:${PYTHONPATH:-}"
        nohup python -u "$script" "$@" >>"$logfile" 2>&1 &
        printf '%s\n' "$!" >"$pidfile"
    )
    sleep 3
    local pid
    pid="$(<"$pidfile")"
    if ! kill -0 "$pid" 2>/dev/null; then
        printf 'ERROR: %s exited during startup; tail follows\n' "$name" >&2
        tail -100 "$logfile" >&2 || true
        exit 1
    fi
    printf 'STARTED %s pid=%s log=%s\n' "$name" "$pid" "$logfile"
}

cleanup_on_exit() {
    local rc=$?
    if (( rc != 0 )); then
        /home/user/lxl/stop_all_pico.sh --managed-only || true
    fi
    exit "$rc"
}
trap cleanup_on_exit EXIT

start_service

# Require live, non-zero controller tracking immediately before any robot
# command path is opened. Press Send in the PICO app and keep both controllers
# awake before using --allow-motion.
PYTHONPATH="$TELEOP_DIST:$PROJECT_DIR:${PYTHONPATH:-}" \
    python "$XR_CHECK" --seconds 5

# This process performs the configured three-second initial arm move, then
# opens UDP ports 8888 (arm commands) and 8899 (record control).
start_python all_remote "$REMOTE_DIST" "$REMOTE_DIST" \
    "$REMOTE_DIST/all_remote_lxl.py"
for port in 8888 8899; do
    for _ in {1..30}; do
        ss -lun 2>/dev/null | grep -q ":$port " && break
        sleep 0.5
    done
    if ! ss -lun 2>/dev/null | grep -q ":$port "; then
        printf 'ERROR: expected UDP listener %s did not appear\n' "$port" >&2
        tail -100 "$LOG_DIR/all_remote.log" >&2 || true
        exit 1
    fi
done

start_python teleop "$PROJECT_DIR" "$TELEOP_DIST" \
    "$TELEOP_DIST/scripts/simulation/teleop_lxl.py" \
    --robot-urdf-path "$F1_URDF"

trap - EXIT
printf '\nPICO_ENCRYPTED_TELEOP_STARTED\n'
printf 'Logs: tail -F %s/all_remote.log %s/teleop.log %s/roboticsservice.log\n' \
    "$LOG_DIR" "$LOG_DIR" "$LOG_DIR"
printf 'Stop: /home/user/lxl/stop_all_pico.sh\n'
