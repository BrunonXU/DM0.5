#!/usr/bin/env bash
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
export F1_CONSOLE_RUNTIME="${F1_CONSOLE_RUNTIME:-/home/user/Bruno/F1/F1-4242/runtime}"
mkdir -p "$F1_CONSOLE_RUNTIME/ros_logs"
export ROS_LOG_DIR="$F1_CONSOLE_RUNTIME/ros_logs"
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS
exec /usr/bin/python3 "$HERE/server.py" --runtime "$F1_CONSOLE_RUNTIME" --web-root /home/user/Bruno/F1/F1-4242/dev/web "$@"
