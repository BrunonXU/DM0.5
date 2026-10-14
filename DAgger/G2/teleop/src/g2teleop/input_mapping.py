"""Deterministic and interlocked HEBI input mapping.

This module deliberately emits operator intent rather than G2 commands. It has
no dependency on GDK, kinematics, DM0.5, or robot hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .contracts import ActiveSide, IpadSample, TeleopIntent


class InputMappingError(ValueError):
    """The configured HEBI input contract is invalid."""


@dataclass(frozen=True)
class MappingConfig:
    deadman: str = "b1"
    select_left: str = "b2"
    select_right: str = "b3"
    grip_velocity: str = "a3"
    thumb_clearance: str = "b4"
    grip_deadband: float = 0.1

    def __post_init__(self) -> None:
        if not 0.0 <= self.grip_deadband < 1.0:
            raise InputMappingError("grip_deadband must be in [0, 1)")
        digital = {
            self.deadman,
            self.select_left,
            self.select_right,
            self.thumb_clearance,
        }
        if len(digital) != 4:
            raise InputMappingError("digital controls must use distinct channels")


class InputMapper:
    """Latch side selection only while the deadman is released."""

    def __init__(self, config: MappingConfig | None = None) -> None:
        self.config = config or MappingConfig()
        self._active_side: ActiveSide | None = None

    @property
    def active_side(self) -> ActiveSide | None:
        return self._active_side

    def map(self, sample: IpadSample) -> TeleopIntent:
        cfg = self.config
        deadman = bool(sample.buttons.get(cfg.deadman, False))
        select_left = bool(sample.buttons.get(cfg.select_left, False))
        select_right = bool(sample.buttons.get(cfg.select_right, False))
        warning: str | None = None

        if select_left and select_right:
            warning = "simultaneous_left_right_selection_ignored"
        elif not deadman:
            if select_left:
                self._active_side = ActiveSide.LEFT
            elif select_right:
                self._active_side = ActiveSide.RIGHT
        elif select_left or select_right:
            warning = "side_change_while_deadman_held_ignored"

        motion_enabled = deadman and self._active_side is not None
        if deadman and self._active_side is None:
            warning = "deadman_without_active_side"

        grip_velocity = float(sample.analogs.get(cfg.grip_velocity, 0.0))
        if not math.isfinite(grip_velocity):
            raise InputMappingError("grip velocity is not finite")
        if abs(grip_velocity) <= cfg.grip_deadband:
            grip_velocity = 0.0
        if not motion_enabled:
            grip_velocity = 0.0

        return TeleopIntent(
            active_side=self._active_side,
            motion_enabled=motion_enabled,
            grip_velocity_raw=grip_velocity,
            thumb_clearance=(
                bool(sample.buttons.get(cfg.thumb_clearance, False))
                if motion_enabled
                else False
            ),
            position_m=sample.position_m,
            quaternion_wxyz=sample.quaternion_wxyz,
            warning=warning,
        )

