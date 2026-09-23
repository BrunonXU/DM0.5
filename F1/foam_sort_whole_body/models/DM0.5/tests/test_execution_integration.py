import copy
import json
from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from f1_data.reader import Normalizer
from f1_training.execution import ActionDecoder, FREEZE


def normalizer():
    m = json.loads((FREEZE / 'manifest.json').read_text())
    identity = {'kinematics_contract': {'config_sha256': m['files'][1]['sha256'], 'frame': m['contract']['frame']},
                'pose_unit': 'm', 'arm_order': ['left', 'right'],
                'execution_mapping': {key: m['contract'][key] for key in ('lower_deg', 'upper_deg')}}
    stats = {'schema': 'f1_state19_eef23_rot6d_rows_v1', 'source_split': 'train',
             'kinematics_identity': json.dumps(identity),
             'state': {'center': [0]*19, 'scale': [1]*19},
             'action': {'center': [0]*23, 'scale': [1]*23}}
    return Normalizer(stats)


def test_decoder_uses_fixed_anchor_and_preserves_base_and_grippers():
    decoder = ActionDecoder(normalizer())
    q = np.tile([70, -85, -80, -70, 4, -2, 1], 2)
    action = np.zeros((50, 32)); action[:, [3, 7, 12, 16]] = 1
    action[:, 18:23] = [10, 100, .1, -.03, 0]
    result = decoder.decode(action, q)
    assert result['success']
    np.testing.assert_allclose(result['joint_targets_deg'], np.tile(q, (20, 1)), atol=1e-8)
    np.testing.assert_allclose(result['base_velocity_targets'], action[:20, 20:23])
    np.testing.assert_allclose(result['gripper_targets'], action[:20, 18:20])
    action[1, 12:18] = 0
    failed = decoder.decode(action, q)
    assert not failed['success'] and failed['joint_targets_deg'] is None
    assert 'base_velocity_targets' not in failed and 'gripper_targets' not in failed


def test_decoder_rejects_wrong_contract_and_nonfinite():
    stats = copy.deepcopy(normalizer().stats)
    identity = json.loads(stats['kinematics_identity']); identity['pose_unit'] = 'mm'
    stats['kinematics_identity'] = json.dumps(identity)
    with pytest.raises(ValueError, match='units or arm order'):
        ActionDecoder(Normalizer(stats))
    decoder = ActionDecoder(normalizer())
    with pytest.raises(ValueError, match='H50,32'):
        decoder.decode(np.zeros((20, 32)), np.zeros(14))
    action = np.zeros((50,32)); action[0,0] = np.nan
    assert decoder.decode(action, np.zeros(14))['reason'] == 'nonfinite_action'
