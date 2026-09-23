#!/usr/bin/env bash
# Source only. No services, collection routing, publishers or system changes.
_pu_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PICO_F1_ROOT="$(cd "$_pu_script/.." && pwd)"
export PICO_UPPER_RUNTIME="${PICO_UPPER_RUNTIME:-/home/user/Bruno/F1/pico_upper/runtime}"
export PICO_UPPER_CONTROL_LOCK="${PICO_UPPER_CONTROL_LOCK:-/home/user/Bruno/F1/runtime/control.lock}"
export PICO_F1_CONFIG="$PICO_F1_ROOT/config/ip_config.py"
export PICO_F1_CONTROL_CONFIG="$PICO_F1_ROOT/config/control.json"
export PICO_F1_INITIAL_POSE="$PICO_UPPER_RUNTIME/session_initial_pose.json"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export PICO_UPPER_RELEASE_HASH="$(sha256sum "$PICO_F1_ROOT/UPSTREAM.json" | cut -c1-16)"
_pu_sdk="${PICO_UPPER_SDK_ROOT:-/home/user/lxl/XRoboToolkit-Teleop-Sample-Python/dependencies/XRoboToolkit-PC-Service-Pybind}"
if [[ -z "${PICO_UPPER_PYTHON:-}" ]]; then
    if [[ -x "$PICO_UPPER_RUNTIME/venv/bin/python" ]]; then
        export PICO_UPPER_PYTHON="$PICO_UPPER_RUNTIME/venv/bin/python"
    else
        export PICO_UPPER_PYTHON=/home/user/miniconda3/envs/pico4ultra_py312/bin/python
    fi
fi
set +u
source /opt/ros/jazzy/setup.bash
source "${PICO_UPPER_ROS_SETUP:-/home/user/workspace/f1/install/setup.bash}"
set -u
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS
if [[ -r "${F1_CYCLONEDDS_URI:-/etc/ros/cyclonedds/cyclonedds.xml}" ]]; then
    export CYCLONEDDS_URI="${F1_CYCLONEDDS_URI:-/etc/ros/cyclonedds/cyclonedds.xml}"
fi
export PYTHONPATH="$PICO_F1_ROOT/src:$PICO_F1_ROOT/config:$PICO_F1_ROOT/vendor/xrobotoolkit:$PICO_F1_ROOT/vendor/f1_safety/src/python:$_pu_sdk/build/lib.linux-aarch64-cpython-312${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$_pu_sdk/lib/aarch64:/home/user/miniconda3/envs/pico4ultra_py312/lib/python3.12/site-packages/cmeel.prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TMPDIR="$PICO_UPPER_RUNTIME/tmp"
mkdir -p "$PICO_UPPER_RUNTIME/logs" "$TMPDIR"
unset _pu_script _pu_sdk
