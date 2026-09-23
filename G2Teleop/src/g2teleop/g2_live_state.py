"""Strict read-only G2 kinematic snapshots for Live IK Shadow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

import numpy as np


WAIST_JOINT_NAMES = tuple(f"idx0{i}_body_joint{i}" for i in range(1, 6))
LEFT_ARM_JOINT_NAMES = tuple(f"idx2{i}_arm_l_joint{i}" for i in range(1, 8))
RIGHT_ARM_JOINT_NAMES = tuple(f"idx6{i}_arm_r_joint{i}" for i in range(1, 8))


class LiveStateError(ValueError):
    pass


@dataclass(frozen=True)
class KinematicSnapshot:
    sequence: int
    sampled_at_monotonic: float
    waist: np.ndarray
    arms: np.ndarray

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise LiveStateError("snapshot sequence must be non-negative")
        if not math.isfinite(self.sampled_at_monotonic):
            raise LiveStateError("snapshot time must be finite")
        waist = np.asarray(self.waist, dtype=np.float64)
        arms = np.asarray(self.arms, dtype=np.float64)
        if waist.shape != (5,) or not np.isfinite(waist).all():
            raise LiveStateError("waist must be finite 5D")
        if arms.shape != (14,) or not np.isfinite(arms).all():
            raise LiveStateError("arms must be finite 14D")
        object.__setattr__(self, "waist", waist.copy())
        object.__setattr__(self, "arms", arms.copy())

    def age_seconds(self, now: float) -> float:
        return max(0.0, float(now) - self.sampled_at_monotonic)


def _position(entry: Mapping[str, Any], name: str) -> float:
    for key in ("motor_position", "position"):
        if key in entry and entry[key] is not None:
            value = float(entry[key])
            if not math.isfinite(value):
                raise LiveStateError(f"joint {name} has non-finite {key}")
            return value
    raise LiveStateError(f"joint {name} has no measured position")


def parse_body_kinematics(
    payload: Any,
    *,
    sequence: int,
    sampled_at_monotonic: float,
) -> KinematicSnapshot:
    """Parse waist5 + arms14 from one ``get_joint_states`` payload."""

    if not isinstance(payload, Mapping):
        raise LiveStateError("get_joint_states returned a non-mapping payload")
    entries = payload.get("states")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise LiveStateError("get_joint_states payload has no states sequence")
    mapped: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise LiveStateError("body state contains a non-mapping entry")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise LiveStateError("body state contains an unnamed joint")
        if name in mapped:
            raise LiveStateError(f"body state contains duplicate joint {name}")
        mapped[name] = entry
    expected = WAIST_JOINT_NAMES + LEFT_ARM_JOINT_NAMES + RIGHT_ARM_JOINT_NAMES
    missing = [name for name in expected if name not in mapped]
    if missing:
        raise LiveStateError(f"body state missing required joints: {missing}")
    values = {name: _position(mapped[name], name) for name in expected}
    return KinematicSnapshot(
        sequence=sequence,
        sampled_at_monotonic=sampled_at_monotonic,
        waist=np.asarray([values[name] for name in WAIST_JOINT_NAMES]),
        arms=np.asarray(
            [values[name] for name in LEFT_ARM_JOINT_NAMES + RIGHT_ARM_JOINT_NAMES]
        ),
    )

