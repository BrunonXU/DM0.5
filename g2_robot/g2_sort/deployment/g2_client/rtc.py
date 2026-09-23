"""Pure RTC timing and trajectory contracts for the G2 DM0.5 runtime.

TODO(RTC_UNIFIED_34D_MIGRATION): migrate RTC hardware output to canonical single-34D normal Servo at 100Hz; preserve one Control Robot owner. Pending implementation/validation; current RTC behavior unchanged.

This module is intentionally independent of GDK and Servo.  It can be tested on
the 6000D and is shared by offline validation and the future guarded runtime.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import ACTION_DIM, CHUNK_STEPS, MODEL_HZ, ContractError


@dataclass(frozen=True)
class RTCParameters:
    """Frozen paper-level scheduling parameters for a 50-step chunk."""

    predicted_delay_steps: int = 20
    execution_horizon: int = 25
    max_guidance_weight: float = 10.0

    def __post_init__(self) -> None:
        d = self.predicted_delay_steps
        s = self.execution_horizon
        if not 0 <= d <= s <= CHUNK_STEPS - d:
            raise ContractError(
                "RTC requires d <= s <= H-d; "
                f"got d={d}, s={s}, H={CHUNK_STEPS}"
            )
        if not np.isfinite(self.max_guidance_weight) or self.max_guidance_weight <= 0:
            raise ContractError("RTC max guidance weight must be finite and positive")

    @property
    def committed_tail_steps(self) -> int:
        return CHUNK_STEPS - self.execution_horizon

    @property
    def post_response_steps_before_replan(self) -> int:
        return self.execution_horizon - self.predicted_delay_steps


@dataclass(frozen=True)
class RTCRequest:
    """Absolute robot-space prefix committed when an RTC request starts."""

    prefix_actions_absolute: np.ndarray
    parameters: RTCParameters

    def __post_init__(self) -> None:
        prefix = np.asarray(self.prefix_actions_absolute, dtype=np.float64)
        expected = (self.parameters.committed_tail_steps, ACTION_DIM)
        if prefix.shape != expected:
            raise ContractError(
                f"RTC committed prefix must be {expected}, got {prefix.shape}"
            )
        if not np.all(np.isfinite(prefix)):
            raise ContractError("RTC committed prefix contains NaN or Inf")
        object.__setattr__(self, "prefix_actions_absolute", np.ascontiguousarray(prefix))


@dataclass(frozen=True)
class RTCPlanDisagreement:
    """Future old/new plan disagreement measured after an RTC switch."""

    horizon_steps: int
    arm_direction_reversal_count: int
    arm_reverse_displacement_max_rad: float
    arm_plan_divergence_max_rad: float
    hand_plan_divergence_max_rad: float


def measure_rtc_plan_disagreement(
    candidate_actions_absolute: np.ndarray,
    prefix_actions_absolute: np.ndarray,
    *,
    switch_delay: int,
    horizon_steps: int = 10,
    min_incoming_step_rad: float = 1e-4,
) -> RTCPlanDisagreement:
    """Compare the next few candidate points with the still-committed old plan.

    C0/C1 only constrain the immediate switch.  This metric detects a candidate
    that turns back against the incoming arm direction a few samples later, as
    happened in the first powered RTC experiment.
    """

    candidate = np.asarray(candidate_actions_absolute, dtype=np.float64)
    prefix = np.asarray(prefix_actions_absolute, dtype=np.float64)
    if candidate.shape != (CHUNK_STEPS, ACTION_DIM):
        raise ContractError(
            f"RTC disagreement candidate must be {(CHUNK_STEPS, ACTION_DIM)}"
        )
    if prefix.ndim != 2 or prefix.shape[1] != ACTION_DIM:
        raise ContractError("RTC disagreement prefix must be [T, 34]")
    if not np.all(np.isfinite(candidate)) or not np.all(np.isfinite(prefix)):
        raise ContractError("RTC disagreement inputs must be finite")
    if horizon_steps <= 0:
        raise ContractError("RTC disagreement horizon must be positive")
    if not 1 <= switch_delay < prefix.shape[0]:
        raise ContractError("RTC disagreement requires 1 <= delay < prefix length")
    if not np.isfinite(min_incoming_step_rad) or min_incoming_step_rad < 0:
        raise ContractError("RTC minimum incoming step must be finite and non-negative")

    available = min(
        int(horizon_steps),
        prefix.shape[0] - switch_delay - 1,
        CHUNK_STEPS - switch_delay - 1,
    )
    if available <= 0:
        raise ContractError("RTC disagreement has no future overlap to compare")

    old_future = prefix[switch_delay : switch_delay + available + 1]
    new_future = candidate[switch_delay : switch_delay + available + 1]
    incoming_arm_step = (
        prefix[switch_delay, :14] - prefix[switch_delay - 1, :14]
    )
    new_arm_displacement = new_future[1:, :14] - new_future[0, :14]
    eligible = np.abs(incoming_arm_step) >= float(min_incoming_step_rad)
    reverses = (
        new_arm_displacement * incoming_arm_step[None, :] < 0.0
    ) & eligible[None, :]
    reverse_magnitudes = np.where(reverses, np.abs(new_arm_displacement), 0.0)

    return RTCPlanDisagreement(
        horizon_steps=available,
        arm_direction_reversal_count=int(np.count_nonzero(np.any(reverses, axis=0))),
        arm_reverse_displacement_max_rad=float(reverse_magnitudes.max(initial=0.0)),
        arm_plan_divergence_max_rad=float(
            np.abs(new_future[:, :14] - old_future[:, :14]).max(initial=0.0)
        ),
        hand_plan_divergence_max_rad=float(
            np.abs(new_future[:, 14:] - old_future[:, 14:]).max(initial=0.0)
        ),
    )


def request_from_committed_chunk(
    committed_actions_absolute: np.ndarray,
    parameters: RTCParameters,
) -> RTCRequest:
    """Take the still-unexecuted tail at logical request time ``s``."""

    actions = np.asarray(committed_actions_absolute, dtype=np.float64)
    if actions.shape != (CHUNK_STEPS, ACTION_DIM):
        raise ContractError(
            f"committed RTC source chunk must be {(CHUNK_STEPS, ACTION_DIM)}"
        )
    return RTCRequest(actions[parameters.execution_horizon :], parameters)


def conservative_delay_steps(observation_age_seconds: float) -> int:
    """Convert end-to-end observation age to 30 Hz steps without undercounting."""

    age = float(observation_age_seconds)
    if not np.isfinite(age) or age < 0:
        raise ContractError("RTC observation age must be finite and non-negative")
    scaled = age * MODEL_HZ
    tolerance = np.finfo(np.float64).eps * max(1.0, abs(scaled)) * 8.0
    return min(CHUNK_STEPS, int(np.ceil(scaled - tolerance)))


def arrived_chunk_suffix(
    actions_absolute: np.ndarray,
    real_delay_steps: int,
) -> np.ndarray:
    """Discard action indices whose physical time elapsed during inference."""

    actions = np.asarray(actions_absolute, dtype=np.float64)
    if actions.shape != (CHUNK_STEPS, ACTION_DIM):
        raise ContractError(f"RTC response must be {(CHUNK_STEPS, ACTION_DIM)}")
    if not 0 <= real_delay_steps <= CHUNK_STEPS:
        raise ContractError("RTC real delay must be in [0, 50]")
    return np.ascontiguousarray(actions[real_delay_steps:])


def mixed_prefix_from_absolute(
    prefix_actions_absolute: np.ndarray,
    observation_state: np.ndarray,
) -> np.ndarray:
    """Re-anchor 14D arms while preserving 20D absolute hand commands."""

    prefix = np.asarray(prefix_actions_absolute, dtype=np.float32)
    state = np.asarray(observation_state, dtype=np.float32)
    if prefix.ndim != 2 or prefix.shape[1] != ACTION_DIM:
        raise ContractError("RTC prefix must be [T, 34]")
    if state.shape != (ACTION_DIM,) or not np.all(np.isfinite(state)):
        raise ContractError("RTC observation state must be finite 34D")
    mixed = prefix.copy()
    mixed[:, :14] -= state[:14]
    return mixed


def project_absolute_rtc_prefix(
    actions_absolute: np.ndarray,
    prefix_actions_absolute: np.ndarray,
    *,
    inference_delay: int,
    release_steps: int = 10,
) -> np.ndarray:
    """Restore C0 at the switch and smoothly remove the residual offset."""

    actions = np.asarray(actions_absolute, dtype=np.float64)
    prefix = np.asarray(prefix_actions_absolute, dtype=np.float64)
    if actions.shape != (CHUNK_STEPS, ACTION_DIM):
        raise ContractError(f"RTC candidate must be {(CHUNK_STEPS, ACTION_DIM)}")
    if prefix.ndim != 2 or prefix.shape[1] != ACTION_DIM:
        raise ContractError("RTC committed prefix must be [T, 34]")
    overlap_steps = prefix.shape[0]
    if not 0 <= inference_delay < overlap_steps <= CHUNK_STEPS:
        raise ContractError("RTC projection requires 0 <= delay < overlap <= 50")
    if release_steps <= 0:
        raise ContractError("RTC projection release_steps must be positive")
    if not np.all(np.isfinite(actions)) or not np.all(np.isfinite(prefix)):
        raise ContractError("RTC projection inputs must be finite")

    output = actions.copy()
    stop = min(CHUNK_STEPS, inference_delay + release_steps + 1)
    phase = np.linspace(0.0, 1.0, stop - inference_delay)
    smootherstep = phase**3 * (phase * (phase * 6.0 - 15.0) + 10.0)
    weights = 1.0 - smootherstep
    residual = prefix[inference_delay] - actions[inference_delay]
    output[inference_delay:stop] += weights[:, None] * residual[None, :]
    return np.ascontiguousarray(output)
