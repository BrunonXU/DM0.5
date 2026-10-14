from __future__ import annotations

import math

import numpy as np
import pytest

from g2teleop.pink_ik import IKHold, IKSafety, PinkRelativeArmIK, singularity_policy


def test_safety_defaults_are_shadow_scale() -> None:
    safety = IKSafety()
    assert safety.max_linear_speed_m_s == 0.02
    assert math.degrees(safety.max_angular_speed_rad_s) == pytest.approx(10.0)
    assert safety.max_joint_step_rad <= 0.01


def test_singularity_policy_is_continuous_and_monotonic() -> None:
    safety = IKSafety()
    sigmas = np.linspace(safety.singularity_hard_sigma, safety.singularity_soft_sigma, 101)
    policies = [singularity_policy(float(sigma), safety) for sigma in sigmas]
    scales = np.asarray([item[0] for item in policies])
    damping = np.asarray([item[1] for item in policies])
    assert scales[0] == pytest.approx(safety.singularity_min_speed_scale)
    assert scales[-1] == pytest.approx(1.0)
    assert np.all(np.diff(scales) >= 0.0)
    assert damping[0] == pytest.approx(safety.max_solver_damping)
    assert damping[-1] == pytest.approx(safety.base_solver_damping)
    assert np.all(np.diff(damping) <= 0.0)


def _seven_dof_model(pin):
    model = pin.Model()
    parent = 0
    joints = (
        pin.JointModelRZ(), pin.JointModelRY(), pin.JointModelRX(),
        pin.JointModelRY(), pin.JointModelRX(), pin.JointModelRY(),
        pin.JointModelRZ(),
    )
    for index, joint_model in enumerate(joints, 1):
        placement = pin.SE3(
            np.eye(3), np.array([0.0 if index == 1 else 0.1, 0.0, 0.0])
        )
        joint_id = model.addJoint(parent, joint_model, placement, f"joint{index}")
        model.appendBodyToJoint(joint_id, pin.Inertia.Random(), pin.SE3.Identity())
        parent = joint_id
    model.addFrame(
        pin.Frame(
            "tool", parent, parent,
            pin.SE3(np.eye(3), np.array([0.1, 0.0, 0.0])),
            pin.FrameType.OP_FRAME,
        )
    )
    model.lowerPositionLimit[:] = -3.0
    model.upperPositionLimit[:] = 3.0
    model.velocityLimit[:] = 1.0
    return model


def test_pink_relative_ik_smoke_when_optional_dependencies_exist() -> None:
    pin = pytest.importorskip("pinocchio")
    pytest.importorskip("pink")
    model = _seven_dof_model(pin)
    q = np.array([0.2, -0.4, 0.3, -0.5, 0.2, 0.4, -0.1])
    ik = PinkRelativeArmIK(model, "tool", q)
    ik.advance_target([0.02, 0.0, 0.0], [0.0, 0.0, 0.0], 0.05)
    for _ in range(30):
        result = ik.solve(q, 0.01)
        q = result.q
    assert result.position_error_m < 2e-5
    assert result.orientation_error_rad < 2e-4


def test_relative_ik_rejects_overspeed_before_solver() -> None:
    pin = pytest.importorskip("pinocchio")
    pytest.importorskip("pink")
    model = _seven_dof_model(pin)
    q = np.array([0.2, -0.4, 0.3, -0.5, 0.2, 0.4, -0.1])
    ik = PinkRelativeArmIK(model, "tool", q)
    with pytest.raises(IKHold, match="linear speed"):
        ik.advance_target([0.021, 0.0, 0.0], [0.0, 0.0, 0.0], 0.01)
