"""Read-only inventory before migrating a robot client; emits bounded evidence."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess

ROOT = Path('/home/user/Bruno')
MOTOR = Path('/home/user/workspace/f1/install/motor_ctl/lib/motor_ctl/motor_ctl_node')


def describe(path, text=False):
    path = Path(path)
    result = {'path': str(path), 'exists': path.exists(), 'symlink': path.is_symlink()}
    if path.is_symlink():
        result['target'] = os.readlink(path)
    if path.is_file():
        data = path.read_bytes()
        result.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        if text and len(data) < 300000:
            result['text'] = data.decode()
    return result


result = {'utc': datetime.now(timezone.utc).isoformat(), 'host': socket.gethostname(),
          'architecture': platform.machine(), 'robot_commands_sent': 0,
          'bruno': describe(ROOT), 'task': describe(ROOT / 'F1/foam_sort_whole_body'),
          'files': {}, 'processes': []}
files = [str(MOTOR),
         '/home/user/workspace/f1/install/motor_ctl/lib/motor_ctl/kinematics_node',
         '/home/user/workspace/f1/install/tj_sdk/lib/libKine.so',
         '/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg',
         '/etc/ros/cyclonedds/cyclonedds.xml', '/etc/os-release']
for suffix in ('src/motor_ctl_core.cpp', 'src/tj_arms/omnipicker_gripper.cpp',
               'src/kinematics_core.cpp', 'src/tj_arms/arm_ctl_tj_sdk.cpp'):
    files.append('/home/user/workspace/f1/src/motor_ctl/' + suffix)
for suffix in ('motor_ctl.launch.xml', 'kinematics.launch.xml'):
    files.append('/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/launch/' + suffix)
for name in files:
    result['files'][name] = describe(name, text=not name.endswith(('.so', '_node')))
for path in Path('/proc').iterdir():
    if not path.name.isdigit():
        continue
    try:
        comm = (path / 'comm').read_text().strip()
        if comm not in ('motor_ctl_node', 'kinematics_node'):
            continue
        entry = describe(path / 'exe')
        entry.update(pid=int(path.name), comm=comm,
                     cmdline=(path / 'cmdline').read_bytes().decode().split('\0'))
        # Only ROS transport variables; never collect credentials/general environment.
        entry['ros_environment'] = [x for x in (path / 'environ').read_bytes().decode().split('\0')
                                    if x.startswith(('ROS_DOMAIN_ID=', 'RMW_IMPLEMENTATION=', 'CYCLONEDDS_URI='))]
        result['processes'].append(entry)
    except (FileNotFoundError, PermissionError):
        continue
symbols = subprocess.check_output(['nm', str(MOTOR)], text=True)
result['disassembly'] = {}
for line in symbols.splitlines():
    if any(s in line for s in ('loadJointLimitsEv', 'OmniPickerGripper11setPositionEdd')):
        parts = line.split()
        if len(parts) == 3 and parts[1] == 'T':
            result['disassembly'][parts[2]] = subprocess.check_output(
                ['objdump', '-d', '--disassemble=' + parts[2], str(MOTOR)], text=True)
print(json.dumps(result, ensure_ascii=False, indent=2))
