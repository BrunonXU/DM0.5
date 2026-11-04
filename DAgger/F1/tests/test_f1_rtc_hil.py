"""Real HIL supervisor and Control owner, synthetic input explicitly labelled."""
from concurrent.futures import Future
from dataclasses import replace
import time
from types import SimpleNamespace

import numpy as np
import pytest
from dagger_core.hil_supervisor import HILState
from dagger_core.recording_sinks import NullSink
from f1dagger.rtc_hil import RTCHILPolicyEngine
from f1dagger.standalone import StandaloneFinished
from test_f1_policy import Source, Journal, observation
from test_real_observation_shadow import packet


def make_engine(duration=None):
    journal, calls = Journal(), []
    held = np.r_[np.zeros(14), 32., 66.]
    def infer(obs, rid, epoch, context):
        calls.append((obs, epoch, context))
        actions = np.repeat(held[None], context['coverage_steps'], axis=0)
        actions[:, 7] = rid
        if context['committed_actions16']:
            actions[:context['delay_steps']] = context['committed_actions16']
        return actions, np.zeros((50, 10))
    policy = SimpleNamespace(model_id='synthetic', last_timing={}, infer_rtc=infer,
                             held_getter=lambda: held.copy(), right_only=True)
    e = RTCHILPolicyEngine(journal.record_event, Source(), policy, journal, NullSink(),
        {'capture_hz': 30., 'policy': {'chunk_steps': 20, 'frequency_hz': 30.}},
        duration_seconds=duration, stop_requested=lambda: False,
        rtc_delay_steps=14, rtc_min_steps=16)
    return e, calls, journal


def send(e, seq, toggle, **kw):
    now = time.monotonic_ns()
    e.operator(replace(packet(seq, toggle), **kw), now, now)
    return now


def until(e, predicate, toggle=1, seq=100):
    end = time.monotonic()+2
    while not predicate() and time.monotonic() < end:
        send(e, seq, toggle); seq += 1
        e.tick(time.monotonic_ns())
        time.sleep(.004)
    assert predicate()


def start(e):
    send(e, 0, 0)
    send(e, 1, 1, hil_button_pressed=True)


def test_timing_replan_requires_published_hold_then_fresh_bootstrap():
    e, calls, journal = make_engine()
    e.rtc_timing_replan = True
    try:
        start(e); until(e, lambda: e.chunk is not None)
        epoch = e.supervisor.policy_epoch
        old = Future()
        e.real_future = old; e.real_future_epoch = epoch; e.real_future_request_id = 900
        e.pending_context = dict(bootstrap=False, plan_origin_ns=time.monotonic_ns(), delay_steps=14)
        e.pause_timing(time.monotonic_ns(), 'inference_deadline')
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        assert e.supervisor.policy_epoch == epoch + 1
        assert e.chunk is None and not e.policy_enabled and e.ik_hold_pending
        count = len(calls)
        old.set_result((np.ones((32, 16))*99, np.ones((50, 10)), time.monotonic_ns()))
        e.tick(time.monotonic_ns())
        assert e.real_future is None and e.chunk is None and len(calls) == count
        e.tick(time.monotonic_ns())
        assert len(calls) == count
        committed = time.monotonic_ns()
        e.recovery_hold_committed(committed)
        assert e.policy_enabled and not e.ik_hold_pending
        until(e, lambda: e.chunk is not None, seq=600)
        obs, next_epoch, context = calls[-1]
        assert next_epoch == epoch + 1 and obs.capture_started_monotonic_ns > committed
        assert context['bootstrap'] and context['committed_actions16'] == []
        assert e.supervisor.state is HILState.POLICY
    finally: e.close()


@pytest.mark.parametrize('revoke', ['start', 'input_loss', 'phase_reset', 'camera'])
def test_timing_replan_cannot_survive_authority_or_health_change(revoke):
    e, calls, _ = make_engine(); e.rtc_timing_replan = True
    try:
        start(e); until(e, lambda: e.chunk is not None)
        e.pause_timing(time.monotonic_ns(), 'plan_exhausted')
        assert e.timing_replan_epoch is not None
        if revoke == 'start':
            send(e, 500, 2)
        elif revoke == 'input_loss':
            e.disconnect(time.monotonic_ns(), 'operator_watchdog')
        elif revoke == 'phase_reset':
            e.invalidate(time.monotonic_ns())
            e.policy_enabled = False
        else:
            e.camera_issue = 'missing'
        count = len(calls)
        e.recovery_hold_committed(time.monotonic_ns())
        assert e.timing_replan_epoch is None
        assert e.chunk is None and len(calls) == count
        if revoke == 'start':
            assert e.supervisor.state is HILState.HIL_LATCHED
        else:
            assert not e.policy_enabled
    finally: e.close()


@pytest.mark.parametrize('reason', ['ik_window_coverage', 'horizon_coverage'])
def test_unexpected_coverage_fault_still_requires_operator(reason):
    e, _, _ = make_engine(); e.rtc_timing_replan = True
    try:
        start(e); until(e, lambda: e.chunk is not None)
        e.pause_timing(time.monotonic_ns(), reason)
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert e.timing_replan_epoch is None and not e.policy_enabled
    finally: e.close()


def test_genuine_start_duration_and_no_local_bypass():
    e, calls, _ = make_engine(.1)
    try:
        for seq in range(3):
            send(e, seq, 1, hil_button_pressed=True)
            e.tick(time.monotonic_ns())
        assert not e.started and not calls
        send(e, 3, 1)
        begin = send(e, 4, 2, hil_button_pressed=True)
        assert e.started and e.deadline_ns == begin+100_000_000
        with pytest.raises(RuntimeError): e.authorize_local(begin)
        with pytest.raises(RuntimeError): e.resume_local(begin)
        with pytest.raises(StandaloneFinished): e.before_publication(e.deadline_ns)
    finally: e.close()


@pytest.mark.parametrize('failed', [False, True])
def test_inflight_late_success_and_error_cannot_interrupt_human(failed):
    e, calls, j = make_engine()
    try:
        start(e); until(e, lambda: e.chunk is not None)
        old_epoch = e.supervisor.policy_epoch
        f = Future(); e.real_future = f; e.real_future_epoch = old_epoch
        e.real_future_request_id = 900
        e.pending_context = dict(bootstrap=False, plan_origin_ns=time.monotonic_ns(), delay_steps=14)
        send(e, 500, 2)
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert e.chunk is None and e.absolute_eef is None and e.chunk_anchor is None
        count = len(calls)
        if failed: f.set_exception(RuntimeError('late IK/transport exception'))
        else: f.set_result((np.ones((32, 16)), np.ones((50, 10)), time.monotonic_ns()))
        e.tick(time.monotonic_ns())
        assert e.supervisor.state is HILState.HIL_LATCHED and not e.ik_hold_pending
        assert not e.real_future and len(calls) == count
        events = [fields for (name,), fields in j.events if name == 'stale_policy_result_discarded']
        assert events[-1]['reason'] == 'authority_revoked'
        assert bool(events[-1]['error']) == failed
    finally: e.close()


def test_handoff_waits_for_all_feedback_and_new_images_and_empty_prefix():
    e, calls, _ = make_engine()
    try:
        start(e); until(e, lambda: e.chunk is not None)
        send(e, 500, 2)
        now = time.monotonic_ns()
        e.operator(packet(501, 3), now, now, hand_pending=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
        send(e, 502, 4, deadman=True)
        assert e.supervisor.state is HILState.HIL_LATCHED
        boundary = send(e, 503, 5)
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        # Stop the acquisition thread so controlled snapshots cannot be replaced.
        e.stop_event.set(); e.capture_thread.join(1)
        count = len(calls)
        for index in range(16):
            obs = observation(1000+index, time.monotonic_ns())
            stamps = list(obs.state_timestamps_ns); stamps[index] = boundary
            e.latest = (replace(obs, state_timestamps_ns=tuple(stamps)), e.supervisor.policy_epoch)
            e.tick(time.monotonic_ns())
            assert len(calls) == count and e.real_future is None
        old = observation(2000, boundary-1)
        e.latest = (old, e.supervisor.policy_epoch)
        e.tick(time.monotonic_ns()); assert e.real_future is None
        e.latest = (observation(2001, time.monotonic_ns()), e.supervisor.policy_epoch)
        until(e, lambda: e.chunk is not None, toggle=5, seq=600)
        c = calls[-1][2]
        assert c['bootstrap'] and not c['committed_actions16'] and not c['previous_absolute_eef10']
        assert c['base_plan_id'] == 0 and list(e.delays) == [14]
    finally: e.close()


def test_recovery_callback_revoked_by_start_and_camera_needs_fresh_consent():
    e, _, _ = make_engine()
    try:
        start(e)
        e.pause_timing(time.monotonic_ns(), 'inference_deadline')
        assert e.ik_hold_pending
        send(e, 2, 2)
        assert e.supervisor.state is HILState.HIL_LATCHED and not e.ik_hold_pending
        epoch = e.supervisor.policy_epoch
        e.recovery_hold_committed(time.monotonic_ns())
        assert e.supervisor.policy_epoch == epoch and e.supervisor.state is HILState.HIL_LATCHED
        e._camera_fault_pending = 'missing'; e.camera_issue = 'missing'
        e.tick(time.monotonic_ns())
        assert e.supervisor.state is HILState.SAFE_HOLD
        send(e, 3, 3)  # Consume START during missing camera.
        e.camera_issue = None; e.tick(time.monotonic_ns())
        send(e, 4, 3)
        assert e.supervisor.state is HILState.SAFE_HOLD
        send(e, 5, 4)
        assert e.supervisor.state is HILState.HIL_LATCHED
    finally: e.close()


def test_single_owner_measured_takeover_preserves_gripper_then_manual_rt():
    from f1dagger.runtime import ControlLoop
    from test_f1_output import make_owner
    e, _, j = make_engine(); owner, transport = make_owner()
    clock = [time.monotonic_ns()]
    records = []; j.control = lambda **kw: records.append(kw) or True
    arm = SimpleNamespace(release=lambda: None, target=lambda p,f,last,dt: last.copy())
    loop = ControlLoop(e, owner, arm, j, {}, clock=lambda: clock[0])
    def step(seq, toggle, **kw):
        clock[0] = time.monotonic_ns()
        loop.step(clock[0], (replace(packet(seq, toggle), **kw), clock[0]))
    try:
        step(0, 0); step(1, 1)
        owner.last_submitted[7] = 12.
        owner.selected[15] = 0.  # Unexecuted future policy opening must be discarded.
        step(2, 2)
        assert loop.owner is owner and records[-1]['source'] == 'takeover_hold'
        assert owner.last_submitted[7] == 0.
        np.testing.assert_array_equal(owner.last_submitted[14:], [32., 66.])
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert records[-1]['policy'] is None
        time.sleep(.021); step(3, 2, deadman=True, active_side='right')
        assert records[-1]['source'] == 'human_arm'
        np.testing.assert_array_equal(owner.last_submitted[14:], [32., 66.])
    finally: e.close()


def test_deadline_after_selection_flushes_in_same_publication():
    from f1dagger.runtime import ControlLoop
    from test_f1_output import make_owner
    e, _, j = make_engine(); owner, _ = make_owner(); now = [time.monotonic_ns()]
    loop = ControlLoop(e, owner, SimpleNamespace(release=lambda: None), j, {}, clock=lambda: now[0])
    try:
        start(e); loop.step(now[0]); until(e, lambda: e.chunk is not None)
        now[0] = time.monotonic_ns()
        send(e, 500, 1)
        e.real_future = Future(); e.real_future_epoch = e.supervisor.policy_epoch
        e.real_future_request_id = 2
        e.pending_context = dict(bootstrap=False, plan_origin_ns=now[0]-460_000_000, delay_steps=14)
        original = owner.approve
        def cross(*a, **kw):
            original(*a, **kw); now[0] += 10_000_000
        owner.approve = cross
        loop.step(now[0])
        assert e.supervisor.state is HILState.SAFE_HOLD and loop.source == 'rtc_timing_hold'
        assert owner.last_submitted[7] == 0.
    finally: e.close()


def test_cli_modes_are_explicit(monkeypatch):
    from f1dagger.cli import arguments
    monkeypatch.setattr('sys.argv', ['f1', 'policy-shadow', '--rtc-hil', '--continuous'])
    a = arguments(); assert a.rtc_hil and not a.rtc and a.continuous
    for args in [('sync', '--rtc-hil'), ('run', '--rtc'),
                 ('run', '--rtc-hil', '--recording-mode', 'prebuffer-record')]:
        monkeypatch.setattr('sys.argv', ['f1', *args])
        with pytest.raises(SystemExit): arguments()
