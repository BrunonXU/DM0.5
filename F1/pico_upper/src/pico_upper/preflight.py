"""Read-only preflight. Never constructs teleop or any command publisher."""
from __future__ import annotations
import argparse
from collections import deque
import importlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import time

from pico_upper.contract import COMMAND_TOPICS, UDP_HOST, UDP_PORT

REQUIRED_IMPORTS = ('numpy', 'ruckig', 'mujoco', 'coal', 'placo', 'placo_utils.visualization',
                    'meshcat', 'yaml', 'rclpy', 'sensor_msgs.msg', 'control_msgs.msg', 'xrobotoolkit_sdk')

def dependencies():
    errors = []
    for name in REQUIRED_IMPORTS:
        try:
            module = importlib.import_module(name)
            print(f'IMPORT_OK {name} {getattr(module, "__version__", "")}', flush=True)
        except Exception as exc:
            errors.append(f'{name}: {exc}')
    if errors:
        raise RuntimeError('DEPENDENCY_FAILED ' + '; '.join(errors))
    sdk = importlib.import_module('xrobotoolkit_sdk')
    sdk_required = ('get_left_controller_pose', 'get_right_controller_pose', 'get_headset_pose',
                    'get_left_axis', 'get_right_axis', 'get_left_grip', 'get_right_grip',
                    'get_left_trigger', 'get_right_trigger', 'get_time_stamp_ns',
                    'get_A_button', 'get_B_button', 'get_X_button', 'get_Y_button', 'get_left_menu_button')
    missing = [name for name in sdk_required if not callable(getattr(sdk, name, None))]
    if missing:
        raise RuntimeError(f'XR_SDK_API_MISSING {missing}')
    from pico_f1.config import load_control_config
    config = load_control_config()
    # Importing controller defines classes only; does not construct or publish.
    importlib.import_module('xrobotoolkit_teleop.simulation.placo_teleop_controller')
    print('CONTROLLER_IMPORT_OK no_instance_created=true', flush=True)
    return config

def legacy_processes():
    found = []
    markers = (b'all_remote_lxl.py', b'teleop_lxl.py', b'arm_replay_node', b'/meaxiao/pico/')
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            data = (entry / 'cmdline').read_bytes()
        except (OSError, PermissionError):
            continue
        # Do not expose full commands; only PID and matched executable marker.
        for marker in markers:
            if marker in data:
                found.append({'pid': int(entry.name), 'marker': marker.decode()})
                break
    return found

def capture(config, output: Path, timeout=10.):
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import JointState
    from pico_f1.session_pose import SESSION_JOINT_NAMES, GRIPPER_NAMES, validate_joint_map
    old = legacy_processes()
    if old:
        raise RuntimeError(f'LEGACY_CONTROL_RUNNING {old}; 在现场退出旧lxl/回放，保留PCService')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((UDP_HOST, UDP_PORT))
    rclpy.init()
    node = Node('pico_upper_readonly_check')
    values = {name: deque(maxlen=3) for name in SESSION_JOINT_NAMES}
    seen = {}
    freshness = float(config['feedback_timeout_s'])
    def put(name, val):
        if name in values and math.isfinite(float(val)):
            values[name].append(float(val))
            seen[name] = time.monotonic()
    def hal(msg):
        if len(msg.name) != len(msg.position) or len(set(msg.name)) != len(msg.name):
            return
        for name, val in zip(msg.name, msg.position):
            put(name, val)
    def gripper(i, msg):
        if msg.position:
            put(GRIPPER_NAMES[i], msg.position[0])
    subs = [node.create_subscription(JointState, '/hal/joint_states', hal, qos_profile_sensor_data)]
    for i, side in enumerate(('left', 'right')):
        subs.append(node.create_subscription(JointState, f'/motion_ctl/gripper/{side}/state',
                     lambda msg, i=i: gripper(i, msg), qos_profile_sensor_data))
    started = time.monotonic()
    ready = False
    try:
        while time.monotonic() - started < timeout:
            rclpy.spin_once(node, timeout_sec=.02)
            now = time.monotonic()
            conflicts = [(topic, info.node_name) for topic in COMMAND_TOPICS
                         for info in node.get_publishers_info_by_topic(topic)]
            if conflicts:
                raise RuntimeError(f'CONTROL_PUBLISHERS_ACTIVE {conflicts}; exit existing controller first')
            ready = (now - started >= 3. and all(len(q) == 3 and now - seen.get(n, 0) <= freshness
                     and max(q) - min(q) <= .25 for n, q in values.items()))
            if ready:
                break
        if not ready:
            missing = [n for n,q in values.items() if len(q)<3 or time.monotonic()-seen.get(n,0)>freshness]
            raise RuntimeError(f'FEEDBACK_NOT_READY missing_or_stale={missing}; require stationary full22+grippers2')
    finally:
        node.destroy_node()
        rclpy.shutdown()
    joints = validate_joint_map({n: sum(q)/len(q) for n,q in values.items()})
    from com_and_collision_protect_modules import SafetyProtector
    import numpy as np
    safety = SafetyProtector(use_com_guard=False)
    if safety._collision_guard is None:
        raise RuntimeError('COLLISION_GUARD_UNAVAILABLE')
    current = np.deg2rad([joints[n] for n in SESSION_JOINT_NAMES[:22]] + [0., 0.])
    safety.init_safe_cmd(current)
    result = safety.run(current)
    if not result[0]:
        raise RuntimeError('START_POSE_COLLISION: current measured posture rejected by upstream collision guard')
    payload = {'schema':'pico-f1-session-pose-v1','robot_id':'F1-09','captured_unix':time.time(),
               'name':list(SESSION_JOINT_NAMES),'position':[joints[n] for n in SESSION_JOINT_NAMES],
               'source_topics':['/hal/joint_states','/motion_ctl/gripper/left/state','/motion_ctl/gripper/right/state'],
               'capture_max_age_s':freshness,'reset_enabled':False}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, indent=2)+'\n')
    temp.replace(output)
    print(f'UPPER_READONLY_CHECK_OK pose={output} commands_sent=0', flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dependencies', action='store_true')
    args = parser.parse_args()
    try:
        config = dependencies()
        if not args.dependencies:
            capture(config, Path(os.environ['PICO_F1_INITIAL_POSE']))
    except Exception as exc:
        print(f'UPPER_CHECK_FAILED {exc}', file=sys.stderr, flush=True)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
