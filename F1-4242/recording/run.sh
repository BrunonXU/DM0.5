#!/usr/bin/env bash
# Starts only the independent recorder supervisor; no collection begins by startup.
set -Eeo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "${F1_ENV_LOADED:-}" != 1 && -f "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}" ]]; then
  source "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}"
fi
export F1_INFRA_HOME="${F1_INFRA_HOME:-$HOME/Bruno/F1-infra}"
export F1_CONSOLE_RUNTIME="${F1_CONSOLE_RUNTIME:-$F1_INFRA_HOME/runtime}"
export F1_CONTROL_LOCK="${F1_CONTROL_LOCK:-$F1_CONSOLE_RUNTIME/control.lock}"
source "${F1_ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
source "${F1_ROBOT_SETUP:-/home/user/workspace/f1/install/setup.bash}"
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 F1_ENV_LOADED=1
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}"
unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS
export CYCLONEDDS_URI="${F1_CYCLONEDDS_URI:-/etc/ros/cyclonedds/cyclonedds.xml}"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec "${F1_RECORDING_PYTHON:-$F1_CONSOLE_RUNTIME/recording-venv/bin/python}" -m recording.worker --runtime "$F1_CONSOLE_RUNTIME" "$@"
