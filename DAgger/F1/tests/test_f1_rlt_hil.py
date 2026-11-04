"""C16 real control-path tests; synthetic observations/operator are explicit."""
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
import time

import numpy as np
import pytest

from dagger_core.hil_supervisor import HILState
from dagger_core.recording_sinks import NullSink
from f1dagger.rlt_hil import RLTHILPolicyEngine
from test_f1_policy import Journal, Source, observation
from test_real_observation_shadow import packet


def episode_packet(seq, toggle):
    return replace(packet(seq, toggle), episode_end_id=0, episode_end_pressed=False, policy_switch_id=0, policy_switch_pressed=False, operator_protocol='f1_rlt_local_v4', local_result='success', home_end_id=0)


def make_engine():
    journal = Journal()
    held = np.r_[np.zeros(14), 32., 66.]
    base_calls, actor_calls = [], []
    def base_infer(obs, rid, epoch, context):
        base_calls.append((obs, rid, epoch, context))
        return np.repeat(held[None], context['coverage_steps'], 0), np.zeros((50, 10))
    def actor_infer(obs, rid, epoch):
        actor_calls.append((obs, rid, epoch))
        actions = np.repeat(held[None], 16, 0)
        actions[:, 7] = np.linspace(0, .5, 16)
        return actions
    base = SimpleNamespace(model_id='B', model_version='base-v', last_timing={},
        infer_rtc=base_infer, held_getter=lambda: held.copy(), right_only=True)
    actor = SimpleNamespace(model_id='B+actor', model_version='composite-v', last_timing={},
        infer=actor_infer, config={'shadow_only': False},
        rlt_identity={'actor_version':'head-sha'}, right_only=True)
    episodes = SimpleNamespace(unified_buttons=True, phase='recording', run_id='synthetic', epoch_floor=0, finish=lambda *a, **k: None)
    e = RLTHILPolicyEngine(journal.record_event, Source(), base, journal, NullSink(),
        {'capture_hz':30., 'policy':{'chunk_steps':20, 'frequency_hz':30.}},
        episodes=episodes, duration_seconds=None, stop_requested=lambda:False,
        rtc_delay_steps=14, rtc_min_steps=16, rlt_policy=actor)
    e.stop_event.set(); e.capture_thread.join(1)
    e.started = e.policy_enabled = True
    e.supervisor.state = HILState.POLICY
    e.last_received_ns = time.monotonic_ns()
    return e, journal, base_calls, actor_calls


def receipt(now=None, complete=True, mode='powered'):
    return dict(completed_monotonic_ns=now or time.monotonic_ns(), publication_complete=complete,
                execution_mode=mode, command_sequence=9)


def switch(e):
    now = time.monotonic_ns()
    e.last_received_ns = now
    assert e.enter_actor(now)
    return now


def wait_ready(e):
    for _ in range(200):
        now = time.monotonic_ns(); e.last_received_ns = now
        e.tick(now)
        if e.ready is not None:
            return
        time.sleep(.001)
    pytest.fail('actor not ready')


@pytest.mark.parametrize('condition', ['not_started','not_recording','human','stale_operator','already_actor'])
def test_b_switch_rejected_without_queued_authority(condition):
    e, j, _, calls = make_engine()
    try:
        if condition == 'not_started': e.started = False
        if condition == 'not_recording': e.episodes.phase = 'saving'
        if condition == 'human': e.supervisor.state = HILState.HIL_LATCHED
        if condition == 'stale_operator': e.last_received_ns -= e.watchdog_ns + 1
        if condition == 'already_actor': switch(e)
        assert not e.enter_actor(time.monotonic_ns())
        assert not calls
        assert j.events[-1][1]['queued'] is False
    finally: e.close()


def test_switch_requires_successful_hold_publication_then_all_fresh_inputs():
    e, j, _, calls = make_engine()
    try:
        old_epoch = e.supervisor.policy_epoch
        boundary = switch(e)
        assert e.supervisor.policy_epoch > old_epoch and e.rlt_hold_pending
        e.latest = (observation(500, time.monotonic_ns()), e.supervisor.policy_epoch)
        e.tick(time.monotonic_ns())
        assert e.real_future is None and not calls
        assert not e.rlt_hold_committed(receipt(complete=False), powered=True)
        assert not e.rlt_hold_committed(receipt(mode='shadow'), powered=True)
        held = time.monotonic_ns()
        assert e.rlt_hold_committed(receipt(held), powered=True)
        assert held > boundary and e.minimum_observation_ns == held
        for bad in ('image', 'state'):
            obs = observation(501, time.monotonic_ns())
            if bad == 'image': obs = observation(501, held)
            else: obs = replace(obs, state_timestamps_ns=(held,)*16)
            e.latest = (obs, e.supervisor.policy_epoch)
            e.tick(time.monotonic_ns())
            assert e.real_future is None and not calls
        e.latest = (observation(502, time.monotonic_ns()), e.supervisor.policy_epoch)
        wait_ready(e)
        assert len(calls) == 1 and calls[0][0].capture_started_monotonic_ns > held
        assert e.supervisor.state is HILState.POLICY
    finally: e.close()


@pytest.mark.parametrize('failed', [False, True])
def test_late_B_reply_at_phase_switch_is_discarded_without_unpacking_as_actor(failed):
    e, j, _, _ = make_engine()
    try:
        old = e.supervisor.policy_epoch
        f = Future(); e.real_future = f; e.real_future_epoch = old; e.real_future_request_id = 777
        e.pending_context = {'bootstrap':False, 'delay_steps':14, 'plan_origin_ns':0}
        switch(e)
        if failed: f.set_exception(RuntimeError('late B exception'))
        else: f.set_result((np.ones((32,16)), np.ones((50,10)), time.monotonic_ns()))
        e.tick(time.monotonic_ns())
        assert e.real_future is None and e.ready is None and e.chunk is None
        assert e.rlt_hold_pending and e.supervisor.state is HILState.HANDOFF_HOLD
        assert any(name == ('stale_policy_result_discarded',) for name,_ in j.events)
    finally: e.close()


@pytest.mark.parametrize('failed', [False, True])
def test_START_revokes_actor_hold_and_inflight_result_even_on_late_error(failed):
    e, j, _, calls = make_engine()
    try:
        switch(e)
        old = e.supervisor.policy_epoch
        f = Future(); e.real_future = f; e.real_future_epoch = old; e.real_future_request_id = 900
        now = time.monotonic_ns()
        e.operator(episode_packet(0,0), now, now)
        e.operator(replace(episode_packet(1,1), hil_button_pressed=True), now+1, now+1)
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert not e.rlt_hold_pending
        assert not e.rlt_hold_committed(receipt(), powered=True)
        if failed: f.set_exception(RuntimeError('late actor exception'))
        else: f.set_result((old,900,1,time.monotonic_ns(),np.ones((16,16))))
        e.tick(time.monotonic_ns())
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert not calls and e.chunk is None and e.ready is None
        assert e.control_sampling()['phase'] == 'human'
    finally: e.close()


def test_C16_final_point_published_before_hold_and_next_observation():
    e, j, _, calls = make_engine()
    try:
        switch(e); assert e.rlt_hold_committed(receipt(), powered=True)
        e.latest = (observation(1001,time.monotonic_ns()), e.supervisor.policy_epoch)
        wait_ready(e)
        start = time.monotonic_ns()
        e.sample(start, np.zeros(16))
        assert e.control_sampling()['adopted_monotonic_ns'] == start
        end = start + 533_333_334
        final, source = e.sample(end, np.zeros(16))
        assert source == 'policy' and final[7] == .5
        assert e.rlt_final_pending and not e.rlt_hold_pending
        e.after_publication(receipt(end, complete=False), powered=True)
        assert not e.rlt_hold_pending
        e.after_publication(receipt(end+1), powered=True)
        assert e.rlt_final_pending and not e.rlt_hold_pending
        # A late trajectory remains the same target and the same chunk identity.
        continued, source = e.sample(end+500_000_000, np.zeros(16))
        np.testing.assert_array_equal(continued, final)
        assert source == 'policy' and e.control_sampling()['upper_index'] == 15
        completed = dict(receipt(end+600_000_000), rlt_endpoint_tracking={'complete':True})
        e.after_publication(completed, powered=True)
        assert e.rlt_hold_pending and e.supervisor.state is HILState.HANDOFF_HOLD
        assert e.rlt_hold_completed_target
        assert e.control_sampling()['execution_completed_monotonic_ns'] == end+600_000_000
        assert e.rlt_hold_committed(receipt(end+600_000_001), powered=True)
        assert e.minimum_observation_ns == end+600_000_001
        assert len(calls) == 1
        assert len([1 for name,_ in j.events if name == ('rlt_execution_completed',)]) == 1
    finally: e.close()


def test_START_cancels_finishing_endpoint_without_replaying_it():
    e, j, _, _ = make_engine()
    try:
        switch(e); e.rlt_hold_committed(receipt(), powered=True)
        e.latest = (observation(1001,time.monotonic_ns()), e.supervisor.policy_epoch)
        wait_ready(e)
        start = time.monotonic_ns(); e.sample(start, np.zeros(16))
        end = start+533_333_334; e.sample(end, np.zeros(16))
        assert e.rlt_final_pending
        e.operator(episode_packet(0,0), end+1, end+1)
        e.operator(replace(episode_packet(1,1),hil_button_pressed=True), end+2, end+2)
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert not e.rlt_final_pending and e.rlt_final_target is None
        e.after_publication(dict(receipt(end+3),rlt_endpoint_tracking={'complete':True}),powered=True)
        assert not e.rlt_hold_pending
    finally: e.close()


def test_endpoint_feedback_timeout_is_recoverable_and_does_not_close_local_task():
    e, j, _, _ = make_engine()
    try:
        e.fault_lifecycle = True
        switch(e); e.rlt_hold_committed(receipt(), powered=True)
        e.latest = (observation(1001,time.monotonic_ns()), e.supervisor.policy_epoch)
        wait_ready(e)
        start = time.monotonic_ns(); e.sample(start, np.zeros(16))
        end = start+533_333_334; e.sample(end, np.zeros(16))
        evidence = dict(receipt(end+3_000_000_001),
                        rlt_endpoint_tracking={'complete':False,'settle_timeout_s':3.})
        e.after_publication(evidence,powered=True)
        assert e.local_task.status == 'active'
        assert not e.rlt_final_pending and e.rlt_final_target is None
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert 'rlt_endpoint_tracking_timeout' in e.hard_hold_reason
        assert not any(name == ('rlt_execution_completed',) for name, _ in j.events)
    finally: e.close()


def test_control_owner_commits_measured_hold_before_actor_request():
    from f1dagger.runtime import ControlLoop
    from test_f1_output import make_owner
    e, j, _, calls = make_engine()
    owner, transport = make_owner()
    owner.prepare()
    records = []; j.control = lambda **kw: records.append(kw) or True
    arm = SimpleNamespace(release=lambda:None, target=lambda p,f,last,dt:last.copy())
    clock = [time.monotonic_ns()]
    loop = ControlLoop(e,owner,arm,j,{},clock=lambda:clock[0])
    try:
        loop.started_output = True
        owner.last_submitted[7] = 12.
        owner.selected[15] = 0.
        switch(e)
        now = time.monotonic_ns(); clock[0] = now
        e.last_received_ns = now
        loop.step(now)
        assert not e.rlt_hold_pending and e.rlt_hold_committed_ns is not None
        assert owner.last_submitted[7] == 0. and owner.last_submitted[15] == 66.
        assert records[-1]['source'] == 'rlt_boundary_hold'
        assert records[-1]['publication_complete'] is True
        assert records[-1]['policy_sampling']['phase'] == 'actor'
        assert len(transport.sent) > 0 and not calls
    finally: e.close()


def test_saved_episode_next_start_resets_phase_but_never_replays_old_actor():
    e, j, _, _ = make_engine()
    try:
        switch(e)
        e.episode_buttons.reset_start(time.monotonic_ns())
        e.episodes.phase = 'idle'
        now = time.monotonic_ns()
        e.operator(episode_packet(0,0), now, now)
        assert e.rlt_phase == 'base' and e.policy is e.base_policy
        assert e.chunk_steps == 20 and not e.rlt_hold_pending
        assert e.chunk is None and e.ready is None
    finally: e.close()


def test_b1250_evaluation_right_starts_local_without_selecting_actor():
    e, journal, _, actor_calls = make_engine()
    try:
        e.evaluation = dict(strategy='b1250', attempt_id='pilot-01')
        e.base_policy.model_version = 'base-fixed'
        send_operator(e, 0)
        assert e.enter_actor(time.monotonic_ns())
        assert e.rlt_phase == 'base' and e.policy is e.base_policy
        assert e.local_task.status == 'active' and not actor_calls
        assert not e.enter_actor(time.monotonic_ns())
    finally: e.close()


def test_rlt_cli_requires_hil_episode_contract(monkeypatch):
    import sys
    from f1dagger.cli import arguments
    for argv in (['run','--rlt-hil'], ['sync','--rlt-hil','--episode-controls'],
                 ['run','--rlt-hil','--rtc-hil','--episode-controls']):
        monkeypatch.setattr(sys,'argv',['f1dagger',*argv])
        with pytest.raises(SystemExit):arguments()
    monkeypatch.setattr(sys,'argv',['f1dagger','run','--rlt-hil','--episode-controls','--continuous',
                                   '--rtc-delay-steps','14','--rtc-min-steps','16'])
    args=arguments()
    assert args.rlt_hil and args.continuous and not args.rtc_hil


def send_operator(e, seq, toggle=0, switch_id=0, **kw):
    now = time.monotonic_ns()
    p = replace(episode_packet(seq, toggle), policy_switch_id=switch_id, **kw)
    return e.operator(p, now, now)


def test_gamepad_right_switch_is_once_and_replayed_packets_have_no_authority():
    e, j, _, calls = make_engine()
    try:
        send_operator(e, 0)
        send_operator(e, 1, switch_id=1, policy_switch_pressed=True)
        assert e.rlt_phase == 'actor' and e.rlt_hold_pending
        epoch = e.supervisor.policy_epoch
        send_operator(e, 1, switch_id=1, policy_switch_pressed=True)
        send_operator(e, 2, switch_id=1, policy_switch_pressed=True)
        assert e.supervisor.policy_epoch == epoch and not calls
        assert sum(name == ('rlt_switch_accepted',) for name, _ in j.events) == 1
    finally:
        e.close()


@pytest.mark.parametrize('phase', ['base', 'actor'])
def test_START_takeover_then_return_to_base_in_same_recording(phase):
    e, j, _, calls = make_engine()
    try:
        if phase == 'actor': switch(e)
        send_operator(e, 0)
        send_operator(e, 1, 1, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
        send_operator(e, 2, 1)
        if phase == 'actor': e.local_failure(time.monotonic_ns())
        old_epoch = e.supervisor.policy_epoch
        send_operator(e, 3, 2, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        assert e.supervisor.policy_epoch > old_epoch
        assert e.rlt_phase == 'base' and e.episodes.run_id == 'synthetic'
        if phase == 'actor':
            assert e.local_task.status == 'failure'
            assert not e.rlt_hold_pending and not calls
            assert not e.enter_actor(time.monotonic_ns())
    finally:
        e.close()


@pytest.mark.parametrize('control', ['start', 'rb', 'hand'])
def test_switch_during_conflicting_control_is_consumed_not_queued(control):
    e, j, _, _ = make_engine()
    try:
        send_operator(e, 0)
        now = time.monotonic_ns()
        p = replace(episode_packet(1, int(control == 'start')), policy_switch_id=1,
                    policy_switch_pressed=True, deadman=control == 'rb',
                    hil_button_pressed=control == 'start')
        e.operator(p, now, now, hand_pending=control == 'hand')
        assert e.rlt_phase == 'base'
        send_operator(e, 2, int(control == 'start'), switch_id=1)
        assert e.rlt_phase == 'base'
    finally:
        e.close()


@pytest.mark.parametrize('bad_counter', [-1, 2])
def test_switch_counter_gap_or_regression_revokes_policy(bad_counter):
    e, j, _, _ = make_engine()
    try:
        send_operator(e, 0, switch_id=1)
        send_operator(e, 1, switch_id=1 + bad_counter)
        assert e.supervisor.state is HILState.SAFE_HOLD and e.rlt_phase == 'base'
    finally:
        e.close()


def test_legacy_sender_cannot_start_RLT_and_new_session_counter_is_only_anchor():
    e, j, _, _ = make_engine()
    try:
        e.episode_buttons.reset_start(time.monotonic_ns())
        now = time.monotonic_ns()
        p = replace(episode_packet(0, 0), policy_switch_id=None, policy_switch_pressed=None, operator_protocol=None, home_end_id=None, local_result=None)
        assert not e.operator(p, now, now)
        assert not e.started and e.supervisor.hold_reason == 'rlt_sender_update_required'
        assert not send_operator(e, 1, switch_id=7)
        assert e.rlt_phase == 'base' and not e.started
        assert e.supervisor.hold_reason == 'waiting_for_start'
    finally:
        e.close()

@pytest.mark.parametrize('phase,started,expected', [('idle', False, 'rlt_idle_input_wait'), ('recording', True, 'safe_hold_entered')])
def test_idle_watchdog_keeps_stop_but_does_not_claim_saved_episode_interrupted(phase, started, expected):
    e, journal, _, _ = make_engine()
    try:
        e.episodes.phase = phase
        e.started = started
        e.disconnect(time.monotonic_ns(), 'operator_watchdog_before_publication')
        events = [name[0] for name, _ in journal.events]
        assert expected in events
        assert e.supervisor.state == HILState.SAFE_HOLD
        if not started:
            assert 'safe_hold_entered' not in events
            assert 'rlt_local_closed' not in events
    finally:
        e.close()
