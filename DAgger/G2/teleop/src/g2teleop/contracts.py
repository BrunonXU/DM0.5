"""Pure data contracts for the read-only iPad input stage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Mapping


class ActiveSide(str, Enum):
    LEFT = "left"
    RIGHT = "right"


def _finite_tuple(values: tuple[float, ...], size: int, label: str) -> None:
    if len(values) != size or not all(math.isfinite(value) for value in values):
        raise ValueError(f"{label} must contain {size} finite values")


@dataclass(frozen=True)
class IpadSample:
    """One validated HEBI Mobile I/O feedback sample.

    Quaternion order follows HEBI/ARKit transport: ``w, x, y, z``.
    """

    captured_at_monotonic: float
    position_m: tuple[float, float, float]
    quaternion_wxyz: tuple[float, float, float, float]
    analogs: Mapping[str, float]
    buttons: Mapping[str, bool]

    def __post_init__(self) -> None:
        if not math.isfinite(self.captured_at_monotonic):
            raise ValueError("captured_at_monotonic must be finite")
        _finite_tuple(self.position_m, 3, "position_m")
        _finite_tuple(self.quaternion_wxyz, 4, "quaternion_wxyz")
        norm_sq = sum(value * value for value in self.quaternion_wxyz)
        if norm_sq < 1e-12:
            raise ValueError("quaternion_wxyz must not be a zero quaternion")
        if any(not math.isfinite(float(value)) for value in self.analogs.values()):
            raise ValueError("analogs must be finite")


@dataclass(frozen=True)
class TeleopIntent:
    """Operator intent only; this is not a robot action."""

    active_side: ActiveSide | None
    motion_enabled: bool
    grip_velocity_raw: float
    thumb_clearance: bool
    position_m: tuple[float, float, float]
    quaternion_wxyz: tuple[float, float, float, float]
    warning: str | None = None

