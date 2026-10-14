#!/usr/bin/env bash
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -f "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}" ]]; then
  source "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}"
fi
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 F1_ENV_LOADED=1
export F1_INFRA_HOME="${F1_INFRA_HOME:-$HOME/Bruno/F1-infra}"
export F1_CONSOLE_RUNTIME="${F1_CONSOLE_RUNTIME:-$F1_INFRA_HOME/runtime}"
export PICO_UPPER_ROOT="${PICO_UPPER_ROOT:-$HERE/integrations/pico_upper}"
export F1_CONSOLE_PORT="${F1_CONSOLE_PORT:-4242}"
if [[ " $* " == *" --offline "* ]]; then
  exec "${F1_CONSOLE_PYTHON:-python3}" "$HERE/server.py" --port "$F1_CONSOLE_PORT" "$@"
fi
source "${F1_ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
source "${F1_ROBOT_SETUP:-/home/user/workspace/f1/install/setup.bash}"
source "${F1_AGV_SETUP:-/home/user/workspace/agv/install/setup.bash}"
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
export CYCLONEDDS_URI="${F1_CYCLONEDDS_URI:-/etc/ros/cyclonedds/cyclonedds.xml}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}"
unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS
f1_web_args=()
if [[ -n "${F1_WEB_ROOT:-}" ]]; then f1_web_args=(--web-root "$F1_WEB_ROOT"); fi
exec "${F1_CONSOLE_PYTHON:-/usr/bin/python3}" "$HERE/server.py" --port "$F1_CONSOLE_PORT" "${f1_web_args[@]}" "$@"
