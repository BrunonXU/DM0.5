"""Read already-owned GDK camera/state adapters, never create a control owner."""
import time
import numpy as np
from .recording_observation import CAMERAS, DeviceTimestamp, TimestampedFrame, TimestampedObservation


class TimestampedG2Source:
    def __init__(self, camera_rig, state_reader, *, clock=time.monotonic_ns):
        self.rig, self.state_reader, self.clock = camera_rig, state_reader, clock
        self.observation_id = 0
        self.previous_timestamps = {}
        self.frame_ids = {name: -1 for name in CAMERAS}

    def capture(self, started_ns=None):
        started = self.clock() if started_ns is None else started_ns
        frames = []
        for name in CAMERAS:
            message = self.rig._read_message(name)
            received = self.clock()
            # Reuse native passthrough on the SAME message; never reacquire.
            encoded = self.rig._encoded_from_message(message, name)
            width, height = encoded.width, encoded.height
            if not width or not height:
                rgb = self.rig._decode_rgb(message, name)
                height, width = rgb.shape[:2]
            stamp = message.timestamp_ns
            if self.previous_timestamps.get(name) != stamp:
                self.frame_ids[name] += 1
            self.previous_timestamps[name] = stamp
            frames.append(TimestampedFrame(name, self.frame_ids[name],
                DeviceTimestamp(stamp, "ns", "gdk_device_unverified"), received,
                encoded.data, encoded.media_type, width, height))
        state = np.asarray(self.state_reader.read(), dtype=float)
        state_time = self.clock()
        if state.shape != (34,) or not np.isfinite(state).all():
            raise ValueError("state must be raw finite 34D; no fill/fallback")
        obs = TimestampedObservation(self.observation_id, started, self.clock(), tuple(frames),
            state_time, tuple(state), (True,) * 14 + (False,) * 20,
            "gdk_reported_positions; hand_continuous_feedback_unverified")
        self.observation_id += 1
        return obs


def policy_input_issue(observation):
    """Zero hand positions are ambiguous, not a certified physical open pose."""
    state = np.asarray(observation.measured_34d, dtype=float)
    if state.shape != (34,) or not np.isfinite(state).all():
        return "missing_or_nonfinite_state"
    if np.all(state[14:24] == 0) or np.all(state[24:34] == 0):
        return "zero_hand_state_ambiguous_no_command_fallback"
    return None


def frames_advance(observation, previous):
    if previous is None:
        return True
    return all((f.timestamp.unit, f.timestamp.clock_domain) == (p.timestamp.unit, p.timestamp.clock_domain)
               and f.timestamp.value > p.timestamp.value
               for f, p in zip(observation.frames, previous.frames))
