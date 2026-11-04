"""Operator golden paths with the real engine and synthetic hardware inputs."""
import time
from concurrent.futures import Future
from dataclasses import replace

import pytest

from dagger_core.hil_supervisor import HILState
from test_f1_rlt_hil import make_engine, switch, send_operator, episode_packet
from test_f1_policy import observation


def test_y_revokes_pending_vla_timing_replan_without_starting_new_inference():
    e, journal, base_calls, _ = make_engine()
    e.rtc_timing_replan = True
    try:
        e.pause_timing(time.monotonic_ns(), 'inference_deadline')
        assert e.timing_replan_epoch is not None
        assert e._request_episode_finish(episode_packet(0, 0), time.monotonic_ns(), False)
        e.recovery_hold_committed(time.monotonic_ns())
        assert e.ending_episode and e.home_request
        assert e.timing_replan_epoch is None and not e.policy_enabled
        e.tick(time.monotonic_ns())
        assert not base_calls
    finally:
        e.close()


@pytest.mark.parametrize('boundary', ['y', 'return_vla'])
@pytest.mark.parametrize('failed', [False, True])
def test_pending_request_survives_revocation_until_result_is_discarded(boundary, failed):
    e, journal, base_calls, _ = make_engine()
    pending = Future()
    try:
        now = time.monotonic_ns()
        e.real_future = pending
        e.real_future_epoch = e.supervisor.policy_epoch
        e.real_future_request_id = 900
        e.pending_context = dict(bootstrap=True)
        if boundary == 'y':
            assert e._request_episode_finish(episode_packet(0, 0), now, False)
        else:
            e._return_base(now)
        assert e.pending_context is None
        e.tick(now + 1)
        assert e.real_future is pending and not base_calls and e.chunk is None
        if failed:
            pending.set_exception(RuntimeError('late network reply'))
        else:
            pending.set_result(('revoked result must never be unpacked',))
        e.tick(now + 2)
        assert e.real_future is None and e.chunk is None and not base_calls
        discarded = [data for (name,), data in journal.events if name == 'stale_policy_result_discarded']
        assert discarded[-1]['request_id'] == 900
        assert discarded[-1]['reason'] == 'authority_revoked'
    finally:
        if not pending.done():pending.cancel()
        e.close()


def test_autonomous_success_cuts_local_and_returns_fresh_vla_once():
    e, journal, _, _ = make_engine()
    try:
        switch(e)
        e.latest = (observation(99, time.monotonic_ns()), e.supervisor.policy_epoch)
        send_operator(e, 0)
        send_operator(e, 1, episode_end_id=1, episode_end_pressed=True)
        assert e.local_task.status == 'success' and not e.local_task.assisted
        assert e.rlt_phase == 'base' and e.supervisor.state is HILState.HANDOFF_HOLD
        assert e.episodes.phase == 'recording' and e.started
        send_operator(e, 2, episode_end_id=1)
        send_operator(e, 3, episode_end_id=2, episode_end_pressed=True)
        closed = [data for name, data in journal.events if name == ('rlt_local_closed',)]
        assert len(closed) == 1 and closed[0]['reward'] == 1
        assert closed[0]['terminal_observation']['observation_id'] == 99
        assert closed[0]['physical_reset'] is False
        assert not e.enter_actor(time.monotonic_ns())
    finally:
        e.close()


def test_human_success_stays_human_then_multiple_dagger_takeovers():
    e, journal, _, _ = make_engine()
    try:
        switch(e)
        send_operator(e, 0)
        send_operator(e, 1, 1, hil_button_pressed=True)
        send_operator(e, 2, 1)
        send_operator(e, 3, 1, episode_end_id=1, episode_end_pressed=True)
        assert e.local_task.status == 'success' and e.local_task.assisted
        assert e.supervisor.state is HILState.HIL_LATCHED
        send_operator(e, 4, 1, episode_end_id=1)
        send_operator(e, 5, 2, episode_end_id=1, hil_button_pressed=True)
        assert e.rlt_phase == 'base' and e.local_task.status == 'success'
        send_operator(e, 6, 2, episode_end_id=1)
        send_operator(e, 7, 3, episode_end_id=1, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert e.supervisor.intervention_id == 2
        assert e.control_sampling()['task_phase'] == 'post_placement'
        assert e.episodes.phase == 'recording'
    finally:
        e.close()


def test_y_after_explicit_failure_waits_two_seconds_to_seal():
    e, journal, _, _ = make_engine()
    finished = []
    e.episodes.finish = lambda *args, **kw: finished.append((args, kw))
    try:
        switch(e)
        send_operator(e, 0)
        e.local_failure(time.monotonic_ns())
        send_operator(e, 1, home_end_id=1, home_reset=True)
        assert e.local_task.status == 'failure' and e.ending_episode and e.home_request
        result = next(d for (n,), d in journal.events if n == 'rlt_local_closed')
        assert result['reward'] == 0 and result['terminated'] and result['reward_known']
        assert not finished
        now = time.monotonic_ns()
        e.episode_buttons.hold_since = now
        e.poll_episode_finish(now + 1_999_999_999)
        assert not finished
        e.poll_episode_finish(now + 2_000_000_000)
        assert len(finished) == 1 and finished[0][1]['reason'] == 'y_home_stable'
        assert not e.started and e.rlt_phase == 'base' and e.local_task.status == 'unused'
    finally:
        e.close()


@pytest.mark.parametrize('condition,reason', [
    ('idle', 'not_recording'), ('saving', 'episode_saving'),
    ('starting', 'episode_starting'), ('protected', 'protection_active'),
    ('paused', 'manual_pause'), ('deadman', 'release_rb'),
    ('start', 'release_start'), ('success', 'release_success'),
    ('switch', 'release_switch'), ('gripper', 'wait_for_gripper'),
    ('conflict', 'conflicting_controls'),
])
def test_y_rejection_explains_gate_without_closing_local_or_authorizing_home(condition, reason):
    e, journal, _, _ = make_engine()
    try:
        switch(e)
        p = episode_packet(0, 0)
        if condition in ('idle', 'saving', 'starting'):
            e.episodes.phase = condition
        elif condition == 'protected': e.hard_hold_reason = 'target_rejected:collision'
        elif condition == 'paused': e.input_pause = {'reason':'manual_candidate_collision'}
        elif condition == 'deadman': p = replace(p, deadman=True)
        elif condition == 'start': p = replace(p, hil_button_pressed=True)
        elif condition == 'success': p = replace(p, episode_end_pressed=True)
        elif condition == 'switch': p = replace(p, policy_switch_pressed=True)
        epoch, state = e.supervisor.policy_epoch, e.supervisor.state
        assert not e._request_episode_finish(p, time.monotonic_ns(), condition == 'gripper',
                                            control_boundary=condition != 'conflict')
        assert e.local_task.status == 'active'
        assert not e.home_request and not e.ending_episode
        assert (e.supervisor.policy_epoch, e.supervisor.state) == (epoch, state)
        name, event = journal.events[-1]
        assert name == ('rlt_episode_finish_rejected',)
        assert event['reason'] == reason and event['queued'] is False
    finally:
        e.close()


def test_y_after_protection_sealed_episode_is_reported_once_without_new_start():
    e, journal, _, _ = make_engine()
    try:
        send_operator(e, 0)
        e.hard_hold_reason = 'target_rejected:collision'
        e.episodes.phase = 'idle'
        e.episode_buttons.reset_start(time.monotonic_ns())
        send_operator(e, 1, home_end_id=1, home_reset=True)
        send_operator(e, 1, home_end_id=1, home_reset=True)  # Retransmission.
        send_operator(e, 2, home_end_id=1, home_reset=True)  # Held button.
        rejected = [event for name, event in journal.events if name == ('rlt_episode_finish_rejected',)]
        assert len(rejected) == 1
        assert rejected[0]['reason'] == 'protection_active'
        assert rejected[0]['episode_phase'] == 'idle'
        assert rejected[0]['protection_reason'] == 'target_rejected:collision'
        assert not e.started and not e.home_request and not e.ending_episode
        assert e.hard_hold_reason == 'target_rejected:collision'
    finally:
        e.close()


@pytest.mark.parametrize('fault', ['rtc_inference_deadline', 'ik_rejected:no_solution', 'policy_failed:timeout'])
def test_y_after_policy_fault_requires_explicit_start_takeover_before_home(fault):
    e, journal, _, _ = make_engine()
    try:
        send_operator(e, 0)
        e.disconnect(time.monotonic_ns(), fault)
        epoch, state = e.supervisor.policy_epoch, e.supervisor.state
        assert state is HILState.SAFE_HOLD and e.episodes.phase == 'recording'
        send_operator(e, 1, home_end_id=1, home_reset=True)
        rejected = [event for name, event in journal.events if name == ('rlt_episode_finish_rejected',)]
        assert rejected[-1]['reason'] == 'policy_recovery_required'
        assert rejected[-1]['protection_reason'] == fault
        assert e.hard_hold_reason == fault
        assert (e.supervisor.policy_epoch, e.supervisor.state) == (epoch, state)
        assert not e.home_request and not e.ending_episode
        assert e.episodes.phase == 'recording'

        send_operator(e, 2, home_end_id=1)
        send_operator(e, 3, 1, home_end_id=1, hil_button_pressed=True)
        assert e.supervisor.state is HILState.HIL_LATCHED and e.hard_hold_reason is None
        assert not e.home_request and not e.ending_episode
        send_operator(e, 4, 1, home_end_id=1)
        send_operator(e, 5, 1, home_end_id=2, home_reset=True)
        assert e.home_request and e.ending_episode
    finally:
        e.close()


@pytest.mark.parametrize('fault,phase,collision_latched', [
    ('feedback_stale', 'recording', False),
    ('target_rejected:collision', 'recording', False),
    ('rtc_inference_deadline', 'idle', False),
    ('rtc_inference_deadline', 'recording', True),
])
def test_y_does_not_offer_policy_recovery_for_hard_or_inactive_faults(fault, phase, collision_latched):
    e, journal, _, _ = make_engine()
    try:
        e.hard_hold_reason = fault
        e.episodes.phase = phase
        e.collision_restart_required = collision_latched
        assert not e._request_episode_finish(episode_packet(0, 0), time.monotonic_ns(), False)
        assert journal.events[-1][1]['reason'] == 'protection_active'
        assert e.hard_hold_reason == fault and e.collision_restart_required == collision_latched
        assert not e.home_request and not e.ending_episode
    finally:
        e.close()


def test_y_with_same_packet_start_has_explicit_rejection_and_keeps_takeover():
    e, journal, _, _ = make_engine()
    try:
        switch(e)
        send_operator(e, 0)
        send_operator(e, 1, 1, hil_button_pressed=True, home_end_id=1, home_reset=True)
        rejected = [event for name, event in journal.events if name == ('rlt_episode_finish_rejected',)]
        assert rejected[-1]['reason'] == 'release_start'
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert e.local_task.status == 'active' and not e.home_request
    finally:
        e.close()


def test_collision_latch_blocks_new_episodes_after_repeated_start_held_and_reconnect():
    e, journal, base_calls, actor_calls = make_engine()
    starts = []
    e.episodes.start = lambda now: starts.append(now) or True
    e.episodes.finish = lambda *args, **kw: setattr(e.episodes, 'phase', 'idle')
    reason = 'target_rejected:F1 collision/CoM rejected target: Arm-body collision risk'
    try:
        switch(e)
        send_operator(e, 0)
        e.disconnect(time.monotonic_ns(), reason)
        e.episode_buttons.poll(time.monotonic_ns())
        assert e.collision_restart_required and not e.started
        assert e.episodes.phase == 'idle'
        seq = 1
        for toggle in (1, 2, 3):
            send_operator(e, seq, toggle-1)
            send_operator(e, seq+1, toggle, hil_button_pressed=True)
            send_operator(e, seq+2, toggle, hil_button_pressed=True)
            seq += 3
            assert not e.started and not e.policy_enabled and not e.home_request
            assert e.hard_hold_reason == reason and e.collision_restart_required
        # A newly connected sender cannot clear the process-owned latch.
        send_operator(e, 0, 50, session_id='replacement', hil_button_pressed=True)
        send_operator(e, 1, 50, session_id='replacement', hil_button_pressed=True)
        send_operator(e, 2, 50, session_id='replacement')
        send_operator(e, 3, 51, session_id='replacement', hil_button_pressed=True)
        send_operator(e, 4, 51, session_id='replacement', home_end_id=1, home_reset=True)
        assert not e.started and not e.policy_enabled and not e.home_request
        assert e.collision_restart_required and e.hard_hold_reason == reason
        assert not starts and not base_calls and not actor_calls
        rejected = [event for name, event in journal.events if name == ('episode_start_rejected',)]
        assert len(rejected) == 4
        assert all(event['reason'] == 'collision_requires_restart' and not event['queued'] for event in rejected)
    finally:
        e.close()


def test_normal_y_seal_still_allows_next_explicit_start_from_vla():
    e, journal, _, _ = make_engine()
    starts = []
    e.episodes.finish = lambda *args, **kw: setattr(e.episodes, 'phase', 'idle')
    def start(now):
        starts.append(now)
        e.episodes.phase = 'recording'
        return True
    e.episodes.start = start
    try:
        switch(e)
        send_operator(e, 0)
        e.local_failure(time.monotonic_ns())
        send_operator(e, 1, home_end_id=1, home_reset=True)
        now = time.monotonic_ns()
        e.episode_buttons.hold_since = now
        e.poll_episode_finish(now+2_000_000_000)
        assert not e.started and not e.collision_restart_required
        send_operator(e, 2, home_end_id=1)
        send_operator(e, 3, 1, home_end_id=1, hil_button_pressed=True)
        assert len(starts) == 1 and e.started
        assert e.rlt_phase == 'base' and e.local_task.status == 'unused'
        assert not e.collision_restart_required
    finally:
        e.close()


def test_noncollision_fault_does_not_enable_collision_session_latch():
    e, _, _, _ = make_engine()
    try:
        e.disconnect(time.monotonic_ns(), 'target_rejected:stale F1 joint feedback')
        assert not e.collision_restart_required
    finally:
        e.close()


def test_disconnect_is_pending_not_failure_and_no_success_overwrite():
    e, journal, _, _ = make_engine()
    try:
        switch(e)
        e.disconnect(time.monotonic_ns(), 'operator_disconnected')
        assert e.local_task.status == 'interrupted'
        assert not e.local_success(time.monotonic_ns())
        closed = [data for name, data in journal.events if name == ('rlt_local_closed',)]
        assert closed[0]['reward'] is None and closed[0]['truncated']
    finally:
        e.close()


def test_old_v1_sender_cannot_start_or_save_with_new_meaning():
    e, journal, _, _ = make_engine()
    try:
        now = time.monotonic_ns()
        old = replace(episode_packet(0, 0), operator_protocol=None, home_end_id=None, local_result=None)
        assert not e.operator(old, now, now)
        assert e.supervisor.state is HILState.SAFE_HOLD
    finally:
        e.close()


def test_process_shutdown_closes_active_local_as_pending_review():
    e, journal, _, _ = make_engine()
    switch(e)
    e.close()
    assert e.local_task.status == 'interrupted'
    closed = [data for name, data in journal.events if name == ('rlt_local_closed',)]
    assert len(closed) == 1 and closed[0]['reward'] is None
