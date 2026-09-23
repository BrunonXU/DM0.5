#!/usr/bin/env bash
# D071 task preparation; no driver startup or changes to other controllers.
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
client="$root/releases/20260918-pose-192-4f-v1/workspace/F1/foam_sort_whole_body"
mode="${1:-plan-start}"
if (( $# )); then shift; fi
if (( $# )); then echo 'Usage: ./pose_192.sh [target|status|plan-start|to-start]' >&2; exit 2; fi
case "$mode" in
  target) cat "$client/results/pose_192_4f_20260918/target.json"; exit 0 ;;
  status|plan-start|to-start) ;;
  *) echo 'Usage: ./pose_192.sh [target|status|plan-start|to-start]' >&2; exit 2 ;;
esac
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="$client/configs/cyclonedds.f1-29.xml"
export ROS_LOG_DIR="$root/runtime/pose_runs/ros_logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$client/robot${PYTHONPATH:+:$PYTHONPATH}"
/usr/bin/python3 -m f1_replay.pose_cli "$mode" --config "$client/configs/pose_192.f1-29.json"
if [[ "$mode" == to-start ]]; then
  echo '192关节目标已稳定到达、停车已确认；只读记录本次身体参考。'
  "$root/inference_5k.sh" capture-body
fi
