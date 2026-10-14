"""G2 observation contract and GDK frame adapter."""
from dataclasses import dataclass, asdict
import math
from dagger_core.recording_observation import CAMERAS, DeviceTimestamp, TimestampedFrame

def frame_from_gdk_message(message, *, camera, frame_id, host_received_monotonic_ns,
                           media_type, clock_domain="gdk_device_unverified"):
    """Adapt an ALREADY acquired message; caller maps verified encoding enums.

    GDK 2.6.3 documents timestamp_ns in nanoseconds, but does not establish its
    relationship to host monotonic. Do not infer a clock domain from magnitude.
    """
    return TimestampedFrame(camera, frame_id,
        DeviceTimestamp(message.timestamp_ns, "ns", clock_domain),
        host_received_monotonic_ns, bytes(message.data), media_type,
        int(message.width), int(message.height))


@dataclass(frozen=True)
class TimestampedObservation:
    observation_id: int
    capture_started_monotonic_ns: int
    available_monotonic_ns: int
    frames: tuple[TimestampedFrame, ...]
    state_sample_monotonic_ns: int
    measured_34d: tuple[float | None, ...]
    measured_valid_34d: tuple[bool, ...]
    state_source: str

    def __post_init__(self):
        object.__setattr__(self, "frames", tuple(self.frames))
        object.__setattr__(self, "measured_34d", tuple(self.measured_34d))
        object.__setattr__(self, "measured_valid_34d", tuple(self.measured_valid_34d))
        if self.observation_id < 0 or not 0 <= self.capture_started_monotonic_ns <= self.available_monotonic_ns:
            raise ValueError("invalid observation identity/timing")
        if tuple(f.camera for f in self.frames) != CAMERAS:
            raise ValueError("three camera frames required in contract order")
        if any(not self.capture_started_monotonic_ns <= f.host_received_monotonic_ns <= self.available_monotonic_ns for f in self.frames):
            raise ValueError("frame receive outside acquisition interval")
        if not 0 <= self.state_sample_monotonic_ns <= self.available_monotonic_ns or not self.state_source or len(self.state_source) > 256:
            raise ValueError("invalid measured state provenance")
        if len(self.measured_34d) != 34 or len(self.measured_valid_34d) != 34:
            raise ValueError("strict 34D feedback and validity required")
        for value, valid in zip(self.measured_34d, self.measured_valid_34d):
            if type(valid) is not bool or (valid and (value is None or not math.isfinite(value))):
                raise ValueError("valid feedback must be finite measured data")
            if value is not None and not math.isfinite(value):
                raise ValueError("use None and invalid mask for missing feedback")

    @property
    def retained_bytes(self):
        # Payload plus conservative fixed metadata allowance; also bound item count.
        return sum(len(f.payload) for f in self.frames) + 4096

    def metadata(self):
        return {"observation_id": self.observation_id,
                "capture_started_monotonic_ns": self.capture_started_monotonic_ns,
                "available_monotonic_ns": self.available_monotonic_ns,
                "state_sample_monotonic_ns": self.state_sample_monotonic_ns,
                "state_source": self.state_source, "measured_34d": self.measured_34d,
                "measured_valid_34d": self.measured_valid_34d,
                "frames": [f.metadata() for f in self.frames]}


class SharedObservationStream:
    """Called by one acquisition worker even when inference is paused in HIL.

    Both consumers receive the same immutable object. The control owner supplies
    measured state through the capture callback; this class never reads Robot.
    """
    def __init__(self, capture, sink):
        self.capture = capture
        self.sink = sink
        self.latest = None

    def acquire(self):
        observation = self.capture()
        if not isinstance(observation, TimestampedObservation):
            raise TypeError("capture must return TimestampedObservation")
        self.latest = observation
        self.sink.submit(observation)
        return observation

    def policy_observation(self):
        return self.latest
