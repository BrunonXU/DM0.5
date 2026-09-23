"""Deterministic admission gate for three independent 30 Hz camera streams."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence


@dataclass(frozen=True)
class CameraFrameStamp:
    sequence: int
    integrity_timestamp_ns: int
    sync_timestamp_ns: int


@dataclass(frozen=True)
class CameraSyncDecision:
    ready: bool = False
    fatal: bool = False
    reason: str = ""
    source_gaps: tuple = ()


class CameraSyncGate:
    """Consume exactly one fresh frame from every camera per dataset row.

    Cross-camera synchronization uses the caller's common-domain acquisition
    timestamps. Per-camera raw timestamps detect duplicates and frame gaps.
    """

    def __init__(
        self,
        keys: Sequence[str],
        max_sync_skew_s: float,
        max_source_gap_s: float,
        allow_source_gaps: bool = False,
    ) -> None:
        self.keys = tuple(keys)
        if not self.keys or len(set(self.keys)) != len(self.keys):
            raise ValueError("camera keys must be non-empty and unique")
        for value, label in (
            (max_sync_skew_s, "max_sync_skew_s"),
            (max_source_gap_s, "max_source_gap_s"),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{label} must be positive and finite")
        self.max_sync_skew_ns = int(max_sync_skew_s * 1e9)
        self.max_source_gap_ns = int(max_source_gap_s * 1e9)
        self.allow_source_gaps = bool(allow_source_gaps)
        self._last_sequence = {key: -1 for key in self.keys}
        self._last_integrity_timestamp_ns: dict[str, int] = {}

    def arm(self, frames: Mapping[str, CameraFrameStamp]) -> None:
        """Start after the current images so pre-trigger frames are excluded."""
        missing = [key for key in self.keys if key not in frames]
        if missing:
            raise ValueError(f"cannot arm with missing cameras: {missing}")
        self._last_sequence = {
            key: int(frames[key].sequence) for key in self.keys
        }
        self._last_integrity_timestamp_ns.clear()

    def evaluate(
        self, frames: Mapping[str, CameraFrameStamp],
        extras: Mapping[str, Sequence[CameraFrameStamp]] | None = None,
    ) -> CameraSyncDecision:
        missing = [key for key in self.keys if key not in frames]
        if missing:
            return CameraSyncDecision(reason=f"waiting for cameras: {missing}")
        if any(
            int(frames[key].sequence) <= self._last_sequence[key]
            for key in self.keys
        ):
            return CameraSyncDecision(reason="waiting for one fresh frame per camera")

        stamps = [int(frames[key].sync_timestamp_ns) for key in self.keys]
        if max(stamps) - min(stamps) > self.max_sync_skew_ns:
            return CameraSyncDecision(reason="waiting for source-aligned camera set")

        source_gaps = []
        for key in self.keys:
            source = int(frames[key].integrity_timestamp_ns)
            aligned = int(frames[key].sync_timestamp_ns)
            if source <= 0 or aligned <= 0:
                return CameraSyncDecision(
                    fatal=True, reason=f"{key} has a non-positive timestamp"
                )
            previous = self._last_integrity_timestamp_ns.get(key)
            sequence = self._last_sequence[key]
            chain = list((extras or {}).get(key, ())) + [frames[key]]
            for sample in chain:
                if previous is None:
                    previous = sample.integrity_timestamp_ns
                    sequence = sample.sequence
                    continue
                gap = sample.integrity_timestamp_ns - previous
                if gap <= 0:
                    return CameraSyncDecision(
                        fatal=True,
                        reason=f"{key} integrity timestamp repeated or reversed",
                    )
                if gap > self.max_source_gap_ns:
                    if not self.allow_source_gaps:
                        return CameraSyncDecision(
                            fatal=True,
                            reason=f"{key} integrity frame gap {gap / 1e6:.3f}ms",
                        )
                    source_gaps.append({"camera": key, "before_ns": int(previous),
                                        "after_ns": int(sample.integrity_timestamp_ns),
                                        "gap_ns": int(gap)})
                if sample.sequence != sequence + 1:
                    return CameraSyncDecision(fatal=True, reason=f"{key} unaccounted frame sequence")
                previous = sample.integrity_timestamp_ns
                sequence = sample.sequence

        self._last_sequence = {
            key: int(frames[key].sequence) for key in self.keys
        }
        self._last_integrity_timestamp_ns = {
            key: int(frames[key].integrity_timestamp_ns) for key in self.keys
        }
        return CameraSyncDecision(ready=True, source_gaps=tuple(source_gaps))


def align_frame_queues(queues, max_skew_ns):
    """Advance obsolete FIFO heads; return them for lossless sidecar storage.

    Frame tuples use (JPEG, source_ns, ...). Selected heads stay in their queues
    until state history and the integrity gate have both approved the row.
    """
    extras = []
    while all(queues.values()):
        newest = max(queue[0][1] for queue in queues.values())
        obsolete = [key for key, queue in queues.items()
                    if queue[0][1] < newest - max_skew_ns]
        if not obsolete:
            return {key: queue[0] for key, queue in queues.items()}, extras
        for key in obsolete:
            extras.append((key, queues[key].popleft()))
    return None, extras
