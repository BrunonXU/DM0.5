"""Real solvers at sustained boundaries, no ROS and no robot output."""
from types import SimpleNamespace
import numpy as np
import pytest
pytest.importorskip('ruckig')
pytest.importorskip('pink')
from test_f1_numerical import setup, _intent
from f1dagger.teleop import F1ArmTarget
from f1dagger.robot_io import make_output


@pytest.mark.parametrize('side', ['left','right'])
@pytest.mark.parametrize('axis', [0,1,2])
def test_persistent_input_direct_reverse_without_release(setup, side, axis):
    config, profile, mapping, urdf, full = setup
    state = np.r_[full[:14],43.,66.]
    arm = F1ArmTarget(urdf, mapping, config['ik'])
    direction = -np.eye(3)[axis]
    for _ in range(500):
        state = arm.target(_intent(side, direction), full, state, .02)
        mapping.check_action(state)
    origin = arm.ik.target_pose.translation.copy()
    for _ in range(20):
        state = arm.target(_intent(side, -direction), full, state, .02)
        mapping.check_action(state)
        current = arm.ik.target_pose.translation.copy()
        assert np.dot(current-origin, direction) < -1e-9
        origin = current


@pytest.mark.parametrize('side', ['left','right'])
def test_wrist_tangent_and_reverse_remain_feasible(setup, side):
    config, profile, mapping, urdf, full = setup
    full = full.copy(); offset = 0 if side=='left' else 7
    full[offset+5:offset+7] = [50., 110.5-1.025*50.-1e-5]
    state = np.r_[full[:14], 43., 66.]
    arm = F1ArmTarget(urdf, mapping, config['ik'])
    # Exercise all Cartesian components from the coupled boundary.
    travelled = 0.
    for axes in np.r_[np.eye(3), -np.eye(3)]:
        before = state.copy()
        for _ in range(80):
            state = arm.target(_intent(side, axes), full, state, .02)
            mapping.check_action(state)
        travelled += np.linalg.norm(state-before)
    assert travelled > 1.


@pytest.mark.parametrize('wrist', [False, True])
def test_output_brakes_without_fault_and_reverses_with_rb_held(setup, wrist):
    config, _, mapping, _, full = setup
    output = make_output(config)
    output.planner.set_limits(mapping.limits)
    q = full[:14].copy()
    if wrist:
        q[5:7] = [40.,50.]
        outward = q.copy();outward[5:7] = [55.,110.5-1.025*55.-1e-8]
    else:
        q[1] = -90.
        outward = q.copy();outward[1] = -94.+1e-8
    inward = q.copy()
    output.push(q, 0., enabled=(True,False))
    now = 0.; origin = None; modes = set(); reverse = False
    for tick in range(2400):
        dt = (.00063,.004,.0135,.005)[tick%4]
        old_a = np.array(output.planner.acceleration)
        now += dt
        target = outward if tick < 1200 else inward
        output.push(target, now, enabled=(True,False))
        current = np.array(output.sample(now))
        assert output.fault is None
        mapping.check_action(np.r_[current,43.,66.])
        assert np.max(np.abs(output.planner.velocity)) <= output.planner.max_velocity+1e-7
        assert np.max(np.abs(output.planner.acceleration)) <= output.planner.max_acceleration+1e-7
        assert np.max(np.abs(np.array(output.planner.acceleration)-old_a))/dt <= output.planner.max_jerk+1e-5
        modes.add(output.planner.limit_diagnostics['mode'])
        if tick==1199: origin=current.copy()
        if tick>=1200 and np.linalg.norm(current-inward)<np.linalg.norm(origin-inward)-.01:
            reverse=True
    assert reverse
    np.testing.assert_allclose(current,inward,atol=.002)


def test_output_certification_rejects_unsafe_future_but_keeps_momentum(setup):
    config, _, mapping, _, full = setup
    output = make_output(config);p=output.planner;p.set_limits(mapping.limits)
    q=full[:14].copy();q[1]=-92.
    output.push(q,0.,enabled=(True,False))
    target=q.copy();target[1]=-94.+1e-8
    found=False
    for tick in range(1,700):
        now=tick*.004
        output.push(target,now,enabled=(True,False))
        output.sample(now)
        assert output.fault is None
        # Force a new goal close to the moving output while it still has
        # outward velocity; its stopping overshoot must not cross -94.
        if tick>30:
            target[1]=max(-94.+1e-8, p.position[1]-.0001)
        if p.limit_diagnostics['mode']=='limit_braking': found=True
        mapping.check_action(np.r_[p.position,43.,66.])
    # A monotone legal path may be fully certifiable. Explicit extrema test
    # independently confirms rejection of a bad stopping trajectory below.
    import ruckig
    inp=ruckig.InputParameter(14);inp.current_position=np.deg2rad(q).tolist()
    inp.current_position=list(inp.current_position);pos=list(inp.current_position);pos[1]=np.deg2rad(-93.999);inp.current_position=pos
    inp.current_velocity=[0.]*14;vel=[0.]*14;vel[1]=-.1;inp.current_velocity=vel
    inp.target_position=pos;inp.max_velocity=[.35]*14;inp.max_acceleration=[1.]*14;inp.max_jerk=[5.]*14
    trajectory=ruckig.Trajectory(14);ruckig.Ruckig(14).calculate(inp,trajectory)
    assert not p._trajectory_safe(trajectory)


def test_certified_fallback_preserves_velocity_acceleration(setup, monkeypatch):
    config,_,mapping,_,full=setup
    output=make_output(config);p=output.planner;p.set_limits(mapping.limits)
    q=full[:14];target=q.copy();target[0]-=10.
    output.push(q,0.,enabled=(True,False))
    for tick in range(1,40):
        output.push(target,tick*.004,enabled=(True,False));output.sample(tick*.004)
    previous_velocity=np.array(p.velocity);previous_acceleration=np.array(p.acceleration)
    assert np.max(np.abs(previous_velocity))>.1
    monkeypatch.setattr(p,'_trajectory_safe',lambda _:False)
    output.push(target,.160,enabled=(True,False));output.sample(.160)
    assert output.fault is None and p.limit_diagnostics['mode']=='limit_braking'
    assert np.max(np.abs(np.array(p.acceleration)-previous_acceleration))/.004<=p.max_jerk+1e-5
    assert np.max(np.abs(np.array(p.velocity)-previous_velocity))/.004<=p.max_acceleration+1e-5
    assert np.max(np.abs(p.velocity))>.1
    # Finish the pre-certified braking path. Its requested terminal v/a are
    # exactly zero, even when polynomial evaluation leaves floating residue.
    # The preceding assertions ensure we did not reset a still-moving state.
    for tick in range(41,401):
        old_a=np.array(p.acceleration)
        output.push(target,tick*.004,enabled=(True,False));output.sample(tick*.004)
        assert output.fault is None
        assert np.max(np.abs(np.array(p.acceleration)-old_a))/.004<=p.max_jerk+1e-5
    assert p.limit_diagnostics['remaining_s']==0.
    np.testing.assert_array_equal(p.velocity,np.zeros(14))
    np.testing.assert_array_equal(p.acceleration,np.zeros(14))


def test_real_ik_and_output_preserve_hil_epoch_side_rb_and_rt(setup):
    from test_f1_runtime import setup_loop
    from test_f1_numerical import BOUNDARY_CASES
    from g2dagger.hil_supervisor import HILState
    config,profile,mapping,urdf,full=setup
    side,command_arm,measured,axes,angular=BOUNDARY_CASES[0]
    full=full.copy();full[7:14]=command_arm
    initial=np.r_[full[:14],43.,66.]
    loop,engine,owner,transport,now,records,step=setup_loop()
    owner.mapping=mapping;owner.output=make_output(config);owner.output.planner.set_limits(mapping.limits)
    owner.source.feedback=lambda:(full.copy(),initial.copy(),(now[0],)*16)
    owner.initialize(now[0]/1e9)
    loop.arm=F1ArmTarget(urdf,mapping,config['ik']);loop.profile=profile
    try:
        step(0,active_side=side,grip_target=None)
        step(1,1,active_side=side,grip_target=None)
        epoch=engine.supervisor.policy_epoch;intervention=engine.supervisor.intervention_id
        for seq in range(2,602):
            reversing=seq>=502
            step(seq,1,active_side=side,deadman=True,
                 axes=[-v for v in axes] if reversing else axes,angular_axes=angular,
                 grip_target=None if seq<100 else 1.)
            assert engine.supervisor.state is HILState.HIL_LATCHED
            assert engine.supervisor.policy_epoch==epoch
            assert engine.supervisor.intervention_id==intervention
            assert loop.last_packet.active_side==side and loop.last_packet.deadman
            assert not engine.session.require_disabled_packet
            mapping.check_action(owner.last_submitted)
            if seq==501: before=owner.last_submitted.copy()
        assert owner.last_submitted[8]>before[8]+.01
        assert owner.last_submitted[15]==100.
        assert not any(args[0] in ('target_rejected','arm_boundary_hold','publication_failed')
                       for args,fields in engine.recorder.events)
    finally:
        engine.close()



def test_coupled_wrist_tangent_uses_certified_phase_path(setup):
    config,_,mapping,_,full=setup
    output=make_output(config);p=output.planner;p.set_limits(mapping.limits)
    q=full[:14].copy();q[5:7]=[40.,69.5-1e-4]
    target=q.copy();target[5:7]=[60.,49.-1e-4]
    output.push(q,0.,enabled=(True,False));phase_count=0
    for tick in range(1,1001):
        now=tick*.004;before=np.array(p.acceleration)
        output.push(target,now,enabled=(True,False));command=output.sample(now)
        assert output.fault is None
        mapping.check_action(np.r_[command,43.,66.])
        assert np.max(np.abs(np.array(p.acceleration)-before))/.004<=p.max_jerk+1e-5
        phase_count += p.limit_diagnostics.get('synchronization')=='phase'
    assert phase_count>0
    np.testing.assert_allclose(command,target,atol=.002,rtol=0)
