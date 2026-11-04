"""RTC scheduling through real acquisition/worker threads, without robot IO."""
import time
from types import SimpleNamespace
from concurrent.futures import Future

import numpy as np
import pytest

from dagger_core.recording_sinks import NullSink
from f1dagger.rtc import RTCPolicyEngine, RTCTimingError
from test_f1_policy import Source, Journal, observation


def make_engine(delay=.01):
    journal=Journal();calls=[]
    held=np.zeros(16)
    def infer(obs, rid, epoch, context):
        calls.append(context)
        time.sleep(delay)
        actions=np.repeat(held[None],context['coverage_steps'],axis=0)
        actions[:,7]=np.arange(len(actions))*.01+rid
        committed=context['committed_actions16']
        if len(committed):
            actions[:len(committed)]=committed
        return actions,np.zeros((50,10))
    policy=SimpleNamespace(model_id='fake',last_timing={},infer_rtc=infer,held_getter=lambda:held.copy())
    engine=RTCPolicyEngine(journal.record_event,Source(),policy,journal,NullSink(),
        {'capture_hz':30.,'policy':{'chunk_steps':20,'frequency_hz':30.}},
        duration_seconds=None,stop_requested=lambda:False)
    return engine,calls,journal


def run_until(e, predicate, seconds=2):
    end=time.monotonic()+seconds
    while not predicate() and time.monotonic()<end:
        now=time.monotonic_ns();e.tick(now)
        e.sample(now,np.zeros(16));time.sleep(.004)
    assert predicate()


def test_replans_during_execution_and_preserves_time_grid():
    e,calls,journal=make_engine()
    try:
        e.authorize_local(time.monotonic_ns())
        run_until(e,lambda:e.plan_id==1)
        old=e.chunk.copy();origin=e.chunk_started_ns
        run_until(e,lambda:e.plan_id==2)
        context=calls[1];s=context['shift_steps'];d=context['delay_steps']
        assert context['base_plan_id']==1 and s==20
        assert e.chunk_started_ns==origin+round(s/30*1e9)
        np.testing.assert_array_equal(e.chunk[:d],old[s:s+d])
        assert e.real_future is None
        assert e.action_index < d  # starts at elapsed time, not restarting the prefix
        assert all(c['coverage_steps'] >= c['shift_steps']+c['delay_steps']+1 for c in calls)
    finally:e.close()


def test_pending_deadline_pauses_discards_late_result_and_requires_enter():
    e,_,_=make_engine()
    try:
        e.authorize_local(time.monotonic_ns());run_until(e,lambda:e.plan_id==1)
        now=time.monotonic_ns()
        old_epoch=e.supervisor.policy_epoch
        pending=Future();e.real_future=pending;e.real_future_epoch=old_epoch;e.real_future_request_id=2
        e.pending_context=dict(bootstrap=False,plan_origin_ns=now-600_000_000,delay_steps=15)
        e.tick(now)
        assert e.ik_state=='paused' and e.ik_hold_pending and not e.policy_enabled
        assert e.chunk is None and e.plan_id==0 and e.supervisor.policy_epoch>old_epoch
        assert not e.resume_local(now+1)  # HOLD must be committed first.
        pending.set_result((np.ones((37,16)),np.ones((50,10)),now+2))
        e.tick(now+3)
        assert e.real_future is None and e.chunk is None
        e.recovery_hold_committed(now+4)
        e.tick(now+5)
        assert e.ik_state=='paused' and not e.policy_enabled
        assert e.resume_local(time.monotonic_ns())
        e.recovery_hold_committed(time.monotonic_ns())
        run_until(e,lambda:e.chunk is not None)
        assert e.pending_context['bootstrap'] and e.pending_context['base_plan_id']==0
        assert e.ik_state=='running'
    finally:e.close()


def test_exhaustion_enters_hold_without_replaying_last_waypoint():
    e,_,_=make_engine()
    try:
        e.authorize_local(time.monotonic_ns());run_until(e,lambda:e.plan_id==1)
        now=e.chunk_started_ns+2_000_000_000
        action,source=e.sample(now,np.zeros(16))
        assert e.ik_state=='paused' and e.chunk is None and e.ik_hold_pending
        assert source!='policy'
    finally:e.close()


def test_deadline_crossed_after_target_selection_flushes_before_publish():
    from f1dagger.runtime import ControlLoop
    from test_f1_output import make_owner
    e,_,journal=make_engine()
    owner,transport=make_owner();clock=[time.monotonic_ns()]
    loop=ControlLoop(e,owner,SimpleNamespace(release=lambda:None),journal,{},clock=lambda:clock[0])
    try:
        e.authorize_local(clock[0]);loop.step(clock[0])
        run_until(e,lambda:e.plan_id==1)
        clock[0]=time.monotonic_ns()+20_000_000
        origin=clock[0]-490_000_000
        e.real_future=Future();e.real_future_epoch=e.supervisor.policy_epoch;e.real_future_request_id=2
        e.pending_context=dict(bootstrap=False,plan_origin_ns=origin,delay_steps=15)
        original=owner.approve
        def cross(*args,**kwargs):
            result=original(*args,**kwargs);clock[0]=origin+501_000_000;return result
        owner.approve=cross
        loop.step(clock[0])
        assert e.ik_state=='paused' and not e.ik_hold_pending
        assert loop.source=='rtc_timing_hold'
        np.testing.assert_array_equal(owner.last_submitted[7:14],np.zeros(7))
        assert e.chunk is None
    finally:e.close()


def test_epoch_invalidates_prefix_and_discards_old_future():
    e,_,_=make_engine()
    try:
        e.authorize_local(time.monotonic_ns());run_until(e,lambda:e.plan_id==1)
        f=Future();f.set_exception(RuntimeError('stale transport failure'))
        e.real_future=f;e.real_future_epoch=e.supervisor.policy_epoch;e.real_future_request_id=2
        e.pending_context=dict(bootstrap=False,plan_origin_ns=0,delay_steps=1)
        e.supervisor.policy_epoch+=1;e.invalidate(time.monotonic_ns())
        e.tick(time.monotonic_ns())
        assert e.plan_id==0 and e.chunk is None and e.absolute_eef is None
    finally:e.close()


def test_measured_delay_can_increase_spacing_and_use_exact_remaining_prefix():
    e,_,_=make_engine()
    try:
        e.authorize_local(time.monotonic_ns());run_until(e,lambda:e.plan_id==1)
        now=time.monotonic_ns()
        e.chunk=np.zeros((42,16));e.absolute_eef=np.zeros((50,10))
        e.chunk_started_ns=now-700_000_000
        e.delays.append(21)
        with e.latest_lock:
            e.latest=(observation(e.last_requested.observation_id+1,now-100),e.supervisor.policy_epoch)
        e.tick(now)
        assert e.pending_context['shift_steps']==21
        assert e.pending_context['delay_steps']==21
        assert len(e.pending_context['committed_actions16'])==21
        assert e.pending_context['coverage_steps']==44
    finally:e.close()
