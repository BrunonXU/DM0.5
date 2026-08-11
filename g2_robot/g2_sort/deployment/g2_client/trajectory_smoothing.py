"""Pure trajectory smoothing for DM0.5 arm chunks and 5 Hz O10 commands.

The smoother stays entirely in absolute joint-position space.  It minimizes
distance from the policy waypoints plus a discrete third-difference penalty,
pins the first and last waypoint exactly, and never modifies the 20 hand axes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np


ARM_DIM = 14
HAND_DIM = 20
HAND_HZ = 5.0


@dataclass(frozen=True)
class ArmSmoothingStats:
    jerk_weight: float
    delta_p95_rad: float
    max_delta_rad: float
    max_delta_joint_index: int | None


@dataclass(frozen=True)
class HandTrackingDecision:
    raw_target: np.ndarray
    guided_target: np.ndarray
    command: np.ndarray
    boundary_active: bool
    velocity_limit_hits: int
    acceleration_limit_hits: int
    direction_reversal_holds: int
    emergency_cap_hits: int


class CausalHandCommandTracker:
    """Stateful 5 Hz tracker used identically by replay and powered runtime."""

    def __init__(
        self,
        *,
        enabled: bool,
        boundary_blend_seconds: float = 0.4,
        max_velocity_rad_s: float = 2.0,
        max_acceleration_rad_s2: float = 10.0,
        max_delta_rad: float = 0.4,
        reversal_error_rad: float = 0.15,
    ) -> None:
        finite_positive = {
            "boundary_blend_seconds": boundary_blend_seconds,
            "max_velocity_rad_s": max_velocity_rad_s,
            "max_acceleration_rad_s2": max_acceleration_rad_s2,
            "max_delta_rad": max_delta_rad,
        }
        for name, value in finite_positive.items():
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not np.isfinite(reversal_error_rad) or reversal_error_rad < 0:
            raise ValueError("reversal_error_rad must be finite and non-negative")
        self.enabled = bool(enabled)
        self.boundary_blend_seconds = float(boundary_blend_seconds)
        self.max_velocity_rad_s = float(max_velocity_rad_s)
        self.max_acceleration_rad_s2 = float(max_acceleration_rad_s2)
        self.max_delta_rad = float(max_delta_rad)
        self.reversal_error_rad = float(reversal_error_rad)
        self._command: np.ndarray | None = None
        self._velocity = np.zeros(HAND_DIM, dtype=np.float64)
        self._last_step_at: float | None = None
        self._segment_started_at: float | None = None
        self._segment_anchor: np.ndarray | None = None

    def reset(self, command: np.ndarray | None = None) -> None:
        self._command = self._validate_optional(command)
        self._velocity.fill(0.0)
        self._last_step_at = None
        self._segment_started_at = None
        self._segment_anchor = None

    def begin_segment(self, *, started_at: float) -> None:
        if not np.isfinite(started_at):
            raise ValueError("segment start time must be finite")
        self._segment_started_at = float(started_at)
        self._segment_anchor = (
            None if self._command is None else self._command.copy()
        )
        self._velocity.fill(0.0)
        self._last_step_at = None

    def step(
        self,
        raw_target: np.ndarray,
        *,
        now: float,
    ) -> HandTrackingDecision:
        raw = self._validate(raw_target)
        if not np.isfinite(now):
            raise ValueError("hand command time must be finite")
        now = float(now)
        if not self.enabled or self._command is None:
            self._command = raw.copy()
            self._velocity.fill(0.0)
            self._last_step_at = now
            return HandTrackingDecision(
                raw_target=raw,
                guided_target=raw.copy(),
                command=raw.copy(),
                boundary_active=False,
                velocity_limit_hits=0,
                acceleration_limit_hits=0,
                direction_reversal_holds=0,
                emergency_cap_hits=0,
            )

        nominal_dt = 1.0 / HAND_HZ
        dt = (
            nominal_dt
            if self._last_step_at is None
            else now - self._last_step_at
        )
        dt = float(np.clip(dt, 0.5 * nominal_dt, 1.5 * nominal_dt))
        guided = raw.copy()
        boundary_active = False
        if self._segment_anchor is not None and self._segment_started_at is not None:
            elapsed = max(0.0, now - self._segment_started_at)
            if elapsed < self.boundary_blend_seconds:
                u = float(np.clip(elapsed / self.boundary_blend_seconds, 0.0, 1.0))
                alpha = u * u * (3.0 - 2.0 * u)
                guided = self._segment_anchor + alpha * (
                    raw - self._segment_anchor
                )
                boundary_active = True

        error = guided - self._command
        unconstrained_velocity = error / dt
        desired_velocity = np.clip(
            unconstrained_velocity,
            -self.max_velocity_rad_s,
            self.max_velocity_rad_s,
        )
        velocity_hits = int(
            np.count_nonzero(
                np.abs(unconstrained_velocity) > self.max_velocity_rad_s
            )
        )
        reversing = (
            self._velocity * desired_velocity < 0
        ) & (np.abs(error) > self.reversal_error_rad)
        reversal_holds = int(np.count_nonzero(reversing))
        if reversal_holds:
            desired_velocity = desired_velocity.copy()
            desired_velocity[reversing] = 0.0

        velocity_delta = desired_velocity - self._velocity
        max_velocity_delta = self.max_acceleration_rad_s2 * dt
        acceleration_hits = int(
            np.count_nonzero(np.abs(velocity_delta) > max_velocity_delta)
        )
        next_velocity = self._velocity + np.clip(
            velocity_delta,
            -max_velocity_delta,
            max_velocity_delta,
        )
        command_step = next_velocity * dt
        overshoot = (
            (command_step * error >= 0)
            & (np.abs(command_step) > np.abs(error))
        )
        command_step[overshoot] = error[overshoot]
        cap_hits = int(np.count_nonzero(np.abs(command_step) > self.max_delta_rad))
        command_step = np.clip(
            command_step,
            -self.max_delta_rad,
            self.max_delta_rad,
        )
        command = self._command + command_step
        self._command = command.copy()
        self._velocity = command_step / dt
        self._last_step_at = now
        return HandTrackingDecision(
            raw_target=raw,
            guided_target=guided,
            command=command,
            boundary_active=boundary_active,
            velocity_limit_hits=velocity_hits,
            acceleration_limit_hits=acceleration_hits,
            direction_reversal_holds=reversal_holds,
            emergency_cap_hits=cap_hits,
        )

    @staticmethod
    def _validate(values: np.ndarray) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        if array.shape != (HAND_DIM,) or not np.all(np.isfinite(array)):
            raise ValueError("hand target must be a finite 20D vector")
        return array.copy()

    @classmethod
    def _validate_optional(cls, values: np.ndarray | None) -> np.ndarray | None:
        return None if values is None else cls._validate(values)


@lru_cache(maxsize=16)
def _fixed_endpoint_jerk_operator(
    waypoint_count: int,
    jerk_weight: float,
) -> np.ndarray:
    if waypoint_count < 4:
        raise ValueError("jerk smoothing requires at least four waypoints")
    if not np.isfinite(jerk_weight) or jerk_weight <= 0:
        raise ValueError("jerk_weight must be finite and positive")

    identity = np.eye(waypoint_count, dtype=np.float64)
    third_difference = np.diff(identity, n=3, axis=0)
    system = identity + jerk_weight * (
        third_difference.T @ third_difference
    )
    fixed = np.asarray([0, waypoint_count - 1])
    free = np.arange(1, waypoint_count - 1)

    # The unconstrained normal equation is system @ q = q_policy.  Eliminate
    # the two fixed endpoint variables once and cache the resulting linear map.
    rhs_map = identity[free].copy()
    rhs_map[:, fixed] -= system[np.ix_(free, fixed)]
    operator = np.zeros_like(identity)
    operator[fixed, fixed] = 1.0
    operator[free] = np.linalg.solve(system[np.ix_(free, free)], rhs_map)
    operator.setflags(write=False)
    return operator


def smooth_arm_action_chunk(
    actions: np.ndarray,
    *,
    jerk_weight: float,
) -> tuple[np.ndarray, ArmSmoothingStats]:
    """Return a smoothed copy and the arm-only policy deviation summary."""
    array = np.asarray(actions, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] < ARM_DIM:
        raise ValueError("actions must be an NxD array with at least 14 dimensions")
    if len(array) < 4 or not np.all(np.isfinite(array)):
        raise ValueError("actions must contain at least four finite waypoints")
    if not np.isfinite(jerk_weight) or jerk_weight < 0:
        raise ValueError("jerk_weight must be finite and non-negative")

    result = array.copy()
    result[:, :ARM_DIM] = smooth_fixed_endpoint_trajectory(
        array[:, :ARM_DIM], jerk_weight=jerk_weight
    )

    arm_delta = np.abs(result[:, :ARM_DIM] - array[:, :ARM_DIM])
    if np.any(arm_delta):
        flat_index = int(np.argmax(arm_delta))
        _, joint_index = np.unravel_index(flat_index, arm_delta.shape)
        max_joint: int | None = int(joint_index)
    else:
        max_joint = None
    return result, ArmSmoothingStats(
        jerk_weight=float(jerk_weight),
        delta_p95_rad=float(np.percentile(arm_delta, 95)),
        max_delta_rad=float(np.max(arm_delta)),
        max_delta_joint_index=max_joint,
    )


def smooth_fixed_endpoint_trajectory(
    values: np.ndarray,
    *,
    jerk_weight: float,
) -> np.ndarray:
    """Smooth every column of a generic trajectory while pinning both ends."""
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or len(array) < 4 or array.shape[1] == 0:
        raise ValueError("values must be an NxD trajectory with at least four rows")
    if not np.all(np.isfinite(array)):
        raise ValueError("trajectory values must all be finite")
    if not np.isfinite(jerk_weight) or jerk_weight < 0:
        raise ValueError("jerk_weight must be finite and non-negative")
    if jerk_weight == 0:
        return array.copy()
    operator = _fixed_endpoint_jerk_operator(len(array), float(jerk_weight))
    result = operator @ array
    result[0] = array[0]
    result[-1] = array[-1]
    return result
