"""Direct Y uses the actual 4242 curve, without ROS, IK or robot actions."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from f1dagger.config import DAGGER_ROOT, infra_import
from f1dagger.contract import ARM_NAMES, FULL_NAMES, JointMapping, home_target
from f1dagger.clearance_home import DirectWorkbenchHomeMotion, ClearanceHomeStopped


@pytest.fixture
def direct_setup():
    root = Path(os.environ.get('F1_INFRA_ROOT', str(DAGGER_ROOT.parent.parent/'F1-4242')))
    profile = json.loads((root/'configs/f1-09.json').read_text())
    profile['f710_home'] = json.loads((DAGGER_ROOT/'configs/f1_09_hil.json').read_text())['f710_home']
    mapping = JointMapping(json.loads((root/'configs/model_mapping.json').read_text()), profile,
        root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf')
    kind = infra_import(root, 'robot.motion_plan').SmoothBodyPlan
    full = np.array([profile['default_presets'][1]['joints'][n] for n in FULL_NAMES])
    def forbidden(*args, **kwargs):
        raise AssertionError('direct Home must not initialize or solve IK')
    arm = SimpleNamespace(mapping=mapping, home_plan_type=kind,
        safety=SimpleNamespace(max_dt_s=.05), begin=forbidden, warmup=forbidden)
    return profile, arm, full, np.r_[full[:14], 43., 66.]


def test_direct_curve_matches_workbench_preserves_inactive_and_only_commits(direct_setup):
    p, arm, full, start = direct_setup
    # Even an incorrectly supplied left/gripper goal cannot enable these axes.
    goal = home_target(p, start); goal[:7] = 0.; goal[14:] = 0.
    motion = DirectWorkbenchHomeMotion(p, start, goal, arm, full, 0)
    reference = arm.home_plan_type(dict(enumerate(start[:14])), dict(enumerate(motion.goal[:14])),
        dict.fromkeys(range(14), 30.), dict.fromkeys(range(14), 75.), p.get('max_pose_task_s', 60.)-.3)
    assert motion.enabled == (False, True) and motion.stage == 'return'
    assert motion.retry_context() is None and not hasattr(motion, 'ik')
    q = start.copy(); actual = start.copy()
    for tick in range(1, 1000):
        now = tick*20_000_000
        target = motion.propose(.02, full, q, actual, (now,)*16, now)
        np.testing.assert_array_equal(target, motion.propose(.02, full, q, actual, (now,)*16, now))
        np.testing.assert_allclose(target[:14], list(reference.sample(tick*.02).values()), atol=1e-11)
        np.testing.assert_array_equal(target[:7], start[:7])
        np.testing.assert_array_equal(target[14:], start[14:])
        assert motion.stage == 'return'
        motion.commit(); q = target; actual = q.copy(); full[:14] = actual[:14]
        if motion.done: break
    assert motion.done
    np.testing.assert_array_equal(q, motion.goal)


@pytest.mark.parametrize('failure', ['stale_arrival', 'stuck_wrist'])
def test_direct_requires_fresh_physical_arrival_and_retains_settling_budget(direct_setup, failure):
    p, arm, full, start = direct_setup
    start[12] = full[12] = 20.
    motion = DirectWorkbenchHomeMotion(p, start, home_target(p, start), arm, full, 0)
    q = start.copy(); held = float(start[12]); goal_since = None
    with pytest.raises(ClearanceHomeStopped, match='return feedback timeout after settling wait'):
        for tick in range(1, 1000):
            now = tick*20_000_000; actual = q.copy()
            if failure == 'stuck_wrist': actual[12] = held
            full[:14] = actual[:14]
            if np.max(np.abs(q[7:14]-motion.goal[7:14])) <= .01 and goal_since is None:
                goal_since = now
            stamps = (0,)*16 if failure == 'stale_arrival' else (now,)*16
            target = motion.propose(.02, full, q, actual, stamps, now)
            motion.commit(); q = target
            assert not motion.done
    assert goal_since is not None and (now-goal_since)/1e9 > p['settle_timeout_s']
    assert motion.retry_context() is None


@pytest.mark.parametrize('failure', ['body_change', 'bad_interval', 'invalid_publication'])
def test_direct_keeps_global_guard_failures(direct_setup, failure):
    p, arm, full, start = direct_setup
    motion = DirectWorkbenchHomeMotion(p, start, home_target(p, start), arm, full, 0)
    dt = .02
    if failure == 'body_change': full[15] += .51
    elif failure == 'bad_interval': dt = .051
    else: start[12:14] = [60., 90.]
    with pytest.raises((ValueError, RuntimeError)) as exc:
        motion.propose(dt, full, start, start, (20_000_000,)*16, 20_000_000)
    assert not isinstance(exc.value, ClearanceHomeStopped)


@pytest.mark.parametrize('fault', ['scope', 'mixed_route', 'unknown_route'])
def test_direct_contract_rejects_ambiguous_routes(direct_setup, fault):
    p, _, _, q = direct_setup
    p = copy.deepcopy(p)
    if fault == 'scope': p['f710_home'].pop('scope')
    elif fault == 'mixed_route': p['f710_home']['clearance'] = {}
    else: p['f710_home']['route'] = 'unknown'
    with pytest.raises(ValueError, match='route/scope'): home_target(p, q)


@pytest.mark.parametrize('cancel', ['rb', 'start'])
def test_direct_y_edge_uses_return_only_and_cancels_without_restarting(direct_setup, cancel):
    from test_f1_runtime import setup_loop
    from g2dagger.hil_supervisor import HILState
    p, arm, _, _ = direct_setup
    loop, engine, owner, transport, now, records, step = setup_loop()
    loop.profile = p
    arm.release = lambda: None
    arm.target = lambda packet, full, last, dt: last.copy()
    loop.arm = arm
    owner.source.feedback = lambda: (np.r_[owner.last_submitted[:14], np.zeros(8)],
                                     owner.last_submitted.copy(), (now[0],)*16)
    try:
        step(0); step(1, 1); step(2, 1, home_reset=True)
        assert isinstance(loop.home_motion, DirectWorkbenchHomeMotion)
        assert loop.home_motion.stage == 'return' and owner.output.enabled == (False, True)
        np.testing.assert_array_equal(loop.home[:7], np.zeros(7))
        np.testing.assert_array_equal(loop.home[14:], [32., 66.])
        step(3, 1, home_reset=True)
        starts = [entry for entry in engine.recorder.events if entry[0][0] == 'home_started']
        assert len(starts) == 1 and starts[0][1]['stage'] == 'return'
        if cancel == 'rb': step(4, 1, deadman=True, active_side='right')
        else: step(4, 2)
        assert loop.home is None and loop.home_motion is None
        assert engine.supervisor.state is (HILState.HIL_LATCHED if cancel == 'rb' else HILState.HANDOFF_HOLD)
        step(5, 1 if cancel == 'rb' else 2)
        assert loop.home is None
    finally: engine.close()


def test_direct_tracking_failure_keeps_episode_and_requires_new_y(direct_setup):
    from test_f1_runtime import setup_loop
    from g2dagger.hil_supervisor import HILState
    p, arm, _, _ = direct_setup
    loop, engine, owner, _, now, _, step = setup_loop()
    loop.profile = p; arm.release = lambda: None; loop.arm = arm
    held = [True]; finishes = []
    def feedback():
        measured = owner.last_submitted.copy()
        if held[0]: measured[12] = 20.
        return np.r_[measured[:14], np.zeros(8)], measured, (now[0],)*16
    owner.source.feedback = feedback
    try:
        step(0); step(1, 1)
        engine.episodes = SimpleNamespace(phase='recording', run_id='same-episode',
            finish=lambda *a, **kw: finishes.append((a, kw)))
        for seq in range(2, 1000):
            step(seq, 1, home_reset=True)
            if engine.home_stop_reason: break
        assert engine.home_stop_reason and loop.home is None
        assert engine.supervisor.state is HILState.HIL_LATCHED
        assert engine.episodes.run_id == 'same-episode' and not finishes
        assert loop.home_retry_context is None
        step(seq+1, 1, home_reset=True)
        assert loop.home is None  # Holding Y does not restart a failed Home.
        held[0] = False
        step(seq+2, 1); step(seq+3, 1, home_reset=True)
        assert isinstance(loop.home_motion, DirectWorkbenchHomeMotion)
        assert loop.home_motion.stage == 'return' and engine.home_stop_reason is None
        assert not finishes
    finally: engine.close()
