#!/usr/bin/env bash

_pico_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PICO_F1_ROOT="$(cd "$_pico_script_dir/.." && pwd)"
export PICO_F1_CONFIG="$PICO_F1_ROOT/config/ip_config.py"
export PICO_F1_CONTROL_CONFIG="$PICO_F1_ROOT/config/control.json"
export PICO_F1_DATA_DIR="$PICO_F1_ROOT/data"
# Frozen per-session scene, or collection.yaml when no session exists.
_pico_collection_env="$("$PICO_F1_ROOT/.venv/bin/python" "$PICO_F1_ROOT/scripts/collection_session.py" env)" || return 1
eval "$_pico_collection_env"
unset _pico_collection_env
export PICO_F1_INITIAL_POSE="$PICO_F1_ROOT/run/session_initial_pose.json"

set +u
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
# shellcheck disable=SC1091
source /home/user/workspace/f1/install/setup.bash
# The supplier base stack publishes legacy DDS type metadata; this overlay is
# required so Python and rosbag2 can deserialize move/msg/State.
# shellcheck disable=SC1091
source "${F1_BASE_INTERFACE_SETUP:-/home/user/zxl/install/local_setup.bash}"
set -u

# The arm nodes are local, while the supplier chassis stack is a legacy DDS
# participant on the robot subnet. Cyclone DDS interoperates with both; Fast
# DDS cannot deserialize the live move/msg/State stream on this F1 image.
#
# F1 robots have multiple active NICs. Letting CycloneDDS choose one
# automatically can select WLAN and hide the supplier chassis participant.
# The robot-owned file binds the correct chassis NIC (for example enP3p1s0f1
# on D070 and eth0 on D072). An explicit override remains available for images
# that keep the configuration elsewhere.
unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS
_pico_cyclonedds_uri="${F1_CYCLONEDDS_URI:-/etc/ros/cyclonedds/cyclonedds.xml}"
if [[ -r "$_pico_cyclonedds_uri" ]]; then
    export CYCLONEDDS_URI="$_pico_cyclonedds_uri"
else
    unset CYCLONEDDS_URI
fi
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET

_pico_python_paths=(
    "$PICO_F1_ROOT/src"
    "$PICO_F1_ROOT/config"
    "$PICO_F1_ROOT/vendor/xrobotoolkit"
    "$PICO_F1_ROOT/vendor/xrobotoolkit/sdk/build/lib.linux-aarch64-cpython-312"
    "$PICO_F1_ROOT/vendor/f1_bridge"
    "$PICO_F1_ROOT/vendor/f1_safety/src/python"
)
export PYTHONPATH="$(IFS=:; printf '%s' "${_pico_python_paths[*]}")${PYTHONPATH:+:$PYTHONPATH}"

_pico_library_paths=(
    "$PICO_F1_ROOT/vendor/xrobotoolkit/sdk/lib/aarch64"
    "$PICO_F1_ROOT/vendor/roboticsservice"
    "$PICO_F1_ROOT/vendor/roboticsservice/lib"
    "$PICO_F1_ROOT/vendor/roboticsservice/SDK/arm64"
    "$PICO_F1_ROOT/.venv/lib/python3.12/site-packages/cmeel.prefix/lib"
)
export LD_LIBRARY_PATH="$(IFS=:; printf '%s' "${_pico_library_paths[*]}")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export QT_PLUGIN_PATH="$PICO_F1_ROOT/vendor/roboticsservice/plugins${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
export QT_QML_PATH="$PICO_F1_ROOT/vendor/roboticsservice/qml${QT_QML_PATH:+:$QT_QML_PATH}"
export RCUTILS_LOGGING_MAX_SEVERITY=2

unset _pico_script_dir _pico_cyclonedds_uri _pico_python_paths _pico_library_paths
