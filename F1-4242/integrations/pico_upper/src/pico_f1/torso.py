"""Relative headset-pitch mapping for the F1 ``body_j3`` torso motor."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from pico_f1.chassis import R_XR_TO_F1, _quaternion_matrix_xyzw


def headset_pitch_f1(pose: Iterable[float]) -> float:
    """Return headset forward pitch in the F1 base-axis convention, radians."""
    value = np.asarray(list(pose), dtype=float)
    if value.shape != (7,) or not np.all(np.isfinite(value)):
        raise ValueError("headset pose must contain seven finite values")
    rotation = R_XR_TO_F1 @ _quaternion_matrix_xyzw(value[3:7]) @ R_XR_TO_F1.T
    return math.atan2(
        -float(rotation[2, 0]),
        math.hypot(float(rotation[0, 0]), float(rotation[1, 0])),
    )


class TorsoPitchMapper:
    """Map relative human bow angle onto a session-relative ``body_j3`` target.

    Every hold begins at the live robot feedback and current headset pitch, so
    pressing A cannot recall a target from an earlier activation or reset.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.enabled = False
        self._headset_reference: float | None = None
        self._robot_reference_deg: float | None = None
        self._last_pitch: float | None = None
        self._last_timestamp_ns: int | None = None

    def update(
        self,
        headset_pose: Iterable[float],
        robot_position_deg: float,
        enabled: bool,
        timestamp_ns: int,
    ) -> float | None:
        if not enabled:
            self.deactivate()
            return None
        robot_position_deg = float(robot_position_deg)
        timestamp_ns = int(timestamp_ns)
        if not math.isfinite(robot_position_deg):
            raise ValueError("body_j3 feedback must be finite")
        if timestamp_ns <= 0:
            raise ValueError("headset timestamp is invalid")
        pitch = headset_pitch_f1(headset_pose)

        if not self.enabled:
            lower = float(self.config["min_position_deg"])
            upper = float(self.config["max_position_deg"])
            if not lower <= robot_position_deg <= upper:
                raise ValueError(
                    f"body_j3 feedback {robot_position_deg:.3f}deg is outside "
                    f"configured limits [{lower:.3f}, {upper:.3f}]deg"
                )
            self.enabled = True
            self._headset_reference = pitch
            self._robot_reference_deg = robot_position_deg
            self._last_pitch = pitch
            self._last_timestamp_ns = timestamp_ns
            return robot_position_deg

        if self._last_timestamp_ns is None or self._last_pitch is None:
            raise RuntimeError("torso mapper reference state is incomplete")
        dt = (timestamp_ns - self._last_timestamp_ns) * 1e-9
        if dt <= 1e-4 or dt > float(self.config["input_timeout_s"]):
            raise ValueError(f"headset timestamp discontinuity dt={dt:.6f}s")
        pitch_step = pitch - self._last_pitch
        if abs(pitch_step) > math.radians(float(self.config["max_input_step_deg"])):
            raise ValueError("headset pitch discontinuity")
        self._last_pitch = pitch
        self._last_timestamp_ns = timestamp_ns

        relative_deg = math.degrees(pitch - float(self._headset_reference))
        relative_deg *= float(self.config["pitch_scale"])
        relative_deg = float(
            np.clip(
                relative_deg,
                -float(self.config["max_relative_delta_deg"]),
                float(self.config["max_relative_delta_deg"]),
            )
        )
        target = float(self._robot_reference_deg) + relative_deg
        return float(
            np.clip(
                target,
                float(self.config["min_position_deg"]),
                float(self.config["max_position_deg"]),
            )
        )

    def deactivate(self) -> None:
        self.enabled = False
        self._headset_reference = None
        self._robot_reference_deg = None
        self._last_pitch = None
        self._last_timestamp_ns = None

    recenter = deactivate
