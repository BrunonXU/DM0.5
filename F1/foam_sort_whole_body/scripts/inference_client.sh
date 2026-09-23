#!/usr/bin/env bash
set -eo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
mode="${1:-inspect}"
if [[ "$mode" != inspect && "$mode" != --help && "$mode" != -h ]]; then
  for setup in /opt/ros/jazzy/setup.bash /home/user/workspace/f1/install/setup.bash /home/user/workspace/agv/install/setup.bash; do
    if [[ ! -f "$setup" ]]; then echo "缺少F1已有ROS环境：$setup" >&2; exit 2; fi
    source "$setup"
  done
  if [[ -n "${F1_MESSAGE_OVERLAY:-}" ]]; then source "$F1_MESSAGE_OVERLAY"; fi
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  export CYCLONEDDS_URI="${F1_INFERENCE_DDS_CONFIG:-/etc/ros/cyclonedds/cyclonedds.xml}"
  export ROS_LOG_DIR=/home/user/Bruno/F1/foam_sort_whole_body/runtime/inference/ros_logs
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$task_root/robot${PYTHONPATH:+:$PYTHONPATH}"
exec "${F1_INFERENCE_PYTHON:-/usr/bin/python3}" -m f1_policy_client.cli "$@"
