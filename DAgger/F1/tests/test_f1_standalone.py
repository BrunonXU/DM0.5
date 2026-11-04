"""Standalone authorization must not weaken HIL or the final dispatch boundary."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from dagger_core.recording_sinks import NullSink
from dagger_core.hil_supervisor import HILState
from f1dagger.cli import arguments
from f1dagger.runtime import ControlLoop
from f1dagger.standalone import StandalonePolicyEngine, StandaloneFinished
from test_f1_policy import Source, Journal, CONFIG
from test_f1_output import make_owner


def setup(duration=1., infer=None):
    journal = Journal()
    stop = threading.Event()
    policy = SimpleNamespace(model_id='test', right_only=True,
        infer=infer or (lambda *args: np.zeros((3, 16))))
    engine = StandalonePolicyEngine(journal.record_event, Source(), policy,
        journal, NullSink(), CONFIG, duration_seconds=duration, stop_requested=stop.is_set)
    owner, transport = make_owner()
    clock = [time.monotonic_ns()]
    loop = ControlLoop(engine, owner, SimpleNamespace(release=lambda: None), journal, {}, clock=lambda: clock[0])
    return engine, loop, transport, clock, stop


def test_no_output_before_local_authorization_and_no_operator_needed():
    e, loop, transport, clock, stop = setup()
    try:
        loop.step(clock[0]); assert not transport.sent
        e.authorize_local(clock[0]); loop.step(clock[0])
        # An actual async inference is admitted without a fabricated heartbeat.
        end = time.monotonic() + 1
        while e.ready is None and time.monotonic() < end:
            time.sleep(.01); e.tick(time.monotonic_ns())
        assert e.ready is not None and e.last_received_ns is None and e.packet is None
        clock[0] = time.monotonic_ns(); loop.step(clock[0])
        assert transport.sent
        with pytest.raises(RuntimeError, match='one-shot'): e.authorize_local(clock[0])
    finally: e.close()


@pytest.mark.parametrize('trigger', ['duration', 'ctrl_c'])
def test_stop_during_slow_target_prevents_publication(trigger):
    e, loop, transport, clock, stop = setup()
    try:
        e.authorize_local(clock[0]); loop.step(clock[0])
        clock[0] += 20_000_000
        original = loop.owner.approve
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            if trigger == 'duration': clock[0] = e.deadline_ns
            else: stop.set()
            return result
        loop.owner.approve = slow
        with pytest.raises(StandaloneFinished): loop.step(clock[0])
        assert not transport.sent
    finally: e.close()


def test_policy_failure_stops_and_invalidates_without_automatic_resume():
    def fail(*args): raise RuntimeError('model unavailable')
    e, loop, transport, clock, stop = setup(infer=fail)
    try:
        e.authorize_local(clock[0])
        with pytest.raises(RuntimeError, match='model unavailable'):
            for _ in range(100):
                time.sleep(.01); e.tick(time.monotonic_ns())
        assert e.supervisor.state is HILState.SAFE_HOLD and e.ready is None
        with pytest.raises(RuntimeError): e.before_publication(time.monotonic_ns())
        assert not transport.sent
    finally: e.close()


def test_continuous_still_stops_on_camera_fault():
    e, loop, transport, clock, stop = setup(duration=None)
    try:
        e.authorize_local(clock[0]); assert e.deadline_ns is None
        e._camera_fault_pending = 'camera disconnected'
        with pytest.raises(RuntimeError, match='camera disconnected'): e.tick(clock[0])
        assert not transport.sent
    finally: e.close()


@pytest.mark.parametrize('args', [
    ['sync', '--duration-seconds', '0'], ['sync', '--duration-seconds', 'nan'],
    ['sync', '--duration-seconds', 'inf'], ['sync', '--duration-seconds', '-1'],
    ['sync', '--duration-seconds', '2', '--continuous'], ['run', '--continuous']])
def test_invalid_duration_rejected_before_hardware(monkeypatch, args):
    monkeypatch.setattr('sys.argv', ['f1dagger'] + args)
    with pytest.raises(SystemExit): arguments()


def test_four_ik_failures_pause_then_enter_fresh_prediction_same_owner():
    from f1dagger.policy_errors import IKRejected
    calls = []
    def infer(obs, rid, epoch):
        calls.append((obs.capture_started_monotonic_ns, rid, epoch))
        if len(calls) <= 4:
            raise IKRejected(dict(request_id=rid, observation_id=obs.observation_id,
                                  diagnostics=[dict(success=False, joints=None)]))
        return np.zeros((3, 16))
    e, loop, transport, clock, stop = setup(duration=10, infer=infer)
    def step():
        clock[0] = time.monotonic_ns(); loop.step(clock[0]); time.sleep(.005)
    try:
        e.authorize_local(clock[0]); step()
        # Simulate a previously published target that hardware has not reached.
        loop.owner.last_submitted[7:14] = 2.
        held_other = loop.owner.last_submitted[[0, 14, 15]].copy()
        deadline = time.monotonic() + 2
        while e.ik_state != 'paused' and time.monotonic() < deadline: step()
        assert e.ik_state == 'paused' and len(calls) == 4
        assert not e.ik_hold_pending and e.ready is None and e.chunk is None
        np.testing.assert_equal(loop.owner.last_submitted[7:14], 0.)
        np.testing.assert_equal(loop.owner.last_submitted[[0, 14, 15]], held_other)
        old_epoch = e.supervisor.policy_epoch
        for _ in range(15): step()
        assert len(calls) == 4  # No infinite retry or silent automatic resume.
        resumed = time.monotonic_ns()
        assert e.resume_local(resumed)
        while e.ik_state != 'running' and time.monotonic() < deadline: step()
        assert e.ik_state == 'running' and e.ik_failures == 0
        assert len(calls) == 5 and calls[-1][0] > resumed
        assert calls[-1][2] > old_epoch
        assert len({c[0] for c in calls}) == len({c[1] for c in calls}) == 5
        assert all(b[2] > a[2] for a, b in zip(calls, calls[1:]))
        assert len(transport.prepared) == 3  # Same sole owner, no endpoint recreation.
    finally: e.close()


@pytest.mark.parametrize('fault', ['deadline', 'stop', 'feedback', 'camera'])
def test_paused_recovery_does_not_disable_hard_protections(fault):
    from f1dagger.policy_errors import IKRejected
    e, loop, transport, clock, stop = setup(duration=10)
    try:
        e.authorize_local(clock[0]); loop.step(clock[0])
        e.ik_failures = 3
        e.policy_failed(IKRejected(dict(diagnostics=[dict(success=False)])), clock[0])
        if fault == 'deadline': clock[0] = e.deadline_ns
        if fault == 'stop': stop.set()
        if fault == 'feedback':
            loop.owner.source.feedback = lambda: (_ for _ in ()).throw(RuntimeError('stale feedback'))
        if fault == 'camera': e._camera_fault_pending = 'camera disconnected'
        with pytest.raises((StandaloneFinished, RuntimeError)): loop.step(clock[0])
        assert not transport.sent
    finally: e.close()


def test_enter_before_pause_is_not_queued_as_resume():
    import os
    from f1dagger.standalone import LocalResumeInput
    read_fd, write_fd = os.pipe()
    resumed = []
    engine = SimpleNamespace(ik_state='running', ik_hold_pending=False,
                             resume_local=lambda now: resumed.append(now))
    try:
        with os.fdopen(read_fd, 'rb', buffering=0) as stream:
            ui = LocalResumeInput(stream)
            os.write(write_fd, b'\n'); ui.poll(engine, 1)
            engine.ik_state = 'paused'; ui.poll(engine, 2)
            assert not resumed
            os.write(write_fd, b'\n'); ui.poll(engine, 3)
            assert resumed == [3]
    finally: os.close(write_fd)
