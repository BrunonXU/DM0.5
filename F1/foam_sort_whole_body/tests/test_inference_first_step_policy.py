"""First-step experiment changes only proximity rejection, never the trajectory."""
from copy import deepcopy

import pytest

from test_inference_execution import NOW, BINDING, capture, profile, response, snapshot
from f1_policy_client.execution import FirstArmStepRejected, prepare_prefix
from f1_replay.runtime import ReplayError


def prepare(r, p, feedback=None):
    return prepare_prefix(r, capture(), snapshot() if feedback is None else feedback, p,
                          now_ns=NOW, request_id='request-7',
                          expected_model_binding=BINDING, max_observation_age_s=2)


@pytest.mark.parametrize('delta', [-6.10937, -30.48])
def test_report_only_preserves_all_targets_and_records_delta(delta):
    r = response()
    for row in r['joint_targets_deg']:
        row[3] = delta
    original = deepcopy(r)
    p = profile()
    p['inference_first_arm_max_delta_deg'] = 5.0
    with pytest.raises(FirstArmStepRejected):
        prepare(r, p)
    p.update(inference_first_arm_policy='report_only', inference_first_arm_max_delta_deg=None)
    out = prepare(r, p)
    assert out.first_arm_delta_deg[3] == delta
    assert r == original
    for i, frame in enumerate(out.bundle.frames):
        assert list(frame.action) == r['joint_targets_deg'][i] + r['gripper_targets'][i] + r['base_velocity_targets'][i]
        assert frame.time_ns == round(i * 1e9 / 30)


@pytest.mark.parametrize('failure', ['hard_limit', 'wrist_coupling', 'nan', 'stale_feedback', 'wrong_binding', 'unknown_policy'])
def test_report_only_does_not_remove_other_checks(failure):
    r, p, s = response(), profile(), snapshot()
    p.update(inference_first_arm_policy='report_only', inference_first_arm_max_delta_deg=None)
    if failure == 'hard_limit':
        r['joint_targets_deg'][10][3] = -999
    elif failure == 'wrist_coupling':
        r['joint_targets_deg'][10][5:7] = [70, 70]
    elif failure == 'nan':
        r['joint_targets_deg'][0][3] = float('nan')
    elif failure == 'stale_feedback':
        s['joints']['arm_l_j4']['received_ns'] = NOW - 2_000_000_000
    elif failure == 'wrong_binding':
        r['model_binding'] = {}
    else:
        p['inference_first_arm_policy'] = 'typo'
    with pytest.raises(ReplayError):
        prepare(r, p, s)


@pytest.mark.parametrize('recipe,angle', [('f1_ikfk_demo_pos5mm_v1', 0.001),
                                        ('f1_ikfk_demo_pos5mm_rot005deg_v1', 0.05)])
def test_ood_recipe_requires_exact_explicit_binding(recipe, angle):
    r, p = response(), profile()
    r.update(ik_recipe_version=recipe,
             ik_options={'position_tolerance_mm': 5.0, 'rotation_tolerance_deg': angle})
    with pytest.raises(ReplayError, match='different frozen IK'):
        prepare(r, p)
    p['inference_ik_recipe_version'] = recipe
    out = prepare(r, p)
    assert out.bundle.manifest['ik_recipe_version'] == r['ik_recipe_version']
    r['ik_options']['position_tolerance_mm'] = 1.224
    with pytest.raises(ReplayError, match='tolerance binding'):
        prepare(r, p)
    r['ik_options']['position_tolerance_mm'] = 5.0
    r['ik_options']['rotation_tolerance_deg'] = 0.2
    with pytest.raises(ReplayError, match='tolerance binding'):
        prepare(r, p)


def test_ood_profile_rejects_baseline_ik_response():
    p = profile()
    p['inference_ik_recipe_version'] = 'f1_ikfk_demo_pos5mm_v1'
    with pytest.raises(ReplayError, match='different frozen IK'):
        prepare(response(), p)
