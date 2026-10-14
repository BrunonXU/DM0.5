from __future__ import annotations

import numpy as np
from types import SimpleNamespace

from g2teleop.pink_ik import IKTargetLimit
from g2teleop.powered_teleop import PoweredArmTarget


def test_constructor_initializes_safety_before_slew_limits() -> None:
    target = PoweredArmTarget(pin=None, full_model=None, arm_joints={})
    assert target.NOMINAL_LINEAR_SPEED_M_S == 0.04
    assert target.safety.max_linear_speed_m_s == 0.08
    assert target.safety.max_angular_speed_rad_s == target.NOMINAL_ANGULAR_SPEED_RAD_S * 2.0
    assert target.safety.max_target_translation_m == 0.20
    assert target.linear_acceleration_m_s2 > 0.0
    assert target.angular_acceleration_rad_s2 > 0.0


def test_vector_slew_limits_change_without_changing_direction() -> None:
    current = np.zeros(3)
    target = np.array([0.04, 0.0, 0.0])
    moved = PoweredArmTarget._slew(current, target, max_rate=0.2, dt=0.01)
    np.testing.assert_allclose(moved, [0.002, 0.0, 0.0])


def test_vector_slew_reaches_nearby_target_exactly() -> None:
    current = np.array([0.001, 0.0, 0.0])
    target = np.array([0.002, 0.0, 0.0])
    moved = PoweredArmTarget._slew(current, target, max_rate=0.2, dt=0.01)
    np.testing.assert_allclose(moved, target)


class _FakeIK:
    def __init__(self, safety, candidate_q: np.ndarray, candidate_dq: np.ndarray) -> None:
        self.safety = safety
        self.candidate_q = candidate_q
        self.candidate_dq = candidate_dq
        self.solve_input = None
        self.linear_input = None
        self.angular_input = None

    def jacobian_policy(self, q):
        return 0.2, 1.0, self.safety.base_solver_damping

    def advance_target(self, linear, angular, dt):
        self.linear_input = np.asarray(linear).copy()
        self.angular_input = np.asarray(angular).copy()
        return None

    def solve(self, q, dt):
        self.solve_input = np.asarray(q).copy()
        return SimpleNamespace(q=self.candidate_q.copy(), velocity=self.candidate_dq.copy())


class _FakeOTG:
    def __init__(self, output_q: np.ndarray) -> None:
        self.output_q = output_q
        self.target_velocity = None

    def step(self, target_velocity):
        self.target_velocity = np.asarray(target_velocity).copy()
        return SimpleNamespace(position=self.output_q.copy())


class _LimitOnceIK(_FakeIK):
    def __init__(self, safety, candidate_q: np.ndarray, candidate_dq: np.ndarray) -> None:
        super().__init__(safety, candidate_q, candidate_dq)
        self.advance_count = 0
        self.rebase_q = None

    def advance_target(self, linear, angular, dt):
        self.advance_count += 1
        if self.advance_count == 1:
            raise IKTargetLimit("relative target reached takeover translation workspace")
        return super().advance_target(linear, angular, dt)

    def rebase_target_envelope(self, posture_q):
        self.rebase_q = np.asarray(posture_q).copy()


def test_pink_reference_state_is_independent_from_lagged_otg_command() -> None:
    target = PoweredArmTarget(pin=None, full_model=None, arm_joints={})
    planner_q = np.full(7, 0.1)
    lagged_command_q = np.full(7, 0.02)
    candidate_q = np.full(7, 0.101)
    candidate_dq = np.full(7, 0.1)
    output_q = np.full(7, 0.08)
    fake_ik = _FakeIK(target.safety, candidate_q, candidate_dq)
    fake_otg = _FakeOTG(output_q)
    target.ik = fake_ik
    target.ik_virtual_q = planner_q.copy()
    target.virtual_q = lagged_command_q.copy()
    target.otg = fake_otg

    solved, saturated = target.step(
        mode="translation", axes=(0.0, 0.0, 0.0), dt=0.01
    )

    assert saturated is False
    np.testing.assert_allclose(fake_ik.solve_input, planner_q)
    np.testing.assert_allclose(target.ik_virtual_q, candidate_q)
    np.testing.assert_allclose(fake_otg.target_velocity, candidate_dq)
    np.testing.assert_allclose(target.virtual_q, output_q)
    np.testing.assert_allclose(solved, output_q)


def test_a_button_speed_scale_reaches_twice_nominal_without_changing_safety_caps() -> None:
    target = PoweredArmTarget(pin=None, full_model=None, arm_joints={})
    candidate = np.zeros(7)
    fake_ik = _FakeIK(target.safety, candidate, candidate)
    target.ik = fake_ik
    target.ik_virtual_q = candidate.copy()
    target.virtual_q = candidate.copy()

    # The acceleration limit is intentionally unchanged, so the boosted target
    # takes roughly 300 ms to reach instead of introducing a velocity step.
    for _ in range(30):
        target.step(
            mode="combined",
            axes=(1.0, 0.0, 0.0),
            angular_axes=(0.0, 1.0, 0.0),
            speed_scale=2.0,
            dt=0.01,
        )

    np.testing.assert_allclose(fake_ik.linear_input, [0.08, 0.0, 0.0])
    np.testing.assert_allclose(
        fake_ik.angular_input,
        [0.0, 2.0 * target.NOMINAL_ANGULAR_SPEED_RAD_S, 0.0],
    )
    assert target.safety.max_joint_speed_rad_s == 0.35


def test_relative_envelope_rolls_over_without_saturating_or_stopping() -> None:
    target = PoweredArmTarget(pin=None, full_model=None, arm_joints={})
    planner_q = np.full(7, 0.1)
    candidate_q = np.full(7, 0.101)
    candidate_dq = np.full(7, 0.1)
    fake_ik = _LimitOnceIK(target.safety, candidate_q, candidate_dq)
    target.ik = fake_ik
    target.ik_virtual_q = planner_q.copy()
    target.virtual_q = planner_q.copy()

    solved, saturated = target.step(
        mode="translation", axes=(1.0, 0.0, 0.0), dt=0.01
    )

    assert saturated is False
    assert fake_ik.advance_count == 2
    np.testing.assert_allclose(fake_ik.rebase_q, planner_q)
    np.testing.assert_allclose(solved, candidate_q)
    assert target.target_envelope_recentered is True
    assert target.target_envelope_recenter_count == 1
    assert "translation workspace" in target.target_envelope_recenter_reason
