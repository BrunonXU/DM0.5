"""Captured output-fault regression; pure numerical tests, no ROS or sockets."""
import contextlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

ruckig = pytest.importorskip('ruckig')
from f1dagger.arm_output import RadianArmTrajectory
from f1dagger.config import DAGGER_ROOT, pico_paths
from f1dagger.robot_io import make_output


@pytest.fixture(scope='module')
def captured():
    return json.loads((Path(__file__).parents[1]/'scripts/64_f1_recovery_cases.json').read_text())


@pytest.fixture
def output():
    root = Path(__import__('os').environ.get('F1_INFRA_ROOT', str(DAGGER_ROOT.parent.parent/'F1-4242')))
    pico_paths(root)
    return make_output({'_infra_root': str(root)})


def dynamics(output, old_acceleration, dt):
    planner = output.planner
    assert np.max(np.abs(planner.velocity)) <= planner.max_velocity + 1e-7
    assert np.max(np.abs(planner.acceleration)) <= planner.max_acceleration + 1e-7
    assert np.max(np.abs(np.array(planner.acceleration)-old_acceleration))/dt <= planner.max_jerk + 1e-5


@pytest.mark.parametrize('synchronization', [ruckig.Synchronization.Time, ruckig.Synchronization.No])
def test_captured_degree_state_reproduces_original_step1_failure(captured, synchronization):
    inp, result = ruckig.InputParameter(14), ruckig.OutputParameter(14)
    for name, values in captured['input_degrees'].items():
        setattr(inp, name, values)
    inp.synchronization = synchronization
    with pytest.raises(Exception, match='error in step 1, dof: 2'):
        ruckig.Ruckig(14, .00063).update(inp, result)


@pytest.mark.parametrize('intervals', [(.004,), (.00063,), (.0135,), (.00063, .004, .0135, .001)])
def test_captured_dynamics_converge_in_radians_without_relaxing_limits(captured, output, intervals):
    data = captured['input_degrees']
    output.push(data['current_position'], 0., enabled=(True, True))
    planner = output.planner._radians
    for field in ('position', 'velocity', 'acceleration'):
        setattr(planner, field, np.deg2rad(data['current_'+field]).tolist())
    planner.filtered_target = np.deg2rad(data['target_position']).tolist()
    now, index = 0., 0
    with contextlib.redirect_stdout(io.StringIO()):
        while now < 1.:
            dt = intervals[index % len(intervals)]
            old_acceleration = np.array(output.planner.acceleration)
            now += dt
            output.push(data['target_position'], now, enabled=(True, True))
            command = output.sample(now)
            assert output.fault is None
            dynamics(output, old_acceleration, dt)
            index += 1
    np.testing.assert_allclose(command, data['target_position'], atol=output.planner.deadband, rtol=0)
    assert np.max(np.abs(output.planner.velocity)) < 1e-5
    assert np.max(np.abs(output.planner.acceleration)) < 1e-5


def test_actual_home_targets_and_publication_intervals_settle(captured, output):
    output.push(captured['home_initial_degrees'], 0., enabled=(False, False))
    now = 0.
    steps = captured['home_steps'] + [[.004, captured['home_steps'][-1][1]]] * 500
    for dt, target in steps:
        before = np.array(output.planner.acceleration)
        now += dt
        output.push(target, now, enabled=(True, True))
        command = output.sample(now)
        assert output.fault is None
        dynamics(output, before, dt)
    np.testing.assert_allclose(command, steps[-1][1], atol=output.planner.deadband, rtol=0)


def test_variable_intervals_reversals_and_current_proposal_commit(output):
    output.push(np.zeros(14), 0., enabled=(True, True))
    now = 0.
    for index in range(2400):
        dt = (.0006, .004, .013, .005, .001)[index % 5]
        before = np.array(output.planner.acceleration)
        now += dt
        target = 35*np.sin(now*1.7+np.arange(14)*.31)
        output.push(target, now, enabled=(True, True))
        output.sample(now)
        assert output.fault is None
        dynamics(output, before, dt)
    proposal = output.planner.propose(target, dt=.004)
    native = output.planner._pending[1]
    output.planner.commit(proposal)
    assert output.planner._radians.velocity == list(native.velocity)
    assert output.planner._radians.acceleration == list(native.acceleration)
    with pytest.raises(ValueError, match='current proposal'):
        output.planner.commit(proposal)


def test_stale_output_still_latches_and_active_packets_do_not_rearm(output):
    initial = np.arange(14.)
    output.push(initial, 0., enabled=(True, True))
    held = output.sample(.2)
    assert output.fault and output.timed_out
    for index in range(1, 101):
        now = .2+index*.004
        output.push(initial+30., now, enabled=(True, True))
        np.testing.assert_array_equal(output.sample(now), held)
    assert output.fault


def test_planner_exception_still_latches_and_hold_discards_proposal(output, monkeypatch):
    output.push(np.arange(14.), 0., enabled=(True, True))
    def fail(*args, **kwargs):
        raise RuntimeError('injected native failure')
    monkeypatch.setattr(output.planner._radians, 'propose', fail)
    held = output.sample(.004)
    assert output.fault == 'planner failure: injected native failure'
    for index in range(2, 12):
        output.push(np.ones(14)*40., index*.004, enabled=(True, True))
        np.testing.assert_array_equal(output.sample(index*.004), held)
    assert output.planner._pending is None


def test_units_include_limits_and_deadband_and_inactive_arm_is_exact(output):
    p = output.planner
    assert isinstance(p, RadianArmTrajectory)
    for key in ('max_velocity', 'max_acceleration', 'max_jerk', 'deadband'):
        assert getattr(p._radians, key) == pytest.approx(np.deg2rad(getattr(p, key)))
    assert p._radians.dt == p.dt and p._radians.target_cutoff_hz == p.target_cutoff_hz
    initial = np.linspace(-91.37, 122.78, 14)
    output.push(initial, 0., enabled=(False, False))
    for index in range(1, 101):
        target = initial.copy()
        target[7:] += 3.
        output.push(target, index*.004, enabled=(False, True))
        command = output.sample(index*.004)
        np.testing.assert_array_equal(command[:7], initial[:7])
