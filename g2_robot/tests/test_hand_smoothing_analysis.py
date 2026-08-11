from __future__ import annotations

import numpy as np

from g2_sort.deployment.g2_client.trajectory_smoothing import (
    smooth_fixed_endpoint_trajectory,
)
from g2_sort.validation.evaluate_hand_smoothing import (
    apply_causal_delta_cap,
    apply_causal_hand_tracker,
)


def test_generic_jerk_smoothing_preserves_hand_endpoints():
    hands = np.zeros((50, 20), dtype=np.float64)
    hands[24:, 4] = 0.8

    smoothed = smooth_fixed_endpoint_trajectory(hands, jerk_weight=10.0)

    np.testing.assert_array_equal(smoothed[[0, -1]], hands[[0, -1]])
    assert np.max(np.abs(np.diff(smoothed[:, 4]))) < 0.8


def test_causal_delta_cap_never_exceeds_limit_and_carries_residual():
    targets = [np.vstack([np.zeros(20), np.ones(20), np.ones(20)])]

    commands = apply_causal_delta_cap(targets, max_delta_rad=0.25)[0]

    assert np.max(np.abs(np.diff(commands, axis=0))) <= 0.25
    np.testing.assert_allclose(commands[-1], 0.5)


def test_causal_delta_cap_can_start_from_recorded_previous_command():
    targets = [np.vstack([np.zeros(20), np.zeros(20)])]
    previous = [np.ones(20)]

    commands = apply_causal_delta_cap(
        targets,
        max_delta_rad=0.4,
        initial_commands=previous,
    )[0]

    np.testing.assert_allclose(commands[0], 0.6)
    np.testing.assert_allclose(commands[1], 0.2)


def test_hand_tracker_blends_boundary_and_never_releases_residual_as_jump():
    raw = np.ones((6, 20), dtype=np.float64)
    times = np.asarray([0.1, 0.3, 0.5, 0.7, 0.9, 1.1])

    tracked, stats = apply_causal_hand_tracker(
        [raw],
        initial_commands=[np.zeros(20)],
        command_times=[times],
        boundary_blend_seconds=0.4,
        max_velocity_rad_s=2.0,
        max_acceleration_rad_s2=5.0,
        max_delta_rad=0.4,
    )

    commands = tracked[0]
    assert np.all(commands[0] < 0.2)
    assert np.max(np.abs(np.diff(np.vstack([np.zeros(20), commands]), axis=0))) <= 0.4
    np.testing.assert_allclose(commands[-1], 1.0)
    assert stats.boundary_commands == 2


def test_hand_tracker_decelerates_before_large_direction_reversal():
    raw = np.asarray(
        [
            [0.3] * 20,
            [0.6] * 20,
            [0.2] * 20,
            [-0.2] * 20,
            [-0.2] * 20,
        ],
        dtype=np.float64,
    )

    tracked, stats = apply_causal_hand_tracker(
        [raw],
        initial_commands=[np.zeros(20)],
        boundary_blend_seconds=0.01,
        max_velocity_rad_s=2.0,
        max_acceleration_rad_s2=5.0,
        max_delta_rad=0.4,
    )

    commands = tracked[0][:, 0]
    deltas = np.diff(np.concatenate([[0.0], commands]))
    assert deltas[1] > 0
    assert deltas[2] >= 0
    assert deltas[3] >= 0
    assert deltas[4] < 0
    assert stats.direction_reversal_holds > 0
