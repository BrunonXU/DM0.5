"""Causal, per-DDS-writer absolute gripper command hold; never reads feedback."""
from collections import deque
import math
import threading


class GripperCommandHistory:
    def __init__(self):
        self.lock = threading.RLock()
        self.writers = {'left': None, 'right': None}
        self.history = {side: deque(maxlen=8192) for side in self.writers}
        self.epochs = {side: 0 for side in self.writers}
        self.last_clock = {side: None for side in self.writers}
        self.last_observed = {side: None for side in self.writers}
        self.reasons = {side: 'waiting for unique DDS writer and first actual command' for side in self.writers}

    def _clear(self, side, reason):
        self.epochs[side] += 1
        self.history[side].clear()
        self.last_observed[side] = None
        self.reasons[side] = reason

    def update_writers(self, side, gids, checked_ros_ns):
        with self.lock:
            valid = [bytes(gid) for gid in gids if gid and any(gid)]
            writer = valid[0] if len(valid) == len(gids) == 1 else None
            previous = self.last_clock[side]
            if previous is not None and checked_ros_ns < previous:
                self._clear(side, 'ROS clock moved backwards')
            if writer != self.writers[side]:
                self._clear(side, 'DDS writer changed, disappeared, or is ambiguous')
            self.last_clock[side] = checked_ros_ns
            self.writers[side] = writer
            if writer is None:
                self._clear(side, 'no unique identifiable DDS writer')
            return bool(writer)

    def observe(self, side, position, stamp_ns, publisher_gid):
        with self.lock:
            gid = bytes(publisher_gid or [])
            if not gid or not any(gid) or gid != self.writers[side]:
                self._clear(side, 'message DDS writer does not match unique graph writer')
                raise ValueError(self.reasons[side])
            if isinstance(position, bool) or not math.isfinite(position) or not 0 <= position <= 100:
                self._clear(side, 'invalid absolute gripper command')
                raise ValueError(self.reasons[side])
            previous = self.last_observed[side]
            if stamp_ns <= 0 or (previous is not None and stamp_ns < previous):
                self._clear(side, 'command receipt clock moved backwards')
                raise ValueError(self.reasons[side])
            self.history[side].append((int(stamp_ns), float(position)))
            self.last_observed[side] = stamp_ns
            self.reasons[side] = None

    def at(self, side, reference_ns):
        with self.lock:
            if self.writers[side] is None:
                raise ValueError('no unique DDS writer for ' + side + ' gripper')
            for stamp, value in reversed(self.history[side]):
                if stamp <= reference_ns:
                    age = reference_ns - stamp
                    return {'value': value, 'stamp_ns': stamp, 'age_ns': age,
                            'held': age > 50_000_000, 'writer_gid': self.writers[side].hex(), 'epoch': self.epochs[side]}
            raise ValueError('no causal actual command in current DDS writer epoch: ' + side)
