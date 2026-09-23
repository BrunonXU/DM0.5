"""30Hz模型waypoint到100Hz本地servo的时间插值。

用途：在不改变模型轨迹总时长的前提下生成约3.33倍的本地控制目标。
输入：N×34 absolute joint-position waypoint、经过时间和源/目标频率。
输出：单个插值动作，或目标频率的时间戳与动作序列。
方法：默认用``u=t×30``做逐关节凸线性组合；可选双臂C1三次Hermite，双手仍
保持线性。末点只hold不外推。
"""

from __future__ import annotations

import math

import numpy as np

from .contracts import ACTION_DIM, MODEL_HZ, SERVO_HZ, ContractError


def sample_linear_action(
    actions: np.ndarray,
    elapsed_seconds: float,
    *,
    source_hz: float = MODEL_HZ,
) -> np.ndarray:
    """Sample absolute joint waypoints by continuous time.

    The result is a convex combination of adjacent waypoints.  After reaching
    the last waypoint it is held; no velocity extrapolation is performed.
    """
    array = np.asarray(actions, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != ACTION_DIM or len(array) == 0:
        raise ContractError(
            f"actions must be a non-empty Nx{ACTION_DIM} array, got {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        raise ContractError("cannot interpolate non-finite actions")
    if source_hz <= 0 or not np.isfinite(elapsed_seconds):
        raise ValueError("source_hz and elapsed_seconds must be finite/valid")
    source_position = max(0.0, float(elapsed_seconds)) * source_hz
    lower = min(int(math.floor(source_position)), len(array) - 1)
    upper = min(lower + 1, len(array) - 1)
    alpha = source_position - math.floor(source_position)
    if lower == upper:
        alpha = 0.0
    return (1.0 - alpha) * array[lower] + alpha * array[upper]


def arm_cubic_tangents(actions: np.ndarray) -> np.ndarray:
    """Return centered normalized-time tangents for the first 14 arm axes."""
    array = np.asarray(actions, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != ACTION_DIM or len(array) < 2:
        raise ContractError(
            f"actions must be an Nx{ACTION_DIM} array with N>=2, got {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        raise ContractError("cannot derive tangents from non-finite actions")
    arms = array[:, :14]
    tangents = np.empty_like(arms)
    tangents[0] = arms[1] - arms[0]
    tangents[-1] = arms[-1] - arms[-2]
    if len(arms) > 2:
        tangents[1:-1] = 0.5 * (arms[2:] - arms[:-2])
    return tangents


def arm_cubic_spline_tangents(
    actions: np.ndarray,
    *,
    start_index: int = 0,
    start_velocity_per_step: np.ndarray | None = None,
) -> np.ndarray:
    """Return arm slopes for a C2 cubic spline on the executable suffix.

    A steady RTC switch clamps the first suffix slope to the incoming committed
    velocity.  All later slopes are solved together with a natural far-end
    condition, making acceleration continuous at internal 30 Hz waypoints.
    The tridiagonal Thomas solve is O(N) and avoids runtime BLAS variability.
    """

    array = np.asarray(actions, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != ACTION_DIM or len(array) < 2:
        raise ContractError(
            f"actions must be an Nx{ACTION_DIM} array with N>=2, got {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        raise ContractError("cannot derive spline tangents from non-finite actions")
    if not 0 <= start_index < len(array) - 1:
        raise ValueError("spline start_index must leave at least two waypoints")
    velocity = None
    if start_velocity_per_step is not None:
        velocity = np.asarray(start_velocity_per_step, dtype=np.float64)
        if velocity.shape != (14,) or not np.all(np.isfinite(velocity)):
            raise ValueError("start_velocity_per_step must be finite 14D")

    values = array[start_index:, :14]
    count = len(values)
    lower = np.zeros(count, dtype=np.float64)
    diagonal = np.empty(count, dtype=np.float64)
    upper = np.zeros(count, dtype=np.float64)
    rhs = np.empty_like(values)
    if velocity is None:
        diagonal[0] = 2.0
        upper[0] = 1.0
        rhs[0] = 3.0 * (values[1] - values[0])
    else:
        diagonal[0] = 1.0
        rhs[0] = velocity
    if count > 2:
        lower[1:-1] = 1.0
        diagonal[1:-1] = 4.0
        upper[1:-1] = 1.0
        rhs[1:-1] = 3.0 * (values[2:] - values[:-2])
    lower[-1] = 1.0
    diagonal[-1] = 2.0
    rhs[-1] = 3.0 * (values[-1] - values[-2])

    for index in range(1, count):
        factor = lower[index] / diagonal[index - 1]
        diagonal[index] -= factor * upper[index - 1]
        rhs[index] -= factor * rhs[index - 1]
    solved = np.empty_like(values)
    solved[-1] = rhs[-1] / diagonal[-1]
    for index in range(count - 2, -1, -1):
        solved[index] = (
            rhs[index] - upper[index] * solved[index + 1]
        ) / diagonal[index]

    tangents = arm_cubic_tangents(array)
    tangents[start_index:] = solved
    return tangents


def sample_cubic_arm_linear_hand_action(
    actions: np.ndarray,
    elapsed_seconds: float,
    *,
    start_index: int = 0,
    source_hz: float = MODEL_HZ,
    arm_tangents: np.ndarray | None = None,
) -> np.ndarray:
    """Sample C1 cubic arms and unchanged linear hands at continuous time."""
    array = np.asarray(actions, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != ACTION_DIM or len(array) < 2:
        raise ContractError(
            f"actions must be an Nx{ACTION_DIM} array with N>=2, got {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        raise ContractError("cannot interpolate non-finite actions")
    if not 0 <= start_index < len(array):
        raise ValueError("start_index is outside the action chunk")
    if source_hz <= 0 or not np.isfinite(elapsed_seconds):
        raise ValueError("source_hz and elapsed_seconds must be finite/valid")
    tangents = (
        arm_cubic_tangents(array)
        if arm_tangents is None
        else np.asarray(arm_tangents, dtype=np.float64)
    )
    if tangents.shape != (len(array), 14) or not np.all(np.isfinite(tangents)):
        raise ValueError("arm_tangents must be a finite Nx14 array")

    source_position = start_index + max(0.0, float(elapsed_seconds)) * source_hz
    lower = min(int(math.floor(source_position)), len(array) - 1)
    upper = min(lower + 1, len(array) - 1)
    u = source_position - math.floor(source_position)
    if lower == upper:
        u = 0.0
    result = (1.0 - u) * array[lower] + u * array[upper]
    if lower == upper:
        result[:14] = array[lower, :14]
        return result
    u2 = u * u
    u3 = u2 * u
    result[:14] = (
        (2.0 * u3 - 3.0 * u2 + 1.0) * array[lower, :14]
        + (u3 - 2.0 * u2 + u) * tangents[lower]
        + (-2.0 * u3 + 3.0 * u2) * array[upper, :14]
        + (u3 - u2) * tangents[upper]
    )
    return result


def resample_action_intervals(
    actions: np.ndarray,
    *,
    start_index: int = 0,
    interval_count: int | None = None,
    source_hz: float = MODEL_HZ,
    target_hz: float = SERVO_HZ,
) -> tuple[np.ndarray, np.ndarray]:
    """Return target-rate timestamps and linearly sampled positions.

    ``interval_count`` counts 30 Hz control intervals, not array elements.
    The last model waypoint is held for its final interval.
    """
    array = np.asarray(actions, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != ACTION_DIM or len(array) == 0:
        raise ContractError(
            f"actions must be a non-empty Nx{ACTION_DIM} array, got {array.shape}"
        )
    if start_index < 0 or start_index >= len(array):
        raise ValueError("start_index is outside the action chunk")
    if interval_count is None:
        interval_count = len(array) - start_index
    if interval_count <= 0 or interval_count > len(array) - start_index:
        raise ValueError("interval_count exceeds the available action intervals")
    if source_hz <= 0 or target_hz <= 0:
        raise ValueError("source_hz and target_hz must be positive")
    duration = interval_count / source_hz
    tick_count = max(1, int(math.ceil(duration * target_hz)))
    times = np.arange(tick_count, dtype=np.float64) / target_hz
    targets = np.stack(
        [
            sample_linear_action(
                array[start_index:],
                elapsed,
                source_hz=source_hz,
            )
            for elapsed in times
        ]
    )
    return times, targets
