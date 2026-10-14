"""Recoverable arm-boundary holds preserve ownership, but never bypass safety."""
import numpy as np
import pytest

from f1dagger.teleop import ArmBoundaryHold
from g2dagger.hil_supervisor import HILState
from test_f1_runtime import setup_loop


def recorded_events(engine, name):
    return [fields for args, fields in engine.recorder.events if args[0] == name]


def test_boundary_flushes_pending_motion_and_reverse_recovers_without_start():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        epoch, intervention = e.supervisor.policy_epoch, e.supervisor.intervention_id
        original_reset, original_sample = o.output.reset, o.output.sample
        momentum = [9.]
        def reset():
            momentum[0] = 0.
            original_reset()
        def sample(now_s):
            result = original_sample(now_s)
            result[0] += momentum[0]
            return result
        o.output.reset, o.output.sample = reset, sample
        calls=[]
        def target(packet, full, last, dt):
            calls.append(packet.axes)
            if packet.axes[0] > 0:
                raise ArmBoundaryHold('arm_joint_limit', {'candidate_deg': 120.+len(calls)})
            result = last.copy()
            result[0] -= .1
            return result
        loop.arm.target=target
        for sequence in range(2,7):
            step(sequence,1,deadman=True,axes=(.5,0.,0.))
            assert e.supervisor.state is HILState.HIL_LATCHED
            assert e.supervisor.policy_epoch == epoch
            assert e.supervisor.intervention_id == intervention
            assert not e.session.require_disabled_packet
            assert loop.last_packet.active_side == 'left'
            assert e.last_received_ns == now[0]
            assert o.last_submitted[0] == 0.
            assert records[-1]['source'] == 'human_hold'
        assert momentum[0] == 0.
        assert len(recorded_events(e, 'arm_boundary_hold')) == 1
        assert not recorded_events(e, 'target_rejected')
        step(7,1,deadman=True,axes=(-.5,0.,0.))
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert e.supervisor.policy_epoch == epoch
        assert o.last_submitted[0] == -.1
        assert records[-1]['source'] == 'human_arm'
        assert len(recorded_events(e, 'arm_boundary_recovered')) == 1
        step(8,1,deadman=True,axes=(-.5,0.,0.))
        assert len(recorded_events(e, 'arm_boundary_recovered')) == 1
    finally:
        e.close()


def test_boundary_events_only_enter_reason_change_and_recovery():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        reason=['arm_joint_limit']
        count=[0]
        def boundary(*args):
            count[0]+=1
            raise ArmBoundaryHold(reason[0], {'candidate_deg':float(count[0])})
        loop.arm.target=boundary
        for seq in range(2,7): step(seq,1,deadman=True)
        reason[0]='arm_wrist_limit'
        for seq in range(7,12): step(seq,1,deadman=True)
        events=recorded_events(e, 'arm_boundary_hold')
        assert len(events) == 2
        assert [item['details']['reason'] for item in events] == ['arm_joint_limit','arm_wrist_limit']
        assert not recorded_events(e, 'arm_boundary_recovered')
    finally:
        e.close()


def test_rt_remains_independent_while_arm_is_at_boundary():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1,grip_target=None)
        def boundary(*args): raise ArmBoundaryHold('arm_joint_limit')
        loop.arm.target=boundary
        step(2,1,deadman=True,grip_target=None)
        step(3,1,deadman=True,grip_target=1.)
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert records[-1]['source'] == 'human_hand'
        assert list(o.last_submitted[14:]) == [100.,66.]
        step(4,1,deadman=True,grip_target=1.)
        assert records[-1]['source'] == 'human_hold'
        step(5,1,deadman=True,grip_target=0.)
        assert list(o.last_submitted[14:]) == [0.,66.]
        assert records[-1]['source'] == 'human_hand'
    finally:
        e.close()


@pytest.mark.parametrize('failure', ['collision', 'nonfinite', 'dt', 'owner_boundary'])
def test_nonboundary_or_downstream_failures_stay_safe_hold_and_keep_primary_reason(failure):
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        if failure=='collision':
            def reject(*args): raise RuntimeError('F1 collision/CoM rejected target: test')
            o.gate.check=reject
        elif failure=='owner_boundary':
            # A typed error outside arm.target is not a recoverable IK boundary.
            def reject(*args): raise ArmBoundaryHold('arm_joint_limit')
            o.gate.check=reject
        else:
            def reject(*args): raise RuntimeError('non-finite IK candidate' if failure=='nonfinite' else 'dt outside safety limit')
            loop.arm.target=reject
        step(2,1,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert e.supervisor.hold_reason.startswith('target_rejected:')
        assert 'watchdog' not in e.supervisor.hold_reason
        assert e.session.require_disabled_packet
        assert o.last_submitted[0] == 0.
        assert records[-1]['source'] == 'hold'
        assert not recorded_events(e, 'arm_boundary_hold')
        assert not recorded_events(e, 'arm_boundary_recovered')
        loop.arm.target=lambda p,f,last,dt:np.r_[last[0]-.1,last[1:]]
        o.gate.check=lambda *args:True
        step(3,1,deadman=True,axes=(-.5,0.,0.))
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert o.last_submitted[0] == 0.
    finally:
        e.close()


def test_true_disconnect_after_boundary_never_auto_recovers_on_reverse():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        def boundary(*args): raise ArmBoundaryHold('arm_joint_limit')
        loop.arm.target=boundary
        step(2,1,deadman=True)
        now[0]+=20_000_000
        loop.step(now[0],disconnected='operator_disconnected')
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert e.supervisor.hold_reason == 'operator_disconnected'
        loop.arm.target=lambda p,f,last,dt:np.r_[last[0]-.1,last[1:]]
        step(3,1,deadman=True,axes=(-.5,0.,0.))
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert o.last_submitted[0] == 0.
        step(4,1,deadman=False)
        assert e.supervisor.state is HILState.SAFE_HOLD
        step(5,2,deadman=False)
        assert e.supervisor.state is HILState.HIL_LATCHED
        step(6,2,deadman=True,axes=(-.5,0.,0.))
        assert o.last_submitted[0] == -.1
    finally:
        e.close()


def test_slow_boundary_processing_still_triggers_real_dispatch_watchdog():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        def boundary(*args):
            now[0]+=210_000_000
            raise ArmBoundaryHold('arm_joint_limit')
        loop.arm.target=boundary
        step(2,1,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert e.supervisor.hold_reason == 'operator_watchdog_before_publication'
        assert o.last_submitted[0] == 0.
        assert records[-1]['source'] == 'hold'
    finally:
        e.close()


def test_input_expiry_while_at_boundary_keeps_watchdog_fault_latched():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        def boundary(*args): raise ArmBoundaryHold('arm_joint_limit')
        loop.arm.target=boundary
        step(2,1,deadman=True)
        now[0]+=210_000_000
        loop.step(now[0])
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert e.supervisor.hold_reason == 'operator_watchdog'
        step(3,1,deadman=True,axes=(-.5,0.,0.))
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert o.last_submitted[0] == 0.
    finally:
        e.close()


def test_feedback_failure_after_boundary_aborts_publication_and_cannot_auto_resume():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        feedback=o.source.feedback
        calls=[0]
        def fresh_then_stale():
            calls[0]+=1
            if calls[0] > 1:
                raise RuntimeError('stale F1 joint feedback')
            return feedback()
        def boundary(*args): raise ArmBoundaryHold('arm_joint_limit')
        loop.arm.target=boundary
        o.source.feedback=fresh_then_stale
        published_before=len(t.sent)
        with pytest.raises(RuntimeError, match='stale F1 joint feedback'):
            step(2,1,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert e.supervisor.hold_reason == 'target_rejected:stale F1 joint feedback'
        assert len(t.sent) == published_before
        o.source.feedback=feedback
        loop.arm.target=lambda p,f,last,dt:np.r_[last[0]-.1,last[1:]]
        step(3,1,deadman=True,axes=(-.5,0.,0.))
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert o.last_submitted[0] == 0.
    finally:
        e.close()


def test_boundary_recovery_event_waits_for_collision_approval():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0); step(1,1)
        def boundary(*args): raise ArmBoundaryHold('arm_joint_limit')
        loop.arm.target=boundary
        step(2,1,deadman=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
        loop.arm.target=lambda p,f,last,dt:np.r_[last[0]-.1,last[1:]]
        def collision(*args): raise RuntimeError('F1 collision/CoM rejected target: reverse')
        o.gate.check=collision
        step(3,1,deadman=True,axes=(-.5,0.,0.))
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert e.supervisor.hold_reason.startswith('target_rejected:F1 collision')
        assert o.last_submitted[0] == 0.
        assert not recorded_events(e, 'arm_boundary_recovered')
    finally:
        e.close()
