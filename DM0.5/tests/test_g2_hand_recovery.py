"""Pure-logic tests for the G2 H5 hand recovery tool; no GDK is required."""

import numpy as np

from g2_sort.deployment.g2_hand_recovery import (
    H5_LEFT_HAND_START,
    H5_RIGHT_HAND_START,
    build_hand_recovery_target,
    build_linear_recovery_trajectory,
)


def test_left_recovery_preserves_arms_and_right_hand():
    current = np.linspace(-0.5, 0.5, 34, dtype=np.float32)
    target = build_hand_recovery_target(current, "left")

    np.testing.assert_array_equal(target[:14], current[:14])
    np.testing.assert_allclose(target[14:24], H5_LEFT_HAND_START)
    np.testing.assert_array_equal(target[24:34], current[24:34])


def test_both_recovery_preserves_arms_and_restores_both_hands():
    current = np.zeros(34, dtype=np.float32)
    target = build_hand_recovery_target(current, "both")

    np.testing.assert_array_equal(target[:14], current[:14])
    np.testing.assert_allclose(target[14:24], H5_LEFT_HAND_START)
    np.testing.assert_allclose(target[24:34], H5_RIGHT_HAND_START)


def test_linear_recovery_has_endpoints_duration_and_no_overshoot():
    current = np.zeros(34, dtype=np.float32)
    target = build_hand_recovery_target(current, "left")
    trajectory = build_linear_recovery_trajectory(
        current,
        target,
        duration_seconds=5.0,
        model_hz=30.0,
    )

    assert trajectory.shape == (151, 34)
    np.testing.assert_array_equal(trajectory[0], current)
    np.testing.assert_allclose(trajectory[-1], target)
    lower = np.minimum(current, target)
    upper = np.maximum(current, target)
    assert np.all(trajectory >= lower[None, :] - 1e-7)
    assert np.all(trajectory <= upper[None, :] + 1e-7)
