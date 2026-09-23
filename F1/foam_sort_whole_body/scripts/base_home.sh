#!/usr/bin/env bash
# Independent base-only tool; capture/status never publish commands.
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
release="$root/releases/20260917-base-home-v1"
client="$root/releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body"
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
export ROS_LOG_DIR="$root/runtime/base_home/ros_logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$release/robot${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m f1_replay.base_home "${1:-status}" --profile "$client/configs/replay_f1.json" "${@:2}"
