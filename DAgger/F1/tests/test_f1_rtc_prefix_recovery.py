"""Recovery state-machine tests with no robot, model or synthetic planner reset."""
from dataclasses import replace
from types import SimpleNamespace
import time

import numpy as np
import pytest

from dagger_core.recording_sinks import NullSink
from dagger_core.hil_supervisor import HILState
from f1dagger.policy_errors import IKRejected
from f1dagger.rtc_prefix_recovery import RTCFeasiblePrefixEngine, PrefixStopEvidence
from f1dagger.standalone import StandaloneFinished
from test_f1_policy import Source, Journal, observation


def fixture_engine(enabled=True):
    journal=Journal();held=np.zeros(16);held[15]=50.
    probe=[None];stop=[False]
    def validate(action):
        assert np.isfinite(action).all()
        if abs(action[7])>178:
            raise ValueError('existing_joint_limit')
    policy=SimpleNamespace(model_id='fake',right_only=True,last_timing={},
        held_getter=lambda:held.copy(),validate_action=validate)
    engine=RTCFeasiblePrefixEngine(journal.record_event,Source(),policy,journal,NullSink(),
        {'capture_hz':30.,'policy':{'chunk_steps':20,'frequency_hz':30.}},
        rtc_delay_steps=20,rtc_min_steps=20,duration_seconds=None,stop_requested=lambda:stop[0],
        feasible_prefix_enabled=enabled,stop_probe=lambda request,now:probe[0],prefix_owner_token='owner-A',
        prefix_stop_tolerance_deg=.1,prefix_feedback_max_age_ns=100_000_000,
        prefix_settle_timeout_s=2.,prefix_planner_deadband_rad=1e-6)
    now=time.monotonic_ns();engine.authorize_local(now)
    return engine,held,probe,stop,now,journal


def rejection(engine,held,now,valid=3,bootstrap=False):
    engine.chunk=np.repeat(held[None],42,axis=0)
    engine.chunk_started_ns=now-round(20/30*1e9)
    engine.chunk_anchor=held.copy();engine.plan_id=1
    engine.absolute_eef=np.zeros((50,10))
    ctx=dict(bootstrap=bootstrap,delay_steps=20,shift_steps=20,coverage_steps=42,
        base_plan_id=0 if bootstrap else 1,plan_origin_ns=0 if bootstrap else now,
        committed_actions16=[] if bootstrap else engine.chunk[20:40].tolist())
    engine.pending_context=ctx
    engine.last_requested=observation(2,now-1_000_000)
    engine.last_request_ns=now-500_000
    engine.real_future=None;engine.real_future_request_id=2
    engine.real_future_epoch=engine.supervisor.policy_epoch
    engine.policy.last_timing=dict(response_verified=True,outcome='ik_rejected',request_id=2,
        policy_epoch=engine.supervisor.policy_epoch,session_id='session')
    rows=[dict(success=True,within_limits=True,joints=[.01*(i+1),0.,0.,0.,0.,0.,0.],
        position_error_mm=0.,rotation_error_deg=0.) for i in range(valid)]
    rows.append(dict(success=False,joints=None))
    return IKRejected(dict(request_id=2,policy_epoch=engine.supervisor.policy_epoch,session_id='session',
        success=False,reason='ik_rejected',actions=None,active_action_indices=[7,8,9,10,11,12,13,15],
        rtc_context={k:v for k,v in ctx.items() if k!='committed_actions16'},
        diagnostics=rows,absolute_eef10=np.zeros((50,10)).tolist()))


def evidence(engine):
    request=engine.prefix_recovery['request'];q=np.deg2rad(request.target16[7:14]);end=request.endpoint_monotonic_ns
    return PrefixStopEvidence(policy_epoch=request.policy_epoch,request_id=request.request_id,
        owner_token=request.owner_token,target16=request.target16,certificate_valid=True,
        certified_right_rad=tuple(q),planner_right_position_rad=tuple(q),
        planner_right_velocity_rad_s=(0.,)*7,planner_right_acceleration_rad_s2=(0.,)*7,
        published16=request.target16,published_monotonic_ns=end+1_000_000,
        measured16=request.target16,feedback_timestamps_ns=(end+2_000_000,)*16,
        observed_monotonic_ns=end+3_000_000)


def test_disabled_is_exact_original_hold_path():
    e,held,_,_,now,_=fixture_engine(False)
    try:
        assert not e.tracking_hold_recovery_enabled
        e.policy_failed(rejection(e,held,now),now)
        assert e.prefix_recovery is None and e.chunk is None
        assert e.ik_failures==1 and e.ik_hold_pending
    finally:e.close()


def test_preserves_committed_prefix_origin_and_full_gripper_sequence():
    e,held,_,_,now,journal=fixture_engine()
    try:
        exc=rejection(e,held,now)
        e.chunk[25,15]=47.;e.pending_context['committed_actions16']=e.chunk[20:40].tolist()
        prefix=np.asarray(e.pending_context['committed_actions16']).copy()
        exc.response['absolute_eef10'][20][9]=-.1
        exc.response['absolute_eef10'][21][9]=.2
        exc.response['absolute_eef10'][22][9]=1.1
        e.policy_failed(exc,now)
        assert e.prefix_recovery is not None and e.plan_id==2
        np.testing.assert_array_equal(e.chunk[:20],prefix)
        np.testing.assert_array_equal(e.chunk[20:,15],[45.,60.,100.])
        assert e.chunk_started_ns==now and not e.policy_enabled
        assert len(journal.chunks)==1
        np.testing.assert_array_equal(journal.chunks[0][5],e.chunk)
        event=next(fields for names,fields in journal.events if names==('rtc_prefix_recovery_started',))
        assert event['gripper_values_change'] and len(event['original_absolute_eef10'])==50
        assert event['context']==e.pending_context and event['observation_id']==2
        assert e.ik_failures==0  # Not reset/incremented before stop confirmation.
        target,source=e.sample(now+100_000_000,held)
        assert source=='policy' and e.action_index==2
    finally:e.close()


def test_complete_success_path_still_resets_retry_counter_and_uses_original_rtc():
    from test_f1_rtc import run_until
    e,held,_,_,now,_=fixture_engine()
    try:
        e.ik_failures=2
        def infer(obs,rid,epoch,ctx):
            return np.repeat(held[None],ctx['coverage_steps'],axis=0),np.zeros((50,10))
        e.policy.infer_rtc=infer
        run_until(e,lambda:e.plan_id==1)
        assert e.prefix_recovery is None and e.ik_failures==0 and e.ik_state=='running'
        assert e.policy_enabled
    finally:e.close()


def test_terminal_target_never_exhausts_or_schedules_before_verified_stop():
    e,held,probe,_,now,_=fixture_engine()
    try:
        e.policy_failed(rejection(e,held,now),now)
        original=e.chunk.copy();end=e.prefix_recovery['request'].endpoint_monotonic_ns
        epoch=e.supervisor.policy_epoch
        for offset in (0,10_000_000,40_000_000):
            e.tick(end+offset)
            target,source=e.sample(end+offset,held)
            np.testing.assert_array_equal(target,original[-1])
            assert source=='policy' and e.chunk is not None and e.real_future is None
            assert e.supervisor.policy_epoch==epoch and not e.ik_hold_pending
        proof=evidence(e);probe[0]=proof
        e.tick(proof.observed_monotonic_ns)
        assert e.prefix_recovery is None and e.chunk is None
        assert e.ik_failures==1 and e.ik_hold_pending and not e.policy_enabled
        assert e.supervisor.policy_epoch>epoch and e.supervisor.state is HILState.HANDOFF_HOLD
        e.recovery_hold_committed(proof.observed_monotonic_ns+1)
        assert e.policy_enabled and e.chunk is None
        assert e.minimum_observation_ns>proof.observed_monotonic_ns
    finally:e.close()


@pytest.mark.parametrize('field,value',[
    ('planner_right_velocity_rad_s',(0.,0.,.01,0.,0.,0.,0.)),
    ('planner_right_acceleration_rad_s2',(.01,0.,0.,0.,0.,0.,0.)),
    ('certificate_valid',False),('owner_token','another-owner'),('request_id',99),
    ('feedback_timestamps_ns',(0,)*16),('published_monotonic_ns',0),
    ('measured16',(99.,)*16),('planner_right_position_rad',(99.,)*7),
])
def test_stop_requires_real_stationary_publication_certificate_and_fresh_following(field,value):
    e,held,probe,_,now,_=fixture_engine()
    try:
        e.policy_failed(rejection(e,held,now),now)
        proof=replace(evidence(e),**{field:value});probe[0]=proof
        e.before_publication(proof.observed_monotonic_ns)
        assert e.prefix_recovery is not None and e.ik_failures==0 and not e.ik_hold_pending
    finally:e.close()


@pytest.mark.parametrize('fault',['unverified','stale_epoch','late','changed_prefix','bad_joint','no_new'])
def test_invalid_partial_falls_back_to_original_hold(fault):
    e,held,_,_,now,_=fixture_engine()
    try:
        exc=rejection(e,held,now,valid=0 if fault=='no_new' else 3)
        if fault=='unverified':e.policy.last_timing['response_verified']=False
        if fault=='stale_epoch':e.real_future_epoch-=1
        if fault=='late':now+=700_000_000
        if fault=='changed_prefix':e.pending_context['committed_actions16'][0][7]=1.
        if fault=='bad_joint':exc.response['diagnostics'][0]['joints'][0]=179.
        e.policy_failed(exc,now)
        assert e.prefix_recovery is None and e.chunk is None and e.ik_failures==1 and e.ik_hold_pending
    finally:e.close()


def test_retry_counter_survives_partial_motion_and_bounds_micro_progress():
    e,held,probe,_,now,_=fixture_engine()
    try:
        for prior in range(3):
            exc=rejection(e,held,now,valid=1)
            e.supervisor.state=HILState.POLICY;e.ik_state='running'
            e.policy_failed(exc,now)
            assert e.ik_failures==prior and e.prefix_recovery is not None
            proof=evidence(e);probe[0]=proof
            e.before_publication(proof.observed_monotonic_ns)
            assert e.ik_failures==prior+1
            now=proof.observed_monotonic_ns+1
            e.recovery_hold_committed(now)
        e.supervisor.state=HILState.POLICY;e.ik_state='running'
        e.policy_failed(rejection(e,held,now,valid=1),now)
        assert e.prefix_recovery is None and e.ik_failures==4 and e.ik_state=='paused'
    finally:e.close()


@pytest.mark.parametrize('fault',['ctrl_c','deadline','epoch','camera','timeout'])
def test_recovery_does_not_disable_existing_faults(fault):
    e,held,_,stop,now,_=fixture_engine()
    try:
        e.policy_failed(rejection(e,held,now),now)
        if fault=='ctrl_c':stop[0]=True
        if fault=='deadline':e.deadline_ns=now
        if fault=='epoch':e.supervisor.policy_epoch+=1
        if fault=='camera':e._camera_fault_pending='lost'
        if fault=='timeout':now=e.prefix_recovery['deadline_ns']
        if fault in ('ctrl_c','deadline'):
            with pytest.raises(StandaloneFinished):e.tick(now)
        elif fault=='camera':
            with pytest.raises(RuntimeError):e.tick(now)
        else:
            e.tick(now)
            assert e.prefix_recovery is None and e.ik_state=='paused' and e.ik_hold_pending
    finally:e.close()


@pytest.mark.parametrize('probe_error',[False,True])
def test_stop_timeout_reports_tracking_failure_and_still_pauses(probe_error):
    e,held,_,_,now,journal=fixture_engine()
    try:
        e.policy_failed(rejection(e,held,now),now)
        def diagnostics(request,stamp):
            if probe_error:raise ValueError('feedback unavailable')
            return dict(blockers=['measured_target_not_reached'],max_error_joint=6,max_error_deg=3.)
        e.prefix_stop_diagnostics=diagnostics
        now=e.prefix_recovery['deadline_ns']
        e.tick(now)
        assert e.ik_state=='paused' and e.ik_hold_pending and not e.policy_enabled
        assert '实测到位' in e.pause_message and '响应超时' not in e.pause_message
        reports=[fields for names,fields in journal.events if names==('rtc_prefix_recovery_stop_failed',)]
        assert len(reports)==1 and reports[0]['inference_timeout'] is False
        assert reports[0]['automatic_retry'] is False
        assert reports[0]['diagnostic']['blockers']==[
            'diagnostic_probe_error' if probe_error else 'measured_target_not_reached']
        e.tick(now+1)
        assert len([n for n,_ in journal.events if n==('rtc_prefix_recovery_stop_failed',)])==1
    finally:e.close()


def tracking_timeout_fixture():
    e,held,probe,stop,now,journal=fixture_engine()
    e.tracking_hold_recovery_enabled=True
    e.policy_failed(rejection(e,held,now),now)
    deadline=e.prefix_recovery['deadline_ns'];original=evidence(e)
    measured=np.asarray(original.target16).copy();measured[12]-=3.
    original=replace(original,measured16=tuple(measured),observed_monotonic_ns=deadline,
        feedback_timestamps_ns=(deadline-1,)*16)
    probe[0]=original
    return e,held,probe,stop,deadline,journal,original


def test_tracking_hold_abandons_target_and_waits_for_real_hold_and_newer_feedback():
    e,held,probe,_,now,journal,original=tracking_timeout_fixture()
    try:
        epoch=e.supervisor.policy_epoch
        e.before_publication(now)
        assert e.prefix_recovery is None and e.tracking_hold_recovery is not None
        assert e.ik_failures==1 and e.ik_hold_pending and not e.policy_enabled
        assert e.supervisor.policy_epoch>epoch and e.chunk is None
        e.before_publication(now+1)
        assert not e.policy_enabled  # No HOLD publication yet.
        held[:]=original.measured16  # Simulate the actual owner's committed HOLD.
        e.recovery_hold_committed(now+10)
        assert not e.ik_hold_pending and not e.policy_enabled
        request=e.tracking_hold_recovery['request']
        q=tuple(np.deg2rad(held[7:14]))
        proof=replace(original,policy_epoch=request.policy_epoch,target16=request.target16,
            certified_right_rad=q,planner_right_position_rad=q,published16=tuple(held),
            published_monotonic_ns=now+10,observed_monotonic_ns=now+20)
        probe[0]=proof
        e.before_publication(now+20)
        assert not e.policy_enabled  # Feedback predates the actual HOLD.
        probe[0]=replace(proof,feedback_timestamps_ns=(now+15,)*16)
        e.before_publication(now+20)
        assert e.tracking_hold_recovery is None and e.policy_enabled and e.chunk is None
        assert e.minimum_observation_ns==now+20 and e.ik_failures==1
        confirmed=next(v for n,v in journal.events if n==('rtc_tracking_hold_confirmed',))
        assert confirmed['original_target_reached'] is False
        assert not any(n==('rtc_prefix_recovery_stopped',) for n,_ in journal.events)
    finally:e.close()


@pytest.mark.parametrize('fault',['moving','stale','identity','no_proof','exhausted','disabled'])
def test_tracking_fallback_requires_stationary_owner_fresh_feedback_and_remaining_budget(fault):
    e,_,probe,_,now,_,original=tracking_timeout_fixture()
    try:
        if fault=='moving':probe[0]=replace(original,planner_right_velocity_rad_s=(.1,)*7)
        if fault=='stale':probe[0]=replace(original,feedback_timestamps_ns=(0,)*16)
        if fault=='identity':probe[0]=replace(original,owner_token='wrong-owner')
        if fault=='no_proof':probe[0]=None
        if fault=='exhausted':e.ik_failures=e.ik_retry_limit
        if fault=='disabled':e.tracking_hold_recovery_enabled=False
        e.before_publication(now)
        assert e.ik_state=='paused' and e.tracking_hold_recovery is None
        assert e.ik_hold_pending and not e.policy_enabled
    finally:e.close()


def test_tracking_hold_cannot_loop_or_resume_without_confirmed_hold():
    e,held,probe,_,now,_,original=tracking_timeout_fixture()
    try:
        e.before_publication(now)
        held[:]=original.measured16;e.recovery_hold_committed(now+1)
        e.before_publication(e.tracking_hold_recovery['deadline_ns'])
        assert e.ik_state=='paused' and e.tracking_hold_recovery is None
        assert e.ik_failures==1 and not e.policy_enabled
        assert '实测保持' in e.pause_message
    finally:e.close()


@pytest.mark.parametrize('fault',['ctrl_c','camera','epoch'])
def test_tracking_hold_preserves_existing_interruption_guards(fault):
    e,_,_,stop,now,_,_=tracking_timeout_fixture()
    try:
        e.before_publication(now)
        if fault=='ctrl_c':stop[0]=True
        if fault=='camera':e._camera_fault_pending='lost'
        if fault=='epoch':e.supervisor.policy_epoch+=1
        if fault in ('ctrl_c','camera'):
            with pytest.raises((StandaloneFinished,RuntimeError)):e.tick(now+1)
        else:
            e.tick(now+1)
            assert e.ik_state=='paused' and e.tracking_hold_recovery is None
            assert not e.policy_enabled
    finally:e.close()
