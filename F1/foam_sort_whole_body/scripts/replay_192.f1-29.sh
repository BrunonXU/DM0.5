#!/usr/bin/env bash
# 4F-F1 dataset baseline. Uses the already deployed replay/pose/recorder code.
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
release="$root/releases/20260918-4F-replay-192-v1"
client="$root/releases/20260918-4F-raw20-25k-v1/workspace/F1/foam_sort_whole_body"
mode="${1:-inspect}"
if (( $# > 1 )); then echo '用法：replay_192.sh [inspect|dry-run|to-start|preflight|record-check|execute]' >&2; exit 2; fi
case "$mode" in
  inspect|dry-run|to-start|preflight|record-check|execute) ;;
  *) echo "未知模式：$mode" >&2; exit 2 ;;
esac
if [[ "$(cat /etc/machine-id)" != f8181edeb110424bbd1963cf717dfeee ]]; then
  echo '此入口仅适用于4F-F1 / F1_D071' >&2; exit 2
fi
case "$mode" in
  to-start) exec "$root/pose_192.sh" to-start ;;
  record-check) exec "$root/inference_raw20_25k.sh" record-check --output "$root/runtime/replay_192/record_checks" ;;
  preflight|execute)
    source /opt/ros/jazzy/setup.bash
    source /home/user/workspace/f1/install/setup.bash
    source /home/user/workspace/agv/install/setup.bash
    source "$client/scripts/message_overlay.f1-29.sh"
    export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    export CYCLONEDDS_URI="$client/configs/cyclonedds.f1-29.xml"
    export ROS_LOG_DIR="$root/runtime/replay_192/ros_logs"
    ;;
esac
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$client/robot${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m f1_replay.cli "$mode" \
  --bundle "$release/bundle" --profile "$release/replay.192.f1-29.json" \
  --output "$root/runtime/replay_192"
