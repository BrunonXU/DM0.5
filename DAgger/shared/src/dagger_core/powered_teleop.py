"""Pure arm-target state machine used by the explicitly powered G2 runner."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .joint_otg import OTGError, RuckigVelocityOTG
from .model_audit import build_reduced_arm_model
from .pink_ik import IKCandidate, IKSafety, IKTargetLimit, PinkRelativeArmIK


class ArmTargetIntegrator:
    NOMINAL_LINEAR_SPEED_M_S = 0.04
    NOMINAL_ANGULAR_SPEED_RAD_S = math.radians(10.0)
    MAX_OPERATOR_SPEED_SCALE = 2.0

    def __init__(
        self,
        pin: Any,
        full_model: Any,
        arm_joints: dict[str, list[str]],
        *,
        enable_ruckig_otg: bool = False,
        otg_max_acceleration_rad_s2: float = 2.0,
        otg_max_jerk_rad_s3: float = 20.0,
        otg_synchronization: str = "phase",
        max_otg_ik_lag_rad: float = 0.03,
        safety: IKSafety | None = None,
    ) -> None:
        self.pin = pin
        self.full_model = full_model
        self.arm_joints = arm_joints
        self.ik: PinkRelativeArmIK | None = None
        self.side: str | None = None
        # Pink owns a kinematic reference state.  The commanded state is kept
        # separately in virtual_q and, when enabled, is owned by Ruckig.  If
        # Pink solves from the lagged OTG command, its pose-error feedback can
        # saturate and repeatedly chase the governor after the stick centers.
        self.ik_virtual_q: np.ndarray | None = None
        self.virtual_q: np.ndarray | None = None
        self.safety = safety or IKSafety(
            # Pink's input validator must admit the temporary A-button boost.
            # The ordinary commanded speed remains the NOMINAL value below.
            max_linear_speed_m_s=(
                self.NOMINAL_LINEAR_SPEED_M_S * self.MAX_OPERATOR_SPEED_SCALE
            ),
            max_angular_speed_rad_s=(
                self.NOMINAL_ANGULAR_SPEED_RAD_S * self.MAX_OPERATOR_SPEED_SCALE
            ),
            # Bottle recovery sometimes needs more than the original 10 cm
            # radial clutch envelope.  Keep a finite bounded envelope while
            # avoiding a stop every ~2.5 s at the configured 4 cm/s.
            max_target_translation_m=0.20,
        )
        if (
            self.safety.max_linear_speed_m_s
            < self.NOMINAL_LINEAR_SPEED_M_S * self.MAX_OPERATOR_SPEED_SCALE
            or self.safety.max_angular_speed_rad_s
            < self.NOMINAL_ANGULAR_SPEED_RAD_S * self.MAX_OPERATOR_SPEED_SCALE
        ):
            raise ValueError("IK safety speed limits must admit the 2x operator boost")
        self.filtered_linear = np.zeros(3, dtype=np.float64)
        self.filtered_angular = np.zeros(3, dtype=np.float64)
        self.desired_linear = np.zeros(3, dtype=np.float64)
        self.desired_angular = np.zeros(3, dtype=np.float64)
        self.applied_linear = np.zeros(3, dtype=np.float64)
        self.applied_angular = np.zeros(3, dtype=np.float64)
        self.singularity_scale = 1.0
        self.singularity_sigma_min: float | None = None
        self.solver_damping = self.safety.base_solver_damping
        self.last_candidate: IKCandidate | None = None
        self.last_target_limit_reason: str | None = None
        self.target_envelope_recentered = False
        self.target_envelope_recenter_reason: str | None = None
        self.target_envelope_recenter_count = 0
        self.enable_ruckig_otg = enable_ruckig_otg
        self.otg_max_acceleration_rad_s2 = otg_max_acceleration_rad_s2
        self.otg_max_jerk_rad_s3 = otg_max_jerk_rad_s3
        self.otg_synchronization = otg_synchronization
        if max_otg_ik_lag_rad <= 0.0:
            raise ValueError("max OTG/IK lag must be positive")
        self.max_otg_ik_lag_rad = max_otg_ik_lag_rad
        self.otg: RuckigVelocityOTG | None = None
        # Full-scale stick changes ramp over roughly 150 ms.  RB release still
        # stops immediately because the powered loop bypasses step() in HOLD.
        self.linear_acceleration_m_s2 = self.NOMINAL_LINEAR_SPEED_M_S / 0.15
        self.angular_acceleration_rad_s2 = self.NOMINAL_ANGULAR_SPEED_RAD_S / 0.15

    def release(self) -> None:
        self.ik = None
        self.side = None
        self.ik_virtual_q = None
        self.virtual_q = None
        self.filtered_linear[:] = 0.0
        self.filtered_angular[:] = 0.0
        self.desired_linear[:] = 0.0
        self.desired_angular[:] = 0.0
        self.applied_linear[:] = 0.0
        self.applied_angular[:] = 0.0
        self.singularity_scale = 1.0
        self.singularity_sigma_min = None
        self.solver_damping = self.safety.base_solver_damping
        self.last_candidate = None
        self.last_target_limit_reason = None
        self.target_envelope_recentered = False
        self.target_envelope_recenter_reason = None
        self.target_envelope_recenter_count = 0
        self.otg = None

    def _set(self, q: np.ndarray, name: str, value: float) -> None:
        joint = self.full_model.getJointId(name)
        if joint == 0 or self.full_model.joints[joint].nq != 1:
            raise RuntimeError(f"missing/non-scalar model joint {name}")
        q[self.full_model.idx_qs[joint]] = value

    @staticmethod
    def _slew(current: np.ndarray, target: np.ndarray, max_rate: float, dt: float) -> np.ndarray:
        delta = target - current
        distance = float(np.linalg.norm(delta))
        limit = max_rate * dt
        if distance <= limit or distance == 0.0:
            return target.copy()
        return current + delta * (limit / distance)

    @staticmethod
    def _unit_ball(values: tuple[float, float, float]) -> np.ndarray:
        vector = np.asarray(values, dtype=np.float64)
        norm = float(np.linalg.norm(vector))
        return vector if norm <= 1.0 else vector / norm

    def step(
        self,
        *,
        mode: str,
        axes: tuple[float, float, float],
        angular_axes: tuple[float, float, float] = (0.0, 0.0, 0.0),
        speed_scale: float = 1.0,
        dt: float,
    ) -> tuple[np.ndarray, bool]:
        if self.ik is None or self.ik_virtual_q is None or self.virtual_q is None:
            raise RuntimeError("powered arm target was not initialized")
        if not math.isfinite(speed_scale) or not 1.0 <= speed_scale <= 2.0:
            raise ValueError("speed_scale must be finite and in [1, 2]")
        self.target_envelope_recentered = False
        self.target_envelope_recenter_reason = None
        vector = self._unit_ball(axes)
        if mode == "translation":
            desired_linear = vector * self.NOMINAL_LINEAR_SPEED_M_S * speed_scale
            desired_angular = np.zeros(3)
        elif mode == "rotation":
            desired_linear = np.zeros(3)
            desired_angular = vector * self.NOMINAL_ANGULAR_SPEED_RAD_S * speed_scale
        else:
            desired_linear = vector * self.NOMINAL_LINEAR_SPEED_M_S * speed_scale
            desired_angular = (
                self._unit_ball(angular_axes)
                * self.NOMINAL_ANGULAR_SPEED_RAD_S
                * speed_scale
            )
        self.desired_linear = desired_linear.copy()
        self.desired_angular = desired_angular.copy()
        self.filtered_linear = self._slew(
            self.filtered_linear, desired_linear, self.linear_acceleration_m_s2, dt
        )
        self.filtered_angular = self._slew(
            self.filtered_angular, desired_angular, self.angular_acceleration_rad_s2, dt
        )
        sigma_min, target_scale, solver_damping = self.ik.jacobian_policy(
            self.ik_virtual_q
        )
        # Limit scale changes to avoid turning the singularity guard itself
        # into a 100 Hz velocity discontinuity.
        scale_step = 3.0 * dt
        self.singularity_scale = float(
            np.clip(
                target_scale,
                self.singularity_scale - scale_step,
                self.singularity_scale + scale_step,
            )
        )
        self.singularity_sigma_min = sigma_min
        self.solver_damping = solver_damping
        self.applied_linear = self.filtered_linear * self.singularity_scale
        self.applied_angular = self.filtered_angular * self.singularity_scale
        try:
            self.ik.advance_target(self.applied_linear, self.applied_angular, dt)
        except IKTargetLimit as exc:
            # This bounds one relative integration segment, not the robot's
            # absolute workspace. Re-anchor at the last accepted target and
            # retry the same tick so continuous RB motion does not stop every
            # 20 cm / 20 degrees. Joint/model and singularity guards remain.
            self.ik.rebase_target_envelope(self.ik_virtual_q)
            self.ik.advance_target(self.applied_linear, self.applied_angular, dt)
            self.target_envelope_recentered = True
            self.target_envelope_recenter_reason = str(exc)
            self.target_envelope_recenter_count += 1
        self.last_target_limit_reason = None
        candidate = self.ik.solve(self.ik_virtual_q, dt)
        self.last_candidate = candidate
        self.ik_virtual_q = candidate.q.copy()
        if self.otg is None:
            self.virtual_q = candidate.q
        else:
            governed_q = self.otg.step(candidate.velocity).position
            max_lag = float(np.max(np.abs(self.ik_virtual_q - governed_q)))
            if max_lag > self.max_otg_ik_lag_rad:
                raise OTGError(
                    "Pink/OTG position lag exceeds safety limit: "
                    f"{max_lag:.6f} > {self.max_otg_ik_lag_rad:.6f} rad"
                )
            self.virtual_q = governed_q
        return self.virtual_q.copy(), False

    def diagnostics(self) -> dict[str, object]:
        """Return a copy-only snapshot for telemetry; never touches hardware."""
        target_translation = None
        target_rotation_vector = None
        target_lead_m = None
        if self.ik is not None:
            target_translation = self.ik.target_pose.translation.tolist()
            target_rotation_vector = self.pin.log3(self.ik.target_pose.rotation).tolist()
            target_lead_m = float(
                np.linalg.norm(
                    self.ik.target_pose.translation
                    - self.ik.takeover_pose.translation
                )
            )
        candidate = self.last_candidate
        otg_ik_lag = None
        if self.ik_virtual_q is not None and self.virtual_q is not None:
            otg_ik_lag = (self.ik_virtual_q - self.virtual_q).tolist()
        diagnostics = {
            "desired_linear_velocity_m_s": self.desired_linear.tolist(),
            "desired_angular_velocity_rad_s": self.desired_angular.tolist(),
            "filtered_linear_velocity_m_s": self.filtered_linear.tolist(),
            "filtered_angular_velocity_rad_s": self.filtered_angular.tolist(),
            "applied_linear_velocity_m_s": self.applied_linear.tolist(),
            "applied_angular_velocity_rad_s": self.applied_angular.tolist(),
            "singularity_sigma_min": self.singularity_sigma_min,
            "singularity_speed_scale": self.singularity_scale,
            "ik_solver_damping": self.solver_damping,
            "target_translation_m": target_translation,
            "target_rotation_vector_rad": target_rotation_vector,
            "target_lead_m": target_lead_m,
            "target_limit_reason": self.last_target_limit_reason,
            "target_envelope_recentered": self.target_envelope_recentered,
            "target_envelope_recenter_reason": self.target_envelope_recenter_reason,
            "target_envelope_recenter_count": self.target_envelope_recenter_count,
            "ik_q_ref_rad": None if candidate is None else candidate.q.tolist(),
            "ik_dq_ref_rad_s": (
                None if candidate is None else candidate.velocity.tolist()
            ),
            "ik_position_error_m": (
                None if candidate is None else candidate.position_error_m
            ),
            "ik_orientation_error_rad": (
                None if candidate is None else candidate.orientation_error_rad
            ),
            "joint_lower_margin_rad": (
                None
                if candidate is None
                else candidate.joint_lower_margin_rad.tolist()
            ),
            "joint_upper_margin_rad": (
                None
                if candidate is None
                else candidate.joint_upper_margin_rad.tolist()
            ),
            "jacobian_singular_values_raw": (
                None
                if candidate is None
                else candidate.jacobian_singular_values_raw.tolist()
            ),
            "otg_ik_position_lag_rad": otg_ik_lag,
            "otg_ik_position_lag_limit_rad": self.max_otg_ik_lag_rad,
        }
        if self.otg is None:
            diagnostics.update(
                {
                    "otg_enabled": False,
                    "otg_backend": None,
                    "otg_synchronization": None,
                    "otg_status": None,
                    "otg_target_dq_rad_s": None,
                    "otg_q_cmd_rad": None,
                    "otg_dq_cmd_rad_s": None,
                    "otg_ddq_cmd_rad_s2": None,
                    "otg_jerk_cmd_rad_s3": None,
                    "otg_calculation_duration_us": None,
                    "otg_velocity_error_rad_s": None,
                    "otg_velocity_limit_utilization": None,
                    "otg_acceleration_limit_utilization": None,
                    "otg_jerk_limit_utilization": None,
                    "otg_reversing_dofs": None,
                    "otg_decelerating_dofs": None,
                    "otg_joint_lower_margin_rad": None,
                    "otg_joint_upper_margin_rad": None,
                }
            )
        else:
            diagnostics.update(self.otg.diagnostics())
        return diagnostics


    def begin(self, side, snapshot):
        raise NotImplementedError("robot-specific model reference required")
