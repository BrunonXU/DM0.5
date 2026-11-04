"""RLT RTC scheduling/cancellation tests; synthetic inputs, no robot output."""
import time
from concurrent.futures import Future

import numpy as np

from dagger_core.hil_supervisor import HILState
from test_f1_rlt_hil import make_engine, switch, receipt
from test_f1_policy import observation


def rtc_engine():
    e, journal, _, calls = make_engine()
    e.rlt_rtc = True
    e.rlt_policy.held_getter = e.base_policy.held_getter
    def infer(obs,rid,epoch,context):
        calls.append(context)
        actions = np.repeat(e.base_policy.held_getter()[None],16,0)
        actions[:,7] = np.arange(16)*.001
        committed=context['committed_actions16']
        if committed:actions[:len(committed)] = committed
        return actions,np.zeros((16,10))
    e.rlt_policy.infer_rtc=infer
    return e,journal,calls


def test_rlt_continuously_prefetches_and_keeps_one_fixed_actor_version():
    e,journal,calls=rtc_engine()
    try:
        switch(e)
        e.rlt_hold_committed(receipt(),powered=True)
        deadline=time.monotonic()+1.3
        samples=[]
        oid=800
        while time.monotonic()<deadline:
            now=time.monotonic_ns();e.last_received_ns=now
            oid+=1;e.latest=(observation(oid,now-1000),e.supervisor.policy_epoch)
            e.tick(now)
            value,source=e.sample(now,e.base_policy.held_getter())
            if e.plan_id:samples.append(source)
            time.sleep(.005)
        assert len(calls)>=4 and calls[0]['bootstrap']
        assert all(not c['bootstrap'] and len(c['committed_actions16'])>=6 for c in calls[1:])
        assert set(samples)=={'policy'}
        assert not e.rlt_final_pending and not e.rlt_hold_pending
        assert e.supervisor.state is HILState.POLICY
        assert e.local_task.actor_version == 'head-sha'
    finally:e.close()


def test_rlt_late_reply_cannot_override_handoff_and_base_timing_is_restored():
    e,journal,calls=rtc_engine()
    try:
        switch(e)
        old_epoch=e.supervisor.policy_epoch
        e.real_future=Future();e.real_future_epoch=old_epoch;e.real_future_request_id=123
        e._return_base(time.monotonic_ns())
        e.real_future.set_result(('deliberately-invalid-stale-result',))
        e.tick(time.monotonic_ns())
        assert e.real_future is None and e.chunk is None
        assert e.rtc_horizon==50 and (e.minimum_delay_steps,e.s_min)==(14,16)
        assert e.local_task.status=='active'  # A caller adjudicates the task separately.
    finally:e.close()


def test_rlt_deadline_replans_only_after_measured_hold_without_ending_episode():
    e,journal,calls=rtc_engine()
    try:
        switch(e)
        e.rtc_timing_replan=True
        e.supervisor.state=HILState.POLICY
        e.policy_enabled=True
        now=time.monotonic_ns();e.last_received_ns=now
        epoch=e.supervisor.policy_epoch
        e.pause_timing(now,'inference_deadline')
        assert e.supervisor.policy_epoch>epoch
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        assert not e.policy_enabled and e.ik_hold_pending
        assert e.local_task.status=='active' and e.episodes.phase=='recording'
        e.recovery_hold_committed(now+1)
        assert e.policy_enabled and not e.ik_hold_pending
        assert e.minimum_observation_ns==now+1 and e.chunk is None
        assert e.rlt_phase=='actor' and e.local_task.actor_version=='head-sha'
    finally:e.close()


def test_rlt_prefetch_budget_includes_elapsed_observation_grid_and_full_pipeline():
    e,journal,calls=rtc_engine()
    try:
        switch(e)
        e.chunk=np.zeros((16,16));e.plan_id=37
        e.chunk_started_ns=1_000_000_000
        origin=e.chunk_started_ns+round(8/30*1e9)
        e.rlt_request_durations_ns.append(160_000_000)
        # The request starts 67ms after its genuine observation-grid origin.
        # D6 leaves only133ms; reserve8 known waypoints without moving origin.
        assert e._rlt_prefetch_delay(origin+67_000_000,origin,8)==8
        assert e.chunk_started_ns==1_000_000_000
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        assert not calls
    finally:e.close()


def test_observed_slow_request_survives_epoch_revocation_and_defers_infeasible_prefetch():
    e,journal,calls=rtc_engine()
    try:
        switch(e)
        now=time.monotonic_ns();epoch=e.supervisor.policy_epoch
        future=Future();future.set_result((None,None,now))
        e._rtc_future_timing=dict(future=future,request_id=38,epoch=epoch,
            requested_monotonic_ns=now-294_000_000,completed_monotonic_ns=now,
            observed=False,rlt=True)
        e.invalidate(now);e.supervisor.policy_epoch+=1
        e._observe_rlt_request_duration(now)
        assert list(e.rlt_request_durations_ns)==[294_000_000]
        e._observe_rlt_request_duration(now+100)
        assert len(e.rlt_request_durations_ns)==1
        assert journal.events[-1][1]['revoked_epoch'] is True
        e.chunk=np.zeros((16,16));e.chunk_started_ns=now;e.plan_id=39
        origin=now+round(8/30*1e9)
        assert e._rlt_prefetch_delay(origin+90_000_000,origin,8) is None
        assert e.chunk is not None and e.plan_id==39  # No premature cancellation.
        event=journal.events[-1]
        assert event[0]==('rtc_prefetch_deferred',)
        assert event[1]['available_delay_steps']==8
        assert event[1]['valid_plan_until_ns']==now+round(16/30*1e9)
        before=len(journal.events)
        assert e._rlt_prefetch_delay(origin+95_000_000,origin,8) is None
        assert len(journal.events)==before  # No per-tick error storm.
    finally:e.close()


def test_rlt_infeasible_budget_does_not_submit_guided_request_or_extend_plan():
    e,journal,calls=rtc_engine()
    try:
        switch(e);e.rtc_timing_replan=True
        e.rlt_hold_committed(receipt(),powered=True)
        now=time.monotonic_ns()
        e.supervisor.state=HILState.POLICY;e.policy_enabled=True
        e.chunk=np.zeros((16,16));e.absolute_eef=np.zeros((16,10))
        e.chunk_started_ns=now-400_000_000;e.plan_id=e.chunk_request_id=37
        e.minimum_observation_ns=0
        e.rlt_request_durations_ns.append(294_000_000)
        e.last_received_ns=now
        e.latest=(observation(900,now-80_000_000),e.supervisor.policy_epoch)
        epoch=e.supervisor.policy_epoch;origin=e.chunk_started_ns
        e.tick(now)
        assert e.real_future is None and not calls
        assert e.chunk_started_ns==origin and e.supervisor.policy_epoch==epoch
        assert e.supervisor.state is HILState.POLICY and e.policy_enabled
        # Original expiry remains binding; a late reply is never allowed to
        # extend this plan, and a new observation requires a measured HOLD.
        expired=origin+round(16/30*1e9)+1;e.last_received_ns=expired
        e.before_publication(expired)
        assert e.chunk is None and e.supervisor.policy_epoch>epoch
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        assert e.ik_hold_pending and not e.policy_enabled
        event=next(value for name,value in reversed(journal.events) if name==('rtc_timing_replan',))
        assert event['rlt_phase']=='actor' and event['reason']=='plan_exhausted'
        assert list(e.rlt_request_durations_ns)==[294_000_000]
    finally:e.close()
