"""Model-independent F1 upper-body contract. Versions describe semantics, not UI."""
import hashlib
import json
import math
from pathlib import Path

CONTRACT = 'f1_upper_16d_v1'
SCHEMA = 'f1_capture_v2'
NAMES = tuple(f'arm_{side}_j{i}' for side in ('l', 'r') for i in range(1, 8)) + ('left_gripper_position', 'right_gripper_position')
CAMERAS = ('head', 'left_wrist', 'right_wrist')
CAMERA_ALIASES = dict(zip(('base_0_rgb', 'left_wrist_0_rgb', 'right_wrist_0_rgb'), CAMERAS))
UNITS = ('degree',) * 14 + ('percent',) * 2
HUMAN = {'human', 'human_hold', 'human_arm', 'human_hand', 'human_arm_and_hand', 'pico_human'}
EXCLUDED = {'home', 'safe_hold', 'handoff', 'waiting', 'unknown', 'input_check', 'shadow'}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def atomic_json(path, value):
    import os
    path = Path(path)
    tmp = path.with_name('.' + path.name + '.tmp')
    with tmp.open('wb') as f:
        f.write(json_bytes(value)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def confined(root, relative):
    root = Path(root).absolute()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('unsafe data path')
    p = root
    for part in rel.parts:
        p /= part
        if p.is_symlink():
            raise ValueError('data symlink refused')
    return p


def validate_row(row):
    if row.get('contract') != CONTRACT:
        raise ValueError('unsupported F1 semantic contract')
    if type(row.get('time_ns')) is not int or row['time_ns'] <= 0:
        raise ValueError('positive integer reference time required')
    if not isinstance(row.get('action_valid'), list) or len(row['action_valid']) != 16 or any(type(x) is not bool for x in row['action_valid']):
        raise ValueError('explicit action validity per dimension required')
    for field in ('state', 'action'):
        values = row.get(field)
        if not isinstance(values, list) or len(values) != 16:
            raise ValueError(field + ' must have 16 named hardware coordinates')
        for i, value in enumerate(values):
            if field == 'action' and value is None and row.get('action_valid', [True]*16)[i] is False:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(field + ': finite numeric values required')
            if i >= 14 and not 0 <= value <= 100:
                raise ValueError('gripper must be in 0..100')
    if not row.get('session_id') or not row.get('clock_basis') or not row.get('action_source'):
        raise ValueError('session, clock and action provenance required')
    if set(row.get('images', {})) != set(CAMERAS):
        raise ValueError('three named cameras required')
    for camera, frame in row['images'].items():
        if type(frame.get('frame_id')) is not int or frame['frame_id'] < 0:
            raise ValueError(camera + ': frame identity required')
        if type(frame.get('received_ns')) is not int or frame['received_ns'] <= 0:
            raise ValueError(camera + ': real receipt time required')
    return row


def feature_contract():
    return dict(contract=CONTRACT, schema=SCHEMA, names=NAMES, units=UNITS,
                cameras=CAMERAS, action_representation='absolute_hardware_target',
                gripper='0=open,100=closed', default_fps=30,
                alignment='host receipt; not certified exposure synchronization')
