"""Pure arm-target state machine used by the explicitly powered G2 runner."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .g2_live_state import KinematicSnapshot
from .joint_otg import OTGError, RuckigVelocityOTG
from .model_audit import build_reduced_arm_model
from .pink_ik import IKCandidate, IKSafety, IKTargetLimit, PinkRelativeArmIK


from dagger_core.powered_teleop import ArmTargetIntegrator

class PoweredArmTarget(ArmTargetIntegrator):
    def begin(self, side: str, snapshot: KinematicSnapshot) -> None:
        full_q = self.pin.neutral(self.full_model)
        full_q[:3] = 0.0
        full_q[3:7] = np.asarray([0.0, 0.0, 0.0, 1.0])
        for index, value in enumerate(snapshot.waist, 1):
            self._set(full_q, f"idx0{index}_body_joint{index}", value)
        for index, value in enumerate(snapshot.arms[:7], 1):
            self._set(full_q, f"idx2{index}_arm_l_joint{index}", value)
        for index, value in enumerate(snapshot.arms[7:], 1):
            self._set(full_q, f"idx6{index}_arm_r_joint{index}", value)
        reduced = build_reduced_arm_model(
            self.pin, self.full_model, self.arm_joints[side], reference_q=full_q
        )
        arm = snapshot.arms[:7] if side == "left" else snapshot.arms[7:]
        frame = "hand_l_base_link" if side == "left" else "hand_r_base_link"
        self.ik = PinkRelativeArmIK(reduced, frame, arm, safety=self.safety)
        self.side = side
        self.ik_virtual_q = arm.copy()
        self.virtual_q = arm.copy()
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
        if self.enable_ruckig_otg:
            self.otg = RuckigVelocityOTG(
                7,
                0.01,
                max_velocity_rad_s=self.safety.max_joint_speed_rad_s,
                max_acceleration_rad_s2=self.otg_max_acceleration_rad_s2,
                max_jerk_rad_s3=self.otg_max_jerk_rad_s3,
                lower_position_rad=np.asarray(reduced.lowerPositionLimit, dtype=np.float64),
                upper_position_rad=np.asarray(reduced.upperPositionLimit, dtype=np.float64),
                synchronization=self.otg_synchronization,
            )
            self.otg.reset(arm)

