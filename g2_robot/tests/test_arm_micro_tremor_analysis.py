from __future__ import annotations

import numpy as np

from g2_sort.validation.evaluate_sync_arm_micro_tremor import (
    median_extrema_filter,
    play_deadband,
    reversal_gate,
    stationary_trend_projection,
    turning_point_projection,
)
from g2_sort.deployment.g2_client.servo import (
    project_stationary_arm_minimum_jerk,
)


def test_play_deadband_bounds_error_and_suppresses_small_jitter() -> None:
    values = np.asarray([[0.0], [0.001], [-0.001], [0.0015], [0.02]])
    result = play_deadband(values, 0.002)
    assert np.max(np.abs(result - values)) <= 0.002 + 1e-12
    assert np.allclose(result[:4], 0.0)
    assert np.isclose(result[-1, 0], 0.018)


def test_turning_projection_removes_only_small_local_extrema() -> None:
    values = np.asarray([[0.0], [0.002], [0.0], [0.02], [-0.02]])
    result = turning_point_projection(values, 0.003)
    assert result[1, 0] == 0.0
    assert result[3, 0] == values[3, 0]
    assert result[0, 0] == values[0, 0]
    assert result[-1, 0] == values[-1, 0]


def test_median_extrema_removes_small_spike_but_keeps_large_turn() -> None:
    values = np.asarray([[0.0], [0.002], [0.0], [0.02], [-0.02]])
    result = median_extrema_filter(values, 0.003)
    assert result[1, 0] == 0.0
    assert result[3, 0] == values[3, 0]
    assert result[0, 0] == values[0, 0]
    assert result[-1, 0] == values[-1, 0]


def test_stationary_trend_projects_only_repeated_low_range_reversal() -> None:
    small = np.asarray([0.0, 0.002, 0.0, 0.002] * 5 + [0.001])
    large = np.linspace(0.0, 0.1, 21)
    values = np.column_stack([small, large])
    result = stationary_trend_projection(values, 0.005)
    assert np.allclose(result[:, 0], np.linspace(small[0], small[-1], 21))
    assert np.allclose(result[:, 1], large)


def test_stationary_minimum_jerk_preserves_endpoints_and_monotonic_motion() -> None:
    jitter = np.asarray([0.0, 0.002, -0.001, 0.001, 0.0002])
    monotonic = np.linspace(0.0, 0.004, len(jitter))
    values = np.column_stack([jitter, monotonic])
    padded = np.column_stack(
        [values, np.zeros((len(values), 12), dtype=np.float64)]
    )
    result, active, reversals, _ = project_stationary_arm_minimum_jerk(
        padded, 0.005
    )
    result = result[:, :2]
    assert active == (0,)
    assert reversals >= 2
    assert np.allclose(result[[0, -1]], values[[0, -1]])
    assert np.all(np.diff(result[:, 0]) >= 0.0)
    assert np.allclose(result[:, 1], monotonic)


def test_reversal_gate_holds_small_reversal_and_releases_large_one() -> None:
    values = np.asarray([[0.0], [0.01], [0.009], [0.008], [-0.01]])
    result = reversal_gate(values, 0.003)
    assert result[2, 0] == result[1, 0]
    assert result[3, 0] == result[1, 0]
    assert result[-1, 0] == values[-1, 0]
