"""Local RLT protocol: predictive veto, fresh takeover and real publication gate."""
from dataclasses import replace
from types import SimpleNamespace
import time

import numpy as np
import pytest

from dagger_core.hil_supervisor import HILState
from f1dagger.input_recovery import RecoveryChallenges
from f1dagger.robot_io import CandidateSafetyRejected, CollisionGate
from f1dagger.runtime import ControlLoop
from test_f1_rlt_hil import make_engine, episode_packet
from test_f1_output import make_owner


@pytest.fixture
def system():
    e, journal, _, _ = make_engine()
    owner, transport = make_owner()
    owner.prepare()
    now = [time.monotonic_ns()]
    measured = owner.last_submitted.copy()
    feedback = lambda: (np.r_[measured[:14], np.zeros(8)], measured.copy(), (now[0],)*16)
    owner.source.feedback = e.source.feedback = feedback
    records = []
    journal.control = lambda **row: records.append(row) is None
    e.tick = lambda now: None  # Isolate control arbitration, no inference worker.
    proof = RecoveryChallenges()
    e.recovery_proof = proof.age
    def human(packet, full, held, dt):
        value = held.copy(); value[7] += packet.axes[0]*.1
        return value
    loop = ControlLoop(e, owner, SimpleNamespace(release=lambda:None, target=human),
                       journal, {'joint_tolerance_deg':1.}, clock=lambda:now[0])
    loop.started_output = True
    seq, toggle = [0], [0]
    def send(*, start=False, **kw):
        now[0] = max(time.monotonic_ns(), now[0]+20_000_000)
        toggle[0] += int(start)
        p = replace(episode_packet(seq[0], toggle[0]), active_side='right',
                    recovery_token=proof.issue('test-session', now[0]), **kw)
        p = replace(p, session_id='test-session')
        seq[0] += 1
        loop.step(now[0], (p, now[0]))
    e.sample = lambda now, held: (held.copy(), 'hold')
    send(controls_neutral=True)
    value = SimpleNamespace(e=e, o=owner, t=transport, loop=loop, measured=measured,
                            now=now, send=send, records=records, journal=journal)
    yield value
    e.close()


@pytest.mark.parametrize('phase', ['base', 'actor'])
def test_policy_future_veto_requires_start_then_fresh_rb_and_keeps_local_task(system, phase):
    m = system; e = m.e
    if phase == 'actor':
        e.local_task.begin('synthetic', m.now[0], 'head-sha')
        e.rlt_phase, e.policy = 'actor', e.rlt_policy
    held = m.o.last_submitted.copy()
    future = held.copy(); future[7] = 1.
    e.chunk = np.array([held, future]); e.chunk_request_id = 20; e.action_index = 0
    e.sample = lambda now, last: (held.copy(), 'policy')
    def gate(value, full):
        if value[7] > .05:
            raise CandidateSafetyRejected('future collision')
    m.o.gate.check = gate
    m.send()
    assert e.input_pause['takeover_required'] and e.chunk is None
    pauses = [fields for name, fields in m.journal.events
              if name == ('input_pause_started',)
              and fields.get('reason') == 'policy_candidate_boundary']
    assert len(pauses) == 1 and pauses[0]['requires_start'] is True
    assert e.supervisor.state is HILState.SAFE_HOLD
    assert e.episodes.phase == 'recording' and not e.collision_restart_required
    assert e.local_task.status == ('active' if phase == 'actor' else 'unused')
    for _ in range(3):
        m.send(deadman=True, axes=(-1., 0., 0.))
        assert e.input_pause['takeover_required']
        np.testing.assert_array_equal(m.o.last_submitted, held)
    m.send(start=True, controls_neutral=False, hil_button_pressed=True)
    assert not e.input_pause['takeover_required']
    assert e.supervisor.state is HILState.SAFE_HOLD
    m.send(controls_neutral=True); m.send(controls_neutral=True)
    m.send(deadman=True, axes=(-1., 0., 0.))
    assert e.input_pause is None and e.supervisor.state is HILState.HIL_LATCHED
    assert m.records[-1]['source'] == 'input_recovery_anchor'
    m.send(deadman=True, axes=(-1., 0., 0.))
    assert m.o.last_submitted[7] == pytest.approx(-.1)
    if phase == 'actor':
        assert e.local_task.status == 'active' and e.local_task.assisted
        assert e.local_success(m.now[0])
        assert e.local_task.status == 'success' and e.supervisor.state is HILState.HIL_LATCHED


def test_smoothed_command_is_checked_before_any_topic_write(system):
    m = system
    m.o.output.sample = lambda now: np.r_[np.zeros(7), 99., np.zeros(6)]
    def gate(value, full):
        if value[7] > 1: raise CandidateSafetyRejected('smoothed collision')
    m.o.gate.check = gate
    before = len(m.t.sent)
    with pytest.raises(CandidateSafetyRejected, match='smoothed collision'):
        m.o.publish(m.now[0]/1e9)
    assert len(m.t.sent) == before
    assert m.o.last_evidence['publications'] == []
    assert not m.o.last_evidence['publication_complete']


def test_proxy_overlap_cannot_authorize_recovery_even_if_legacy_gate_passes():
    gate = object.__new__(CollisionGate)
    gate.check = lambda *a: {'clearance_m':-.001}
    with pytest.raises(CandidateSafetyRejected, match='nonpositive'):
        gate.check_preventive(np.zeros(16), np.zeros(22))


def test_current_unsafe_pose_is_not_downgraded_to_candidate_pause(system):
    m = system
    m.e.sample = lambda now, held: (held.copy(), 'policy')
    m.o.gate.check_preventive = lambda *a: (_ for _ in ()).throw(
        CandidateSafetyRejected('F1 preventive boundary: overlap'))
    before = len(m.t.sent)
    with pytest.raises(CandidateSafetyRejected, match='overlap'):
        m.send()
    assert len(m.t.sent) == before
    assert m.e.input_pause is None and m.e.collision_restart_required
    assert m.e.hard_hold_reason


def test_worker_rejection_during_actor_hold_keeps_segment_and_requires_start(system):
    m=system; e=m.e
    e.local_task.begin('synthetic',m.now[0],'head-sha')
    e.rlt_phase='actor';e.policy=e.rlt_policy
    e.supervisor.state=HILState.HANDOFF_HOLD
    e.candidate_rejection_handler=m.loop._rejected_policy_plan
    e.policy_failed(CandidateSafetyRejected('future collision'),m.now[0])
    assert e.input_pause['takeover_required'] and e.local_task.status=='active'
    assert e.hard_hold_reason is None and not e.collision_restart_required


def test_manual_joint_limit_consumes_held_rb_and_does_not_replay_gripper(system):
    from f1dagger.teleop import ArmBoundaryHold
    m=system
    m.send(start=True,hil_button_pressed=True)
    def veto(*args):raise ArmBoundaryHold('arm_joint_limit')
    m.loop.arm.target=veto
    before=m.o.last_submitted.copy()
    m.send(deadman=True,axes=(1.,0.,0.))
    assert m.e.input_pause and not m.e.input_pause.get('takeover_required',False)
    m.send(deadman=True,axes=(-1.,0.,0.),grip_target=0.)
    np.testing.assert_array_equal(m.o.last_submitted,before)
    assert m.records[-1]['candidate'] is None and m.records[-1]['source']=='input_pause_hold'


def test_plan_guard_checks_future_midpoint_without_touching_output_owner():
    from f1dagger.policy_safety import PolicyPlanGuard
    checked=[]
    def check(value, full):
        checked.append(value.copy())
        if .4<value[7]<.6:raise CandidateSafetyRejected('midpoint collision')
    guard=PolicyPlanGuard(SimpleNamespace(check_preventive=check),
                          SimpleNamespace(check_action=lambda q:q),lambda *a,**k:None)
    actions=np.zeros((2,16));actions[1,7]=1.
    with pytest.raises(CandidateSafetyRejected,match='midpoint'):
        guard(SimpleNamespace(state16=np.zeros(16),full22=np.zeros(22)),actions,3,4)
    assert checked[-1][7]==.5


def test_rtc_join_guard_is_bounded_and_rejects_unsafe_join():
    from f1dagger.policy_safety import PolicyPlanGuard
    checked=[];events=[]
    def check(value,full):
        checked.append(value.copy())
        if value[7]>.4:raise CandidateSafetyRejected('unsafe join')
    guard=PolicyPlanGuard(SimpleNamespace(check_preventive=check),
        SimpleNamespace(check_action=lambda q:q),lambda name,**fields:events.append(fields))
    actions=np.zeros((32,16));actions[15:,7]=1.
    obs=SimpleNamespace(state16=np.zeros(16),full22=np.zeros(22))
    guard(obs,actions,1,2,committed_prefix=14,max_new_waypoints=1)
    assert len(checked)==2 and events[-1]['deferred_new_waypoints']==17
    assert events[-1]['scope'].startswith('rtc_join_only')
    actions[14,7]=1.
    with pytest.raises(CandidateSafetyRejected,match='unsafe join'):
        guard(obs,actions,1,3,committed_prefix=14,max_new_waypoints=1)


def test_rtc_bounds_join_screen_but_actor_retains_full_plan_screen():
    from test_f1_policy import observation
    e,j,_,_=make_engine();calls=[]
    e.plan_validator=lambda *a,**kw:calls.append(kw)
    try:
        obs=observation(1,time.monotonic_ns())
        context=dict(bootstrap=True,coverage_steps=32)
        e._rtc_infer(obs,1,1,context)
        context.update(bootstrap=False,committed_actions16=np.zeros((14,16)))
        e._rtc_infer(obs,1,2,context)
        e._infer(obs,1,3)
        assert calls==[dict(committed_prefix=0,max_new_waypoints=1),
                       dict(committed_prefix=14,max_new_waypoints=1),{}]
    finally:e.close()


def test_deferred_rtc_suffix_is_vetoed_at_target_gate_before_publication(system):
    m=system;held=m.o.last_submitted.copy()
    m.e.plan_validator=lambda *a,**k:None  # RTC join already approved.
    target=held.copy();target[7]=1.
    m.e.sample=lambda *a:(target.copy(),'policy')
    def gate(value,full):
        if value[7]>.05:raise CandidateSafetyRejected('later suffix collision')
    m.o.gate.check=gate
    m.send()
    assert m.e.input_pause['takeover_required']
    np.testing.assert_array_equal(m.o.last_submitted,held)
    assert m.records[-1]['source']=='input_pause_hold'
