#!/usr/bin/env bash
# F1-09 camera-only DDS wrapper; never starts motor_ctl or alters old launch files.
# Both D405 color/depth streams explicitly use the verified USB2-supported
# 640x480x30 profiles. Head launch parameters remain unchanged at its 30fps.
# Usage: bash scripts/60_f1_camera_internal.sh check|start head|left|right
# Use check first. The onsite operator ends the selected old camera launch before
# start; no process is stopped automatically. Keep the original launch for rollback.
set -eo pipefail
if [[ $# -ne 2 || ! "$1" =~ ^(check|start)$ || ! "$2" =~ ^(head|left|right)$ ]]; then
  echo "Usage: bash $0 check|start head|left|right" >&2
  exit 2
fi
camera_action="$1"
camera_side="$2"
camera_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
camera_profile="${F1_CAMERA_FASTDDS_PROFILE:-${camera_root}/configs/f1_camera_fastdds_internal.xml}"
camera_profile="$(realpath -e -- "$camera_profile")"
if [[ "$(cat /etc/machine-id)" != '90429b8a96e04ef79dbc12175b538115' ]]; then
  echo 'This camera profile is bound to F1-09; refusing another machine.' >&2
  exit 1
fi
for camera_setup in \
  /opt/ros/jazzy/setup.bash \
  /home/user/workspace/f1/install/setup.bash \
  /home/user/workspace/camera/OrbbecSDK_ROS2/install/setup.bash \
  /home/user/workspace/camera/ros2_ws/install/setup.bash; do
  if [[ ! -r "$camera_setup" ]]; then
    echo "Missing existing camera overlay: $camera_setup" >&2
    exit 1
  fi
  source "$camera_setup"
done
set -u
# Preserve the original Fast DDS implementation. In particular, do not silently
# inherit the DAgger terminal's Cyclone RMW and ignore the Fast DDS XML.
if [[ "${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}" != 'rmw_fastrtps_cpp' ]]; then
  echo 'Use the original camera shell environment: inherited RMW is not rmw_fastrtps_cpp.' >&2
  exit 1
fi
if [[ "${ROS_DOMAIN_ID:-0}" != '0' || "${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}" != 'SUBNET' \
      || -n "${ROS_STATIC_PEERS:-}" || "${ROS_LOCALHOST_ONLY:-0}" != '0' ]]; then
  echo 'Camera ROS discovery differs from the verified domain 0 / SUBNET environment.' >&2
  exit 1
fi
camera_stream_args=()
case "$camera_side" in
  head) camera_package=orbbec_camera; camera_launch=head_gemini330.launch.py; camera_namespace=/camera/head ;;
  left) camera_package=realsense2_camera; camera_launch=rs_launch_left_wrist_d405.py; camera_namespace=/camera/left_wrist; camera_stream_args=(depth_module.color_profile:=640,480,30 depth_module.depth_profile:=640,480,30) ;;
  right) camera_package=realsense2_camera; camera_launch=rs_launch_right_wrist_d405.py; camera_namespace=/camera/right_wrist; camera_stream_args=(depth_module.color_profile:=640,480,30 depth_module.depth_profile:=640,480,30) ;;
esac
/usr/bin/python3 - "$camera_profile" "$camera_action" "$camera_package" "$camera_launch" "$camera_namespace" "${camera_stream_args[@]}" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from ament_index_python.packages import get_package_share_directory

profile, action, package, launch, namespace, *stream_args = sys.argv[1:]
path = Path(profile).resolve(strict=True)
ns = {'d': 'http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'}
tree = ET.parse(path)
transports = tree.findall('.//d:transport_descriptor', ns)
expected = {'f1_camera_internal_udp': 'UDPv4', 'f1_camera_shm': 'SHM'}
actual = {t.findtext('d:transport_id', namespaces=ns): t.findtext('d:type', namespaces=ns) for t in transports}
if actual != expected or len(transports) != 2:
    raise SystemExit('Expected only the internal UDPv4 and original SHM transports')
udp = next(t for t in transports if t.findtext('d:type', namespaces=ns) == 'UDPv4')
addresses = [x.text for x in udp.findall('d:interfaceWhiteList/d:address', ns)]
if sorted(addresses) != ['127.0.0.1', '192.168.127.170']:
    raise SystemExit('Camera UDP whitelist differs from the verified internal addresses')
participants = tree.findall('.//d:participant', ns)
if len(participants) != 1 or participants[0].get('is_default_profile') != 'true':
    raise SystemExit('Expected one default camera participant profile')
rtps = participants[0].find('d:rtps', ns)
if rtps is None or rtps.findtext('d:useBuiltinTransports', namespaces=ns) != 'false':
    raise SystemExit('Unrestricted builtin transports must be disabled')
if sorted(x.text for x in rtps.findall('d:userTransports/d:transport_id', ns)) != sorted(expected):
    raise SystemExit('Camera participant must use both internal UDPv4 and SHM')
links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', 'enP3p1s0f1'], text=True))
if not any('UP' in row.get('flags', []) and any(a.get('local') == '192.168.127.170' for a in row.get('addr_info', [])) for row in links):
    raise SystemExit('Verified internal interface/address is not up; do not fall back to Wi-Fi')
launch_path = Path(get_package_share_directory(package)) / 'launch' / launch
if not launch_path.is_file():
    raise SystemExit('Original camera launch is missing: ' + str(launch_path))
active = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        args = proc.joinpath('cmdline').read_bytes().decode().split('\0')
    except (FileNotFoundError, PermissionError, ProcessLookupError, UnicodeDecodeError):
        continue
    old_launch = any(args[i:i+3] == ['launch', package, launch] for i in range(len(args)-2))
    old_node = '__ns:=' + namespace in args
    if package == 'realsense2_camera':
        parent, _, node = namespace.rpartition('/')
        old_node = old_node or ('__ns:=' + parent in args and '__node:=' + node in args)
    if old_launch or old_node:
        active.append(int(proc.name))
print(json.dumps({'camera_launch': str(launch_path), 'profile': str(path), 'existing_camera_pids': active,
                  'mode': action, 'stream_arguments': stream_args, 'checks_only_no_ros_node': True}, ensure_ascii=False), flush=True)
if active and action == 'start':
    raise SystemExit('Selected camera is already running; onsite operator must end its old launch first')
PY
if [[ "$camera_action" == 'check' ]]; then
  echo 'Camera configuration checks passed; no camera or ROS node was started.'
  exit 0
fi
# Pin the implementation we just validated even if the inherited variable was
# absent; selecting a different build default would silently ignore this XML.
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export FASTRTPS_DEFAULT_PROFILES_FILE="$camera_profile"
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
echo "Starting original $camera_side camera with internal DDS UDP + SHM; no robot control nodes."
exec /usr/bin/python3 /opt/ros/jazzy/bin/ros2 launch "$camera_package" "$camera_launch" "${camera_stream_args[@]}"
