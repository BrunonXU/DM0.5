"""Pure command boundary: upper-only capabilities cannot be enabled in config."""
from __future__ import annotations
import math
import struct

ARM_NAMES = tuple(f'arm_{side}_j{i}' for side in ('l', 'r') for i in range(1, 8))
COMMAND_TOPICS = ('/motion_ctl/joint_ctl', '/motion_ctl/gripper/left', '/motion_ctl/gripper/right')
PUBLISH_TOPICS = frozenset(COMMAND_TOPICS + ('/lead/joint_states', '/lead/left_tcp', '/lead/right_tcp'))
UDP_HOST, UDP_PORT = '127.0.0.1', 42428

def validate_profile(config):
    for name in ('chassis', 'torso_pitch', 'recording'):
        if config.get(name, {}).get('enabled') is not False:
            raise ValueError(f'upper-only profile forbids {name}')
    if config.get('chassis', {}).get('joystick_enabled') is not False:
        raise ValueError('upper-only profile forbids joystick chassis output')

def validate_frame(values):
    q = tuple(float(x) for x in values)
    if len(q) != 18 or not all(math.isfinite(x) for x in q):
        raise ValueError('expected 18 finite values: arms14, grippers2, enables2')
    if not all(0 <= x <= 100 for x in q[14:16]):
        raise ValueError('gripper values must be within [0,100]')
    if q[16] not in (0., 1.) or q[17] not in (0., 1.):
        raise ValueError('per-arm enables must be 0 or 1')
    return q

def decode_frame(data, peer):
    if peer[0] != UDP_HOST or len(data) != 18 * 8:
        raise ValueError('only local 18-value approved command frames accepted')
    return validate_frame(struct.unpack('<18d', data))

def validate_message(topic, message):
    if topic not in PUBLISH_TOPICS:
        raise ValueError(f'upper-only publisher denied: {topic}')
    if topic == '/motion_ctl/joint_ctl':
        if tuple(message.name) != ARM_NAMES or len(message.position) != 14:
            raise ValueError('joint command must contain exactly left7/right7 arms')
        if not all(math.isfinite(float(x)) for x in message.position):
            raise ValueError('invalid arm positions')
    elif topic in COMMAND_TOPICS:
        if not math.isfinite(float(message.position)) or not 0 <= message.position <= 100:
            raise ValueError('invalid gripper command')
