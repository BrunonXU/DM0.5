from __future__ import annotations

import numpy as np
import pytest

from g2teleop.joint_otg import OTGError, RuckigVelocityOTG


ruckig = pytest.importorskip("ruckig")


def make_otg(*, synchronization: str = "phase") -> RuckigVelocityOTG:
    otg = RuckigVelocityOTG(
        2,
        0.01,
        max_velocity_rad_s=0.35,
        max_acceleration_rad_s2=2.0,
        max_jerk_rad_s3=20.0,
        lower_position_rad=[-2.0, -2.0],
        upper_position_rad=[2.0, 2.0],
        synchronization=synchronization,
    )
    otg.reset([0.0, 0.0])
    return otg


def test_requires_measured_reset_before_update() -> None:
    otg = make_otg()
    otg.initialized = False
    with pytest.raises(OTGError, match="reset"):
        otg.step([0.1, 0.0])


def test_velocity_mode_limits_acceleration_and_jerk() -> None:
    otg = make_otg()
    samples = [otg.step([0.2, -0.1]) for _ in range(80)]
    for sample in samples:
        assert np.max(np.abs(sample.velocity)) <= 0.35 + 1e-6
        assert np.max(np.abs(sample.acceleration)) <= 2.0 + 1e-6
        assert np.max(np.abs(sample.jerk)) <= 20.0 + 1e-5
    assert samples[-1].velocity[0] == pytest.approx(0.2, abs=1e-7)
    assert samples[-1].velocity[1] == pytest.approx(-0.1, abs=1e-7)


def test_phase_synchronization_preserves_pink_velocity_direction_from_rest() -> None:
    otg = make_otg(synchronization="phase")
    sample = otg.step([0.1, -0.2])
    assert sample.velocity[0] / sample.velocity[1] == pytest.approx(-0.5)


def test_reversal_crosses_zero_without_one_tick_velocity_flip() -> None:
    otg = make_otg()
    for _ in range(60):
        forward = otg.step([0.2, 0.0])
    assert forward.velocity[0] > 0.19
    reversed_samples = [otg.step([-0.2, 0.0]) for _ in range(80)]
    velocities = np.asarray([sample.velocity[0] for sample in reversed_samples])
    assert velocities[0] > 0.0
    assert np.any(velocities <= 0.0)
    assert np.max(np.abs(np.diff(velocities))) <= 2.0 * 0.01 + 1e-6


def test_safety_hold_zeroes_dynamic_state_without_coasting() -> None:
    otg = make_otg()
    for _ in range(20):
        sample = otg.step([0.2, 0.0])
    otg.emergency_hold(sample.position)
    diagnostics = otg.diagnostics()
    assert diagnostics["otg_status"] == "safety_hold"
    np.testing.assert_allclose(diagnostics["otg_dq_cmd_rad_s"], [0.0, 0.0])
    np.testing.assert_allclose(diagnostics["otg_ddq_cmd_rad_s2"], [0.0, 0.0])


def test_rejects_target_and_output_outside_limits() -> None:
    otg = make_otg()
    with pytest.raises(OTGError, match="target velocity"):
        otg.step([0.36, 0.0])
    with pytest.raises(OTGError, match="position"):
        otg.reset([2.1, 0.0])
