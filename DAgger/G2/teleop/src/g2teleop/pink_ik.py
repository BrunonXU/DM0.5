"""Safety-gated relative differential IK; no GDK and no hardware output."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

import numpy as np


class IKHold(RuntimeError):
    """Raised when an invalid or unsafe IK candidate must become HOLD."""


class IKTargetLimit(IKHold):
    """The requested relative target reached its configured workspace boundary."""


@dataclass(frozen=True)
class IKSafety:
    max_dt_s: float = 0.05
    max_linear_speed_m_s: float = 0.02
    max_angular_speed_rad_s: float = math.radians(10.0)
    max_target_translation_m: float = 0.05
    max_target_rotation_rad: float = math.radians(20.0)
    max_joint_speed_rad_s: float = 0.35
    max_joint_step_rad: float = 0.01
    # Empirical G2 raw 6D Jacobian thresholds from the first T1--T5 trace:
    # stable operation stayed above ~0.07, while repeated joint-2/7 reversals
    # clustered at 0.038--0.051. These are model-specific, not universal Pink
    # constants, and are logged on every tick for later paired tuning.
    singularity_hard_sigma: float = 0.035
    singularity_soft_sigma: float = 0.08
    singularity_min_speed_scale: float = 0.15
    base_solver_damping: float = 1e-8
    max_solver_damping: float = 1e-4


@dataclass(frozen=True)
class IKCandidate:
    q: np.ndarray
    velocity: np.ndarray
    position_error_m: float
    orientation_error_rad: float
    joint_lower_margin_rad: np.ndarray
    joint_upper_margin_rad: np.ndarray
    jacobian_singular_values_raw: np.ndarray
    singularity_speed_scale: float
    solver_damping: float


def _vector(values: Sequence[float], size: int, label: str) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.shape != (size,) or not np.isfinite(array).all():
        raise IKHold(f"{label} must be finite shape ({size},)")
    return array


def _rotation_angle(pin: Any, rotation: np.ndarray) -> float:
    return float(np.linalg.norm(pin.log3(rotation)))


def singularity_policy(sigma_min: float, safety: IKSafety) -> tuple[float, float]:
    """Continuous G2-specific velocity scale and DLS damping schedule."""
    if not math.isfinite(sigma_min):
        raise IKHold("non-finite Jacobian singular value")
    low = safety.singularity_hard_sigma
    high = safety.singularity_soft_sigma
    if not 0.0 < low < high:
        raise IKHold("invalid singularity thresholds")
    normalized = float(np.clip((sigma_min - low) / (high - low), 0.0, 1.0))
    smooth = normalized * normalized * (3.0 - 2.0 * normalized)
    speed_scale = safety.singularity_min_speed_scale + (
        1.0 - safety.singularity_min_speed_scale
    ) * smooth
    # Interpolate damping in log space: the regularization changes smoothly
    # across four orders of magnitude instead of jumping at a threshold.
    log_base = math.log(safety.base_solver_damping)
    log_max = math.log(safety.max_solver_damping)
    damping = math.exp(log_max + smooth * (log_base - log_max))
    return float(speed_scale), float(damping)


class PinkRelativeArmIK:
    """Stateful one-arm IK around a measured takeover configuration.

    The target is initialized from FK at takeover, then receives base/world
    frame twists.  The solver never owns a robot interface and returns only a
    checked seven-joint candidate.
    """

    def __init__(
        self,
        model: Any,
        frame: str,
        takeover_q: Sequence[float],
        *,
        safety: IKSafety = IKSafety(),
        solver: str = "proxqp",
    ) -> None:
        import pinocchio as pin
        import pink
        from pink.tasks import FrameTask, PostureTask

        if model.nq != 7 or model.nv != 7:
            raise ValueError(f"expected 7DOF reduced model, got nq={model.nq} nv={model.nv}")
        if model.getFrameId(frame) >= len(model.frames):
            raise ValueError(f"frame missing from reduced model: {frame}")
        self.pin = pin
        self.pink = pink
        self.model = model
        self.frame = frame
        self.safety = safety
        self.solver = solver
        self.takeover_q = _vector(takeover_q, 7, "takeover_q").copy()

        # Use the stricter project velocity cap in Pink's default QP limits.
        self.model.velocityLimit[:] = np.minimum(
            np.abs(self.model.velocityLimit), safety.max_joint_speed_rad_s
        )
        self.configuration = pink.Configuration(model, model.createData(), self.takeover_q)
        self._diagnostic_data = model.createData()
        self._frame_id = model.getFrameId(frame)
        self.takeover_pose = self.configuration.get_transform_frame_to_world(frame).copy()
        self.target_pose = self.takeover_pose.copy()
        self.frame_task = FrameTask(
            frame,
            position_cost=1.0,
            orientation_cost=0.35,
            lm_damping=1e-6,
            gain=0.4,
        )
        self.frame_task.set_target(self.target_pose)
        self.posture_task = PostureTask(cost=1e-3, lm_damping=1e-8, gain=0.2)
        self.posture_task.set_target(self.takeover_q)

    def jacobian_policy(self, q: Sequence[float]) -> tuple[float, float, float]:
        checked_q = _vector(q, 7, "jacobian_q")
        jacobian = self.pin.computeFrameJacobian(
            self.model,
            self._diagnostic_data,
            checked_q,
            self._frame_id,
            self.pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
        )
        singular_values = np.linalg.svd(
            np.asarray(jacobian, dtype=np.float64), compute_uv=False
        )
        sigma_min = float(np.min(singular_values))
        scale, damping = singularity_policy(sigma_min, self.safety)
        return sigma_min, scale, damping

    def advance_target(
        self,
        linear_base_m_s: Sequence[float],
        angular_base_rad_s: Sequence[float],
        dt: float,
    ) -> None:
        linear = _vector(linear_base_m_s, 3, "linear_base_m_s")
        angular = _vector(angular_base_rad_s, 3, "angular_base_rad_s")
        if not math.isfinite(dt) or dt <= 0.0 or dt > self.safety.max_dt_s:
            raise IKHold(f"dt outside (0,{self.safety.max_dt_s}]: {dt}")
        if np.linalg.norm(linear) > self.safety.max_linear_speed_m_s + 1e-12:
            raise IKHold("linear speed exceeds Shadow limit")
        if np.linalg.norm(angular) > self.safety.max_angular_speed_rad_s + 1e-12:
            raise IKHold("angular speed exceeds Shadow limit")

        candidate = self.target_pose.copy()
        candidate.translation = candidate.translation + linear * dt
        # Pre-multiply: angular command axes are expressed in the fixed G2 base
        # frame, matching the common left/right operator semantics.
        candidate.rotation = self.pin.exp3(angular * dt) @ candidate.rotation
        travel = float(np.linalg.norm(candidate.translation - self.takeover_pose.translation))
        rotation = _rotation_angle(
            self.pin, candidate.rotation @ self.takeover_pose.rotation.T
        )
        if travel > self.safety.max_target_translation_m + 1e-12:
            raise IKTargetLimit("relative target reached takeover translation workspace")
        if rotation > self.safety.max_target_rotation_rad + 1e-12:
            raise IKTargetLimit("relative target reached takeover rotation workspace")
        self.target_pose = candidate
        self.frame_task.set_target(candidate)

    def rebase_target_envelope(self, posture_q: Sequence[float]) -> None:
        """Move the relative envelope anchor without moving the EEF target."""
        checked_q = _vector(posture_q, 7, "envelope_posture_q")
        self.takeover_pose = self.target_pose.copy()
        self.takeover_q = checked_q.copy()
        self.posture_task.set_target(self.takeover_q)

    def solve(self, measured_q: Sequence[float], dt: float) -> IKCandidate:
        q = _vector(measured_q, 7, "measured_q")
        if not math.isfinite(dt) or dt <= 0.0 or dt > self.safety.max_dt_s:
            raise IKHold(f"dt outside (0,{self.safety.max_dt_s}]: {dt}")
        try:
            self.configuration.update(q)
            _, speed_scale, solver_damping = self.jacobian_policy(q)
            velocity = self.pink.solve_ik(
                self.configuration,
                [self.frame_task, self.posture_task],
                dt,
                self.solver,
                damping=solver_damping,
                safety_break=True,
            )
        except Exception as exc:
            raise IKHold(f"Pink solve failed: {type(exc).__name__}: {exc}") from exc
        velocity = _vector(velocity, 7, "IK velocity")
        # QP backends may exceed a box bound by a small feasibility tolerance.
        # Reject a material violation and clamp only the numerical residue so
        # the project limit, rather than the backend tolerance, is authoritative.
        if np.max(np.abs(velocity)) > self.safety.max_joint_speed_rad_s + 1e-3:
            raise IKHold("IK velocity exceeds project limit")
        velocity = np.clip(
            velocity,
            -self.safety.max_joint_speed_rad_s,
            self.safety.max_joint_speed_rad_s,
        )
        candidate_q = np.asarray(self.configuration.integrate(velocity, dt), dtype=np.float64)
        step = float(np.max(np.abs(candidate_q - q)))
        if step > self.safety.max_joint_step_rad + 1e-8:
            raise IKHold("IK joint step exceeds project limit")

        checked = self.pink.Configuration(self.model, self.model.createData(), candidate_q)
        checked.check_limits(safety_break=True)
        actual = checked.get_transform_frame_to_world(self.frame)
        position_error = float(np.linalg.norm(actual.translation - self.target_pose.translation))
        orientation_error = _rotation_angle(
            self.pin, actual.rotation @ self.target_pose.rotation.T
        )
        if not math.isfinite(position_error + orientation_error):
            raise IKHold("non-finite FK round-trip error")
        lower_margin = candidate_q - np.asarray(
            self.model.lowerPositionLimit, dtype=np.float64
        )
        upper_margin = np.asarray(
            self.model.upperPositionLimit, dtype=np.float64
        ) - candidate_q
        jacobian = self.pin.computeFrameJacobian(
            self.model,
            self._diagnostic_data,
            candidate_q,
            self._frame_id,
            self.pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
        )
        singular_values = np.linalg.svd(
            np.asarray(jacobian, dtype=np.float64), compute_uv=False
        )
        return IKCandidate(
            candidate_q,
            velocity,
            position_error,
            orientation_error,
            lower_margin,
            upper_margin,
            singular_values,
            speed_scale,
            solver_damping,
        )
