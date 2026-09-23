"""Normalized model output -> frozen IK. Pure offline math, no ROS dispatch."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys
import numpy as np
from f1_data.reader import require, sha256

TASK_ROOT = Path(__file__).resolve().parents[3]
FREEZE = TASK_ROOT / 'results/ik_fk_demo_freeze_20260916'


def frozen_solver():
    manifest = json.loads((FREEZE / 'manifest.json').read_text())
    require(manifest['version'] == 'f1_ikfk_demo_v1', 'IK baseline version mismatch')
    for entry in manifest['files']:
        require(sha256(FREEZE / entry['snapshot_path']) == entry['sha256'], 'frozen IK artifact changed')
    name = '_f1_ikfk_demo_v1'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, FREEZE / 'snapshot/kinematics.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    module = sys.modules[name]
    return module.ContinuousIK(module.ArmModel(FREEZE / 'snapshot/ccs_m6_40.MvKDCfg'))


class ActionDecoder:
    def __init__(self, normalizer):
        self.normalizer = normalizer
        self.solver = frozen_solver()
        # Check cached-label FK identity against the frozen deployment baseline.
        identity = json.loads(normalizer.stats['kinematics_identity'])
        manifest = json.loads((FREEZE / 'manifest.json').read_text())
        require(identity['kinematics_contract']['config_sha256'] == manifest['files'][1]['sha256'], 'stats/IK config mismatch')
        require(identity['pose_unit'] == 'm' and identity['arm_order'] == ['left', 'right'], 'stats/IK units or arm order mismatch')
        require(identity['kinematics_contract']['frame'] == manifest['contract']['frame'], 'stats/IK frame mismatch')
        for side in ('lower', 'upper'):
            require(identity['execution_mapping'][side + '_deg'] == manifest['contract'][side + '_deg'], 'stats/IK limits mismatch')
        self.kinematics_identity = identity

    def decode(self, normalized_action32, anchor_joints14, reference_joints14=None):
        action = np.asarray(normalized_action32)
        require(action.shape == (50, 32), 'expected one H50,32 normalized chunk')
        # Padding has no meaning; only the 23 physical output dimensions are decoded.
        physical = self.normalizer.denormalize_action(action)
        if not np.isfinite(physical).all():
            return {'success': False, 'joint_targets_deg': None, 'reason': 'nonfinite_action'}
        reference = anchor_joints14 if reference_joints14 is None else reference_joints14
        result = self.solver.solve_eef_prefix(physical, anchor_joints14, reference, execute_steps=20)
        result['ik_recipe_version'] = 'f1_ikfk_demo_v1'
        return result
