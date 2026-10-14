from pathlib import Path
from types import SimpleNamespace
import threading
import time
import pytest
from f1dagger.observation import RosObservationSource
from f1dagger.contract import FULL_NAMES
from f1dagger.config import infra_import,DAGGER_ROOT
from test_f1_policy import observation


def store():
    module=infra_import(Path(__import__('os').environ.get('F1_INFRA_ROOT', str(DAGGER_ROOT.parent.parent/'F1-4242'))),'robot.adapter')
    result=module.StateStore();result.connected=True
    stamp=SimpleNamespace(sec=1,nanosec=0)
    result.update_joints(SimpleNamespace(name=FULL_NAMES,position=[0]*22,header=SimpleNamespace(stamp=stamp)))
    for side in ('left','right'):
        result.update_gripper(side,SimpleNamespace(name=[side+'_gripper_position'],position=[0],header=SimpleNamespace(stamp=stamp)))
    return result


def test_missing_body_feedback_cannot_be_zero_filled():
    s=store();s.joints.pop('head_j2')
    source=RosObservationSource(s,{'feedback_max_age_s':.25},{})
    with pytest.raises(RuntimeError,match='missing'):source.feedback()


def test_capture_uses_actual_new_callbacks_and_preserves_source_clock():
    s=store();config={'camera_max_age_s':.2,'camera_max_skew_s':.1}
    source=RosObservationSource(s,{'feedback_max_age_s':.25},config)
    original=observation()
    def deliver():
        time.sleep(.015)
        for frame in original.frames:
            s.update_camera({'head':'head','left_wrist':'left','right_wrist':'right'}[frame.camera],SimpleNamespace(data=frame.payload,format='jpeg',
                header=SimpleNamespace(stamp=SimpleNamespace(sec=1,nanosec=123))))
    thread=threading.Thread(target=deliver);thread.start()
    started=time.monotonic_ns();obs=source.capture(started);thread.join()
    assert obs.capture_started_monotonic_ns==started
    assert all(f.timestamp.value==1_000_000_123 for f in obs.frames)
    assert all(f.host_received_monotonic_ns>=started for f in obs.frames)
    assert obs.state16==(0.,)*16
    assert tuple(frame.camera for frame in obs.frames)==('head','left_wrist','right_wrist')
    assert set(s.cameras)=={'head','left','right'}


def phase_source(monkeypatch):
    import f1dagger.observation as module
    base, period = 1_000_000_000, 33_333_333
    clock = SimpleNamespace(now=base, waits=0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic_ns=lambda: clock.now))
    s = SimpleNamespace(lock=threading.RLock(), cameras={})
    source = RosObservationSource(s, {'feedback_max_age_s': .25},
        {'camera_max_age_s': .2, 'camera_max_skew_s': .1})
    frames = observation().frames
    def deliver():
        for i, (name, frame) in enumerate(zip(('head', 'left', 'right'), frames)):
            origin = base+5_000_000+i*800_000
            slot = (clock.now-origin)//period
            if slot >= 0:
                received = origin+slot*period
                sec, nanosec = divmod(10_000_000_000+slot*period, 1_000_000_000)
                s.cameras[name] = dict(data=frame.payload, mime='image/jpeg',
                    received_at=received/1e9, source_stamp=dict(sec=sec, nanosec=nanosec))
    class Poll:
        def is_set(self): return False
        def wait(self, seconds):
            clock.waits += 1
            clock.now += round(seconds*1e9)
            deliver()
    source.stop_event = Poll()
    source.feedback = lambda: ((0.,)*22, (0.,)*16, (clock.now,)*16)
    return source, clock, deliver, base, period


def test_unconsumed_frame_before_poll_is_not_dropped_or_restamped(monkeypatch):
    source, clock, deliver, base, period = phase_source(monkeypatch)
    first = source.capture(clock.now)
    assert first.capture_started_monotonic_ns == base
    previous = first
    for slot in range(1, 121):
        # Head callback arrives 1 ms before polling; right wrist 0.6 ms after it.
        clock.now = base+slot*period+6_000_000
        deliver()
        started = clock.now
        obs = source.capture(started)
        assert all(frame.timestamp.value == 10_000_000_000+slot*period for frame in obs.frames)
        assert obs.capture_started_monotonic_ns == base+slot*period+5_000_000
        assert obs.capture_started_monotonic_ns < started
        assert all(frame.host_received_monotonic_ns > old.host_received_monotonic_ns
                   for frame, old in zip(obs.frames, previous.frames))
        assert obs.capture_started_monotonic_ns > previous.capture_started_monotonic_ns
        assert all(frame.frame_id == slot for frame in obs.frames)
        previous = obs
    assert previous.observation_id == 120


@pytest.mark.parametrize('stale_field', ['receipt', 'source_stamp'])
def test_both_receipt_and_source_stamp_must_advance(monkeypatch, stale_field):
    source, clock, deliver, base, period = phase_source(monkeypatch)
    first = source.capture(clock.now)
    old = dict(source.store.cameras['head'])
    clock.now = base+period+10_000_000
    deliver()
    if stale_field == 'receipt':
        source.store.cameras['head']['received_at'] = old['received_at']
    else:
        source.store.cameras['head']['source_stamp'] = old['source_stamp']
    source.stop_event = SimpleNamespace(is_set=lambda: False,
        wait=lambda seconds: setattr(clock, 'now', clock.now+round(seconds*1e9)))
    with pytest.raises(RuntimeError, match='three new synchronized'):
        source.capture(clock.now)
    assert source.sequence == 1 and source.last_capture_started == first.capture_started_monotonic_ns


def queued_source(monkeypatch, **limits):
    import f1dagger.observation as module
    from f1dagger.observation import make_camera_store
    infra = infra_import(Path(__import__('os').environ.get('F1_INFRA_ROOT', str(DAGGER_ROOT.parent.parent/'F1-4242'))), 'robot.adapter')
    clock = SimpleNamespace(now=1_000_000_000, deliveries=[])
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic_ns=lambda: clock.now))
    s = make_camera_store(infra.StateStore, clock=lambda: clock.now/1e9, **limits)
    source = RosObservationSource(s, {'feedback_max_age_s': .25},
        {'camera_max_age_s': .2, 'camera_max_skew_s': .1})
    payloads = [frame.payload for frame in observation().frames]
    def deliver(camera, *, stamp=None, payload=None, receipt=None):
        name = {'head': 'head', 'left_wrist': 'left', 'right_wrist': 'right'}[camera]
        clock.now = clock.now+10_000 if receipt is None else receipt
        value = 10_000_000_000+(s.camera_callback_ids[camera]+1)*33_333_333 if stamp is None else stamp
        sec, nanosec = divmod(value, 1_000_000_000)
        s.update_camera(name, SimpleNamespace(data=payload or payloads[0], format='jpeg',
            header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nanosec))))
    def group(**kwargs):
        for camera in ('head', 'left_wrist', 'right_wrist'):
            deliver(camera, **kwargs)
    class Poll:
        def is_set(self): return False
        def wait(self, seconds):
            clock.now += round(seconds*1e9)
            if clock.deliveries:
                clock.deliveries.pop(0)()
        def set(self): pass
    source.stop_event = Poll()
    source.feedback = lambda: ((0.,)*22, (0.,)*16, (clock.now,)*16)
    return source, s, clock, deliver, group


def test_callback_queues_begin_after_warmup_and_reads_do_not_invent_ids(monkeypatch):
    source, s, clock, deliver, group = queued_source(monkeypatch, max_items=1)
    for _ in range(100):
        group()
    stats = s.camera_diagnostics()
    assert stats['pending_bytes'] == 0 and stats['error'] is None
    assert stats['warmup_callbacks'] == dict.fromkeys(('head', 'left_wrist', 'right_wrist'), 100)
    clock.deliveries.append(group)
    obs = source.capture(clock.now)
    assert [f.frame_id for f in obs.frames] == [100, 100, 100]
    assert obs.metadata()['camera_identity_source'] == 'ros_callback_sequence_v1'
    assert not obs.camera_discontinuity and obs.camera_drop_counts == (0, 0, 0)
    with pytest.raises(RuntimeError, match='three new synchronized'):
        source.capture(clock.now)
    assert source.sequence == 1
    assert s.camera_diagnostics()['last_callback_ids'] == dict.fromkeys(('head', 'left_wrist', 'right_wrist'), 100)


def test_callback_burst_preserves_every_image_even_with_repeated_device_stamp(monkeypatch):
    from dataclasses import replace
    from io import BytesIO
    from PIL import Image
    from f1dagger.observation import frames_advance
    source, s, clock, deliver, group = queued_source(monkeypatch)
    clock.deliveries.append(lambda: group(stamp=10_000_000_000))
    previous = source.capture(clock.now)
    payloads = []
    for pixel in (50, 150, 250):
        data = BytesIO(); Image.new('RGB', (8, 8), (pixel, pixel, pixel)).save(data, format='JPEG')
        payloads.append(data.getvalue())
        group(stamp=10_000_000_000, payload=payloads[-1])
    assert all(len(queue) == 3 for queue in s.camera_queues.values())
    for sequence, payload in enumerate(payloads, 1):
        obs = source.capture(clock.now)
        assert all(f.frame_id == sequence and f.payload == payload for f in obs.frames)
        assert all(f.timestamp.value == 10_000_000_000 for f in obs.frames)
        assert frames_advance(obs, previous)
        assert not frames_advance(replace(obs, camera_identity_source=None),
                                  replace(previous, camera_identity_source=None))
        assert not obs.camera_discontinuity and obs.camera_drop_counts == (0, 0, 0)
        previous = obs
    assert s.camera_diagnostics()['pending_bytes'] == 0
    assert s.camera_diagnostics()['repeated_header_stamps'] == dict.fromkeys(('head', 'left_wrist', 'right_wrist'), 3)


@pytest.mark.parametrize('reason', ['age', 'skew'])
def test_old_queue_entries_have_auditable_drop_counts_and_discontinuity(monkeypatch, reason):
    source, s, clock, deliver, group = queued_source(monkeypatch)
    clock.deliveries.append(group)
    previous = source.capture(clock.now)
    deliver('head')
    clock.now += 220_000_000 if reason == 'age' else 120_000_000
    group()
    obs = source.capture(clock.now)
    assert [f.frame_id for f in obs.frames] == [2, 1, 1]
    assert obs.camera_discontinuity and obs.camera_drop_counts == (1, 0, 0)
    assert s.camera_diagnostics()['drop_reasons']['head'] == {reason: 1}
    assert obs.capture_started_monotonic_ns > previous.capture_started_monotonic_ns
    assert all(obs.available_monotonic_ns-f.host_received_monotonic_ns <= 200_000_000 for f in obs.frames)


def test_camera_queue_overflow_is_explicit_and_cannot_be_recorded_as_success(monkeypatch):
    source, s, clock, deliver, group = queued_source(monkeypatch, max_items=1)
    clock.deliveries.append(group)
    source.capture(clock.now)
    deliver('head'); deliver('head')
    with pytest.raises(RuntimeError, match='queue overflow_items'):
        source.capture(clock.now)
    stats = s.camera_diagnostics()
    assert stats['drop_counts']['head'] == 1 and stats['pending_items']['head'] == 1
    assert stats['peak_items']['head'] == 1 and source.sequence == 1


def test_camera_queue_byte_capacity_is_enforced_without_blocking_callback(monkeypatch):
    source, s, clock, deliver, group = queued_source(monkeypatch, max_bytes=1)
    clock.deliveries.append(group)
    with pytest.raises(RuntimeError, match='queue overflow_bytes'):
        source.capture(clock.now)
    assert s.camera_diagnostics()['pending_bytes'] == 0
    assert source.sequence == 0


@pytest.mark.parametrize('clock_failure', ['invalid_header', 'receipt'])
def test_invalid_header_or_host_receipt_regression_remains_fatal(monkeypatch, clock_failure):
    source, s, clock, deliver, group = queued_source(monkeypatch)
    clock.deliveries.append(lambda: group(stamp=10_000_000_000))
    obs = source.capture(clock.now)
    if clock_failure == 'invalid_header':
        deliver('head', stamp=0)
    else:
        deliver('head', receipt=obs.frames[0].host_received_monotonic_ns)
    with pytest.raises(RuntimeError, match='clock invalid/regressed'):
        source.capture(clock.now)
    stats = s.camera_diagnostics()
    reason = 'invalid_source_stamp' if clock_failure == 'invalid_header' else 'regressed_host_receipt'
    assert stats['drop_reasons']['head'] == {reason: 1}
    assert stats['last_clock_issues']['head']['reason'] == reason
    assert source.sequence == 1


def test_isolated_source_header_regression_is_audited_drop_then_recovers(monkeypatch):
    from f1dagger.observation import frames_advance
    source, s, clock, deliver, group = queued_source(monkeypatch)
    clock.deliveries.append(lambda: group(stamp=10_000_000_000))
    first = source.capture(clock.now)
    baseline = s.camera_last_callback['head']
    deliver('head', stamp=9_999_999_999)
    bad_receipt = clock.now
    assert not s.camera_queues['head'] and s.camera_queue_error is None
    assert s.camera_last_callback['head'] == baseline
    group(stamp=10_000_000_001)
    second = source.capture(clock.now)
    assert source.sequence == 2
    assert [frame.frame_id for frame in second.frames] == [2, 1, 1]
    assert all(frame.timestamp.value == 10_000_000_001 for frame in second.frames)
    assert second.camera_discontinuity and second.camera_drop_counts == (1, 0, 0)
    assert frames_advance(second, first)
    stats = s.camera_diagnostics()
    assert stats['error'] is None and stats['drop_reasons']['head'] == {'regressed_source_stamp': 1}
    assert stats['last_clock_issues']['head'] == dict(reason='regressed_source_stamp', callback_sequence=1,
        previous_callback_sequence=0, previous_accepted_callback_sequence=0,
        previous_host_received_monotonic_ns=first.frames[0].host_received_monotonic_ns,
        host_received_monotonic_ns=bad_receipt, previous_accepted_source_stamp_ns=10_000_000_000,
        source_stamp_ns=9_999_999_999)
    assert len(stats['last_clock_issues']) == 1


def test_continuously_regressed_headers_timeout_without_reusing_last_image(monkeypatch):
    source, s, clock, deliver, group = queued_source(monkeypatch)
    clock.deliveries.append(lambda: group(stamp=10_000_000_000))
    first = source.capture(clock.now)
    baseline = s.camera_last_callback['head']
    started = clock.now
    # Each polling wait receives another actual callback with a bad header.
    # No acceptable head frame is queued, so capture must retain its deadline.
    clock.deliveries = [lambda: deliver('head', stamp=9_999_999_999) for _ in range(500)]
    with pytest.raises(RuntimeError, match='three new synchronized'):
        source.capture(started)
    stats = s.camera_diagnostics()
    assert clock.now-started >= 2_000_000_000
    assert source.sequence == 1 and source.frame_ids['head'] == first.frames[0].frame_id
    assert not s.camera_queues['head'] and s.camera_last_callback['head'] == baseline
    assert stats['error'] is None and stats['drop_counts']['head'] > 100
    assert len(stats['last_clock_issues']) == 1
    assert stats['last_clock_issues']['head']['callback_sequence'] == stats['last_callback_ids']['head']


def test_dropped_bad_header_cannot_hide_a_later_host_receipt_regression(monkeypatch):
    source, s, clock, deliver, group = queued_source(monkeypatch)
    clock.deliveries.append(lambda: group(stamp=10_000_000_000))
    source.capture(clock.now)
    deliver('head', stamp=9_999_999_999)
    bad_receipt = clock.now
    deliver('head', stamp=10_000_000_001, receipt=bad_receipt)
    with pytest.raises(RuntimeError, match='regressed_host_receipt'):
        source.capture(clock.now)
    stats = s.camera_diagnostics()
    assert stats['drop_reasons']['head'] == {'regressed_source_stamp': 1, 'regressed_host_receipt': 1}
    assert stats['last_clock_issues']['head']['previous_callback_sequence'] == 1
    assert stats['last_clock_issues']['head']['previous_accepted_callback_sequence'] == 0
