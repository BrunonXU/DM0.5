"""Control-owned timestamped 34D state ring shared with the policy process.

The control process is the only writer and the only process allowed to touch
the Robot state reader.  The policy process reads immutable snapshots by
sequence number after capturing its cameras.  Images never enter this shared
memory region.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass

import numpy as np

from .contracts import ACTION_DIM, ObservationError


@dataclass(frozen=True)
class StateSnapshot:
    sequence: int
    sampled_at: float
    state: np.ndarray

    def age_seconds(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else float(now)
        return max(0.0, current - self.sampled_at)


class SharedStateRingBuffer:
    """Small spawn-safe, single-writer 34D ring buffer.

    A process-shared lock makes the timestamp, sequence and 34 values one
    atomic logical record.  State traffic is tiny, so a clear correctness
    boundary is preferable to a lock-free protocol with harder failure modes.
    """

    def __init__(
        self,
        *,
        context: mp.context.BaseContext,
        capacity: int = 8,
    ) -> None:
        if capacity < 2:
            raise ValueError("state ring capacity must be at least 2")
        self.capacity = int(capacity)
        self._states = context.RawArray("d", self.capacity * ACTION_DIM)
        self._sampled_at = context.RawArray("d", self.capacity)
        self._sequences = context.RawArray("q", self.capacity)
        self._latest_sequence = context.RawValue("q", -1)
        self._lock = context.Lock()
        for index in range(self.capacity):
            self._sequences[index] = -1

    def publish(
        self,
        state: np.ndarray,
        *,
        sampled_at: float | None = None,
    ) -> StateSnapshot:
        values = np.asarray(state, dtype=np.float64)
        if values.shape != (ACTION_DIM,) or not np.all(np.isfinite(values)):
            raise ObservationError(
                f"state ring write must be finite ({ACTION_DIM},), "
                f"got {values.shape}"
            )
        timestamp = time.monotonic() if sampled_at is None else float(sampled_at)
        if not np.isfinite(timestamp) or timestamp < 0:
            raise ObservationError("state ring timestamp must be finite and non-negative")
        with self._lock:
            sequence = int(self._latest_sequence.value) + 1
            slot = sequence % self.capacity
            state_matrix = np.frombuffer(
                self._states,
                dtype=np.float64,
            ).reshape(self.capacity, ACTION_DIM)
            state_matrix[slot] = values
            self._sampled_at[slot] = timestamp
            # Publish the slot sequence last, then advance the global pointer.
            self._sequences[slot] = sequence
            self._latest_sequence.value = sequence
        return StateSnapshot(sequence, timestamp, values.copy())

    def read(
        self,
        sequence: int | None = None,
        *,
        max_age_seconds: float | None = None,
        now: float | None = None,
    ) -> StateSnapshot:
        if max_age_seconds is not None and max_age_seconds <= 0:
            raise ValueError("max state age must be positive")
        with self._lock:
            latest = int(self._latest_sequence.value)
            requested = latest if sequence is None else int(sequence)
            if requested < 0:
                raise ObservationError("state ring has no published snapshot")
            if latest - requested >= self.capacity:
                raise ObservationError(
                    f"state ring sequence {requested} was overwritten; latest={latest}"
                )
            slot = requested % self.capacity
            if int(self._sequences[slot]) != requested:
                raise ObservationError(
                    f"state ring sequence {requested} is unavailable"
                )
            timestamp = float(self._sampled_at[slot])
            state_matrix = np.frombuffer(
                self._states,
                dtype=np.float64,
            ).reshape(self.capacity, ACTION_DIM)
            values = state_matrix[slot].copy()
        snapshot = StateSnapshot(requested, timestamp, values)
        if (
            max_age_seconds is not None
            and snapshot.age_seconds(now=now) > max_age_seconds
        ):
            raise ObservationError(
                "state ring snapshot is stale: "
                f"age={snapshot.age_seconds(now=now):.6f}s > "
                f"{max_age_seconds:.6f}s"
            )
        return snapshot

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return int(self._latest_sequence.value)
