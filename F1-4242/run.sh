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
# An explicitly empty path means setup could not identify this environment.
# Keep legacy defaults only when the variable was never configured.
f1_ros_setup="${F1_ROS_SETUP-/opt/ros/jazzy/setup.bash}"
f1_robot_setup="${F1_ROBOT_SETUP-/home/user/workspace/f1/install/setup.bash}"
f1_agv_setup="${F1_AGV_SETUP-/home/user/workspace/agv/install/setup.bash}"
if [[ "${F1_CYCLONEDDS_URI+x}" == x && -z "$F1_CYCLONEDDS_URI" ]]; then
  echo "DDS 配置尚未确认：请核对 configs/installation.env 和 runtime/setup-report.json。仅预览页面可加 --offline。" >&2
  exit 2
fi
for f1_setup_path in "$f1_ros_setup" "$f1_robot_setup" "$f1_agv_setup"; do
  if [[ -z "$f1_setup_path" || ! -f "$f1_setup_path" ]]; then
    echo "机器人环境尚未配置完成：请核对 configs/installation.env 和 runtime/setup-report.json。仅预览页面可加 --offline。" >&2
    exit 2
  fi
done
source "$f1_ros_setup"
source "$f1_robot_setup"
source "$f1_agv_setup"
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
export CYCLONEDDS_URI="${F1_CYCLONEDDS_URI:-/etc/ros/cyclonedds/cyclonedds.xml}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}"
unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS
f1_web_args=()
if [[ -n "${F1_WEB_ROOT:-}" ]]; then f1_web_args=(--web-root "$F1_WEB_ROOT"); fi
exec "${F1_CONSOLE_PYTHON:-/usr/bin/python3}" "$HERE/server.py" --port "$F1_CONSOLE_PORT" "${f1_web_args[@]}" "$@"
