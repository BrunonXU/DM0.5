"""30Hz模型waypoint到100Hz本地servo的时间插值。

用途：在不改变模型轨迹总时长的前提下生成约3.33倍的本地控制目标。
输入：N×34 absolute joint-position waypoint、经过时间和源/目标频率。
输出：单个插值动作，或目标频率的时间戳与动作序列。
方法：用``u=t×30``定位相邻点并做逐关节凸线性组合，末点只hold不外推。
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
