"""Pure trajectory smoothing for DM0.5 arm chunks and sampled O10 commands.

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
HAND_V11_MAX_DELTA_RAD = 0.175
HAND_V11_MAX_VELOCITY_RAD_S = 1.75
HAND_V11_MAX_ACCELERATION_RAD_S2 = 7.5
SHARED_PREVIEW_HORIZON_STEPS = 6
SHARED_PREVIEW_MAX_VELOCITY_RAD_S = 1.75
SHARED_PREVIEW_MAX_ACCELERATION_RAD_S2 = 7.5
SHARED_PREVIEW_MAX_JERK_RAD_S3 = 80.0
SHARED_PREVIEW_ACCELERATION_PENALTY = 0.02
SHARED_PREVIEW_JERK_PENALTY = 5.0
SHARED_PREVIEW_DECAY = 0.2


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


@dataclass(frozen=True)
class HandTrajectoryConfig:
    """Mode-independent constraints for the shared 10 Hz hand planner."""

    strategy: str
    nominal_hz: float = 10.0
    horizon_steps: int = 6
    max_velocity_rad_s: float = 1.75
    max_acceleration_rad_s2: float = 7.5
    max_jerk_rad_s3: float = 50.0
    acceleration_penalty: float = 0.02
    jerk_penalty: float = 0.05
    preview_decay: float = 0.35
    admm_rho: float = 20.0
    admm_iterations: int = 120
    admm_tolerance: float = 5e-4
    enforce_derivative_limits: bool = True
    position_lower_rad: tuple[float, ...] | None = None
    position_upper_rad: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if self.strategy not in {"jerk_limited_p2p", "preview_qp"}:
            raise ValueError(
                "hand trajectory strategy must be jerk_limited_p2p or preview_qp"
            )
        finite_positive = {
            "nominal_hz": self.nominal_hz,
            "max_velocity_rad_s": self.max_velocity_rad_s,
            "max_acceleration_rad_s2": self.max_acceleration_rad_s2,
            "max_jerk_rad_s3": self.max_jerk_rad_s3,
            "admm_rho": self.admm_rho,
            "admm_tolerance": self.admm_tolerance,
        }
        for name, value in finite_positive.items():
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        finite_nonnegative = {
            "acceleration_penalty": self.acceleration_penalty,
            "jerk_penalty": self.jerk_penalty,
            "preview_decay": self.preview_decay,
        }
        for name, value in finite_nonnegative.items():
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.horizon_steps < 2:
            raise ValueError("horizon_steps must be at least two")
        if self.admm_iterations < 1:
            raise ValueError("admm_iterations must be positive")
        if (self.position_lower_rad is None) != (
            self.position_upper_rad is None
        ):
            raise ValueError("position lower/upper bounds must be supplied together")
        if self.position_lower_rad is not None:
            lower = np.asarray(self.position_lower_rad, dtype=np.float64)
            upper = np.asarray(self.position_upper_rad, dtype=np.float64)
            if (
                lower.shape != (HAND_DIM,)
                or upper.shape != (HAND_DIM,)
                or not np.all(np.isfinite(lower))
                or not np.all(np.isfinite(upper))
                or np.any(lower > upper)
            ):
                raise ValueError("position bounds must be finite ordered 20D vectors")


@dataclass(frozen=True)
class HandTrajectoryDecision:
    reference_target: np.ndarray
    command: np.ndarray
    velocity: np.ndarray
    acceleration: np.ndarray
    jerk: np.ndarray
    planned_commands: np.ndarray
    converged: bool
    iterations: int

    @property
    def raw_target(self) -> np.ndarray:
        return self.reference_target

    @property
    def boundary_active(self) -> bool:
        return False

    @property
    def velocity_limit_hits(self) -> int:
        limit = self.configured_max_velocity_rad_s
        return int(np.count_nonzero(np.abs(self.velocity) >= limit - 1e-7))

    @property
    def acceleration_limit_hits(self) -> int:
        limit = self.configured_max_acceleration_rad_s2
        return int(np.count_nonzero(np.abs(self.acceleration) >= limit - 1e-7))

    @property
    def jerk_limit_hits(self) -> int:
        limit = self.configured_max_jerk_rad_s3
        return int(np.count_nonzero(np.abs(self.jerk) >= limit - 1e-7))

    direction_reversal_holds: int = 0
    emergency_cap_hits: int = 0
    configured_max_velocity_rad_s: float = float("inf")
    configured_max_acceleration_rad_s2: float = float("inf")
    configured_max_jerk_rad_s3: float = float("inf")


class CausalHandTrajectoryPlanner:
    """Shared state-continuous hand reference governor for Sync and RTC.

    The planner never reads scheduler mode or resets at a policy boundary.  A
    caller supplies the future 20D reference visible at the current 10 Hz hand
    tick.  ``jerk_limited_p2p`` repeats the current target over the horizon;
    ``preview_qp`` tracks the supplied future waypoints.  Both solve the same
    deterministic box-constrained quadratic program and preserve q/v/a state.
    """

    def __init__(self, config: HandTrajectoryConfig) -> None:
        self.config = config
        self._position: np.ndarray | None = None
        self._velocity = np.zeros(HAND_DIM, dtype=np.float64)
        self._acceleration = np.zeros(HAND_DIM, dtype=np.float64)
        self._operators = self._build_operators(config.horizon_steps)

    @property
    def initialized(self) -> bool:
        return self._position is not None

    def reset(
        self,
        position: np.ndarray | None = None,
        *,
        velocity: np.ndarray | None = None,
        acceleration: np.ndarray | None = None,
    ) -> None:
        self._position = CausalHandCommandTracker._validate_optional(position)
        self._velocity = self._state_vector(velocity, "velocity")
        self._acceleration = self._state_vector(
            acceleration, "acceleration"
        )

    def step(self, reference_horizon: np.ndarray) -> HandTrajectoryDecision:
        reference = np.asarray(reference_horizon, dtype=np.float64)
        if (
            reference.ndim != 2
            or reference.shape[1] != HAND_DIM
            or len(reference) == 0
            or not np.all(np.isfinite(reference))
        ):
            raise ValueError("reference_horizon must be a finite Nx20 array")
        horizon = self.config.horizon_steps
        if len(reference) < horizon:
            reference = np.vstack(
                [
                    reference,
                    np.repeat(
                        reference[-1][None, :], horizon - len(reference), axis=0
                    ),
                ]
            )
        else:
            reference = reference[:horizon].copy()
        if self.config.strategy == "jerk_limited_p2p":
            reference = np.repeat(reference[0][None, :], horizon, axis=0)
        if self._position is None:
            self._position = reference[0].copy()
            zeros = np.zeros(HAND_DIM, dtype=np.float64)
            return HandTrajectoryDecision(
                reference_target=reference[0].copy(),
                command=self._position.copy(),
                velocity=zeros.copy(),
                acceleration=zeros.copy(),
                jerk=zeros.copy(),
                planned_commands=np.repeat(
                    self._position[None, :], horizon, axis=0
                ),
                converged=True,
                iterations=0,
                configured_max_velocity_rad_s=(
                    self.config.max_velocity_rad_s
                ),
                configured_max_acceleration_rad_s2=(
                    self.config.max_acceleration_rad_s2
                ),
                configured_max_jerk_rad_s3=self.config.max_jerk_rad_s3,
            )

        position = self._position.copy()
        velocity = self._velocity.copy()
        acceleration = self._acceleration.copy()
        plan, converged, iterations = self._solve(
            position, velocity, acceleration, reference
        )
        dt = 1.0 / self.config.nominal_hz
        command = plan[0].copy()
        next_velocity = (command - position) / dt
        next_acceleration = (next_velocity - velocity) / dt
        jerk = (next_acceleration - acceleration) / dt
        self._position = command.copy()
        self._velocity = next_velocity.copy()
        self._acceleration = next_acceleration.copy()
        return HandTrajectoryDecision(
            reference_target=reference[0].copy(),
            command=command,
            velocity=next_velocity,
            acceleration=next_acceleration,
            jerk=jerk,
            planned_commands=plan,
            converged=converged,
            iterations=iterations,
            configured_max_velocity_rad_s=self.config.max_velocity_rad_s,
            configured_max_acceleration_rad_s2=(
                self.config.max_acceleration_rad_s2
            ),
            configured_max_jerk_rad_s3=self.config.max_jerk_rad_s3,
        )

    def _solve(
        self,
        position: np.ndarray,
        velocity: np.ndarray,
        acceleration: np.ndarray,
        reference: np.ndarray,
    ) -> tuple[np.ndarray, bool, int]:
        dt = 1.0 / self.config.nominal_hz
        previous = position - velocity * dt
        previous_previous = (
            position - 2.0 * velocity * dt + acceleration * dt**2
        )
        history = np.stack([previous_previous, previous, position])
        first, second, third = self._operators
        offsets = [
            operator[:, :3] @ history
            for operator in (first, second, third)
        ]
        matrices = [
            operator[:, 3:]
            for operator in (first, second, third)
        ]

        weights = np.exp(
            -self.config.preview_decay
            * np.arange(self.config.horizon_steps, dtype=np.float64)
        )
        weight_matrix = np.diag(weights)
        hessian = weight_matrix.copy()
        gradient = -(weight_matrix @ reference)
        for penalty, matrix, offset in (
            (self.config.acceleration_penalty, matrices[1], offsets[1]),
            (self.config.jerk_penalty, matrices[2], offsets[2]),
        ):
            if penalty:
                hessian += penalty * (matrix.T @ matrix)
                gradient += penalty * (matrix.T @ offset)
        # ADMM uses the conventional 0.5*x'Px + q'x form.
        quadratic = 2.0 * hessian
        linear = 2.0 * gradient
        constraint_parts: list[np.ndarray] = []
        lower_parts: list[np.ndarray] = []
        upper_parts: list[np.ndarray] = []
        if self.config.enforce_derivative_limits:
            derivative_constraint = np.vstack(matrices)
            derivative_offset = np.vstack(offsets)
            limits = np.concatenate(
                [
                    np.full(
                        self.config.horizon_steps,
                        self.config.max_velocity_rad_s * dt,
                    ),
                    np.full(
                        self.config.horizon_steps,
                        self.config.max_acceleration_rad_s2 * dt**2,
                    ),
                    np.full(
                        self.config.horizon_steps,
                        self.config.max_jerk_rad_s3 * dt**3,
                    ),
                ]
            )[:, None]
            constraint_parts.append(derivative_constraint)
            lower_parts.append(-limits - derivative_offset)
            upper_parts.append(limits - derivative_offset)
        position_bounds = self._position_bounds()
        if position_bounds is not None:
            position_lower, position_upper = position_bounds
            constraint_parts.append(np.eye(self.config.horizon_steps))
            lower_parts.append(
                np.repeat(position_lower[None, :], self.config.horizon_steps, axis=0)
            )
            upper_parts.append(
                np.repeat(position_upper[None, :], self.config.horizon_steps, axis=0)
            )
        if not constraint_parts:
            plan = np.linalg.solve(quadratic, -linear)
            return plan, True, 1
        constraint = np.vstack(constraint_parts)
        lower = np.vstack(lower_parts)
        upper = np.vstack(upper_parts)
        rho = self.config.admm_rho
        system = quadratic + rho * (constraint.T @ constraint)
        # Start from a constant-position feasible trajectory.  State supplied
        # by the prior decision already respects the same derivative bounds.
        x = np.repeat(position[None, :], self.config.horizon_steps, axis=0)
        projected = np.clip(constraint @ x, lower, upper)
        dual = np.zeros_like(projected)
        converged = False
        iterations = self.config.admm_iterations
        for iteration in range(1, self.config.admm_iterations + 1):
            previous_x = x
            rhs = -linear + rho * constraint.T @ (projected - dual)
            x = np.linalg.solve(system, rhs)
            transformed = constraint @ x
            projected = np.clip(transformed + dual, lower, upper)
            dual += transformed - projected
            primal_residual = float(np.max(np.abs(transformed - projected)))
            iterate_change = float(np.max(np.abs(x - previous_x)))
            if max(primal_residual, iterate_change) <= self.config.admm_tolerance:
                converged = True
                iterations = iteration
                break
        # Numerical ADMM tolerance can leave a tiny first-step violation.  A
        # sequential q/v/a/j projection makes the executed prefix fail-safe;
        # later samples are only a preview and will be replanned next tick.
        plan = self._project_plan(
            x,
            position=position,
            velocity=velocity,
            acceleration=acceleration,
        )
        return plan, converged, iterations

    def _project_plan(
        self,
        plan: np.ndarray,
        *,
        position: np.ndarray,
        velocity: np.ndarray,
        acceleration: np.ndarray,
    ) -> np.ndarray:
        position_bounds = self._position_bounds()
        if not self.config.enforce_derivative_limits:
            if position_bounds is None:
                return plan.copy()
            return np.clip(plan, position_bounds[0], position_bounds[1])
        dt = 1.0 / self.config.nominal_hz
        result = np.empty_like(plan)
        current_position = position.copy()
        current_velocity = velocity.copy()
        current_acceleration = acceleration.copy()
        for index, target in enumerate(plan):
            desired_velocity = (target - current_position) / dt
            desired_velocity = np.clip(
                desired_velocity,
                -self.config.max_velocity_rad_s,
                self.config.max_velocity_rad_s,
            )
            desired_acceleration = (
                desired_velocity - current_velocity
            ) / dt
            desired_acceleration = np.clip(
                desired_acceleration,
                current_acceleration - self.config.max_jerk_rad_s3 * dt,
                current_acceleration + self.config.max_jerk_rad_s3 * dt,
            )
            desired_acceleration = np.clip(
                desired_acceleration,
                -self.config.max_acceleration_rad_s2,
                self.config.max_acceleration_rad_s2,
            )
            next_velocity = current_velocity + desired_acceleration * dt
            next_velocity = np.clip(
                next_velocity,
                -self.config.max_velocity_rad_s,
                self.config.max_velocity_rad_s,
            )
            next_position = current_position + next_velocity * dt
            if position_bounds is not None:
                next_position = np.clip(
                    next_position, position_bounds[0], position_bounds[1]
                )
            result[index] = next_position
            current_position = next_position
            current_velocity = next_velocity
            current_acceleration = desired_acceleration
        return result

    def _position_bounds(self) -> tuple[np.ndarray, np.ndarray] | None:
        if self.config.position_lower_rad is None:
            return None
        return (
            np.asarray(self.config.position_lower_rad, dtype=np.float64),
            np.asarray(self.config.position_upper_rad, dtype=np.float64),
        )

    @staticmethod
    def _build_operators(
        horizon_steps: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        identity = np.eye(horizon_steps + 3, dtype=np.float64)
        return tuple(
            np.diff(identity, n=order, axis=0)[-(horizon_steps) :]
            for order in (1, 2, 3)
        )

    @staticmethod
    def _state_vector(values: np.ndarray | None, label: str) -> np.ndarray:
        if values is None:
            return np.zeros(HAND_DIM, dtype=np.float64)
        array = np.asarray(values, dtype=np.float64)
        if array.shape != (HAND_DIM,) or not np.all(np.isfinite(array)):
            raise ValueError(f"{label} must be a finite 20D vector")
        return array.copy()


class CausalHandCommandTracker:
    """Stateful rate-aware tracker used identically by replay and runtime."""

    def __init__(
        self,
        *,
        enabled: bool,
        boundary_blend_seconds: float = 0.4,
        max_velocity_rad_s: float = 2.0,
        max_acceleration_rad_s2: float = 10.0,
        max_delta_rad: float = 0.4,
        reversal_error_rad: float = 0.15,
        reversal_boundary_only: bool = False,
        nominal_hz: float = 5.0,
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
        if not np.isfinite(nominal_hz) or nominal_hz <= 0:
            raise ValueError("nominal_hz must be finite and positive")
        self.enabled = bool(enabled)
        self.boundary_blend_seconds = float(boundary_blend_seconds)
        self.max_velocity_rad_s = float(max_velocity_rad_s)
        self.max_acceleration_rad_s2 = float(max_acceleration_rad_s2)
        self.max_delta_rad = float(max_delta_rad)
        self.reversal_error_rad = float(reversal_error_rad)
        self.reversal_boundary_only = bool(reversal_boundary_only)
        self.nominal_hz = float(nominal_hz)
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

        nominal_dt = 1.0 / self.nominal_hz
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
        if self.reversal_boundary_only and not boundary_active:
            reversing.fill(False)
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


class CausalHandBoundaryCapTracker:
    """RTC hand tracker with a tighter first-command cap and mild inner cap.

    Unlike V1 this tracker does not reshape timing, acceleration, or reversal
    semantics.  It only carries residual position error forward after limiting
    the first 5 Hz command of a replanned segment and subsequent commands.
    """

    def __init__(
        self,
        *,
        boundary_max_delta_rad: float = 0.15,
        internal_max_delta_rad: float = 0.30,
    ) -> None:
        if not (
            np.isfinite(boundary_max_delta_rad)
            and np.isfinite(internal_max_delta_rad)
            and 0 < boundary_max_delta_rad <= internal_max_delta_rad
        ):
            raise ValueError(
                "require finite 0 < boundary hand cap <= internal hand cap"
            )
        self.boundary_max_delta_rad = float(boundary_max_delta_rad)
        self.internal_max_delta_rad = float(internal_max_delta_rad)
        self._command: np.ndarray | None = None
        self._first_segment_command = True

    def reset(self, command: np.ndarray | None = None) -> None:
        self._command = CausalHandCommandTracker._validate_optional(command)
        self._first_segment_command = True

    def begin_segment(self, *, started_at: float) -> None:
        if not np.isfinite(started_at):
            raise ValueError("segment start time must be finite")
        self._first_segment_command = True

    def step(self, raw_target: np.ndarray, *, now: float) -> HandTrackingDecision:
        raw = CausalHandCommandTracker._validate(raw_target)
        if not np.isfinite(now):
            raise ValueError("hand command time must be finite")
        boundary_active = self._first_segment_command and self._command is not None
        if self._command is None:
            command = raw.copy()
            cap_hits = 0
        else:
            limit = (
                self.boundary_max_delta_rad
                if boundary_active
                else self.internal_max_delta_rad
            )
            error = raw - self._command
            cap_hits = int(np.count_nonzero(np.abs(error) > limit))
            command = self._command + np.clip(error, -limit, limit)
        self._command = command.copy()
        self._first_segment_command = False
        return HandTrackingDecision(
            raw_target=raw,
            guided_target=raw.copy(),
            command=command,
            boundary_active=boundary_active,
            velocity_limit_hits=0,
            acceleration_limit_hits=0,
            direction_reversal_holds=0,
            emergency_cap_hits=cap_hits,
        )


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


def smooth_rtc_arm_action_suffix(
    actions: np.ndarray,
    *,
    start_index: int,
    boundary_arm_position: np.ndarray,
    boundary_arm_velocity_per_step: np.ndarray,
    jerk_weight: float,
) -> tuple[np.ndarray, ArmSmoothingStats]:
    """Smooth only an RTC arm suffix while preserving its C0/C1 boundary.

    RTC indices before ``start_index`` describe an already committed timeline.
    They must never enter the smoothing objective because a discontinuity in
    that stale prefix would otherwise leak into the executable suffix.  The
    first suffix waypoint is pinned to the committed switch position and the
    second to one incoming-velocity step beyond it.  Hands are left unchanged.
    """

    array = np.asarray(actions, dtype=np.float64)
    position = np.asarray(boundary_arm_position, dtype=np.float64)
    velocity = np.asarray(boundary_arm_velocity_per_step, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] < ARM_DIM:
        raise ValueError("actions must be an NxD array with at least 14 dimensions")
    if not 0 <= start_index <= len(array) - 4:
        raise ValueError("RTC arm suffix must contain at least four waypoints")
    if position.shape != (ARM_DIM,) or velocity.shape != (ARM_DIM,):
        raise ValueError("RTC arm boundary position and velocity must be 14D")
    if not np.all(np.isfinite(array)) or not np.all(np.isfinite(position)):
        raise ValueError("RTC arm smoothing inputs must be finite")
    if not np.all(np.isfinite(velocity)):
        raise ValueError("RTC arm boundary velocity must be finite")
    if not np.isfinite(jerk_weight) or jerk_weight < 0:
        raise ValueError("jerk_weight must be finite and non-negative")

    result = array.copy()
    suffix = array[start_index:, :ARM_DIM]
    fixed_indices = np.asarray([0, 1, len(suffix) - 1], dtype=np.int64)
    fixed_values = np.stack(
        [
            position,
            position + velocity,
            suffix[-1],
        ],
        axis=0,
    )
    result[start_index:, :ARM_DIM] = _smooth_constrained_trajectory(
        suffix,
        jerk_weight=float(jerk_weight),
        fixed_indices=fixed_indices,
        fixed_values=fixed_values,
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


def _smooth_constrained_trajectory(
    values: np.ndarray,
    *,
    jerk_weight: float,
    fixed_indices: np.ndarray,
    fixed_values: np.ndarray,
) -> np.ndarray:
    """Solve the jerk-regularized trajectory with explicit waypoint values."""

    array = np.asarray(values, dtype=np.float64)
    fixed = np.asarray(fixed_indices, dtype=np.int64)
    boundary = np.asarray(fixed_values, dtype=np.float64)
    if array.ndim != 2 or len(array) < 4 or array.shape[1] == 0:
        raise ValueError("values must be an NxD trajectory with at least four rows")
    if fixed.ndim != 1 or len(fixed) == 0 or len(np.unique(fixed)) != len(fixed):
        raise ValueError("fixed_indices must contain unique trajectory indices")
    if np.any(fixed < 0) or np.any(fixed >= len(array)):
        raise ValueError("fixed trajectory index is out of range")
    if boundary.shape != (len(fixed), array.shape[1]):
        raise ValueError("fixed_values shape does not match the constraints")
    if not np.all(np.isfinite(array)) or not np.all(np.isfinite(boundary)):
        raise ValueError("trajectory and fixed values must be finite")
    if not np.isfinite(jerk_weight) or jerk_weight < 0:
        raise ValueError("jerk_weight must be finite and non-negative")

    result = array.copy()
    result[fixed] = boundary
    if jerk_weight == 0:
        return result

    identity = np.eye(len(array), dtype=np.float64)
    third_difference = np.diff(identity, n=3, axis=0)
    system = identity + jerk_weight * (third_difference.T @ third_difference)
    free = np.setdiff1d(np.arange(len(array)), fixed, assume_unique=True)
    rhs = array[free] - system[np.ix_(free, fixed)] @ boundary
    result[free] = np.linalg.solve(system[np.ix_(free, free)], rhs)
    result[fixed] = boundary
    return result


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
