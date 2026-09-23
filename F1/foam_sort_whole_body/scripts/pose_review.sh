#!/usr/bin/env bash
# Existing pose implementation and policy, with explicit per-sample bindings.
# This does NOT fix or certify the existing body-motion vibration issue.
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
release="$root/releases/20260917-base-home-v1"
client="$root/releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body"
mode="${1:-status}"
sample="${2:-003}"
case "$mode" in status|capture|plan-start|to-start) ;; *) echo '模式：status capture plan-start to-start' >&2; exit 2;; esac
case "$sample" in 003|020|096|093|192) ;; *) echo '样本：003 020 096 093 192' >&2; exit 2;; esac
if [[ "$mode" == to-start ]]; then
  echo '注意：沿用旧起姿实现，身体震动问题未修复；出现明显震动立即Ctrl-C，未停止则现场急停。' >&2
fi
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
export ROS_LOG_DIR="$root/runtime/pose_runs/ros_logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$client/robot${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m f1_replay.pose_cli "$mode" --config "$release/configs/pose_$sample.json"
