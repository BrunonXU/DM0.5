"""Real trajectory/collision math only; never creates robot command endpoints."""
import json
from pathlib import Path
import numpy as np
import pytest
from test_f1_numerical import setup
from f1dagger.contract import ARM_NAMES, FULL_NAMES, home_target
from f1dagger.robot_io import CollisionGate
from f1dagger.teleop import HomeMotion


@pytest.mark.parametrize('body_pose', ['nominal', 'demo'])
def test_legacy_to_demo_home_trajectory_keeps_non_arm_state(setup, tmp_path, body_pose):
    config, profile, mapping, _, full = setup
    import copy
    profile = copy.deepcopy(profile)
    profile['f710_home'].pop('clearance', None)
    profile['f710_home'].pop('route', None)
    profile['f710_home'].pop('scope', None)
    full = full.copy()
    source = json.loads((Path(__file__).parent/'fixtures/demo_v0_preset.json').read_text())
    profile['f710_home']['joints'] = {name:source['joints'][name] for name in ARM_NAMES}
    if body_pose == 'demo':
        full[14:] = [source['joints'][name] for name in FULL_NAMES[14:]]
    start = np.r_[full[:14], 43., 66.]
    target = mapping.check_action(home_target(profile, start))
    np.testing.assert_array_equal(target[:14], [source['joints'][name] for name in ARM_NAMES])
    guard = CollisionGate(config, mapping, tmp_path/'collision', full)
    motion = HomeMotion(profile, start, target)
    previous = start.copy()
    for _ in range(1000):
        q = mapping.check_action(motion.propose(.02))
        assert guard.check(q, full)['safe']
        assert np.max(np.abs(q[:14]-previous[:14])) <= profile['max_joint_speed_deg_s']*.02+1e-8
        np.testing.assert_array_equal(q[14:], start[14:])
        motion.commit()
        previous = q
        if np.max(np.abs(q[:14]-target[:14])) < 1e-4:
            break
    else:
        pytest.fail('configured demo home did not converge')
