"""Deterministic acquisition clocks: no sleeping, sensors or robot output."""
import threading
from types import SimpleNamespace as NS

import pytest

from f1dagger.policy import F1PolicyEngine
from g2dagger.hil_real_shadow import RealObservationHILShadow
from g2dagger.hil_supervisor import HILState
from test_f1_policy import observation


class Clock:
    def __init__(self, wake_delay_ns=0, stop_after_waits=None):
        self.now = 1_000_000_000
        self.wake_delay_ns = wake_delay_ns
        self.waits = []
        self.stopped = False
        self.stop_after_waits = stop_after_waits
    def is_set(self): return self.stopped
    def set(self): self.stopped = True
    def wait(self, seconds):
        self.waits.append(seconds)
        if self.stopped:
            return True
        if seconds > 0:
            self.now += round(seconds*1e9)+self.wake_delay_ns
        if self.stop_after_waits is not None and len(self.waits) >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


def scheduler(monkeypatch, clock, *, use_f1=True):
    import f1dagger.policy as f1_module
    import dagger_core.hil_real_shadow as shared
    monkeypatch.setattr(f1_module, 'time', NS(monotonic_ns=lambda: clock.now))
    monkeypatch.setattr(shared, 'time', NS(monotonic_ns=lambda: clock.now))
    cls = F1PolicyEngine if use_f1 else RealObservationHILShadow
    engine = object.__new__(cls)
    engine.capture_period = 1/30
    engine.stop_event = clock
    engine._capture_deadline_ns = None
    return engine


def acquisition(engine, clock, *, processing_ns=2_000_000, interrupt_at=None):
    engine.context_lock, engine.latest_lock = threading.RLock(), threading.Lock()
    engine.record_trigger = threading.Event()
    engine.supervisor = NS(state=HILState.HIL_LATCHED, policy_epoch=4, intervention_id=2)
    engine.strict_recording = True
    engine.acquisition_error = None
    engine.latest = None
    captured, saved, recorded = [], [], []
    def capture(started):
        result = observation(len(captured), started)
        captured.append(result)
        clock.now += processing_ns
        if len(captured) == interrupt_at:
            clock.set()
        return result
    engine.source = NS(capture=capture)
    engine.sink = NS(submit=lambda obs: saved.append(obs) or True, trigger=lambda: True)
    engine.recorder = NS(record_timestamped_observation=lambda obs, **fields: recorded.append((obs, fields)) or True)
    return captured, saved, recorded


def test_g2_loop_retains_relative_started_time_wait(monkeypatch):
    clock = Clock(wake_delay_ns=3_000_000, stop_after_waits=5)
    engine = scheduler(monkeypatch, clock, use_f1=False)
    captured, _, _ = acquisition(engine, clock)
    initial = clock.now
    engine._capture_loop()
    assert len(captured) == 5 and engine.acquisition_error is None
    assert clock.now == initial+5*(33_333_333+3_000_000)
    assert all(abs(wait-(1/30-.002)) < 1e-12 for wait in clock.waits)


def test_g2_loop_retains_no_wait_after_capture_overrun(monkeypatch):
    clock = Clock(wake_delay_ns=3_000_000, stop_after_waits=1)
    engine = scheduler(monkeypatch, clock, use_f1=False)
    acquisition(engine, clock, processing_ns=40_000_000)
    initial = clock.now
    engine._capture_loop()
    assert clock.waits == [0] and clock.now == initial+40_000_000


def test_f1_absolute_grid_does_not_accumulate_wakeup_delay(monkeypatch):
    clock = Clock(wake_delay_ns=3_000_000)
    engine = scheduler(monkeypatch, clock)
    initial = clock.now
    for _ in range(1000):
        started = clock.now
        clock.now += 2_000_000
        engine._wait_for_capture(started)
    assert clock.now == initial+1000*33_333_333+3_000_000
    assert engine._capture_deadline_ns == initial+1000*33_333_333
    assert clock.waits[1] < clock.waits[0]


def test_late_capture_catches_up_without_resetting_grid(monkeypatch):
    clock = Clock()
    engine = scheduler(monkeypatch, clock)
    initial = clock.now
    started = clock.now
    clock.now += 80_000_000
    engine._wait_for_capture(started)
    assert clock.waits[-1] == 0
    assert engine._capture_deadline_ns == initial+33_333_333
    started = clock.now
    clock.now += 1_000_000
    engine._wait_for_capture(started)
    assert clock.waits[-1] == 0
    started = clock.now
    clock.now += 1_000_000
    engine._wait_for_capture(started)
    assert 0 < clock.waits[-1] < .02
    assert clock.now == initial+3*33_333_333


def test_f1_loop_records_distinct_observations_and_stops_promptly(monkeypatch):
    clock = Clock(wake_delay_ns=3_000_000)
    engine = scheduler(monkeypatch, clock)
    captured, saved, recorded = acquisition(engine, clock, interrupt_at=10)
    initial = clock.now
    engine._capture_loop()
    assert engine.acquisition_error is None
    assert captured == saved == [row[0] for row in recorded]
    assert [obs.observation_id for obs in saved] == list(range(10))
    assert all(row[1] == dict(hil_state='hil_latched', policy_epoch=4, intervention_id=2) for row in recorded)
    assert clock.now == initial+9*33_333_333+3_000_000+2_000_000
    assert engine.latest == (captured[-1], 4)


@pytest.mark.parametrize('failure', ['trigger', 'payload', 'journal', 'source'])
def test_f1_cadence_keeps_shared_fail_closed_admission_semantics(monkeypatch, failure):
    clock = Clock(stop_after_waits=2)
    engine = scheduler(monkeypatch, clock)
    captured, saved, recorded = acquisition(engine, clock)
    if failure == 'trigger':
        engine.record_trigger.set()
        engine.sink.trigger = lambda: False
    elif failure == 'payload':
        engine.sink.submit = lambda obs: False
    elif failure == 'journal':
        engine.recorder.record_timestamped_observation = lambda *args, **kwargs: False
    else:
        def fail(started): raise RuntimeError('camera source failed')
        engine.source.capture = fail
    engine._capture_loop()
    assert engine.acquisition_error and engine.latest is None
    assert not clock.waits
