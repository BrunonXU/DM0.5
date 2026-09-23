#!/usr/bin/env bash
# Default is read-only status. to-start/restore require onsite Enter in Python.
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
client="$root/releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body"
mode="${1:-status}"
if [[ $# -gt 0 ]]; then shift; fi
case "$mode" in status|capture|plan-start|plan-restore|to-start|restore) ;; *)
  echo 'Usage: ./pose_v3.sh [status|capture|plan-start|plan-restore|to-start|restore] [057]' >&2
  exit 2;;
esac
if [[ $# -gt 0 && "$1" == 057 ]]; then shift; fi
if [[ $# -gt 0 ]]; then echo 'This release is bound to 057 and the saved baseline pose.' >&2; exit 2; fi
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
export ROS_LOG_DIR="$root/runtime/pose_runs/ros_logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$client/robot${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m f1_replay.pose_cli "$mode" --config "$client/configs/pose_setup.json"
