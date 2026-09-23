#!/usr/bin/env bash
# Reviewed diagnostic sample packages; retains the original source clock.
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
release="$root/releases/20260917-base-home-v1"
client="$root/releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body"
mode="${1:-inspect}"
sample="${2:-003}"
case "$sample" in
  003) episode=all_data_2_derived_episode_000003;;
  020|096) episode=all_data_1_derived_episode_000"$sample";;
  093|192) episode=all_data_derived_episode_000"$sample";;
  *) echo '诊断样本：003 020 096 093 192；057沿用原入口，058/135暂不加入。' >&2; exit 2;;
esac
case "$mode" in inspect|dry-run|preflight|execute) ;; *) echo '模式：inspect dry-run preflight execute' >&2; exit 2;; esac
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
export ROS_LOG_DIR="$root/runtime/replay/ros_logs"
export PYTHONDONTWRITEBYTECODE=1
source /home/user/workspace/agv/install/setup.bash
exec "$client/scripts/replay.sh" "$mode" --bundle "$release/bundles/$episode" --profile "$client/configs/replay_f1.json"
