"""Independent FK fixture, derivative/constraint checks and deployment semantics."""
import sys
from pathlib import Path

import numpy as np
import pytest

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'robot'))
from kinematics import ArmModel, ContinuousIK, checked_pose, pose_error

CONFIG = TASK.parent/'reference/20260916_kinematics/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg'


@pytest.fixture
def model():
    return ArmModel(CONFIG)


def test_fk_matches_independent_native_fixture(model):
    # Baseline native response 0:feedback:59, arm0; not produced by this model.
    q = [76.24260711669922,-90.2033920288086,-86.82852172851562,-66.81692504882812,
         4.235263824462891,-2.1092581748962402,0.7797614932060242]
    expected = [[.12232523259603631,-.052998644983484375,.9910740038202381,.486240004883124],
                [-.9883810871149171,.08426630533244153,.12649907665752888,.35573271174654725],
                [-.09021842426875527,-.9950328303013947,-.04207496294443307,.1524136890169941],
                [0,0,0,1]]
    np.testing.assert_allclose(model.fk(0,q), expected, atol=1e-9, rtol=0)


def test_residual_jacobian_central_difference(model):
    rng = np.random.default_rng(41)
    for arm in (0,1):
        for _ in range(10):
            q = rng.uniform(model.low, model.high)
            target = model.fk_rad(arm, rng.uniform(model.low, model.high))
            _, analytical = model.residual(arm,q,target)
            numerical = np.column_stack([(model.residual(arm,q+e*1e-6,target)[0]
                                         -model.residual(arm,q-e*1e-6,target)[0])/2e-6
                                        for e in np.eye(7)])
            np.testing.assert_allclose(analytical,numerical,atol=2e-7,rtol=2e-7)


def test_pose_validation_and_no_input_mutation(model):
    q = [70,-90,-80,-70,4,-2,1]
    t = model.fk(0,q); before = t.copy()
    ContinuousIK(model).solve(0,t,q)
    np.testing.assert_array_equal(t,before)
    for bad in [np.full((4,4),np.nan), np.diag([-1,1,1,1]), np.zeros((4,4))]:
        with pytest.raises(ValueError): checked_pose(bad)
    with pytest.raises(ValueError): model.fk(2,q)


def test_limits_include_wrist_coupling(model):
    q = np.zeros(7);q[5:]=[60,60]
    assert not model.admissible_rad(np.deg2rad(q))
    q[6]=49
    assert model.admissible_rad(np.deg2rad(q))
    q[1]=-94.001
    assert not model.admissible_rad(np.deg2rad(q))


def test_determinism_accuracy_and_redundancy(model):
    solver = ContinuousIK(model)
    original = np.array([70,-85,-80,-70,4,-2,1.])
    ref = original+np.array([2,1,-1,1,2,1,-1])
    target = model.fk(0,original)
    a,b = solver.solve(0,target,ref), solver.solve(0,target,ref)
    assert a['success'] and b['success']
    np.testing.assert_array_equal(a['joints'],b['joints'])
    assert model.admissible_rad(np.deg2rad(a['joints']))
    assert pose_error(target,model.fk(0,a['joints']))[0]<.01
    assert np.max(np.abs(np.array(a['joints'])-original)) > .01


def test_unreachable_never_exposes_command(model):
    target = np.eye(4);target[:3,3]=[5,5,5]
    result = ContinuousIK(model).solve(0,target,[70,-85,-80,-70,4,-2,1])
    assert not result['success'] and result['joints'] is None


def test_elbow_bound_restoration_does_not_inherit_failed_iterate(model):
    # Source 000135, chunk353 steps0..2: exact -145 elbow caused primary SQP
    # infeasibility and a bad fallback seed formerly amplified a <1deg change.
    reference=[120.0671,-80.3457,-105.0003,-142.3781,-16.4023,7.2518,-5.4172]
    targets=[[117.0481,-79.226,-106.1805,-145,-17.7543,8.9384,-5.5366],
             [116.1337,-78.9843,-106.3177,-145,-18.0488,9.5106,-5.4906],
             [115.194,-78.7587,-106.378,-145,-18.3324,10.0905,-5.4184]]
    solver=ContinuousIK(model)
    for i,q in enumerate(targets):
        result=solver.solve(0,model.fk(0,q),reference)
        assert result['success'] and result['within_limits']
        if i: assert result['reference_step_max_deg']<2
        reference=result['joints']


def test_action_prefix_fixed_anchor_and_atomic_rejection(model):
    solver = ContinuousIK(model)
    anchor = np.array([70,-85,-80,-70,4,-2,1]*2,dtype=float)
    actions = np.zeros((3,23));targets=[]
    for step in range(3):
        pair=[]
        for arm in (0,1):
            q=anchor[arm*7:arm*7+7]+(step+1)*np.array([.2,.1,-.1,.3,0,.1,-.1])
            target=model.fk(arm,q);pair.append(target)
            delta=np.linalg.inv(model.fk(arm,anchor[arm*7:arm*7+7]))@target
            actions[step,arm*9:arm*9+9]=np.r_[delta[:3,3],delta[:2,:3].ravel()]
        targets.append(pair)
    actions[:,18:]=[.2,.7,.1,-.2,.3]
    result=solver.solve_eef_prefix(actions,anchor,anchor,execute_steps=3)
    assert result['success']
    for step,q in enumerate(result['joint_targets_deg']):
        for arm in (0,1):
            assert pose_error(targets[step][arm],model.fk(arm,q[arm*7:arm*7+7]))[0]<.01
    np.testing.assert_array_equal(result['base_velocity_targets'],actions[:,20:])
    actions[1,3:9]=0
    result=solver.solve_eef_prefix(actions,anchor,anchor,execute_steps=3)
    assert not result['success'] and result['joint_targets_deg'] is None and result['step']==1
