"""Explicit dependency roots, pinned source assets, no implicit robot startup."""
import importlib
import json
import math
import os
from pathlib import Path
import sys
from .contract import sha256, home_target

DAGGER_ROOT = Path(__file__).resolve().parents[2]


def load_config(path, *, infra_root=None, profile_path=None):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    if config.get('schema') != 'f1_hgdagger_v1':
        raise ValueError('unknown F1 DAgger config')
    root = infra_root or os.environ.get('F1_INFRA_ROOT')
    if not root:
        raise ValueError('F1_INFRA_ROOT must explicitly select an F1-infra release')
    root = Path(root).resolve(strict=True)
    profile_path = profile_path or os.environ.get('F1_PROFILE_PATH')
    if not profile_path:
        raise ValueError('F1_PROFILE_PATH must explicitly select the current machine profile')
    profile = json.loads(Path(profile_path).read_text())
    if profile.get('robot_id') != 'F1-09':
        raise ValueError('this adapter is bound to F1-09; another machine requires its own verification')
    if 'f710_home' in config:
        # A session snapshot, bound by config SHA; never edit the infra profile.
        profile['f710_home'] = json.loads(json.dumps(config['f710_home']))
        home_target(profile, [0.] * 16)
    policy = config['policy']
    if type(policy['chunk_steps']) is not int or not 1 <= policy['chunk_steps'] <= 1000:
        raise ValueError('chunk_steps must be in 1..1000')
    for value in (policy['frequency_hz'], policy['timeout_s'], policy['max_age_s'], config['capture_hz'], config['camera_max_age_s'], config['camera_max_skew_s']):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError('positive finite timing parameters required')
    if config['capture_hz'] > 30:
        raise ValueError('capture_hz must be <=30')
    manifest = json.loads((DAGGER_ROOT / config['infra_manifest']).read_text())
    for relative, expected in manifest['files'].items():
        candidate = (root / relative).resolve(strict=True)
        if root not in candidate.parents or sha256(candidate) != expected:
            raise ValueError(f'F1-infra dependency mismatch: {relative}')
    if not manifest['files']:
        raise ValueError('empty F1-infra binding')
    config['_infra_root'] = str(root)
    config['_profile_path'] = str(Path(profile_path).resolve())
    config['_config_sha256'] = sha256(path)
    config['_profile_sha256'] = sha256(profile_path)
    config['_infra_manifest_sha256'] = sha256(DAGGER_ROOT / config['infra_manifest'])
    return config, profile


def infra_import(root, module):
    root = Path(root).resolve()
    existing = sys.modules.get('robot')
    if existing is not None and Path(existing.__file__).resolve().parent != root / 'robot':
        raise RuntimeError('another robot package is already loaded')
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    result = importlib.import_module(module)
    if root not in Path(result.__file__).resolve().parents:
        raise RuntimeError(f'wrong F1 dependency imported: {module}')
    return result


def pico_paths(root):
    upper = Path(root).resolve() / 'integrations/pico_upper'
    for relative in ('src', 'vendor/f1_safety/src/python'):
        path = str(upper / relative)
        if path not in sys.path:
            sys.path.insert(0, path)
    return upper
