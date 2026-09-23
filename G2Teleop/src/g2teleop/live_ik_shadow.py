"""Live measured-state to virtual IK Shadow engine; no robot API imports."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from .g2_live_state import KinematicSnapshot
from .model_audit import build_reduced_arm_model
from .pink_ik import IKHold, IKSafety, PinkRelativeArmIK
from .protocol import IntentPacket


@dataclass(frozen=True)
class LiveShadowResult:
    state: str
    reason: str | None
    side: str | None
    candidate_arms14: np.ndarray | None
    position_error_m: float | None = None
    orientation_error_deg: float | None = None


class LiveIKShadowEngine:
    """Integrate F710 intent in a virtual arm while verifying real G2 is still."""

    def __init__(
        self,
        pin: Any,
        full_model: Any,
        arm_joints: dict[str, list[str]],
        *,
        max_state_age_s: float = 0.05,
        max_measured_arm_drift_rad: float = 0.02,
        max_waist_drift_rad: float = 0.003,
    ) -> None:
        self.pin = pin
        self.full_model = full_model
        self.arm_joints = arm_joints
        self.max_state_age_s = max_state_age_s
        self.max_measured_arm_drift_rad = max_measured_arm_drift_rad
        self.max_waist_drift_rad = max_waist_drift_rad
        self.nominal_linear_speed_m_s = 0.04
        self.nominal_angular_speed_rad_s = math.radians(10.0)
        self.ik_safety = IKSafety(
            max_linear_speed_m_s=0.08,
            max_angular_speed_rad_s=math.radians(20.0),
            max_target_translation_m=0.20,
        )
        self.reset("startup")

    def reset(self, reason: str) -> None:
        self.ik: PinkRelativeArmIK | None = None
        self.side: str | None = None
        self.takeover_arms: np.ndarray | None = None
        self.takeover_waist: np.ndarray | None = None
        self.virtual_q: np.ndarray | None = None
        self.last_update_at: float | None = None
        self.last_reason = reason

    def _full_q(self, snapshot: KinematicSnapshot) -> np.ndarray:
        q = self.pin.neutral(self.full_model)
        q[:3] = 0.0
        q[3:7] = np.array([0.0, 0.0, 0.0, 1.0])
        for index, value in enumerate(snapshot.waist, 1):
            self._set_scalar(q, f"idx0{index}_body_joint{index}", value)
        for index, value in enumerate(snapshot.arms[:7], 1):
            self._set_scalar(q, f"idx2{index}_arm_l_joint{index}", value)
        for index, value in enumerate(snapshot.arms[7:], 1):
            self._set_scalar(q, f"idx6{index}_arm_r_joint{index}", value)
        return q

    def _set_scalar(self, q: np.ndarray, name: str, value: float) -> None:
        joint_id = self.full_model.getJointId(name)
        if joint_id == 0 or self.full_model.joints[joint_id].nq != 1:
            raise IKHold(f"missing/non-scalar model joint {name}")
        q[self.full_model.idx_qs[joint_id]] = value

    def _begin(self, packet: IntentPacket, snapshot: KinematicSnapshot, now: float) -> None:
        assert packet.active_side in {"left", "right"}
        side = packet.active_side
        arm_slice = slice(0, 7) if side == "left" else slice(7, 14)
        full_q = self._full_q(snapshot)
        reduced = build_reduced_arm_model(
            self.pin,
            self.full_model,
            self.arm_joints[side],
            reference_q=full_q,
        )
        frame = "hand_l_base_link" if side == "left" else "hand_r_base_link"
        q0 = snapshot.arms[arm_slice]
        self.ik = PinkRelativeArmIK(reduced, frame, q0, safety=self.ik_safety)
        self.side = side
        self.takeover_arms = snapshot.arms.copy()
        self.takeover_waist = snapshot.waist.copy()
        self.virtual_q = q0.copy()
        self.last_update_at = now

    def process(
        self,
        packet: IntentPacket,
        snapshot: KinematicSnapshot,
        *,
        now: float,
    ) -> LiveShadowResult:
        if snapshot.age_seconds(now) > self.max_state_age_s:
            self.reset("stale_state")
            return LiveShadowResult("HOLD", "stale_state", packet.active_side, None)
        if not packet.deadman:
            self.reset("deadman_released")
            return LiveShadowResult("HOLD", None, packet.active_side, None)
        try:
            if self.ik is None:
                self._begin(packet, snapshot, now)
                return LiveShadowResult(
                    "SHADOW_ACTIVE", "takeover_initialized", self.side,
                    snapshot.arms.copy(),
                )
            if packet.active_side != self.side:
                raise IKHold("active side changed while deadman held")
            assert self.takeover_arms is not None
            assert self.takeover_waist is not None
            assert self.virtual_q is not None
            assert self.last_update_at is not None
            if np.max(np.abs(snapshot.arms - self.takeover_arms)) > self.max_measured_arm_drift_rad:
                raise IKHold("measured arm drifted during no-output Shadow")
            if np.max(np.abs(snapshot.waist - self.takeover_waist)) > self.max_waist_drift_rad:
                raise IKHold("measured waist drifted during takeover")
            dt = now - self.last_update_at
            if not 0.0 < dt <= self.ik.safety.max_dt_s:
                raise IKHold(f"live update dt out of range: {dt}")
            axes = np.asarray(packet.axes, dtype=np.float64)
            axes_norm = float(np.linalg.norm(axes))
            if axes_norm > 1.0:
                axes = axes / axes_norm
            if packet.mode == "translation":
                linear = axes * self.nominal_linear_speed_m_s * packet.speed_scale
                angular = np.zeros(3)
            elif packet.mode == "rotation":
                linear = np.zeros(3)
                angular = axes * self.nominal_angular_speed_rad_s * packet.speed_scale
            else:
                angular_axes = np.asarray(packet.angular_axes, dtype=np.float64)
                angular_norm = float(np.linalg.norm(angular_axes))
                if angular_norm > 1.0:
                    angular_axes = angular_axes / angular_norm
                linear = axes * self.nominal_linear_speed_m_s * packet.speed_scale
                angular = (
                    angular_axes
                    * self.nominal_angular_speed_rad_s
                    * packet.speed_scale
                )
            self.ik.advance_target(linear, angular, dt)
            candidate = self.ik.solve(self.virtual_q, dt)
            self.virtual_q = candidate.q
            self.last_update_at = now
            arms = self.takeover_arms.copy()
            if self.side == "left":
                arms[:7] = candidate.q
            else:
                arms[7:] = candidate.q
            return LiveShadowResult(
                "SHADOW_ACTIVE",
                None,
                self.side,
                arms,
                candidate.position_error_m,
                math.degrees(candidate.orientation_error_rad),
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.reset(reason)
            return LiveShadowResult("HOLD", reason, packet.active_side, None)
