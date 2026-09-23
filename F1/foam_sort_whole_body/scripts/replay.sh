#!/usr/bin/env bash
# inspect/dry-run are offline; ROS is loaded only for explicitly selected modes.
set -eo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
mode="${1:-inspect}"
case "$mode" in
  check-env|feedback|preflight|execute)
    ros_setup="${F1_REPLAY_ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
    workspace_setup="${F1_REPLAY_WORKSPACE_SETUP:-/home/user/workspace/f1/install/setup.bash}"
    if [[ -n "${F1_REPLAY_ROS_SETUP:-}" && ! -f "$ros_setup" ]]; then
      echo "ROS setup不存在：$ros_setup" >&2
      exit 2
    fi
    if [[ -n "${F1_REPLAY_WORKSPACE_SETUP:-}" && ! -f "$workspace_setup" ]]; then
      echo "workspace setup不存在：$workspace_setup" >&2
      exit 2
    fi
    if [[ -f "$ros_setup" ]]; then source "$ros_setup"; fi
    if [[ -f "$workspace_setup" ]]; then source "$workspace_setup"; fi
    ;;
esac
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$task_root/robot${PYTHONPATH:+:$PYTHONPATH}"
exec "${F1_REPLAY_PYTHON:-/usr/bin/python3}" -m f1_replay.cli "$@"
