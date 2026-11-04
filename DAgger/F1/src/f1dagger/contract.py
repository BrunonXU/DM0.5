"""Hardware coordinates only: HAL degrees, OmniPicker 0=open/100=closed."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import numpy as np

CONTRACT = 'f1_upper_16d_v1'
ARM_NAMES = tuple(f'arm_{side}_j{i}' for side in ('l', 'r') for i in range(1, 8))
HEAD_NAMES = tuple(f'head_j{i}' for i in range(1, 4))
BODY_NAMES = tuple(f'body_j{i}' for i in range(1, 6))
FULL_NAMES = ARM_NAMES + HEAD_NAMES + BODY_NAMES
FEATURE_NAMES = ARM_NAMES + ('left_gripper_position', 'right_gripper_position')
CAMERAS = ('head', 'left_wrist', 'right_wrist')
MODEL_NAMES = tuple(f'arm_{side}_joint{i}' for side in ('L', 'R') for i in range(1, 8)) + tuple(f'head_joint{i}' for i in range(1, 4)) + tuple(f'body_joint{i}' for i in range(5, 0, -1))


class ArmLimitRejected(ValueError):
    """A well-formed arm target violates the bound hardware workspace."""


def vector(value, size, name='vector'):
    result = np.asarray(value, dtype=float)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError(f'{name}: expected {size} finite values')
    return result.copy()


def sha256(path):
    digest = hashlib.sha256()
    # Bound audit memory even when a camera pack spans a long episode.
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class JointMapping:
    def __init__(self, mapping, profile, urdf):
        if mapping.get('input_units') != 'hal_degrees' or mapping.get('offset_deg') != 0:
            raise ValueError('unsupported F1 HAL/model mapping')
        if sha256(urdf) != mapping.get('model_urdf_sha256'):
            raise ValueError('URDF differs from the bound F1 model mapping')
        signs = mapping.get('joint_signs', {})
        if set(signs) - set(FULL_NAMES) or any(x not in (-1, 1) for x in signs.values()):
            raise ValueError('invalid model signs')
        self.signs = np.array([signs.get(n, 1) for n in FULL_NAMES], dtype=float)
        self.limits = np.array([profile['joint_limits_deg'][n] for n in ARM_NAMES], dtype=float)
        if self.limits.shape != (14, 2) or not np.isfinite(self.limits).all() or np.any(self.limits[:, 0] >= self.limits[:, 1]):
            raise ValueError('invalid F1 arm limits')

    def to_model(self, hal22):
        return np.deg2rad(vector(hal22, 22, 'HAL22') * self.signs)

    def to_hardware(self, model22):
        return np.rad2deg(vector(model22, 22, 'model22') * self.signs)

    def model_limits(self):
        return np.sort(np.deg2rad(self.limits * self.signs[:14, None]), axis=1)

    def check_action(self, action):
        q = vector(action, 16, 'action16')
        if np.any(q[:14] < self.limits[:, 0]) or np.any(q[:14] > self.limits[:, 1]):
            raise ArmLimitRejected('arm target outside F1 HAL limits')
        for offset in (0, 7):
            if 1.025 * abs(q[offset + 5]) + abs(q[offset + 6]) > 110.5 + 1e-7:
                raise ArmLimitRejected('F1 coupled wrist limit exceeded')
        if np.any(q[14:] < 0) or np.any(q[14:] > 100):
            raise ValueError('gripper outside 0..100')
        return q


def home_target(profile, last_submitted):
    if 'f710_home' in profile:
        preset = profile['f710_home']
        if (not isinstance(preset, dict) or preset.get('schema') != 'f1_upper_home_v1'
                or preset.get('robot_id') != profile.get('robot_id')
                or preset.get('units') != 'degree'
                or not isinstance(preset.get('joints'), dict)
                or set(preset['joints']) != set(ARM_NAMES)):
            raise ValueError('invalid F1 F710 home preset; expected this robot and exactly 14 arm joints')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in preset['joints'].values()):
            raise ValueError('invalid F1 F710 home joint value')
    else:
        # Compatibility for explicitly selected older site configurations.
        presets = [p for p in profile.get('default_presets', []) if p.get('key') == 'lxl-xy-reset-v1']
        if len(presets) != 1 or presets[0].get('units') != 'degree':
            raise ValueError('F1 approved upper home preset missing')
        preset = presets[0]
    result = vector(last_submitted, 16)
    result[:14] = vector([preset['joints'][name] for name in ARM_NAMES], 14, 'home arm targets')
    route = preset.get('route')
    if route is not None and (route != 'direct' or 'clearance' in preset
            or preset.get('scope') != 'right_arm_only_preserve_left_grippers_head_body'):
        raise ValueError('invalid F1 F710 Home route/scope')
    if 'clearance' in preset:
        clearance_config(preset)
    if preset.get('scope') == 'right_arm_only_preserve_left_grippers_head_body':
        result[:7] = vector(last_submitted, 16)[:7]
    return result


def clearance_config(preset):
    route = preset.get('clearance')
    relaxed = isinstance(route, dict) and route.get('schema') == 'f1_right_clearance_v2'
    keys = {'schema', 'frame', 'lift_m', 'right_m'} | ({'max_rotation_deg'} if relaxed else set())
    if relaxed and 'linear_speed_m_s' in route:
        keys.add('linear_speed_m_s')
    if (not isinstance(route, dict) or set(route) != keys
            or route.get('schema') not in ('f1_right_clearance_v1', 'f1_right_clearance_v2') or route.get('frame') != 'base_link'
            or preset.get('scope') != 'right_arm_only_preserve_left_grippers_head_body'):
        raise ValueError('invalid right-arm clearance route')
    for key in ('lift_m', 'right_m'):
        value = route[key]
        if type(value) not in (int, float) or not np.isfinite(value) or not 0 < value <= .2:
            raise ValueError('clearance distance must be finite and in (0, 0.2] metres')
    if relaxed:
        angle = route['max_rotation_deg']
        if type(angle) not in (int, float) or not np.isfinite(angle) or not 0 < angle <= 10:
            raise ValueError('clearance rotation budget must be in (0, 10] degrees')
    if 'linear_speed_m_s' in route:
        speed = route['linear_speed_m_s']
        if type(speed) not in (int, float) or not np.isfinite(speed) or not 0 < speed <= .08:
            raise ValueError('clearance speed must be in (0, 0.08] metres per second')
    return route.copy()
