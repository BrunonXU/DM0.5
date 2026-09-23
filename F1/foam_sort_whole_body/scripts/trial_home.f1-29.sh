#!/usr/bin/env bash
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
client="$root/releases/20260918-4F-home-v2/workspace/F1/foam_sort_whole_body"
mode="${1:-status}"
if (( $# > 1 )); then echo 'Usage: ./trial_home.sh [capture|status|align|return-base|restore-pose|restore]' >&2; exit 2; fi
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
source "$client/scripts/message_overlay.f1-29.sh"
export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="$client/configs/cyclonedds.f1-29.xml"
export ROS_LOG_DIR="$root/runtime/base_home/ros_logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$client/robot${PYTHONPATH:+:$PYTHONPATH}"
/usr/bin/python3 -m f1_replay.trial_home "$mode" --config "$client/configs/trial_home.f1-29.json"
