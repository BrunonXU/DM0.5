"""Immutable ROS acquisitions. Cached reads never create new camera timestamps."""
from collections import deque
from dataclasses import dataclass
from io import BytesIO
import threading
import time
import numpy as np
from dagger_core.recording_observation import DeviceTimestamp, TimestampedFrame
from .contract import ARM_NAMES, FULL_NAMES, CAMERAS, vector
from .hardware import state_mask, validate_mask
from .faults import FeedbackUnavailable

CAMERA_IDENTITY = 'ros_callback_sequence_v1'


class CameraUnavailable(RuntimeError):
    """No fresh camera group within the existing capture deadline."""


@dataclass(frozen=True)
class Observation:
    observation_id: int
    capture_started_monotonic_ns: int
    available_monotonic_ns: int
    frames: tuple
    state16: tuple
    state_timestamps_ns: tuple
    full22: tuple
    state_source: str = 'f1_ros_hal_and_omnipicker'
    camera_identity_source: str | None = None
    camera_drop_counts: tuple | None = None
    camera_discontinuity: bool = False
    state_valid_mask: tuple | None = None

    def __post_init__(self):
        if self.camera_identity_source not in (None, CAMERA_IDENTITY):
            raise ValueError('unsupported camera identity source')
        if self.camera_identity_source == CAMERA_IDENTITY and (
                self.camera_drop_counts is None or len(self.camera_drop_counts) != 3
                or any(type(n) is not int or n < 0 for n in self.camera_drop_counts)
                or type(self.camera_discontinuity) is not bool):
            raise ValueError('callback identity requires explicit drop counts/discontinuity')
        vector(self.state16, 16, 'state16')
        vector(self.full22, 22, 'full22')
        if tuple(f.camera for f in self.frames) != CAMERAS:
            raise ValueError('ordered three-camera observation required')
        mask = validate_mask(self.state_valid_mask)
        if any(not valid and (self.state16[i] != 0 or self.state_timestamps_ns[i] != 0) for i, valid in enumerate(mask)):
            raise ValueError('Absent hardware must use explicit zero placeholder and no measurement timestamp')
        if len(self.state_timestamps_ns) != 16 or any((t <= 0 or t > self.available_monotonic_ns) if valid else t != 0 for t, valid in zip(self.state_timestamps_ns, mask)):
            raise ValueError('invalid measured-state timestamps')
        if not 0 <= self.capture_started_monotonic_ns <= self.available_monotonic_ns:
            raise ValueError('invalid acquisition interval')
        if any(not self.capture_started_monotonic_ns <= f.host_received_monotonic_ns <= self.available_monotonic_ns for f in self.frames):
            raise ValueError('camera receipt outside acquisition interval')

    @property
    def retained_bytes(self):
        return sum(len(f.payload) for f in self.frames) + 4096

    def metadata(self):
        metadata = dict(observation_id=self.observation_id,
                    capture_started_monotonic_ns=self.capture_started_monotonic_ns,
                    available_monotonic_ns=self.available_monotonic_ns,
                    state16=list(self.state16), state_timestamps_ns=list(self.state_timestamps_ns),
                    full22=list(self.full22), state_source=self.state_source,
                    frames=[f.metadata() for f in self.frames])
        if self.camera_identity_source is not None:
            metadata.update(camera_identity_source=self.camera_identity_source,
                camera_drop_counts=dict(zip(CAMERAS, self.camera_drop_counts)),
                camera_discontinuity=self.camera_discontinuity)
        if self.state_valid_mask is not None:
            metadata.update(state_valid_mask=list(self.state_valid_mask),
                absent_state_semantics='zero_storage_placeholder_not_measurement')
        return metadata


def input_issue(obs):
    try:
        vector(obs.state16, 16)
        if any(not 0 <= x <= 100 for x in obs.state16[14:]):
            return 'invalid_gripper_feedback'
    except (ValueError, AttributeError):
        return 'missing_or_nonfinite_state16'
    return None  # All-zero open grippers are valid F1 feedback.


def frames_advance(obs, previous):
    if previous is None:
        return True
    identity = getattr(obs, 'camera_identity_source', None)
    if identity != getattr(previous, 'camera_identity_source', None):
        return False
    return all(f.frame_id > p.frame_id
        and f.host_received_monotonic_ns > p.host_received_monotonic_ns
        and (f.timestamp.value >= p.timestamp.value if identity == CAMERA_IDENTITY
             else f.timestamp.value > p.timestamp.value)
        and f.timestamp.clock_domain == p.timestamp.clock_domain
        and f.timestamp.unit == p.timestamp.unit
        for f, p in zip(obs.frames, previous.frames))


# F1-infra StateStore uses profile camera keys, while policy/recording use
# the shared canonical three-camera names. Keep the translation at this edge.
CAMERA_STORE_KEYS = {'head':'head', 'left_wrist':'left', 'right_wrist':'right'}


def make_camera_store(base_class, *, max_items=32, max_bytes=64*1024**2, **kwargs):
    """Keep infra's latest view and add a bounded F1 callback archive.

    Queueing begins at first capture, after IK warmup/local Enter. Sequence IDs
    count real callbacks from transport startup; reads never advance them.
    """
    if type(max_items) is not int or type(max_bytes) is not int or min(max_items, max_bytes) <= 0:
        raise ValueError('positive integer camera queue capacities required')
    reverse = {value: key for key, value in CAMERA_STORE_KEYS.items()}

    class CallbackStateStore(base_class):
        def __init__(self, **options):
            super().__init__(**options)
            self.camera_queues = {name: deque() for name in CAMERAS}
            self.camera_callback_ids = dict.fromkeys(CAMERAS, -1)
            self.camera_queue_bytes = self.camera_queue_peak_bytes = 0
            self.camera_queue_peak_items = dict.fromkeys(CAMERAS, 0)
            self.camera_queue_enabled = False
            self.camera_capture_closed = False
            self.camera_queue_error = None
            self.camera_drop_reasons = {name: {} for name in CAMERAS}
            self.camera_drop_ranges = {name: {} for name in CAMERAS}
            self.camera_repeated_stamps = dict.fromkeys(CAMERAS, 0)
            self.camera_last_callback = {}
            self.camera_last_host_callback = {}
            self.camera_last_clock_issues = {}
            self.camera_warmup_callbacks = dict.fromkeys(CAMERAS, 0)

        def start_camera_capture(self):
            with self.lock:
                if self.camera_capture_closed:
                    raise RuntimeError('camera capture is closed')
                self.camera_queue_enabled = True

        def stop_camera_capture(self):
            with self.lock:
                self.camera_capture_closed = True
                self.camera_queue_enabled = False

        def note_camera_drop(self, camera, sequence, reason):
            counts = self.camera_drop_reasons[camera]
            counts[reason] = counts.get(reason, 0)+1
            bounds = self.camera_drop_ranges[camera].setdefault(reason, [sequence, sequence])
            bounds[1] = sequence

        def pop_camera(self, camera, *, reason=None):
            row = self.camera_queues[camera].popleft()
            self.camera_queue_bytes -= len(row['data'])
            if reason:
                self.note_camera_drop(camera, row['callback_sequence'], reason)
            return row

        def update_camera(self, name, msg):
            camera = reverse.get(name)
            if camera is None:
                return super().update_camera(name, msg)
            with self.lock:
                self.camera_callback_ids[camera] += 1
                sequence = self.camera_callback_ids[camera]
                old = self.cameras.get(name)
                super().update_camera(name, msg)
                row = self.cameras.get(name)
                if not self.camera_queue_enabled:
                    self.camera_warmup_callbacks[camera] += 1
                    return
                if row is old:
                    self.note_camera_drop(camera, sequence, 'invalid_payload')
                    self.camera_queue_error = 'invalid camera payload: ' + camera
                    return
                if self.camera_queue_error:
                    self.note_camera_drop(camera, sequence, 'error_latched')
                    return
                received = round(row['received_at']*1e9)
                stamp = row.get('source_stamp')
                value = None if not stamp else stamp['sec']*1_000_000_000+stamp['nanosec']
                previous = self.camera_last_callback.get(camera)
                previous_host = self.camera_last_host_callback.get(camera)
                clock_issue = ('regressed_host_receipt' if previous_host and received <= previous_host[0] else
                               'invalid_source_stamp' if value is None or value <= 0 else
                               'regressed_source_stamp' if previous and value < previous[1] else None)
                if clock_issue:
                    self.note_camera_drop(camera, sequence, clock_issue)
                    # Bounded evidence: one last anomaly per camera, including
                    # both the accepted source baseline and the last real host
                    # callback. A rejected header must not hide host regression.
                    self.camera_last_clock_issues[camera] = dict(reason=clock_issue,
                        callback_sequence=sequence,
                        previous_callback_sequence=None if previous_host is None else previous_host[1],
                        previous_accepted_callback_sequence=None if previous is None else previous[2],
                        previous_host_received_monotonic_ns=None if previous_host is None else previous_host[0],
                        host_received_monotonic_ns=received,
                        previous_accepted_source_stamp_ns=None if previous is None else previous[1],
                        source_stamp_ns=value)
                    if clock_issue == 'regressed_source_stamp':
                        # Discard only this demonstrably out-of-order header.
                        # Keep the accepted source baseline unchanged; a new
                        # usable triple must still arrive within capture's 2 s.
                        self.camera_last_host_callback[camera] = (received, sequence)
                    else:
                        self.camera_queue_error = 'camera callback clock invalid/regressed: ' + camera + ' (' + clock_issue + ')'
                    return
                self.camera_last_host_callback[camera] = (received, sequence)
                if previous and value == previous[1]:
                    self.camera_repeated_stamps[camera] += 1
                self.camera_last_callback[camera] = (received, value, sequence)
                reason = ('overflow_items' if len(self.camera_queues[camera]) >= max_items else
                          'overflow_bytes' if self.camera_queue_bytes+len(row['data']) > max_bytes else None)
                if reason:
                    self.note_camera_drop(camera, sequence, reason)
                    self.camera_queue_error = f'camera callback queue {reason}: {camera} callback={sequence}'
                    return
                queued = dict(row, callback_sequence=sequence, host_received_monotonic_ns=received)
                self.camera_queues[camera].append(queued)
                self.camera_queue_bytes += len(row['data'])
                self.camera_queue_peak_bytes = max(self.camera_queue_peak_bytes, self.camera_queue_bytes)
                self.camera_queue_peak_items[camera] = max(self.camera_queue_peak_items[camera], len(self.camera_queues[camera]))

        def camera_diagnostics(self):
            with self.lock:
                return dict(camera_identity_source=CAMERA_IDENTITY,
                    enabled=self.camera_queue_enabled, max_items_per_camera=max_items, max_bytes=max_bytes,
                    pending_items={name: len(queue) for name, queue in self.camera_queues.items()},
                    pending_bytes=self.camera_queue_bytes, peak_bytes=self.camera_queue_peak_bytes,
                    peak_items=dict(self.camera_queue_peak_items), error=self.camera_queue_error,
                    last_callback_ids=dict(self.camera_callback_ids), warmup_callbacks=dict(self.camera_warmup_callbacks),
                    repeated_header_stamps=dict(self.camera_repeated_stamps),
                    last_clock_issues={name: dict(issue) for name, issue in self.camera_last_clock_issues.items()},
                    drop_counts={name: sum(counts.values()) for name, counts in self.camera_drop_reasons.items()},
                    drop_reasons={name: dict(counts) for name, counts in self.camera_drop_reasons.items()},
                    drop_ranges={name: {reason: list(bounds) for reason, bounds in reasons.items()}
                                 for name, reasons in self.camera_drop_ranges.items()})

    return CallbackStateStore(**kwargs)


class RosObservationSource:
    def __init__(self, store, profile, config):
        self.store, self.profile, self.config = store, profile, config
        self.state_valid_mask = state_mask(config)
        self.stop_event = threading.Event()
        self.sequence = 0
        self.last_stamps = {}
        self.last_received = {}
        self.last_capture_started = None
        self.frame_ids = {c: -1 for c in CAMERAS}
        self.callback_identity = hasattr(store, 'camera_queues')
        self.queue_started = False
        self.last_drop_counts = (0, 0, 0)

    def feedback(self):
        with self.store.lock:
            now = self.store.clock()
            if not self.store.connected or self.store.error:
                raise RuntimeError('F1 feedback disconnected: ' + str(self.store.error))
            rows = [dict(self.store.joints.get(name, {})) for name in FULL_NAMES]
            grippers = [dict(self.store.grippers.get(side, {})) for side in ('left', 'right')]
        if not self.state_valid_mask[14]:
            # Ignore cached driver values entirely; never forge a hardware receipt.
            grippers[0] = dict(position=0., received_at=0.)
        for row in rows + [r for r, valid in zip(grippers, self.state_valid_mask[14:]) if valid]:
            if not row or not 0 <= now - row['received_at'] <= self.profile['feedback_max_age_s']:
                raise FeedbackUnavailable('missing/stale F1 full-body or gripper feedback')
        full = vector([r['position'] for r in rows], 22)
        state = vector(list(full[:14]) + [r['position'] for r in grippers], 16)
        if any(not 0 <= v <= 100 for v in state[14:]):
            raise RuntimeError('invalid gripper feedback')
        stamps = tuple(round(r['received_at'] * 1e9) for r in rows[:14] + grippers)
        return full, state, stamps

    def stop_capture(self):
        self.stop_event.set()
        if self.callback_identity:
            self.store.stop_camera_capture()

    def diagnostics(self):
        result = self.store.camera_diagnostics() if self.callback_identity else dict(camera_identity_source=None)
        return dict(result, observations=self.sequence, last_consumed_callback_ids=dict(self.frame_ids))

    def queued_rows(self, now, started):
        with self.store.lock:
            if self.store.camera_queue_error:
                raise RuntimeError(self.store.camera_queue_error)
            while True:
                for name in CAMERAS:
                    queue = self.store.camera_queues[name]
                    while queue:
                        stamp = queue[0]['host_received_monotonic_ns']
                        reason = ('before_initial_capture' if self.sequence == 0 and stamp < started else
                                  'age' if now-stamp > self.config['camera_max_age_s']*1e9 else None)
                        if reason is None:
                            break
                        self.store.pop_camera(name, reason=reason)
                if any(not self.store.camera_queues[name] for name in CAMERAS):
                    return None
                received = [self.store.camera_queues[name][0]['host_received_monotonic_ns'] for name in CAMERAS]
                if max(received) > now:
                    return None
                if max(received)-min(received) > self.config['camera_max_skew_s']*1e9:
                    self.store.pop_camera(CAMERAS[received.index(min(received))], reason='skew')
                    continue
                rows = [self.store.pop_camera(name) for name in CAMERAS]
                counts = tuple(sum(self.store.camera_drop_reasons[name].values()) for name in CAMERAS)
                return rows, counts

    def capture(self, started_ns=None):
        started = time.monotonic_ns() if started_ns is None else started_ns
        deadline = started + 2_000_000_000
        drop_counts = None
        if self.callback_identity and not self.queue_started:
            self.store.start_camera_capture()
            self.queue_started = True
        while not self.stop_event.is_set():
            now = time.monotonic_ns()
            if self.callback_identity:
                selected = self.queued_rows(now, started)
                rows, drop_counts = selected if selected is not None else ([{}]*3, None)
            else:
                with self.store.lock:
                    rows = [dict(self.store.cameras.get(CAMERA_STORE_KEYS[name], {})) for name in CAMERAS]
            received = [round(r.get('received_at', 0) * 1e9) for r in rows]
            # A callback just before this polling cycle is still a new frame.
            # Only the first capture waits for post-call receipts; subsequent
            # captures require strictly unconsumed receipts and source stamps.
            fresh = all(self.last_received.get(name, started-1) < t <= now
                        and now - t <= self.config['camera_max_age_s'] * 1e9
                        for name, t in zip(CAMERAS, received))
            if fresh and max(received) - min(received) <= self.config['camera_max_skew_s'] * 1e9:
                stamps = []
                for name, row in zip(CAMERAS, rows):
                    stamp = row.get('source_stamp')
                    if not stamp:
                        raise CameraUnavailable('camera source timestamp missing: ' + name)
                    value = stamp['sec'] * 1_000_000_000 + stamp['nanosec']
                    if value <= 0 or value < self.last_stamps.get(name, -1):
                        # Clock identity cannot be repaired by forgetting its
                        # previous value. Keep the evidence and latch supervision.
                        raise RuntimeError('camera source clock invalid/regressed: ' + name)
                    stamps.append(value)
                if self.callback_identity:
                    if any(row['callback_sequence'] <= self.frame_ids[name] for name, row in zip(CAMERAS, rows)):
                        raise CameraUnavailable('camera callback identity did not advance')
                    break
                if all(value > self.last_stamps.get(name, -1) for name, value in zip(CAMERAS, stamps)):
                    break
            if now > deadline:
                with self.store.lock:
                    stale = [name for name in CAMERAS
                             if now-round(self.store.cameras.get(CAMERA_STORE_KEYS[name], {}).get(
                                 'received_at', 0)*1e9) > self.config['camera_max_age_s']*1e9]
                raise CameraUnavailable('three new synchronized camera frames unavailable; stale=' +
                                        (','.join(stale) or 'pairing'))
            self.stop_event.wait(.005)
        else:
            raise RuntimeError('observation source closed')
        from PIL import Image
        frames = []
        for name, row, received_ns, stamp in zip(CAMERAS, rows, received, stamps):
            try:
                with Image.open(BytesIO(row['data'])) as im:
                    width, height = im.size
                    im.verify()
            except (OSError, ValueError) as exc:
                raise CameraUnavailable('camera frame decode failed: ' + name) from exc
            frame_id = row['callback_sequence'] if self.callback_identity else self.frame_ids[name]+1
            frames.append(TimestampedFrame(name, frame_id, DeviceTimestamp(stamp, 'ns', 'f1_ros_header_unverified'),
                received_ns, row['data'], row['mime'], width, height))
        full, state, state_times = self.feedback()
        # Preserve the actual receipt interval. Never relabel an already
        # arrived image as post-START/handoff: the policy's existing minimum
        # observation boundary rejects any interval that began before it.
        begin = min(started, min(received))
        if self.last_capture_started is not None and begin <= self.last_capture_started:
            raise RuntimeError('F1 acquisition interval regressed')
        discontinuity = self.callback_identity and (
            any(new > old for new, old in zip(drop_counts, self.last_drop_counts))
            or (self.sequence > 0 and any(frame.frame_id != self.frame_ids[frame.camera]+1 for frame in frames)))
        obs = Observation(self.sequence, begin, time.monotonic_ns(), tuple(frames), tuple(state), state_times, tuple(full),
            camera_identity_source=CAMERA_IDENTITY if self.callback_identity else None,
            camera_drop_counts=drop_counts, camera_discontinuity=bool(discontinuity),
            state_valid_mask=self.state_valid_mask if not all(self.state_valid_mask) else None)
        self.last_capture_started = begin
        self.last_stamps.update(zip(CAMERAS, stamps))
        self.last_received.update(zip(CAMERAS, received))
        self.frame_ids.update({frame.camera: frame.frame_id for frame in frames})
        if drop_counts is not None:
            self.last_drop_counts = drop_counts
        self.sequence += 1
        return obs
