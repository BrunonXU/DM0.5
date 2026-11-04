from types import SimpleNamespace
import numpy as np
import pytest
from dagger_core.hil_supervisor import HILState
from f1dagger.robot_io import StationaryOutputTimingFault
from test_f1_output import make_owner
from test_f1_preventive_boundary import system

@pytest.mark.parametrize('case', ['stationary','enabled','moving','pending','invalid_clock'])
def test_only_disabled_settled_timing_fault_is_recoverable(case):
    o,t=make_owner();o.prepare();o.updated=1.027
    out=o.output;out.sampled=1.;out.updated=1.027;out.enabled=[False,False]
    out.planner=SimpleNamespace(velocity=np.zeros(14),acceleration=np.zeros(14))
    if case=='enabled':out.enabled=[False,True]
    if case=='moving':out.planner.velocity[:]=.001
    if case=='pending':out.target=out.position+.1
    if case=='invalid_clock':out.sampled=1.1
    def sample(now):
        out.timed_out=True;out.fault='target_age_ms=29.7 output_gap_ms=56.7'
        return out.position.copy()
    out.sample=sample
    with pytest.raises(RuntimeError) as caught:o.publish(1.0567)
    assert isinstance(caught.value,StationaryOutputTimingFault)==(case=='stationary')
    assert not t.sent and not o.last_evidence['publications']

def fault_on_publish(m):
    def fault(now):
        m.o.hold(now)
        m.o.last_evidence=dict(publication_complete=False,publications=[],stage='output_sample')
        raise StationaryOutputTimingFault('injected disabled timing overrun')
    m.o.publish=fault

def test_same_episode_requires_stable_output_and_new_rb(system):
    m=system;e=m.e;m.send(start=True,hil_button_pressed=True)
    e.local_task.begin('synthetic',m.now[0],'actor')
    original=m.o.publish;fault_on_publish(m)
    before=len(m.records);sent=len(m.t.sent);m.send(controls_neutral=True)
    assert len(m.records)==before and len(m.t.sent)==sent
    assert e.input_pause['reason']=='stationary_output_timing'
    assert e.local_task.status=='active' and e.episodes.phase=='recording'
    m.o.publish=original
    m.send(controls_neutral=True);m.send(controls_neutral=True);m.send(deadman=True)
    assert e.input_pause
    for _ in range(13):m.send(controls_neutral=True)
    m.send(deadman=True)
    assert e.input_pause is None and e.supervisor.state is HILState.HIL_LATCHED
    assert m.records[-1]['source']=='input_recovery_anchor'

@pytest.mark.parametrize('reason',['unsafe_pose','active_rb','hard_lock'])
def test_fault_type_alone_cannot_authorize_recovery(system,reason):
    m=system
    m.send(start=True,hil_button_pressed=True)
    if reason=='hard_lock':m.e.hard_hold_reason='publication_fault'
    if reason=='unsafe_pose':m.o.gate.check=lambda *a:(_ for _ in ()).throw(RuntimeError('unsafe pose'))
    fault_on_publish(m)
    with pytest.raises(RuntimeError):m.send(deadman=reason=='active_rb')
    assert m.e.input_pause is None


@pytest.mark.parametrize('state', [HILState.POLICY, HILState.HANDOFF_HOLD])
def test_stationary_policy_hold_requires_start_then_stable_fresh_rb(system, state):
    m=system;e=m.e
    e.local_task.begin('synthetic',m.now[0],'actor')
    e.rlt_phase='actor';e.policy=e.rlt_policy;e.supervisor.state=state
    original=m.o.publish;fault_on_publish(m)
    count=len(m.records);m.send(controls_neutral=True)
    assert len(m.records)==count and e.episodes.phase=='recording'
    assert e.local_task.status=='active' and not e.local_task.assisted
    assert e.input_pause['takeover_required']
    assert e.supervisor.hold_reason=='input_wait_start_takeover'
    m.o.publish=original
    for _ in range(13):m.send(controls_neutral=True)
    m.send(deadman=True)
    assert e.input_pause['takeover_required']
    m.send(controls_neutral=True)
    m.send(start=True,hil_button_pressed=True)
    assert not e.input_pause['takeover_required']
    m.send(deadman=True)
    assert e.input_pause
    for _ in range(13):m.send(controls_neutral=True)
    m.send(deadman=True)
    assert e.supervisor.state is HILState.HIL_LATCHED and e.input_pause is None
    assert e.local_task.status=='active' and e.local_task.assisted
    assert m.records[-1]['source']=='input_recovery_anchor'


def test_repeated_timing_preserves_existing_pause_cause_and_takeover_requirement(system):
    m=system;e=m.e
    e.pause_policy_candidate(m.now[0],dict(error='future boundary'))
    pause=e.input_pause;pause['retired_sessions'].add('old-session');before=dict(pause)
    assert e.pause_stationary_output(m.now[0]+1,dict(error='timing while paused'))
    assert e.input_pause is pause and pause['reason']=='policy_candidate_boundary'
    assert pause['details']==dict(error='future boundary')
    assert pause['takeover_required'] and pause['retired_sessions']=={'old-session'}
    assert pause['invalid_from']==before['invalid_from']
    assert pause['output_timing_recovery_required'] and pause['output_timing_paused_at']==m.now[0]+1
    assert pause['neutral'] is None and not pause['neutral_ready']
    e.hard_hold_reason='real hard failure'
    assert not e.pause_stationary_output(m.now[0]+2,dict(error='another timing'))
    assert pause['output_timing_paused_at']==m.now[0]+1


def test_policy_and_manual_timing_pauses_have_distinct_instructions():
    from f1dagger.rtc_hil_feedback import RTCHILFeedback
    from test_f1_rtc_hil_feedback import Audio
    lines=[];audio=Audio()
    f=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        f.notify('input_pause_started',dict(reason='stationary_output_timing',requires_start=True))
        f.notify('input_pause_started',dict(reason='stationary_output_timing',requires_start=False))
        f.jobs.join()
        assert '按START接管' in lines[0] and 'START不恢复' not in lines[0]
        assert 'START不恢复' in lines[1] and '按START接管' not in lines[1]
        assert audio.events==['need_takeover','manual_recovery']
    finally:f.close()


def test_timing_during_another_pause_keeps_start_and_output_health_gates(system):
    m=system;e=m.e
    e.pause_policy_candidate(m.now[0],dict(error='future boundary'))
    original=m.o.publish;fault_on_publish(m)
    m.send(controls_neutral=True)
    assert e.input_pause['reason']=='policy_candidate_boundary'
    assert e.input_pause['takeover_required'] and e.input_pause['output_timing_recovery_required']
    m.o.publish=original
    m.send(start=True,hil_button_pressed=True)
    m.send(controls_neutral=True);m.send(controls_neutral=True)
    m.send(deadman=True)
    assert e.input_pause  # Old collision pause cannot bypass the newer timing gate.
    for _ in range(13):m.send(controls_neutral=True)
    m.send(deadman=True)
    assert e.input_pause is None and e.supervisor.state is HILState.HIL_LATCHED
