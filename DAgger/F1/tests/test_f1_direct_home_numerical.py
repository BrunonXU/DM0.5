"""Recorded starts through the existing output/collision stack; no robot I/O."""
import json
from pathlib import Path
import numpy as np
import pytest
from test_f1_numerical import setup
from f1dagger.clearance_home import DirectWorkbenchHomeMotion
from f1dagger.contract import home_target
from f1dagger.teleop import F1ArmTarget
from f1dagger.robot_io import make_output, CollisionGate

CASES = json.loads((Path(__file__).parent/'fixtures/direct_home_20261015.json').read_text())


@pytest.mark.parametrize('sample', CASES, ids=[sample['id'] for sample in CASES])
@pytest.mark.parametrize('synthetic_cap', [None, 54.8], ids=['delayed', 'plateau_fault_model'])
def test_recorded_direct_home_with_real_output_and_collision(setup, tmp_path, monkeypatch, sample, synthetic_cap):
    c, p, m, u, _ = setup
    q = np.array(sample['start_submitted']); full = np.array(sample['full22']); fixed = q.copy()
    measured = np.r_[full[:14], q[14:]]
    def no_ik(*args, **kwargs): raise AssertionError('direct Home invoked IK')
    monkeypatch.setattr(F1ArmTarget, 'begin', no_ik)
    monkeypatch.setattr(F1ArmTarget, 'warmup', no_ik)
    arm = F1ArmTarget(u, m, c['ik'])
    motion = DirectWorkbenchHomeMotion(p, q, home_target(p, q), arm, full, 0)
    output = make_output(c); output.planner.set_limits(m.limits)
    output.push(q[:14], 0., enabled=(False, False))
    guard = CollisionGate(c, m, tmp_path/'collision', full)
    history = [measured.copy()]*11
    for tick in range(1, 5001):
        now = tick*4_000_000; measured = history[-11].copy()
        # Reproduce an upper plateau as a negative plant model, never a new
        # hardware limit. Direct return reduces J6 and must not revisit it.
        if synthetic_cap is not None: measured[12] = min(measured[12], synthetic_cap)
        full[:14] = measured[:14]
        if tick % 5 == 0:
            target = motion.propose(.02, full, q, measured, (now,)*16, now)
            guard.check(target, full)
            output.push(target[:14], now/1e9, enabled=motion.enabled); motion.commit()
        nxt = np.r_[output.sample(now/1e9), q[14:]]
        assert output.fault is None
        m.check_action(nxt); guard.check(nxt, full)
        assert np.max(np.abs(nxt-q)) <= p['max_joint_speed_deg_s']*.004+1e-7
        assert nxt[12] <= fixed[12]+1e-7
        np.testing.assert_array_equal(nxt[:7], fixed[:7])
        np.testing.assert_array_equal(nxt[14:], fixed[14:])
        assert motion.stage == 'return' and arm.ik is None
        q = nxt; history.append(q.copy()); history = history[-12:]
        if motion.done: break
    assert motion.done and motion.retry_context() is None
    np.testing.assert_allclose(q[7:14], motion.goal[7:14], atol=.01)
    np.testing.assert_allclose(measured[7:14], motion.goal[7:14], atol=p['joint_tolerance_deg'])


from test_f1_reset_script import script


@pytest.mark.parametrize('interrupt', [False, True])
def test_standalone_direct_uses_same_right_only_owner_and_interrupt(setup, tmp_path, monkeypatch, script, interrupt):
    from test_f1_reset_script import test_only_right_arm_and_interrupt_ends_publication
    def no_ik(*args, **kwargs): raise AssertionError('standalone direct Home invoked IK')
    monkeypatch.setattr(F1ArmTarget, 'begin', no_ik)
    monkeypatch.setattr(F1ArmTarget, 'warmup', no_ik)
    test_only_right_arm_and_interrupt_ends_publication(setup, tmp_path, monkeypatch, script, interrupt)


def test_standalone_preview_is_only_return_and_never_claims_physical_arrival(setup, tmp_path, monkeypatch, script):
    c, p, m, u, _ = setup
    sample = CASES[-1]; full = np.array(sample['full22']); q = np.array(sample['start_submitted'])
    def no_ik(*args, **kwargs): raise AssertionError('standalone direct preview invoked IK')
    monkeypatch.setattr(F1ArmTarget, 'begin', no_ik)
    gate = CollisionGate(c, m, tmp_path/'collision', full)
    result = script.preview(c, p, m, u, full, q, gate)
    assert result['stages'] == ['return'] and result['physical_arrival_verified'] is False
