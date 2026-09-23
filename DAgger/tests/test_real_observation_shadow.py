from types import SimpleNamespace
import threading
import time
import numpy as np

from g2dagger.real_observation_source import TimestampedG2Source, frames_advance, policy_input_issue
from g2dagger.hil_real_shadow import RealObservationHILShadow
from g2dagger.recording_sinks import NullSink
from g2dagger.recording_observation import CAMERAS, TimestampedFrame, DeviceTimestamp, TimestampedObservation
from g2dagger.protocol import IntentPacket


def packet(seq, toggle=0):
    return IntentPacket(session_id="s", sequence=seq, sent_at_unix_ns=1, deadman=False,
        active_side="left", mode="combined", axes=(0., 0., 0.), grip_velocity=0.,
        thumb_clearance=False, hil_toggle_id=toggle)


def test_camera_passthrough_reads_once_retains_raw_timestamps_and_repeated_ids():
    calls = []
    class Rig:
        def _read_message(self, name):
            calls.append(name)
            return SimpleNamespace(timestamp_ns=100 + CAMERAS.index(name), data=b"image")
        def _encoded_from_message(self, message, name):
            return SimpleNamespace(data=message.data, media_type="image/jpeg", width=4, height=3)
    source = TimestampedG2Source(Rig(), SimpleNamespace(read=lambda: np.ones(34)))
    first, second = source.capture(), source.capture()
    assert calls == list(CAMERAS) * 2
    assert [f.timestamp.value for f in first.frames] == [100, 101, 102]
    assert not frames_advance(second, first)
    assert [f.frame_id for f in second.frames] == [0, 0, 0]
    assert not any(first.measured_valid_34d[14:])
    assert policy_input_issue(first) is None


def test_zero_hand_state_is_preserved_but_not_used_for_policy():
    class Rig:
        def _read_message(self, name):
            return SimpleNamespace(timestamp_ns=1)
        def _encoded_from_message(self, message, name):
            return SimpleNamespace(data=b"image", media_type="image/jpeg", width=4, height=3)
    obs = TimestampedG2Source(Rig(), SimpleNamespace(read=lambda: np.zeros(34))).capture()
    assert obs.measured_34d == (0.,) * 34
    assert policy_input_issue(obs) == "zero_hand_state_ambiguous_no_command_fallback"


class Source:
    def __init__(self):
        self.count = 0
    def capture(self, started):
        i = self.count
        self.count += 1
        return TimestampedObservation(i, started, time.monotonic_ns(), tuple(
            TimestampedFrame(name, i, DeviceTimestamp(i, "ns", "test"), started,
                             b"image", "image/jpeg", 4, 3) for name in CAMERAS),
            started, (1.,) * 34, (True,) * 14 + (False,) * 20, "test")


class Journal:
    def __init__(self):
        self.observations, self.chunks = [], []
    def record_timestamped_observation(self, obs, **context):
        self.observations.append((obs, context))
    def record_policy_chunk(self, **row):
        self.chunks.append(row)


def test_capture_continues_during_blocked_inference_and_hil_rejects_old_response():
    entered, release = threading.Event(), threading.Event()
    journal, source, events = Journal(), Source(), []
    def infer(obs, request_id):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(actions=np.ones((50, 34)))
    engine = RealObservationHILShadow(lambda e, **f: events.append((e, f)), source, infer,
                                       journal, NullSink(), capture_hz=30, request_period=.01)
    try:
        engine.receive(packet(0), time.monotonic_ns())
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            engine.tick(time.monotonic_ns())
            time.sleep(.005)
        assert entered.is_set()
        engine.receive(packet(1, 1), time.monotonic_ns())
        count = source.count
        time.sleep(.1)
        assert source.count > count
        release.set()
        deadline = time.monotonic() + 2
        while not engine.real_future.done() and time.monotonic() < deadline:
            time.sleep(.005)
        engine.tick(time.monotonic_ns())
        assert engine.supervisor.state.value == "hil_latched"
        results = [f for e, f in events if e == "real_policy_result"]
        assert len(results) == 1 and not results[0]["accepted"]
        assert len(journal.chunks) == 1
    finally:
        release.set()
        engine.close()


def test_capture_error_is_explicit_and_stops_runtime():
    class Broken:
        def capture(self, started):
            raise ValueError("missing camera")
    engine = RealObservationHILShadow(lambda *a, **k: None, Broken(), lambda *a: None, Journal(), NullSink())
    try:
        engine.capture_thread.join(timeout=1)
        import pytest
        with pytest.raises(RuntimeError, match="missing camera"):
            engine.tick(time.monotonic_ns())
    finally:
        engine.close()
